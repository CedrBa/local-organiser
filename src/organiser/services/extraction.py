"""Bounded text extraction: UTF-8 text/markdown here, PDF in an isolated process.

The split follows the trust boundary, not the file type. UTF-8 decoding is a pure
function on bytes we already hold, so it runs in-process. A PDF parser is a
third-party state machine with a history of pathological inputs, so it runs in a
separate, resource-limited process and returns one bounded JSON object.

What this module guarantees:

* The parser receives **bytes**, never a path. There is no code path in which the
  parser resolves an address the parent did not read.
* The parser cannot outgrow its budget: wall-clock timeout, CPU, address space,
  output size and file-descriptor count are all capped, and the whole process
  group is killed on timeout or output overflow.
* Cleanup is unconditional. The child runs in a private empty working directory
  that is removed afterwards, whether the child succeeded, failed, was killed or
  never started.
* Extraction is a pure function of ``(bytes, extractor_version)``. The content
  hash always belongs to the original bytes, never to the extracted text.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import logging
import tempfile
import threading
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from organiser.domain.resources import DEFAULT_LIMITS, IngestionLimits, SnapshotStatus


TEXT_EXTRACTOR_VERSION = "text-utf8-1.0.0"
PDF_EXTRACTOR_VERSION_PREFIX = "pdf-pypdf-"

logger = logging.getLogger(__name__)

WORKER_PATH = Path(__file__).resolve().parent.parent / "extraction_worker.py"

# Failure codes of the isolation layer itself, as opposed to codes the parser
# reports about a document.
CODE_PDF_TIMEOUT = "pdf_worker_timeout"
CODE_PDF_CRASHED = "pdf_worker_crashed"
CODE_PDF_OUTPUT_LIMIT = "pdf_worker_output_limit"
CODE_PDF_PROTOCOL = "pdf_worker_protocol_error"
CODE_PDF_UNAVAILABLE = "pdf_worker_unavailable"
CODE_TEXT_NOT_UTF8 = "text_not_utf8"
CODE_UNSUPPORTED_TYPE = "unsupported_resource_type"
CODE_EMPTY_FILE = "empty_file"


@dataclass(frozen=True)
class ExtractionOutcome:
    """Result of one extraction attempt over already read bytes."""

    status: SnapshotStatus
    extractor_version: str
    text: str | None = None
    truncated: bool = False
    error_code: str | None = None
    character_count: int = 0
    page_count: int | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return self.status == SnapshotStatus.READY


def pypdf_version() -> str | None:
    """Version of the installed parser, or ``None`` when it is not available.

    ``importlib.metadata`` answers without importing the parser. The worker is
    started with the parent's import path, so the parent's view is authoritative
    for whether the worker can import it at all.
    """
    try:
        return metadata.version("pypdf")
    except metadata.PackageNotFoundError:
        return None
    except Exception:  # pragma: no cover - broken metadata is treated as absent
        return None


def pdf_extractor_version() -> str:
    """Extractor version for PDFs, bound to the parser version.

    A snapshot is unique per ``(content_hash, extractor_version)``. Folding the
    parser version into the extractor version means a parser upgrade produces a
    new snapshot instead of silently reusing a text that an older parser
    produced from the same bytes.
    """
    return f"{PDF_EXTRACTOR_VERSION_PREFIX}{pypdf_version() or 'unavailable'}"


def extractor_version_for(resource_type: str) -> str | None:
    """Extractor version for a resource type, or ``None`` if it is not supported."""
    if resource_type in ("text", "markdown"):
        return TEXT_EXTRACTOR_VERSION
    if resource_type == "pdf":
        return pdf_extractor_version()
    return None


def extract_text_bytes(
    data: bytes, *, limits: IngestionLimits = DEFAULT_LIMITS
) -> ExtractionOutcome:
    """Decode UTF-8 text/markdown with a hard character budget.

    A non-UTF-8 file is *unsupported*, not failed: this component does not guess
    an encoding, because a guessed encoding produces confident wrong quotes in a
    document the user will later cite.
    """
    if not data:
        return ExtractionOutcome(
            status=SnapshotStatus.FAILED,
            extractor_version=TEXT_EXTRACTOR_VERSION,
            error_code=CODE_EMPTY_FILE,
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ExtractionOutcome(
            status=SnapshotStatus.UNSUPPORTED,
            extractor_version=TEXT_EXTRACTOR_VERSION,
            error_code=CODE_TEXT_NOT_UTF8,
        )
    truncated = len(text) > limits.max_text_characters
    if truncated:
        text = text[: limits.max_text_characters]
    return ExtractionOutcome(
        status=SnapshotStatus.READY,
        extractor_version=TEXT_EXTRACTOR_VERSION,
        text=text,
        truncated=truncated,
        character_count=len(text),
        metadata={
            "kind": "text",
            "encoding": "utf-8",
            "byte_size": len(data),
            "characters": len(text),
            "truncated": truncated,
        },
    )


@dataclass(frozen=True)
class PdfWorkerOutcome:
    """What the isolated parser process reported."""

    ok: bool
    payload: dict = field(default_factory=dict)
    error_code: str | None = None
    duration_seconds: float = 0.0


def _drain(stream, cap: int, sink: list[bytes], overflow: threading.Event) -> None:
    """Read a pipe into memory up to ``cap`` bytes, then stop and signal."""
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            total += len(chunk)
            if total > cap:
                overflow.set()
                return
            sink.append(chunk)
    except (OSError, ValueError):  # pragma: no cover - pipe closed under us
        return


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill the child and everything it started, then reap it."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):  # pragma: no cover - already gone
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
        pass


def _worker_environment(limits: IngestionLimits) -> dict[str, str]:
    """A minimal environment: nothing of the operator's shell reaches the parser."""
    return {
        "PATH": "",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": "",  # replaced by the caller with the private work directory
        "ORGANISER_PDF_MAX_PAGES": str(limits.max_pdf_pages),
        "ORGANISER_PDF_MAX_CHARACTERS": str(limits.max_text_characters),
        "ORGANISER_PDF_MAX_INPUT_BYTES": str(limits.max_file_bytes),
        "ORGANISER_PDF_CPU_SECONDS": str(limits.pdf_worker_cpu_seconds),
        "ORGANISER_PDF_ADDRESS_SPACE_BYTES": str(limits.pdf_worker_address_space_bytes),
        "ORGANISER_PDF_OUTPUT_BYTES": str(limits.pdf_worker_output_bytes),
        "ORGANISER_PDF_WORKER_SYS_PATH": json.dumps([str(entry) for entry in sys.path]),
    }


