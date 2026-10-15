"""Descriptor-based no-follow access and containment; callers own returned descriptors."""

from __future__ import annotations

import errno
import os
import stat
from organiser.domain.resources import (
    CODE_SOURCE_NOT_ALLOWED,
    CODE_SOURCE_UNSAFE,
    ResourceIngestionError,
    SourceAccessError,
    SourceUnsafeError,
    is_within,
)
from .policy import IngestionPolicy
from .types import PROCFS_AVAILABLE


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _file_flags() -> int:
    # O_NONBLOCK matters: opening a FIFO for reading would otherwise block until
    # a writer appears, i.e. an attacker-chosen special file could park a scan.
    # It has no effect on regular files, and the fstat check rejects everything
    # that is not a regular file right after the open.
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _assert_contained(fd: int, root: str, *, what: str) -> None:
    """Verify through ``/proc/self/fd`` that an open descriptor is inside ``root``.

    This is the independent containment check. The no-follow walk already
    guarantees that no component was a link, but a path that resolves outside the
    approved root must never be readable even if some future caller opens it
    differently.
    """
    proc_link = f"/proc/self/fd/{fd}"
    try:
        actual = os.readlink(proc_link)
    except FileNotFoundError:
        if not PROCFS_AVAILABLE:
            # No procfs at all: the per-component O_NOFOLLOW walk remains the
            # guarantee, and the caller is told that this check is unavailable.
            return
        raise SourceUnsafeError(
            f"{what} konnte nicht geprueft werden",
            code=CODE_SOURCE_UNSAFE,
            reason="containment_unavailable",
        ) from None
    except OSError:
        raise SourceUnsafeError(
            f"{what} konnte nicht geprueft werden",
            code=CODE_SOURCE_UNSAFE,
            reason="containment_unavailable",
        ) from None
    if not actual.startswith("/"):
        return
    if actual == root or is_within(root, actual):
        return
    # Allow a trailing-slash difference for directory descriptors.
    if actual.rstrip("/") == root.rstrip("/"):
        return
    raise SourceUnsafeError(
        f"{what} verlaesst das freigegebene Wurzelverzeichnis",
        code=CODE_SOURCE_UNSAFE,
        reason="containment_failed",
    )


