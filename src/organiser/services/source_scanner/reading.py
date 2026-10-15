"""Bounded file reads and hashes with identity and short-read checks."""

from __future__ import annotations

import hashlib
import os
import stat
from organiser.domain.resources import (
    DEFAULT_LIMITS,
    IngestionLimits,
    ResourceIngestionError,
    SourceFileChangedError,
    SourceFileShortReadError,
    SourceFileTooLargeError,
)
from .access import _fingerprint, _name_still_refers_to, _open_relative_file
from .policy import IngestionPolicy
from .types import (
    CHUNK_SIZE,
    FileRead,
    REASON_DIRECTORY,
    REASON_EMPTY,
    REASON_FILE_TOO_LARGE,
    REASON_READ_BUDGET,
    REASON_SHORT_READ,
    REASON_UNSTABLE,
    ReadBudget,
)


def _hash_open_file(
    fd: int, *, budget: ReadBudget, limits: IngestionLimits
) -> tuple[str | None, int, int, str | None]:
    """Hash the bytes of an open descriptor, or explain why it was not read.

    Returns ``(content_hash, byte_size, mtime_ns, unread_reason)``. Size and
    mtime always come from the *same* ``fstat`` as the hash, so the metadata a
    caller stores can never describe a different inode than the hash.

    The empty-observation policy
    ----------------------------

    Two ``fstat`` calls cannot see a write that happens *after* them. A file that
    another process truncated and has not refilled yet therefore looks exactly
    like a stable, empty file here -- and registering the hash of zero bytes would
    silently retire a real, successfully observed version and make an older
    snapshot look like the current state of the document.

    So a zero-length observation is deliberately **not** a version: no hash is
    reported and the reason ``empty_file`` is. An empty file stays completely
    legitimate and visible -- it is enumerated, it becomes a resource, it is
    counted and it is reported -- it simply has no current version until it has
    bytes again, and an explicit extraction of it stores the visible, failing
    ``empty_file`` snapshot. This is a conservative refusal to claim a version
    from an observation that cannot support one; it is not a claim that the file
    did not change, and it is not a global stability guarantee.
    """
    status = os.fstat(fd)
    mtime_ns = int(status.st_mtime_ns)
    if not stat.S_ISREG(status.st_mode):
        return None, int(status.st_size), mtime_ns, REASON_DIRECTORY
    if status.st_size > limits.max_file_bytes:
        return None, int(status.st_size), mtime_ns, REASON_FILE_TOO_LARGE
    if status.st_size == 0:
        return None, 0, mtime_ns, REASON_EMPTY
    if budget.remaining <= 0:
        return None, int(status.st_size), mtime_ns, REASON_READ_BUDGET
    # The read is bounded three times: by the file, by the per-file limit and by
    # what is left of the scan-wide budget. Which of them bound the read decides
    # why fewer bytes than `st_size` came back: our own budget is a budget
    # exhaustion, a descriptor that ends earlier than its own size announced is a
    # short read and is reported as such.
    limit = min(limits.max_file_bytes, int(status.st_size), budget.remaining)
    digest = hashlib.sha256()
    read_bytes = 0
    while read_bytes < limit:
        chunk = os.read(fd, min(CHUNK_SIZE, limit - read_bytes))
        if not chunk:
            break
        digest.update(chunk)
        read_bytes += len(chunk)
        budget.charge(len(chunk))
    after = os.fstat(fd)
    mtime_ns = int(after.st_mtime_ns)
    if _fingerprint(status) != _fingerprint(after):
        # The file was rewritten while we read it. Half old, half new bytes are
        # not a version of anything, so no hash is reported.
        return None, int(after.st_size), mtime_ns, REASON_UNSTABLE
    if read_bytes < int(status.st_size):
        if limit < int(status.st_size):
            # We stopped on purpose: the scan-wide budget was smaller than the file.
            return None, int(after.st_size), mtime_ns, REASON_READ_BUDGET
        return None, int(after.st_size), mtime_ns, REASON_SHORT_READ
    return digest.hexdigest(), int(after.st_size), mtime_ns, None