def run_pdf_worker(
    data: bytes,
    *,
    limits: IngestionLimits = DEFAULT_LIMITS,
    worker_path: Path | None = None,
    timeout: float | None = None,
) -> PdfWorkerOutcome:
    """Run the isolated parser over ``data`` and return its bounded report.

    ``worker_path`` and ``timeout`` exist so tests can substitute a hostile
    process (hang, output explosion, crash). Production always uses the shipped
    worker and the 20 second budget from :class:`IngestionLimits`.
    """
    worker = Path(worker_path) if worker_path is not None else WORKER_PATH
    if not worker.is_file():
        return PdfWorkerOutcome(ok=False, error_code=CODE_PDF_UNAVAILABLE)
    # The test seam may only *shorten* the budget. A caller cannot buy a slower
    # parser than the fixed 20 seconds, not even through a keyword argument.
    effective_timeout = (
        limits.pdf_worker_timeout_seconds
        if timeout is None
        else min(timeout, limits.pdf_worker_timeout_seconds)
    )
    workdir = Path(tempfile.mkdtemp(prefix="organiser-pdf-"))
    try:
        os.chmod(workdir, 0o700)
        env = _worker_environment(limits)
        env["TMPDIR"] = str(workdir)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        overflow = threading.Event()
        stderr_overflow = threading.Event()
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False, no PATH
                [sys.executable, "-I", str(worker)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workdir),
                env=env,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            return PdfWorkerOutcome(ok=False, error_code=CODE_PDF_UNAVAILABLE)

        readers = [
            threading.Thread(
                target=_drain,
                args=(process.stdout, limits.pdf_worker_output_bytes, stdout_chunks, overflow),
                daemon=True,
            ),
            # stderr has its own flag: parser chatter must not be able to fake an
            # output-limit violation of the result.
            threading.Thread(
                target=_drain,
                args=(process.stderr, 64 * 1024, stderr_chunks, stderr_overflow),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            try:
                process.stdin.write(data)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            violation = _wait_bounded(process, effective_timeout, overflow)
        except BaseException:
            # A cancellation or an unexpected error must not orphan a parser that
            # may still be running with the payload in memory.
            _kill_process_group(process)
            _join(readers)
            _close_quietly(process)
            raise
        _join(readers)
        _close_quietly(process)
        if violation is not None:
            return PdfWorkerOutcome(ok=False, error_code=violation)
        if process.returncode != 0:
            return PdfWorkerOutcome(
                ok=False, error_code=CODE_PDF_CRASHED, payload={"returncode": process.returncode}
            )
        raw = b"".join(stdout_chunks)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return PdfWorkerOutcome(ok=False, error_code=CODE_PDF_PROTOCOL)
        if not isinstance(payload, dict) or "status" not in payload:
            return PdfWorkerOutcome(ok=False, error_code=CODE_PDF_PROTOCOL)
        return PdfWorkerOutcome(ok=True, payload=payload)
    finally:
        # Unconditional cleanup. ``ignore_errors=True`` would make "the sandbox is
        # always removed" unobservable, so a failure is reported instead.
        try:
            shutil.rmtree(workdir)
        except OSError:  # pragma: no cover - only on a broken filesystem
            logger.warning(
                "Parser-Arbeitsverzeichnis konnte nicht entfernt werden: %s", workdir.name
            )


def _wait_bounded(
    process: subprocess.Popen, timeout: float, overflow: threading.Event
) -> str | None:
    """Wait for the parser, killing it on timeout *or* on output explosion.

    A child that writes past the output cap blocks on a full pipe, so waiting
    for its normal exit would waste the whole timeout. The loop therefore polls
    the overflow flag and kills the process group as soon as it is set.
    """
    deadline = time.monotonic() + timeout
    while True:
        if overflow.is_set():
            _kill_process_group(process)
            return CODE_PDF_OUTPUT_LIMIT
        if process.poll() is not None:
            return None
        if time.monotonic() >= deadline:
            _kill_process_group(process)
            return CODE_PDF_TIMEOUT
        time.sleep(0.02)


def _join(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=5)


def _close_quietly(process: subprocess.Popen) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):  # pragma: no cover - already closed
            pass


def extract_pdf_bytes(
    data: bytes,
    *,
    limits: IngestionLimits = DEFAULT_LIMITS,
    worker_path: Path | None = None,
    timeout: float | None = None,
) -> ExtractionOutcome:
    """Extract PDF text through the isolated parser and map its report.

    The worker's own status is preserved: ``unsupported`` stays unsupported
    (encrypted, image-only, missing dependency) so the registry can show *why* a
    document has no text, and ``failed`` stays failed. Isolation failures of the
    process itself are always ``failed``: a parser that had to be killed produced
    no evidence about the document.
    """
    version = pdf_extractor_version()
    if pypdf_version() is None:
        return ExtractionOutcome(
            status=SnapshotStatus.UNSUPPORTED,
            extractor_version=version,
            error_code="pdf_dependency_missing",
        )
    if not data:
        return ExtractionOutcome(
            status=SnapshotStatus.FAILED, extractor_version=version, error_code=CODE_EMPTY_FILE
        )
    outcome = run_pdf_worker(data, limits=limits, worker_path=worker_path, timeout=timeout)
    if not outcome.ok:
        return ExtractionOutcome(
            status=SnapshotStatus.FAILED, extractor_version=version, error_code=outcome.error_code
        )
    payload = outcome.payload
    status = payload.get("status")
    error_code = payload.get("error_code")
    page_count = payload.get("page_count")
    metadata_payload = {
        "kind": "pdf",
        "parser": payload.get("parser", "pypdf-unknown"),
        "page_count": page_count,
        "pages": payload.get("pages", []),
        "truncated": bool(payload.get("truncated", False)),
        # What the child was actually able to enforce. The parent always enforces
        # the wall clock and the output cap itself, so these are defence in depth
        # -- and they are recorded so the limits are auditable.
        "limits_applied": payload.get("limits_applied", {}),
    }
    if status == SnapshotStatus.READY:
        text = payload.get("text")
        if not isinstance(text, str) or len(text) > limits.max_text_characters:
            return ExtractionOutcome(
                status=SnapshotStatus.FAILED,
                extractor_version=version,
                error_code=CODE_PDF_PROTOCOL,
            )
        metadata_payload["characters"] = len(text)
        return ExtractionOutcome(
            status=SnapshotStatus.READY,
            extractor_version=version,
            text=text,
            truncated=bool(payload.get("truncated", False)),
            character_count=len(text),
            page_count=page_count,
            metadata=metadata_payload,
        )
    if status == SnapshotStatus.UNSUPPORTED:
        return ExtractionOutcome(
            status=SnapshotStatus.UNSUPPORTED,
            extractor_version=version,
            error_code=error_code or "pdf_unsupported",
            truncated=bool(payload.get("truncated", False)),
            page_count=page_count,
            metadata=metadata_payload,
        )
    return ExtractionOutcome(
        status=SnapshotStatus.FAILED,
        extractor_version=version,
        error_code=error_code or CODE_PDF_CRASHED,
        page_count=page_count,
        metadata={**metadata_payload, "parser_error": payload.get("error_type")},
    )


def extract_bytes(
    resource_type: str,
    data: bytes,
    *,
    limits: IngestionLimits = DEFAULT_LIMITS,
    worker_path: Path | None = None,
    timeout: float | None = None,
) -> ExtractionOutcome:
    """Dispatch to the extractor for ``resource_type``.

    An unsupported *type* is reported as unsupported, not as a failure: there is
    no extractor for it, which is a property of the registry, not of the file.
    """
    if resource_type in ("text", "markdown"):
        return extract_text_bytes(data, limits=limits)
    if resource_type == "pdf":
        return extract_pdf_bytes(data, limits=limits, worker_path=worker_path, timeout=timeout)
    return ExtractionOutcome(
        status=SnapshotStatus.UNSUPPORTED,
        extractor_version="none",
        error_code=CODE_UNSUPPORTED_TYPE,
        metadata={"kind": "other"},
    )


def content_hash_for(data: bytes) -> str:
    """SHA256 of the original bytes. Never a hash of extracted text."""
    return hashlib.sha256(data).hexdigest()