def _fingerprint(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def _reject_special(status: os.stat_result, *, what: str) -> None:
    if stat.S_ISLNK(status.st_mode):
        raise SourceUnsafeError(
            f"{what} ist ein Symlink", code=CODE_SOURCE_UNSAFE, reason="symlink"
        )
    if not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
        raise SourceUnsafeError(
            f"{what} ist keine regulaere Datei oder kein Verzeichnis",
            code=CODE_SOURCE_UNSAFE,
            reason="special_file",
        )


def open_source_root(
    locator: str, policy: IngestionPolicy, *, expect: str = "dir"
) -> tuple[int, str]:
    """Open a source root as a directory descriptor and return ``(fd, root)``.

    Every component between the approved root and the source itself is opened one
    at a time with ``O_NOFOLLOW`` relative to its parent descriptor. Opening the
    whole path in one ``os.open`` would only protect the *last* component: a
    symlinked intermediate directory would be followed silently and the registry
    would store a locator that no longer describes the bytes it read.

    The approved root itself is opened by name. It comes from the operator's
    configuration, it was resolved once when the settings were built, and its
    containment is proven right after the open.
    """
    root = policy.authorize(locator, expect=expect)
    if locator == root:
        # The source is the approved root itself. It was resolved once when the
        # settings were built; open it and prove the containment right after.
        return _open_root_itself(root)
    components = _relative_parts(locator, root, what="Quellverzeichnis")
    fd = _open_below_root(components, root, what="Quellverzeichnis")
    try:
        status = os.fstat(fd)
        _reject_special(status, what="Quellverzeichnis")
        if not stat.S_ISDIR(status.st_mode):
            raise SourceUnsafeError(
                "Die Quelle ist kein Verzeichnis", code=CODE_SOURCE_UNSAFE, reason="not_a_directory"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd, root


def _oserror_to_refusal(error: OSError, locator: str, *, what: str) -> ResourceIngestionError:
    """Translate an OS error into a coded refusal without leaking the raw text."""
    del locator  # never echoed: absolute addresses stay internal
    if error.errno in (errno.ELOOP, errno.EMLINK):
        return SourceUnsafeError(
            f"{what} ist ein Symlink", code=CODE_SOURCE_UNSAFE, reason="symlink"
        )
    if error.errno == errno.ENOTDIR:
        return SourceUnsafeError(
            f"{what} ist kein Verzeichnis", code=CODE_SOURCE_UNSAFE, reason="not_a_directory"
        )
    if error.errno in (errno.EACCES, errno.EPERM):
        return SourceAccessError(
            f"{what} ist nicht lesbar", code=CODE_SOURCE_NOT_ALLOWED, reason="permission"
        )
    if error.errno in (errno.ENOENT, errno.ENOTDIR):
        return SourceUnsafeError(
            f"{what} existiert nicht", code=CODE_SOURCE_UNSAFE, reason="not_found"
        )
    return SourceUnsafeError(
        f"{what} ist nicht lesbar", code=CODE_SOURCE_UNSAFE, reason="unreadable"
    )


def _open_relative_file(locator: str, root: str, *, what: str) -> int:
    """Open a file below ``root`` component by component, refusing every link."""
    parts = _relative_parts(locator, root, what=what)
    current_fd = _open_below_root(parts[:-1], root, what=what)
    try:
        name = parts[-1]
        try:
            file_fd = os.open(name, _file_flags(), dir_fd=current_fd)
        except OSError as error:
            raise _oserror_to_refusal(error, locator, what=what) from error
    finally:
        os.close(current_fd)
    try:
        _assert_contained(file_fd, root, what=what)
        status = os.fstat(file_fd)
        _reject_special(status, what=what)
        if not stat.S_ISREG(status.st_mode):
            raise SourceUnsafeError(
                f"{what} ist keine regulaere Datei", code=CODE_SOURCE_UNSAFE, reason="special_file"
            )
    except BaseException:
        os.close(file_fd)
        raise
    return file_fd


def _relative_parts(locator: str, root: str, *, what: str) -> list[str]:
    parts = [part for part in locator[len(root) :].strip("/").split("/") if part]
    if not parts:
        raise SourceUnsafeError(
            f"{what} liegt nicht unterhalb des freigegebenen Wurzelverzeichnisses",
            code=CODE_SOURCE_UNSAFE,
            reason="containment_failed",
        )
    return parts


def _open_root_itself(root: str) -> tuple[int, str]:
    """Open the operator-approved root itself, with the same guarantees."""
    try:
        link_status = os.lstat(root)
    except OSError as error:
        raise _oserror_to_refusal(error, root, what="Quellverzeichnis") from error
    if stat.S_ISLNK(link_status.st_mode):
        raise SourceUnsafeError(
            "Quellverzeichnis ist ein Symlink", code=CODE_SOURCE_UNSAFE, reason="symlink"
        )
    try:
        fd = os.open(root, _directory_flags())
    except OSError as error:
        raise _oserror_to_refusal(error, root, what="Quellverzeichnis") from error
    try:
        status = os.fstat(fd)
        _reject_special(status, what="Quellverzeichnis")
        if not stat.S_ISDIR(status.st_mode):
            raise SourceUnsafeError(
                "Die Quelle ist kein Verzeichnis", code=CODE_SOURCE_UNSAFE, reason="not_a_directory"
            )
        _assert_contained(fd, root, what="Quellverzeichnis")
    except BaseException:
        os.close(fd)
        raise
    return fd, root


def _open_below_root(components: list[str], root: str, *, what: str) -> int:
    """Open the directory chain ``root``/``components`` with ``O_NOFOLLOW``.

    Each component is stat'ed with ``follow_symlinks=False`` before it is opened,
    because ``O_NOFOLLOW`` on a *symlinked directory* surfaces as ``ENOTDIR``:
    without the stat a link would be reported with the wrong -- and much less
    informative -- reason.
    """
    try:
        current_fd = os.open(root, _directory_flags())
    except OSError as error:
        raise _oserror_to_refusal(error, root, what=what) from error
    try:
        _assert_contained(current_fd, root, what=what)
        for component in components:
            try:
                status = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
            except OSError as error:
                raise _oserror_to_refusal(error, component, what="Verzeichnis") from error
            if stat.S_ISLNK(status.st_mode):
                raise SourceUnsafeError(
                    "Verzeichnis ist ein Symlink", code=CODE_SOURCE_UNSAFE, reason="symlink"
                )
            if not stat.S_ISDIR(status.st_mode):
                raise SourceUnsafeError(
                    "Pfad ist kein Verzeichnis", code=CODE_SOURCE_UNSAFE, reason="not_a_directory"
                )
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except OSError as error:
                raise _oserror_to_refusal(error, component, what="Verzeichnis") from error
            os.close(current_fd)
            current_fd = next_fd
            _assert_contained(current_fd, root, what=what)
    except BaseException:
        os.close(current_fd)
        raise
    return current_fd


def _name_still_refers_to(locator: str, root: str, fingerprint: tuple[int, ...]) -> bool:
    """Re-check through the parent directory that the name still maps to the same inode.

    The read descriptor already proved the bytes were one version. This proves
    the *address* did not move underneath us: a rename or a replace between read
    and commit would otherwise be persisted as "this document", while the bytes
    belong to a name that no longer exists.
    """
    parts = _relative_parts(locator, root, what="Datei")
    parent_fd = _open_below_root(parts[:-1], root, what="Verzeichnis")
    try:
        try:
            status = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return False
    finally:
        os.close(parent_fd)
    return _fingerprint(status)[:2] == fingerprint[:2] and not stat.S_ISLNK(status.st_mode)