def _hash_named_file(
    fd: int, locator: str, root: str, *, budget: ReadBudget, limits: IngestionLimits
) -> tuple[str | None, int, int, str | None]:
    """Hash an open file and revalidate its name before publishing a version.

    fstat alone cannot detect an atomic replacement after the final descriptor
    check: the old descriptor remains readable while the locator names new bytes.
    Reopen the parent with the no-follow walk; an inaccessible parent also fails
    closed instead of making this a complete enumeration.
    """
    result = _hash_open_file(fd, budget=budget, limits=limits)
    # Device/inode cannot change on the open descriptor. Capture them after the
    # hash so its first fstat remains the authoritative content observation
    # (including a transient zero-length file before a writer refills it).
    fingerprint = _fingerprint(os.fstat(fd))
    try:
        same_name = _name_still_refers_to(locator, root, fingerprint)
    except (ResourceIngestionError, OSError):
        same_name = False
    if not same_name:
        return None, result[1], result[2], REASON_UNSTABLE
    return result


def read_local_file(
    locator: str, policy: IngestionPolicy, limits: IngestionLimits = DEFAULT_LIMITS
) -> FileRead:
    """Read one local file safely and prove that the bytes are one version.

    The read is refused when the file is a link, a special file, larger than the
    per-file budget, outside the approved root, changed while it was read (device,
    inode, size, mtime or ctime differ between ``fstat`` before and after, or the
    name no longer points at the same inode), or when it delivered fewer bytes than
    its own size announced (``file_short_read``). The caller therefore never has to
    decide whether a hash belongs to a consistent file.

    A zero-length original is *not* refused -- it is a legitimate file -- but it is
    flagged through ``FileRead.is_empty``. Its ``content_hash`` is the hash of no
    bytes, so a caller that persists versions has to keep the one it already had
    instead of overwriting it with this observation. See the empty-observation
    policy in :func:`_hash_open_file` for why.
    """
    root = policy.authorize(locator, expect="file")
    file_fd = _open_relative_file(locator, root, what="Datei")
    try:
        before = os.fstat(file_fd)
        if before.st_size > limits.max_file_bytes:
            raise SourceFileTooLargeError(
                "Die Datei ist groesser als das zulaessige Dateilimit",
                limit="max_file_bytes",
                byte_size=int(before.st_size),
            )
        # The loop is bounded by the size observed *before* the read. A file that
        # grows while it is read is refused below, but it must not be able to
        # grow the process memory first.
        limit = min(limits.max_file_bytes, before.st_size)
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        read_bytes = 0
        while read_bytes < limit:
            chunk = os.read(file_fd, min(CHUNK_SIZE, limit - read_bytes))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            read_bytes += len(chunk)
        after = os.fstat(file_fd)
        before_print = _fingerprint(before)
        if before_print != _fingerprint(after):
            raise SourceFileChangedError(
                "Die Datei wurde waehrend des Lesens veraendert; es gibt keine konsistente Version"
            )
        if not _name_still_refers_to(locator, root, before_print):
            raise SourceFileChangedError(
                "Die Datei wurde waehrend des Lesens verschoben oder ersetzt; es gibt keine konsistente Version"
            )
        if read_bytes < int(before.st_size):
            # The fingerprint is unchanged, so the file did not shrink -- yet the
            # descriptor handed out fewer bytes than it announced. Whatever came
            # back is not the file, and reporting it would store the hash of no
            # bytes as the current version of the document.
            raise SourceFileShortReadError(
                "Die Datei lieferte beim Lesen weniger Bytes als ihre Groesse angibt; "
                "es gibt keine konsistente Version",
                expected_bytes=int(before.st_size),
                read_bytes=read_bytes,
            )
    finally:
        os.close(file_fd)
    return FileRead(
        data=b"".join(chunks),
        byte_size=read_bytes,
        # From the same fstat that proved the bytes are one version, never from a
        # stat taken before the read.
        mtime_ns=int(after.st_mtime_ns),
        content_hash=digest.hexdigest(),
        is_empty=int(after.st_size) == 0,
    )
