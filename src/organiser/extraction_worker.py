"""Isolated PDF text parser. Runs as a separate process and opens no file.

The worker is started as ``python -I <this file>``. It takes **no** arguments, it
never resolves a path, and it never touches the filesystem: the bytes arrive on
stdin and a single JSON object leaves on stdout. That is deliberate. A parser that
re-opened "the PDF we were given" by name would re-resolve the path after the
parent proved it safe, and a path is exactly what an attacker can swap.

Everything the worker needs is passed explicitly:

* the payload, on stdin, already read and hashed by the parent's no-follow reader;
* the parser's own resource limits, as environment values, applied by the worker
  to itself before the parser is imported (``RLIMIT_CPU``, ``RLIMIT_AS``,
  ``RLIMIT_FSIZE``, ``RLIMIT_CORE``);
* ``ORGANISER_PDF_WORKER_SYS_PATH``, the import path the parent resolved, because
  ``-I`` ignores ``PYTHONPATH`` and the dependency may live outside the venv.

The parent enforces what the worker cannot enforce for itself: wall-clock
timeout, output size and process-group kill.
"""

from __future__ import annotations

import io
import json
import os
import sys


EXIT_PROTOCOL_ERROR = 3
EXIT_USAGE_ERROR = 4

ENV_MAX_PAGES = "ORGANISER_PDF_MAX_PAGES"
ENV_MAX_CHARACTERS = "ORGANISER_PDF_MAX_CHARACTERS"
ENV_MAX_INPUT_BYTES = "ORGANISER_PDF_MAX_INPUT_BYTES"
ENV_CPU_SECONDS = "ORGANISER_PDF_CPU_SECONDS"
ENV_ADDRESS_SPACE = "ORGANISER_PDF_ADDRESS_SPACE_BYTES"
ENV_OUTPUT_BYTES = "ORGANISER_PDF_OUTPUT_BYTES"
ENV_SYS_PATH = "ORGANISER_PDF_WORKER_SYS_PATH"

MAX_HARD_INPUT_BYTES = 64 * 1024 * 1024
PAGE_SEPARATOR = "\n\n"


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def _fail(error_code: str, exit_code: int = 0) -> None:
    _emit({"status": "failed", "error_code": error_code, "text": None, "truncated": False})
    raise SystemExit(exit_code)


def _apply_limits() -> dict:
    """Lower this process' own limits and report what was actually applied.

    The result is not swallowed: a limit that could not be set is reported to the
    parent, so "the parser ran bounded" is an auditable claim instead of an
    assumption. ``RLIMIT_FSIZE`` only bounds file writes -- it does not bound a
    pipe -- which is why the parent caps and kills the output itself.
    """
    applied: dict[str, int] = {}
    try:
        import resource
    except ImportError:  # pragma: no cover - non POSIX, the parent refuses anyway
        return applied

    def _set(name: str, limit: int, value: int) -> None:
        try:
            _soft, hard = resource.getrlimit(limit)
            ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(limit, (ceiling, hard))
        except (ValueError, OSError):
            return
        applied[name] = value

    _set("cpu_seconds", resource.RLIMIT_CPU, int(os.environ.get(ENV_CPU_SECONDS, "15")))
    _set(
        "address_space_bytes",
        resource.RLIMIT_AS,
        int(os.environ.get(ENV_ADDRESS_SPACE, str(512 * 1024 * 1024))),
    )
    _set(
        "file_size_bytes",
        resource.RLIMIT_FSIZE,
        int(os.environ.get(ENV_OUTPUT_BYTES, str(8 * 1024 * 1024))),
    )
    _set("core_bytes", resource.RLIMIT_CORE, 0)
    return applied


def _extend_sys_path() -> None:
    raw = os.environ.get(ENV_SYS_PATH, "[]")
    try:
        entries = json.loads(raw)
    except ValueError:  # pragma: no cover - the parent always writes valid JSON
        return
    if isinstance(entries, list):
        sys.path[:0] = [entry for entry in entries if isinstance(entry, str)]


def _read_payload(max_bytes: int) -> bytes:
    buffer = bytearray()
    while True:
        chunk = sys.stdin.buffer.read(1024 * 1024)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            _fail("pdf_input_too_large", EXIT_PROTOCOL_ERROR)
    return bytes(buffer)


