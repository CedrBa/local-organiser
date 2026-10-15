"""Bounded, non-creating evidence reads through the shared filesystem component."""

from __future__ import annotations

import hashlib
import os
import stat
from .contracts import (
    KnowledgeContentError,
    KnowledgeExcerptRead,
    KnowledgeExcerptTooLargeError,
    KnowledgeNotFoundError,
    KnowledgeUnstableError,
    MAX_EXCERPT_BYTES,
    MAX_TOPIC_FILE_SCAN,
    TOPICS_DIR,
    UnsafeKnowledgePathError,
)
from .document import _truncate_text
from .filesystem import KnowledgeFilesystem


class KnowledgeReader:
    """Read bounded evidence through the shared filesystem adapter; never create files."""

    def __init__(self, files: KnowledgeFilesystem) -> None:
        self._files = files

    def read_excerpt(
        self, relative_path: str, max_chars: int, max_bytes: int = MAX_EXCERPT_BYTES
    ) -> KnowledgeExcerptRead:
        """Read a bounded excerpt plus the complete file hash, without any write.

        Guarantees, in the order they are enforced:

        * The knowledge root is never created; a missing root is a plain
          ``KnowledgeNotFoundError``.
        * Only regular files below the root are read. Symlinks (also in
          intermediate segments), FIFOs, sockets and devices are refused, and every
          path component -- not just the last one -- is opened with ``O_NOFOLLOW``
          relative to the previously opened directory descriptor, so a swap between
          check and open cannot redirect the read. ``O_NONBLOCK`` keeps a file that
          was replaced by a FIFO in that window from blocking the reader forever;
          it has no effect on a regular file.
        * The size limit is checked *before* the read from ``lstat`` and again
          from ``fstat`` of the open descriptor. ``max_bytes`` is clamped to
          :data:`MAX_EXCERPT_BYTES`; a caller can only lower it.
        * A file that grew or was replaced while being read is refused as
          :class:`KnowledgeUnstableError` rather than reported as a consistent
          excerpt.
        * ``content_hash`` covers the complete file bytes, which is only defined
          because the file is within the size limit.
        * ``text`` is decoded as UTF-8 with replacement and cut to ``max_chars``;
          ``truncated`` reports that cut.

        This method never writes, never creates a directory and never learns
        anything. It is the only filesystem access the workspace reader has.
        """
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            raise KnowledgeContentError("max_chars muss eine positive ganze Zahl sein")
        if max_bytes is None:
            limit = MAX_EXCERPT_BYTES
        elif isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise KnowledgeContentError("max_bytes muss eine ganze Zahl sein")
        else:
            limit = max(1, min(max_bytes, MAX_EXCERPT_BYTES))
        parts = self._files.relative_parts(relative_path, allow_reserved=True)
        if not parts[-1].endswith(".md"):
            raise UnsafeKnowledgePathError(
                "gelesene Knowledge-Dateien muessen Markdown-Dateien sein"
            )
        with self._files._lock:
            path = self._files.resolve_for_read(parts)
            try:
                before = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                raise KnowledgeNotFoundError(
                    f"Knowledge-Datei nicht gefunden: {'/'.join(parts)}"
                ) from None
            except OSError as error:
                raise UnsafeKnowledgePathError(f"Pfad nicht pruefbar: {path} ({error})") from error
            if not stat.S_ISREG(before.st_mode):
                raise UnsafeKnowledgePathError(f"Pfad ist keine regulaere Datei: {path}")
            if before.st_size > limit:
                raise KnowledgeExcerptTooLargeError(
                    f"Knowledge-Datei {'/'.join(parts)} ist {before.st_size} Bytes und ueberschreitet die "
                    f"Lesegrenze von {limit} Bytes; sie wird uebersprungen"
                )
            descriptor = self._files.open_below_root(parts, expect_file=True)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise UnsafeKnowledgePathError(f"Pfad ist keine regulaere Datei: {path}")
                if opened.st_size > limit:
                    raise KnowledgeExcerptTooLargeError(
                        f"Knowledge-Datei {'/'.join(parts)} ist {opened.st_size} Bytes und ueberschreitet die "
                        f"Lesegrenze von {limit} Bytes; sie wird uebersprungen"
                    )
                data = stream.read(limit + 1)
                final = os.fstat(stream.fileno())
            if len(data) > limit:
                raise KnowledgeExcerptTooLargeError(
                    f"Knowledge-Datei {'/'.join(parts)} waechst ueber die Lesegrenze von {limit} Bytes; "
                    "sie wird uebersprungen"
                )
            if (final.st_size, final.st_mtime_ns, final.st_ino) != (
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ino,
            ):
                raise KnowledgeUnstableError(
                    f"Knowledge-Datei {'/'.join(parts)} wurde waehrend des Lesens veraendert; "
                    "der Auszug waere kein konsistenter Stand"
                )
            text = data.decode("utf-8", errors="replace")
            excerpt, truncated = _truncate_text(text, max_chars)
            return KnowledgeExcerptRead(
                relative_path="/".join(parts),
                text=excerpt,
                truncated=truncated,
                content_hash=hashlib.sha256(data).hexdigest(),
                byte_size=len(data),
                char_length=len(text),
            )

    def list_topic_files(self, context_path: str, *, limit: int = 64) -> tuple[str, ...]:
        """Sorted, bounded, read-only names of the topic files of one context.

        Same no-symlink policy as :meth:`read_excerpt`, and it creates nothing: a
        missing root or missing ``topics/`` directory is an empty result, not a new
        directory. Symlinked and non-regular entries are skipped instead of followed.
        ``list_topics`` keeps its existing behaviour for the write path; this method
        exists so a read model can enumerate legacy topic files without acquiring a
        filesystem side effect.

        An empty result means *absent*, and only that. A ``topics/`` that exists but is
        not a plain directory -- a regular file, a symlink, a device -- is refused with
        :class:`UnsafeKnowledgePathError`, exactly as the write path refuses it and for
        the same reason: ``O_DIRECTORY`` with ``O_NOFOLLOW`` cannot distinguish "not a
        directory" from "a directory that was swapped for one", and both are a store
        whose layout this module is not willing to guess at. Returning an empty tuple
        there would be worse than an error, because it is indistinguishable from "this
        context has no legacy topic files" -- the read model would then report a
        complete enumeration of a directory it never saw. Callers inside the
        ``KnowledgeError`` family (the workspace builder) turn it into a visible warning.

        The enumeration is descriptor-based, like :meth:`read_excerpt`, and that is not
        a detail. Checking ``topics/`` by path and then handing that path to ``scandir``
        left a window in which the directory could be replaced between the two calls --
        so the very policy this method advertises ("symlinks are skipped, not followed")
        was only true for the check and not for the read. Scanning the opened descriptor
        closes it: ``DirEntry.is_symlink`` and ``DirEntry.stat`` then resolve *below that
        descriptor*, and a swap of the directory cannot redirect the enumeration.

        The bound counts **every** entry the directory yields, not only the accepted
        ones. Counting the names that were kept let a directory full of ``.txt`` files,
        sockets or dangling symlinks through without ever reaching the limit, so five
        thousand such entries were walked to find two markdown files -- and each entry
        costs a name, an ``is_symlink`` and possibly an ``lstat``, which is exactly the
        work the bound exists for. ``next`` rather than ``for``: a ``for`` loop has
        already pulled the (N+1)-th entry by the time its body can break, so it would
        honour the limit only cosmetically.

        What that does and does not buy: it removes the check/use window inside this
        process. It is not a guarantee against a concurrent writer -- the store is a
        single-writer, single-process design, and a hostile thread rewriting the tree
        during the enumeration is out of scope. The names that were read are a consistent
        sample of *one* directory, not a transaction over the tree. The truncation point
        is arbitrary, which is why the selection is documented as best-effort for a
        pathologically large legacy directory.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise KnowledgeContentError("limit muss eine nichtnegative ganze Zahl sein")
        if limit == 0:
            return ()
        parts = self._files.relative_parts(context_path)
        with self._files._lock:
            try:
                descriptor = self._files.open_below_root((*parts, TOPICS_DIR), expect_file=False)
            except KnowledgeNotFoundError:
                return ()
            stems: list[str] = []
            examined = 0
            try:
                with os.scandir(descriptor) as entries:
                    iterator = iter(entries)
                    while examined < MAX_TOPIC_FILE_SCAN:
                        entry = next(iterator, None)
                        if entry is None:
                            break
                        examined += 1
                        if not entry.name.endswith(".md") or entry.is_symlink():
                            continue
                        try:
                            status = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if not stat.S_ISREG(status.st_mode):
                            continue
                        stems.append(entry.name[: -len(".md")])
            except OSError as error:
                raise UnsafeKnowledgePathError(
                    f"Topic-Verzeichnis nicht lesbar: {TOPICS_DIR} ({error})"
                ) from error
            finally:
                # ``os.scandir(fd)`` documents that it does *not* close the descriptor,
                # and every exit path -- including the OSError above -- has to.
                os.close(descriptor)
            return tuple(sorted(stems)[:limit])
