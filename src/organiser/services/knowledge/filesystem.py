"""One root and process-local lock for guarded reads and atomic file replacement."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import threading
from pathlib import Path
from organiser.domain.knowledge_path import (
    KnowledgePathError,
    validate_knowledge_path,
)
from .contracts import (
    DIRECTORY_MODE,
    FILE_MODE,
    KnowledgeError,
    KnowledgeNotFoundError,
    KnowledgeWrite,
    TOPICS_DIR,
    UnsafeKnowledgePathError,
)
from .paths import _slug


class KnowledgeFilesystem:
    """Own one root and one process-local reentrant lock for guarded filesystem access."""

    def __init__(self, root: Path | str) -> None:
        resolved = Path(root).expanduser().resolve()
        if resolved.exists() and not resolved.is_dir():
            raise KnowledgeError(f"Knowledge Root ist kein Verzeichnis: {resolved}")
        self._root = resolved
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def root_ready(self) -> Path:
        """Create the root on demand; ``__init__`` already resolved it to a real path."""
        self._root.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise UnsafeKnowledgePathError(f"Knowledge Root ist kein Verzeichnis: {self._root}")
        return self._root

    @staticmethod
    def relative_parts(context_path: str, *, allow_reserved: bool = False) -> tuple[str, ...]:
        """Validate a model supplied path and return its canonical relative segments.

        The contract is strict on purpose: the input must already be a canonical
        relative POSIX path. Nothing is silently normalised, so a caller never gets
        a different path back than it asked for. Validation has no filesystem
        side effects.

        Store-reserved segments are rejected here on purpose: a caller that wants
        to write ``topics/...`` or ``overview.md`` addresses the store through
        ``topic_path``/``read``, never by smuggling a reserved name in as a context
        path segment.
        """
        try:
            validate_knowledge_path(
                context_path, field="context_path", allow_reserved_segments=allow_reserved
            )
        except KnowledgePathError as error:
            raise UnsafeKnowledgePathError(str(error)) from error
        return tuple(context_path.split("/"))

    def resolve(self, parts: tuple[str, ...]) -> Path:
        """Join validated segments below the root and prove the result stays inside."""
        root = self.root_ready()
        candidate = root.joinpath(*parts)
        real_root = Path(os.path.realpath(root))
        real_candidate = Path(os.path.realpath(candidate))
        if real_candidate != real_root and real_root not in real_candidate.parents:
            raise UnsafeKnowledgePathError(f"Pfad verlaesst den Knowledge Root: {candidate}")
        current = root
        for index, part in enumerate(parts):
            current = current / part
            if current.is_symlink():
                raise UnsafeKnowledgePathError(f"Pfad enthaelt einen Symlink: {current}")
            is_last_file = index == len(parts) - 1 and part.endswith(".md")
            if not is_last_file and current.exists() and not current.is_dir():
                raise UnsafeKnowledgePathError(f"Pfadsegment ist kein Verzeichnis: {current}")
        return candidate

    def context_directory(self, context_path: str) -> Path:
        """Public, path-safe accessor for the directory of one context."""
        with self._lock:
            return self.resolve(self.relative_parts(context_path))

    def topic_path(self, context_path: str, topic: str) -> Path:
        """Public, path-safe accessor for the markdown file of one topic."""
        parts = self.relative_parts(context_path)
        stem = _slug(topic)
        return self.resolve(parts + (TOPICS_DIR, f"{stem}.md"))

    @staticmethod
    def atomic_write(path: Path, content: str) -> None:
        """Write via a temporary file in the same directory and replace atomically.

        An interrupted write leaves either the previous or the new content behind,
        never a truncated file. The temporary file lives in the target directory so
        ``os.replace`` stays atomic (same filesystem).
        """
        path.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, FILE_MODE)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        except OSError:
            pass
        finally:
            os.close(directory)

    def guard(self, path: Path, *, expect: str) -> Path:
        """Approve a path before any access, or raise.

        ``expect`` is ``"file"``, ``"dir"`` or ``"any"``. Symlinks are refused
        wherever they point, and anything that is not a regular file or directory
        (FIFO, socket, device) is refused. This runs *before* ``read_text``,
        ``exists`` or a directory listing, so a rejected path is never touched.
        """
        if path.is_symlink():
            raise UnsafeKnowledgePathError(f"Symlink wird nicht unterstuetzt: {path}")
        try:
            status = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            # A path that does not exist yet is not a security problem; callers that
            # require it to exist check ``exists()`` right after this guard.
            return path
        except OSError as error:
            raise UnsafeKnowledgePathError(f"Pfad nicht pruefbar: {path} ({error})") from error
        if not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
            raise UnsafeKnowledgePathError(
                f"Pfad ist keine regulaere Datei oder kein Verzeichnis: {path}"
            )
        if expect == "file" and not stat.S_ISREG(status.st_mode):
            raise UnsafeKnowledgePathError(f"Pfad ist keine regulaere Datei: {path}")
        if expect == "dir" and not stat.S_ISDIR(status.st_mode):
            raise UnsafeKnowledgePathError(f"Pfad ist kein Verzeichnis: {path}")
        real_root = Path(os.path.realpath(self._root))
        real_path = Path(os.path.realpath(path))
        if real_path != real_root and real_root not in real_path.parents:
            raise UnsafeKnowledgePathError(f"Pfad verlaesst den Knowledge Root: {path}")
        return path

    def guard_context_file(self, directory: Path, name: str) -> Path:
        """Approve ``directory / name`` and require it to be a real file if present."""
        path = directory / name
        self.guard(path, expect="any")
        if path.exists():
            self.guard(path, expect="file")
        return path

    def write(self, path: Path, content: str) -> KnowledgeWrite:
        self.guard(path, expect="any")
        existing = self.read_guarded(path)
        if existing == content:
            return KnowledgeWrite(path=path, content=content, created=False, changed=False)
        self.atomic_write(path, content)
        return KnowledgeWrite(path=path, content=content, created=existing is None, changed=True)

    def read_guarded(self, path: Path) -> str | None:
        """Read a knowledge file, refusing anything that is not a regular file."""
        self.guard(path, expect="any")
        if not path.exists():
            return None
        self.guard(path, expect="file")
        return path.read_text(encoding="utf-8")

    def read_or_default(self, path: Path, default: str) -> str:
        content = self.read_guarded(path)
        return default if content is None else content

    def read(self, relative_path: str) -> str:
        """Read a markdown file below the knowledge root; used for context packets.

        Reserved store names are allowed here: this is the only entry point that
        legitimately addresses ``overview.md``/``topics/...`` as a file.
        """
        parts = self.relative_parts(relative_path, allow_reserved=True)
        if not parts[-1].endswith(".md"):
            raise UnsafeKnowledgePathError(
                "gelesene Knowledge-Dateien muessen Markdown-Dateien sein"
            )
        with self._lock:
            path = self.guard(self.resolve(parts), expect="any")
            if not path.exists():
                raise KnowledgeNotFoundError(f"Knowledge-Datei nicht gefunden: {'/'.join(parts)}")
            self.guard(path, expect="file")
            return path.read_text(encoding="utf-8")

    def open_below_root(self, parts: tuple[str, ...], *, expect_file: bool) -> int:
        """Open a validated path component by component and return a file descriptor.

        Every component is opened with ``O_NOFOLLOW`` *relative to the descriptor of
        its parent*, so a segment that is swapped for a symlink between the check and
        the open fails with ``ELOOP`` instead of redirecting the read. Checking
        ``is_symlink()`` on the assembled path is not enough for that: it says
        something about the moment of the check, not about the moment of the open.
        ``O_NONBLOCK`` additionally keeps a final component that was replaced by a
        FIFO from blocking this thread indefinitely.

        Each parent descriptor is closed as soon as the child is open, because the
        child descriptor alone is enough to reach the file and a long-lived process
        reading many excerpts would otherwise exhaust its descriptor limit after a few
        hundred reads.

        Nothing is created. ``O_DIRECTORY`` on the intermediate descriptors is itself
        the check that a segment is a directory.
        """
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        if not self._root.is_dir():
            raise KnowledgeNotFoundError(f"Knowledge Root existiert nicht: {self._root}")
        try:
            current = os.open(self._root, directory_flags)
        except OSError as error:
            # ``is_dir()`` follows a symlink; the descriptor open below does not. A
            # root that turned into a symlink between those two calls therefore used
            # to leave this function with a raw ``OSError`` (ENOTDIR on Linux), which
            # is outside the ``KnowledgeError`` family every caller catches -- the
            # workspace builder only knows how to skip those, so one hostile or merely
            # surprising root turned a bounded GET into an HTTP 500.
            raise UnsafeKnowledgePathError(
                f"Knowledge Root nicht lesbar: {self._root} ({error})"
            ) from error
        try:
            for index, part in enumerate(parts):
                is_last = index == len(parts) - 1
                last_is_file = is_last and part.endswith(".md")
                flags = file_flags if (is_last and expect_file) else directory_flags
                try:
                    child = os.open(part, flags, dir_fd=current)
                except OSError as error:
                    if is_last and not last_is_file and error.errno in (errno.ENOTDIR, errno.ELOOP):
                        # A symlinked directory surfaces as ENOTDIR here, because
                        # O_DIRECTORY and O_NOFOLLOW are combined.
                        raise UnsafeKnowledgePathError(
                            f"Pfadsegment ist kein Verzeichnis: {part}"
                        ) from error
                    if error.errno == errno.ENOENT:
                        raise KnowledgeNotFoundError(
                            f"Knowledge-Datei nicht gefunden: {'/'.join(parts)}"
                        ) from error
                    raise UnsafeKnowledgePathError(
                        f"Pfad nicht lesbar: {part} ({error})"
                    ) from error
                os.close(current)
                current = child
        except BaseException:
            os.close(current)
            raise
        return current

    def resolve_for_read(self, parts: tuple[str, ...]) -> Path:
        """Join validated segments below the root *without creating anything*.

        Deliberately separate from :meth:`_resolve`: that one calls
        ``_root_ready()`` and would therefore create the knowledge root as a side
        effect of a read. A read model must never bring a store into existence --
        an uninitialised context has to stay visibly uninitialised instead of
        gaining an empty directory tree.

        Only a non-creating check: the authoritative access is
        :meth:`_open_below_root`, which resolves the same segments through file
        descriptors. Neither this method nor the open creates or opens anything
        that is not already there.
        """
        root = self._root
        if not root.is_dir():
            raise KnowledgeNotFoundError(f"Knowledge Root existiert nicht: {root}")
        candidate = root
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise UnsafeKnowledgePathError(f"Pfad enthaelt einen Symlink: {candidate}")
        real_root = Path(os.path.realpath(root))
        real_candidate = Path(os.path.realpath(candidate))
        if real_candidate != real_root and real_root not in real_candidate.parents:
            raise UnsafeKnowledgePathError(f"Pfad verlaesst den Knowledge Root: {candidate}")
        return candidate
