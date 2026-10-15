"""Lossless Markdown structure, bounded sections and controlled document rendering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from .contracts import (
    CORE_HEADINGS,
    KnowledgeContentError,
    MAX_SECTION_ENTRIES,
    MAX_TEXT_LENGTH,
    _DOCUMENT_TITLE,
    _FENCE,
    _HISTORY_ENTRY,
    _SECTION_HEADING,
)


@dataclass(frozen=True)
class _Document:
    """A parsed knowledge markdown file.

    ``preamble`` is everything before the first ``##`` heading (usually the H1
    title plus optional free text). It is kept verbatim so an update never
    discards content the store does not understand.
    """

    title: str
    preamble: str
    sections: list[tuple[str, str]]

    def render(self) -> str:
        blocks: list[str] = [self.preamble.strip("\n")] if self.preamble.strip("\n") else []
        for heading, body in self.sections:
            blocks.append(f"## {heading}\n\n{body}" if body else f"## {heading}")
        return "\n\n".join(blocks).rstrip("\n") + "\n"

    def body_of(self, heading: str) -> str:
        matches = [body for title, body in self.sections if title.casefold() == heading.casefold()]
        if len(matches) > 1:
            raise KnowledgeContentError(
                f"Abschnitt '{heading}' kommt mehrfach vor; die Datei ist nicht eindeutig bearbeitbar"
            )
        return matches[0] if matches else ""

    def require_editable(self) -> "_Document":
        """Refuse a document whose managed sections are ambiguous.

        Duplicated core headings (for example ``## Status`` and ``## status``) make it
        impossible to decide which block is authoritative. Rather than guessing and
        silently dropping one of them, the caller gets a controlled error.
        """
        counts: dict[str, int] = {}
        for title, _ in self.sections:
            counts[title.casefold()] = counts.get(title.casefold(), 0) + 1
        duplicated = sorted(
            title
            for title, count in counts.items()
            if count > 1 and title in {h.casefold() for h in CORE_HEADINGS}
        )
        if duplicated:
            raise KnowledgeContentError(
                f"Verwaltete Abschnitte kommen mehrfach vor: {', '.join(duplicated)}; "
                "die Datei ist nicht eindeutig bearbeitbar"
            )
        return self

    def with_body(self, heading: str, body: str, *, append: bool = False) -> "_Document":
        """Return a copy with one section replaced or extended.

        Replace writes ``body`` as the complete new section body. Append adds
        ``body`` as one further block and recognises a repeated block by
        comparing the whole block against the tail of the section, never only
        its last paragraph.
        """
        for index, (existing, _) in enumerate(self.sections):
            if existing.casefold() == heading.casefold():
                if not append:
                    return self._replaced(index, existing, body)
                current = self.sections[index][1].strip("\n")
                incoming = body.strip()
                if not incoming or _ends_with_block(current, incoming):
                    return self
                # An empty placeholder is filled instead of being kept as a lead-in.
                combined = (
                    incoming
                    if not current or current == "_noch leer_"
                    else f"{current}\n\n{incoming}"
                )
                return self._replaced(index, existing, combined)
        if len(self.sections) >= MAX_SECTION_ENTRIES:
            raise KnowledgeContentError("Dokument hat zu viele Abschnitte")
        return self._replaced(len(self.sections), heading, body)

    def _replaced(self, index: int, heading: str, body: str) -> "_Document":
        sections = list(self.sections)
        if index >= len(sections):
            sections.append((heading, body))
        else:
            sections[index] = (heading, body)
        return _Document(self.title, self.preamble, sections)

    def with_body_first(self, heading: str, body: str) -> "_Document":
        """Drop any existing occurrence of ``heading`` and put it back at the front.

        The managed ``Status`` block always stays the first section, no matter how
        the file was edited by hand.
        """
        kept = [item for item in self.sections if item[0].casefold() != heading.casefold()]
        return _Document(self.title, self.preamble, [(heading, body), *kept])


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _sanitize_text(text: str, *, field: str = "text") -> str:
    if not isinstance(text, str):
        raise KnowledgeContentError(f"{field} muss Text sein")
    if len(text) > MAX_TEXT_LENGTH:
        raise KnowledgeContentError(f"{field} ueberschreitet {MAX_TEXT_LENGTH} Zeichen")
    if "\x00" in text:
        raise KnowledgeContentError(f"{field} darf kein Nullzeichen enthalten")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def _fence_scan(lines: list[str]) -> tuple[list[bool], bool]:
    """Mark fenced lines and report whether a code fence is left open.

    A ``##`` heading inside a fence is example text, not a section, so it must
    never be treated as an editable boundary.
    """
    inside: list[bool] = []
    fence: str | None = None
    for line in lines:
        match = _FENCE.match(line)
        if fence is None:
            if match:
                fence = match.group("fence")
                inside.append(True)
            else:
                inside.append(False)
        else:
            inside.append(True)
            if (
                match
                and match.group("fence")[0] == fence[0]
                and len(match.group("fence")) >= len(fence)
            ):
                fence = None
    return inside, fence is not None


def _ends_with_block(current: str, incoming: str) -> bool:
    """Is ``incoming`` already the block that was appended last?

    The comparison is deliberately structural: it holds for a block with any
    number of paragraphs, but not when ``incoming`` is only a suffix of the last
    block, so a genuinely new block that happens to start like the previous one
    is still appended. This is what makes an immediate identical repeat a no-op
    without a retry history.
    """
    if not incoming:
        return False
    return current == incoming or current.endswith(f"\n\n{incoming}")


def _reject_ambiguous_structure(text: str, *, field: str = "text") -> None:
    """Refuse text that would silently redefine the document structure.

    A ``##`` line outside a fenced code block opens a new store-managed section,
    and a code fence that is never closed would swallow every section that
    follows it. Both make the document ambiguous rather than merely unusual: the
    target section would no longer hold the whole text, a repeated write would
    duplicate only a fragment, and a later update could drop content. The store
    refuses such input before anything is written instead of guessing which part
    was meant. An H2 line inside a *closed* fence stays valid content.
    """
    lines = text.splitlines()
    fenced, unclosed = _fence_scan(lines)
    for line, inside in zip(lines, fenced):
        if not inside and _SECTION_HEADING.match(line):
            raise KnowledgeContentError(
                f"{field} darf keine eigene H2-Ueberschrift enthalten: {line.strip()!r}; "
                "der Store verwaltet Abschnittsgrenzen selbst"
            )
    if unclosed:
        raise KnowledgeContentError(f"{field} hat einen nicht geschlossenen Codeblock")


def _check_heading(heading: str) -> str:
    """Return a section heading that cannot open further sections."""
    cleaned = heading.strip()
    if not cleaned or "\n" in cleaned or "\r" in cleaned or cleaned.startswith("#"):
        raise KnowledgeContentError(f"Abschnittstitel ist kein einfacher Titel: {heading!r}")
    return cleaned


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """Kuerze Text auf ``limit`` Zeichen und melde, ob gekuerzt wurde.

    Der Marker zaehlt zum Budget: das Ergebnis ist nie laenger als ``limit``, damit
    ein Aufrufer die Grenze durch Addition mehrerer Felder nicht versehentlich
    reisst.
    """
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text, False
    marker = " [...]"
    if limit <= len(marker):
        return text[:limit], True
    return text[: limit - len(marker)] + marker, True


class MarkdownCodec:
    """Lossless Markdown parsing and document construction; no filesystem effects."""

    @staticmethod
    def _document(title: str, sections: list[tuple[str, str]]) -> str:
        lines = [f"# {title.strip()}", ""]
        for heading, body in sections:
            lines.append(f"## {heading.strip()}")
            lines.append("")
            lines.append(body.strip("\n") if body.strip("\n") else "_noch leer_")
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"

    @classmethod
    def _progress_document(cls, recorded_at: datetime | None = None) -> str:
        status_block = _timestamped_status(_status_lines("", None, None), recorded_at or _now())
        sections = [
            ("Status", status_block),
            ("Fortschritt", "Noch kein Fortschritt dokumentiert."),
        ]
        return cls._document("Fortschritt", sections)

    @staticmethod
    def _split_fences(lines: list[str]) -> list[bool]:
        """Mark which lines sit inside a fenced code block."""
        return _fence_scan(lines)[0]

    @classmethod
    def _parse(cls, content: str) -> _Document:
        """Split markdown into a title block and its ``##`` sections, losslessly.

        Everything before the first ``##`` heading becomes ``preamble`` and is kept
        verbatim, so free text before the first section survives every update. A
        document without any ``##`` heading keeps its whole text as ``preamble``.
        A ``##`` line inside a fenced code block is example text, not a section.
        """
        if not content.strip():
            return _Document("", "", [])
        lines = content.splitlines()
        in_fence = cls._split_fences(lines)

        headings: list[tuple[int, str]] = []
        for index, (line, fenced) in enumerate(zip(lines, in_fence)):
            if fenced:
                continue
            match = _SECTION_HEADING.match(line)
            if match:
                headings.append((index, match.group("title")))

        if not headings:
            return _Document("", content.strip("\n"), [])

        preamble = "\n".join(lines[: headings[0][0]]).strip("\n")
        first_line = next((line for line in preamble.splitlines() if line.strip()), "")
        title_match = _DOCUMENT_TITLE.match(first_line)
        title = title_match.group("title") if title_match else ""

        sections: list[tuple[str, str]] = []
        for position, (index, heading) in enumerate(headings):
            start = index + 1
            end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
            sections.append((heading, "\n".join(lines[start:end]).strip("\n")))
        return _Document(title, preamble, sections)


def parts_title(context_path: str) -> str:
    """Fallback document title taken from the last context path segment."""
    segments = [part for part in context_path.replace("\\", "/").split("/") if part]
    return segments[-1] if segments else "Knowledge"


def _status_lines(
    current: str, status: str | None, percent: int | None, *, keep_timestamp: bool = False
) -> list[str]:
    """Compute the status lines without a timestamp.

    With ``keep_timestamp`` the existing ``Aktualisiert`` value is preserved, which
    lets a caller detect a semantically unchanged status before adding a new one.
    """
    lines = [line for line in current.splitlines() if line.strip()]
    known = {
        line.lstrip("- ").split(":", 1)[0].strip().casefold(): index
        for index, line in enumerate(lines)
        if ":" in line
    }

    def set_value(label: str, value: str) -> None:
        key = label.casefold()
        if key in known:
            lines[known[key]] = f"- {label}: {value}"
        else:
            known[key] = len(lines)
            lines.append(f"- {label}: {value}")

    if status is not None:
        set_value("Status", status)
    elif not lines:
        set_value("Status", "unbekannt")
    if percent is not None:
        set_value("Fortschritt", f"{percent} %")
    if not keep_timestamp:
        set_value("Aktualisiert", "")
    return [line for line in lines if not line.endswith(": ")]


def _status_semantics(current: str, status: str | None, percent: int | None) -> tuple[str, ...]:
    """The status block without its timestamp, for change detection."""
    return tuple(
        line
        for line in _status_lines(current, status, percent, keep_timestamp=True)
        if "Aktualisiert" not in line
    )


def _timestamped_status(lines: list[str], moment: datetime) -> str:
    stamped = [line for line in lines if not line.lstrip("- ").startswith("Aktualisiert:")]
    stamped.append(f"- Aktualisiert: {_timestamp(moment)}")
    return "\n".join(stamped)


def _last_history_facts(history: str) -> str:
    """Facts of the newest history entry, without its timestamp.

    The timestamp is stripped structurally, not by splitting on spaces, because it
    contains spaces itself.
    """
    for line in reversed(history.splitlines()):
        match = _HISTORY_ENTRY.match(line.strip())
        if match:
            return match.group("facts")
    return ""


def _history_entry(
    body: str | None, status: str | None, percent: int | None, moment: datetime
) -> str:
    return f"- {_timestamp(moment)} {_history_facts(body, status, percent)}".rstrip()


def _history_facts(body: str | None, status: str | None, percent: int | None) -> str:
    parts: list[str] = []
    if status is not None:
        parts.append(f"Status: {status}")
    if percent is not None:
        parts.append(f"Fortschritt: {percent} %")
    if body:
        first_line = next((line for line in body.splitlines() if line.strip()), "")
        parts.append(first_line.strip()[:200])
    return " ".join(parts)
