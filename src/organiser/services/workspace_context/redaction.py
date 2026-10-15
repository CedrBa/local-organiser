"""Bounded safe projections of untrusted payloads and stored knowledge paths."""

from __future__ import annotations

from itertools import islice
from typing import Any
from organiser.domain.knowledge_path import (
    KnowledgePathError,
    validate_knowledge_path,
)
from .constants import (
    _ABSOLUTE_PATH_MARKER,
    _PATH_PREFIX_PATTERN,
    _TRUNCATION_MARKER,
    _WINDOWS_ABSOLUTE_PATH,
)
from .policy import WorkspacePolicy
from .values import _first_int


def _truncate(text: str | None, limit: int) -> tuple[str | None, bool]:
    """Cut text to ``limit`` characters, counting the marker inside the limit."""
    if text is None:
        return None, False
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text, False
    if limit <= len(_TRUNCATION_MARKER):
        return text[:limit], True
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER, True


def _looks_like_absolute_path(value: str) -> bool:
    """Is this string, as a whole, an absolute host address?

    The anchored form, kept because it is the only rule that also recognises a
    single-segment address such as ``/geheim`` or ``C:/x`` -- shapes the token scan
    below deliberately accepts as ordinary prose. A value that *is* an address must
    never be shown, so every caller checks this first and falls back to
    :func:`_contains_absolute_path` for the address hidden inside a longer string.
    """
    probe = value.strip()
    if not probe:
        return False
    return probe.startswith(("/", "file://", "\\\\")) or bool(_WINDOWS_ABSOLUTE_PATH.match(probe))


def _absolute_path_spans(text: str) -> list[tuple[int, int]]:
    """Every absolute host address in ``text``, as non-overlapping spans, in order.

    Recognises POSIX paths, Windows drive paths (both / and \\), Windows UNC paths,
    and file:// URLs anywhere in text, including across path segments containing
    spaces, '=' and punctuation (e.g. '/home/user/my documents/project=1/file.txt').
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < len(text):
        m = _PATH_PREFIX_PATTERN.search(text, pos)
        if not m:
            break
        start = m.start()
        end = m.end()
        # Merge preceding scheme if directly adjacent (e.g. 'file:/', 'https:/')
        if start > 0 and text[start - 1] == ":":
            colon_idx = start - 1
            scheme_start = colon_idx
            while scheme_start > 0 and (
                text[scheme_start - 1].isalnum() or text[scheme_start - 1] in "+.-"
            ):
                scheme_start -= 1
            if scheme_start < colon_idx:
                start = scheme_start

        # Follow directory segments separated by / or \
        while True:
            next_slash = None
            for i in range(end, len(text)):
                if text[i] in "/\\":
                    next_slash = i
                    break
                if text[i] in "\"'`<>|\r\n":
                    break
            if next_slash is not None:
                seg_chars = text[end:next_slash]
                if any(c in "\"'`<>|\r\n" for c in seg_chars):
                    break
                end = next_slash + 1
            else:
                break

        # Consume leaf filename/segment
        leaf_start = end
        while end < len(text) and text[end] not in " \t\r\n\"'`<>|":
            end += 1
        # Strip trailing sentence punctuation
        while end > leaf_start and text[end - 1] in ".,;:!?)]}":
            end -= 1

        if end > start and text[start:end] != "/":
            spans.append((start, end))
        pos = max(end, m.end())
    return spans


def _contains_absolute_path(value: str) -> bool:
    """Does this string name a host address anywhere, not only at its start?"""
    if _looks_like_absolute_path(value):
        return True
    return bool(_absolute_path_spans(value))


def _redact_absolute_paths(value: str) -> tuple[str, bool]:
    """Replace every absolute host address in ``value``; report whether it did.

    Only the address is withheld, so the rest of the sentence survives.
    If the value as a whole represents an absolute address (including single-segment
    or paths with spaces/punctuation), the entire value becomes the marker.
    """
    spans = _absolute_path_spans(value)
    if _looks_like_absolute_path(value):
        if not spans:
            return _ABSOLUTE_PATH_MARKER, True
        if spans[0][0] == 0:
            tail = value[spans[0][1] :].strip()
            if not tail or ("/" not in tail and "\\" not in tail and len(tail.split()) <= 2):
                if not any(f" {w} " in f" {tail} " for w in ("und", "oder", "abgelegt", "wurde")):
                    return _ABSOLUTE_PATH_MARKER, True
    if not spans:
        if not _looks_like_absolute_path(value):
            return value, False
        return _ABSOLUTE_PATH_MARKER, True
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(value[cursor:start])
        pieces.append(_ABSOLUTE_PATH_MARKER)
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces), True


_INVALID_KNOWLEDGE_PATH_MARKER = "<invalid_knowledge_path>"


def _safe_knowledge_path(raw_path: str | None) -> str | None:
    """Non-leaking representation of knowledge_path for packet and manifest.

    A valid relative knowledge_path is returned unchanged.
    An absolute or invalid knowledge_path (which violates the write contract)
    is replaced with a fixed marker so that:
    1. No host path leaks into the packet, recent history, manifest, or warnings.
    2. Manifest verification treats any invalid path as fail-closed.
    """
    if raw_path is None:
        return None
    try:
        return validate_knowledge_path(raw_path, allow_reserved_segments=False)
    except KnowledgePathError:
        return _INVALID_KNOWLEDGE_PATH_MARKER


def _is_safe_knowledge_path(path: Any) -> bool:
    """Is this a valid, relative, non-marker knowledge path?"""
    if (
        not isinstance(path, str)
        or not path
        or path == _INVALID_KNOWLEDGE_PATH_MARKER
        or path.startswith("<")
    ):
        return False
    try:
        validate_knowledge_path(path, allow_reserved_segments=False)
        return True
    except KnowledgePathError:
        return False


def _bound_payload_key(key: Any, policy: WorkspacePolicy) -> tuple[str, bool]:
    """Bound one payload key, and withhold it entirely if it *is* a host address.

    Keys are part of the projection and were part of the leak: a legacy payload keyed by
    ``"/home/operator/GEHEIM/plan.pdf"`` reaches the client through the key even when
    every value is redacted. An address-shaped key is therefore replaced by the marker
    rather than shortened -- the consumer sees that a key existed and that its content
    named a host location. Two such keys collapse into one marker key; that is visible
    and count is not part of the contract, so it is preferred over leaking one of them.
    """
    key_str = str(key)
    redacted, dropped = _redact_absolute_paths(key_str)
    if dropped or _contains_absolute_path(key_str) or _looks_like_absolute_path(key_str):
        return _ABSOLUTE_PATH_MARKER, True
    return _truncate(redacted, policy.max_event_key_chars)[0] or "", False


def _bound_payload(value: Any, depth: int, policy: WorkspacePolicy) -> tuple[Any, bool]:
    """Project an event payload into a bounded value of the same JSON shape.

    The type is preserved so a consumer can still branch on it, but the size is
    not: keys, items, nesting depth and scalar length are all capped, and the second
    return value reports whether anything was dropped. Key order is the stored
    order, which is stable for unchanged data.

    Two bounds matter for work, not just for size:

    * containers are cut with :func:`itertools.islice`, never with ``list(...)``.
      A legacy payload may hold a few hundred thousand keys or items; copying it in
      full just to keep the first twelve is exactly the unbounded work this module
      refuses to do -- and this function runs once per selected event *per budget
      pass*.
    * an absolute host address is replaced by a marker in **values and keys**, because
      the packet promises no host addresses and a legacy ``CONTEXT_SOURCE_ADDED``
      payload contains one -- in the value of a field and, just as easily, in the name
      of that field.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        # Redaction runs *before* truncation: cutting first could leave the beginning
        # of an address in the packet while hiding its tail. Both bounds report through
        # the same flag, so ``payload_truncated`` still says "not all of it is here".
        redacted, dropped = _redact_absolute_paths(value)
        if _contains_absolute_path(redacted):
            redacted = _ABSOLUTE_PATH_MARKER
            dropped = True
        text, cut = _truncate(redacted, policy.max_event_value_chars)
        return text, dropped or cut
    if isinstance(value, (dict, list)):
        if depth >= policy.max_event_payload_depth:
            # Deliberately not serialised: ``json.dumps`` of the remaining subtree
            # would allocate megabytes to produce a 200-character result, and an
            # event payload column has no database size constraint.
            kind = "object" if isinstance(value, dict) else "array"
            return (
                _truncate(
                    f"<{kind} with {len(value)} entries, cut at depth {depth}>",
                    policy.max_event_value_chars,
                )[0]
                or "",
                True,
            )
        dropped = False
        if isinstance(value, dict):
            # One element beyond the cap is enough to know that something was cut.
            window = list(islice(value.items(), policy.max_event_payload_keys + 1))
            dropped = len(window) > policy.max_event_payload_keys
            bounded: dict[str, Any] = {}
            for key, item in window[: policy.max_event_payload_keys]:
                child, child_dropped = _bound_payload(item, depth + 1, policy)
                bounded_key, key_dropped = _bound_payload_key(key, policy)
                bounded[bounded_key] = child
                dropped = dropped or child_dropped or key_dropped
            return bounded, dropped
        window = list(islice(iter(value), policy.max_event_payload_items + 1))
        dropped = len(window) > policy.max_event_payload_items
        bounded_list: list[Any] = []
        for item in window[: policy.max_event_payload_items]:
            child, child_dropped = _bound_payload(item, depth + 1, policy)
            bounded_list.append(child)
            dropped = dropped or child_dropped
        return bounded_list, dropped
    return _truncate(str(value), policy.max_event_value_chars)