def _extract(payload: bytes) -> dict:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return {
            "status": "unsupported",
            "error_code": "pdf_dependency_missing",
            "text": None,
            "truncated": False,
        }

    max_pages = int(os.environ.get(ENV_MAX_PAGES, "200"))
    max_characters = int(os.environ.get(ENV_MAX_CHARACTERS, "200000"))
    try:
        reader = PdfReader(io.BytesIO(payload), strict=False)
        if reader.is_encrypted:
            # No decryption attempt with an empty or guessed password: a document
            # the operator cannot open is not silently opened by the assistant.
            return {
                "status": "unsupported",
                "error_code": "pdf_encrypted",
                "text": None,
                "truncated": False,
            }
        pages = reader.pages
        total_pages = len(pages)
    except (PdfReadError, ValueError, TypeError, OSError, MemoryError, RecursionError) as error:
        return {
            "status": "failed",
            "error_code": "pdf_parse_error",
            "error_type": type(error).__name__,
            "text": None,
            "truncated": False,
        }
    except Exception as error:  # pragma: no cover - parser specific surprises
        return {
            "status": "failed",
            "error_code": "pdf_parse_error",
            "error_type": type(error).__name__,
            "text": None,
            "truncated": False,
        }

    spans: list[dict] = []
    parts: list[str] = []
    cursor = 0
    truncated = False
    used_pages = min(total_pages, max_pages)
    for index in range(used_pages):
        try:
            page_text = pages[index].extract_text() or ""
        except Exception as error:
            return {
                "status": "failed",
                "error_code": "pdf_page_error",
                "error_type": type(error).__name__,
                "text": None,
                "truncated": truncated,
            }
        if index:
            if max_characters - cursor < len(PAGE_SEPARATOR):
                truncated = True
                break
            parts.append(PAGE_SEPARATOR)
            cursor += len(PAGE_SEPARATOR)
        remaining = max_characters - cursor
        if remaining <= 0:
            truncated = True
            break
        if len(page_text) > remaining:
            page_text = page_text[:remaining]
            truncated = True
        parts.append(page_text)
        spans.append({"page": index + 1, "char_start": cursor, "char_end": cursor + len(page_text)})
        cursor += len(page_text)
        if truncated:
            break
    if used_pages < total_pages:
        truncated = True

    text = "".join(parts)
    if not text.strip():
        # No text layer at all: an image-only scan. OCR is explicitly out of
        # scope, so this is reported as unsupported instead of as an empty
        # success that would look like a document with no content.
        return {
            "status": "unsupported",
            "error_code": "pdf_needs_ocr",
            "text": None,
            "truncated": truncated,
            "page_count": total_pages,
            "pages": spans,
        }
    return {
        "status": "ready",
        "error_code": None,
        "text": text,
        "truncated": truncated,
        "page_count": total_pages,
        "pages": spans,
    }


def main(argv: list[str]) -> int:
    """Extract bounded stdin bytes and emit the isolated PDF worker JSON protocol.

    Apply process limits before parsing; accept no source path or CLI arguments.
    """
    if argv:
        # The worker has no arguments by design. A path on the command line would
        # be a path the parent did not read.
        _fail("pdf_worker_usage_error", EXIT_USAGE_ERROR)
    limits_applied = _apply_limits()
    _extend_sys_path()
    try:
        max_input = min(
            int(os.environ.get(ENV_MAX_INPUT_BYTES, str(MAX_HARD_INPUT_BYTES))),
            MAX_HARD_INPUT_BYTES,
        )
    except ValueError:
        max_input = MAX_HARD_INPUT_BYTES
    payload = _read_payload(max_input)
    if not payload:
        return _emit_empty()
    result = _extract(payload)
    result["limits_applied"] = limits_applied
    try:
        import pypdf

        result["parser"] = f"pypdf-{getattr(pypdf, '__version__', 'unknown')}"
    except Exception:  # pragma: no cover - version is informational
        result["parser"] = "pypdf-unknown"
    _emit(result)
    return 0


def _emit_empty() -> int:
    _emit({"status": "failed", "error_code": "pdf_empty_payload", "text": None, "truncated": False})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
