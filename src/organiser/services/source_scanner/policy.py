"""Operator-owned source roots and exclusions, independent of DB registration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from organiser.core.config import PROJECT_ROOT, Settings
from organiser.domain.resources import (
    CODE_SOURCE_EXCLUDED,
    CODE_SOURCE_NOT_ALLOWED,
    SourceAccessError,
    canonical_locator,
    is_excluded_component,
    is_within,
)


@dataclass(frozen=True)
class IngestionPolicy:
    """The read allowlist, derived from operator settings only.

    ``allowed_roots`` comes from ``ORGANISER_SOURCE_ALLOWED_ROOTS`` and is empty
    by default, which denies everything. ``excluded_roots`` is the mirror image:
    the knowledge root and the project root are readable by nobody as a source,
    because their content is owned by another subsystem and is addressed through
    the database, not through a file walk.
    """

    allowed_roots: tuple[str, ...] = ()
    excluded_roots: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Settings) -> "IngestionPolicy":
        """Derive allowed roots and exclusions once from operator-owned settings."""
        excluded: list[str] = []
        for candidate in (settings.knowledge_root, PROJECT_ROOT):
            try:
                resolved = str(Path(candidate).resolve())
            except (OSError, ValueError):  # pragma: no cover - defensive
                continue
            if resolved not in excluded:
                excluded.append(resolved)
        allowed: list[str] = []
        for root in settings.source_allowed_roots:
            value = canonical_locator(str(root), field="source_allowed_roots")
            if value not in allowed:
                allowed.append(value)
        return cls(allowed_roots=tuple(allowed), excluded_roots=tuple(excluded))

    def with_allowed_roots(self, roots: tuple[str, ...]) -> "IngestionPolicy":
        """Return a narrowed policy for explicitly supplied roots; retain safety exclusions."""
        return replace(self, allowed_roots=tuple(roots))

    def root_for(self, locator: str) -> str | None:
        """Return the allowed root that contains ``locator``, or ``None``."""
        matches = [root for root in self.allowed_roots if is_within(root, locator)]
        if not matches:
            return None
        return max(matches, key=len)

    def excluded_reason(self, locator: str) -> str | None:
        """Return the stable exclusion reason for a path, or None when not excluded."""
        for root in self.excluded_roots:
            if is_within(root, locator):
                return root
        return None

    def authorize(self, locator: str, *, expect: str = "dir") -> str:
        """Return the allowed root for ``locator`` or refuse the access.

        ``expect`` is ``"dir"`` for a folder source and ``"file"`` for a document
        or note source. It decides which path components are directory names and
        therefore subject to the hidden/VCS/venv/build exclusion.

        Refusal codes are specific on purpose: a client needs to tell "not
        configured" from "configured, but that is the knowledge root" from
        "configured, but that is a hidden directory".
        """
        if expect not in ("dir", "file"):
            raise ValueError("expect muss 'dir' oder 'file' sein")
        if not self.allowed_roots:
            raise SourceAccessError(
                "Es ist kein Quellwurzelverzeichnis freigegeben",
                code=CODE_SOURCE_NOT_ALLOWED,
                reason="no_allowed_roots_configured",
            )
        root = self.root_for(locator)
        if root is None:
            raise SourceAccessError(
                "Der Quellpfad liegt nicht unter einem freigegebenen Wurzelverzeichnis",
                code=CODE_SOURCE_NOT_ALLOWED,
                reason="outside_allowed_roots",
            )
        if self.excluded_reason(locator) is not None:
            raise SourceAccessError(
                "Wissens- und Projektverzeichnisse sind keine Quellen",
                code=CODE_SOURCE_EXCLUDED,
                reason="excluded_root",
            )
        parts = [part for part in locator[len(root) :].strip("/").split("/") if part]
        directory_parts = parts[:-1] if expect == "file" else parts
        candidates = [*directory_parts, *([parts[-1]] if expect == "file" and parts else [])]
        for part in candidates:
            if is_excluded_component(part):
                raise SourceAccessError(
                    "Versteckte, VCS-, Venv- oder Build-Verzeichnisse sind keine Quellen",
                    code=CODE_SOURCE_EXCLUDED,
                    reason="excluded_component",
                )
        return root