def _page_span(metadata: Any, char_start: int, char_end: int) -> tuple[int | None, int | None]:
    """Best-effort page range for a character range inside a snapshot.

    The exact extractor metadata shape belongs to the ingestion component, so this
    is a defensive scan rather than a schema assumption: it recognises the obvious
    span shapes and returns ``(None, None)`` otherwise. A missing page range is not
    an error -- the character range is the authoritative locator and is always
    present. Returning a guessed page number would be worse than returning none,
    because a page citation is checkable evidence.
    """
    if not isinstance(metadata, dict):
        return None, None
    for key in ("page_spans", "pages", "page_ranges", "spans"):
        spans = metadata.get(key)
        if not isinstance(spans, list):
            continue
        for span in spans:
            if not isinstance(span, dict):
                continue
            start = _first_int(span, ("char_start", "start", "offset_start", "from"))
            end = _first_int(span, ("char_end", "end", "offset_end", "to"))
            page_start = _first_int(span, ("page_start", "page", "page_number"))
            page_end = _first_int(span, ("page_end",)) or page_start
            if start is None or end is None or page_start is None:
                continue
            if start <= char_start and char_end <= end:
                return page_start, page_end
    return None, None


def _source_label(source: Any) -> str:
    """Relative, non-identifying label for a source.

    Only the last segment of the registered path is used. The full path is an
    absolute host address, and a source registration is not a read permission, so
    the packet carries no more than a name to show the user.
    """
    raw = (getattr(source, "path", None) or "").replace("\\", "/").rstrip("/")
    tail = raw.rsplit("/", 1)[-1] if raw else ""
    return tail or (getattr(source, "source_type", None) or "")
