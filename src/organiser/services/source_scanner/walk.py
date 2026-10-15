"""Bounded descriptor-based enumeration; completeness controls missing decisions."""

from __future__ import annotations

import os
import posixpath
import stat
from organiser.domain.resources import (
    DEFAULT_LIMITS,
    IngestionLimits,
    ResourceIngestionError,
    SourceUnsafeError,
    is_excluded_component,
    relative_path_for,
    resource_type_for,
    title_for,
)
from .access import (
    _assert_contained,
    _directory_flags,
    _file_flags,
    _open_relative_file,
    open_source_root,
)
from .policy import IngestionPolicy
from .reading import _hash_named_file
from .types import (
    ObservedFile,
    PERMANENT_REFUSALS,
    REASON_READ_BUDGET,
    REASON_UNREADABLE,
    REASON_UNSTABLE,
    ReadBudget,
    STOP_DEPTH_LIMIT,
    STOP_ENTRY_LIMIT,
    STOP_READ_BUDGET,
    STOP_UNREADABLE_DIRECTORY,
    STOP_UNSAFE_DIRECTORY,
    SourceTreeResult,
)


def _walk(
    dir_fd: int,
    prefix: tuple[str, ...],
    depth: int,
    root_locator: str,
    root: str,
    limits: IngestionLimits,
    budget: ReadBudget,
    files: list[ObservedFile],
    counters: dict[str, int],
    stop: list[str | None],
) -> None:
    if stop[0] is not None:
        return
    if depth > limits.max_depth:
        stop[0] = STOP_DEPTH_LIMIT
        return
    try:
        scanner = os.scandir(dir_fd)
    except OSError:
        counters["unreadable_directories"] += 1
        stop[0] = STOP_UNREADABLE_DIRECTORY
        return
    with scanner:
        # The entry budget counts *every* directory entry this walk looks at --
        # regular files, subdirectories, excluded names, unsafe objects -- because
        # all of them cost the walk something and, before this, only the last of
        # those groups was bounded at all. `sorted(scandir)` also materialised a
        # whole directory before a single entry was processed, so a source full of
        # excluded or unsafe entries could make the walk allocate without the entry
        # budget ever being charged.
        #
        # Names are therefore pulled one at a time and charged before they are
        # kept, so the buffer can never grow past `max_entries`. If a name exists
        # that no budget is left for, the walk *saw* more than it could afford --
        # which is exactly what an incomplete enumeration has to mean.
        entries: list[os.DirEntry] = []
        overflowed = False
        while True:
            try:
                entry = next(scanner)
            except StopIteration:
                break
            except OSError:  # pragma: no cover - defensive
                counters["unreadable_directories"] += 1
                stop[0] = STOP_UNREADABLE_DIRECTORY
                return
            if counters["entries"] >= limits.max_entries:
                overflowed = True
                break
            counters["entries"] += 1
            entries.append(entry)
        # Everything the walk could afford is still processed -- an observation is
        # a fact about a real file and is not worth throwing away -- but the
        # enumeration has not seen the whole scope, so the caller must not conclude
        # absence from it.
        deferred_stop = STOP_ENTRY_LIMIT if overflowed else None
        # Deterministic selection: same directory, same budget, same order.
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            if stop[0] is not None:
                return
            name = entry.name
            if is_excluded_component(name):
                counters["skipped_excluded"] += 1
                continue
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError:
                # We cannot tell what this entry is, so we cannot claim to have
                # seen the whole scope.
                counters["skipped_unsafe"] += 1
                stop[0] = STOP_UNREADABLE_DIRECTORY
                return
            if stat.S_ISLNK(status.st_mode):
                counters["skipped_unsafe"] += 1
                continue
            if stat.S_ISDIR(status.st_mode):
                try:
                    child_fd = os.open(name, _directory_flags(), dir_fd=dir_fd)
                except OSError:
                    # A directory we cannot enter may contain resources of this
                    # source. Reporting "complete" here would let the caller mark
                    # them missing, so the traversal stops as incomplete instead.
                    counters["unreadable_directories"] += 1
                    stop[0] = STOP_UNREADABLE_DIRECTORY
                    return
                try:
                    try:
                        _assert_contained(child_fd, root, what="Unterverzeichnis")
                    except SourceUnsafeError:
                        counters["skipped_unsafe"] += 1
                        stop[0] = STOP_UNSAFE_DIRECTORY
                        return
                    _walk(
                        child_fd,
                        (*prefix, name),
                        depth + 1,
                        root_locator,
                        root,
                        limits,
                        budget,
                        files,
                        counters,
                        stop,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(status.st_mode):
                # FIFO, socket, device: never a resource and never followed.
                counters["skipped_unsafe"] += 1
                continue
            try:
                relative = relative_path_for((*prefix, name))
            except ValueError:
                counters["skipped_excluded"] += 1
                continue
            locator = f"{root_locator.rstrip('/')}/{relative}"
            try:
                file_fd = os.open(name, _file_flags(), dir_fd=dir_fd)
            except OSError:
                counters["skipped_unsafe"] += 1
                files.append(
                    ObservedFile(
                        relative_path=relative,
                        locator=locator,
                        title=title_for(relative),
                        resource_type=resource_type_for(name),
                        byte_size=int(status.st_size),
                        mtime_ns=int(status.st_mtime_ns),
                        unread_reason=REASON_UNREADABLE,
                    )
                )
                continue
            try:
                try:
                    _assert_contained(file_fd, root, what="Datei")
                except SourceUnsafeError:
                    counters["skipped_unsafe"] += 1
                    files.append(
                        ObservedFile(
                            relative_path=relative,
                            locator=locator,
                            title=title_for(relative),
                            resource_type=resource_type_for(name),
                            byte_size=int(status.st_size),
                            mtime_ns=int(status.st_mtime_ns),
                            unread_reason=REASON_UNREADABLE,
                        )
                    )
                    continue
                content_hash, size, mtime_ns, reason = _hash_named_file(
                    file_fd, locator, root, budget=budget, limits=limits
                )
            finally:
                os.close(file_fd)
            files.append(
                ObservedFile(
                    relative_path=relative,
                    locator=locator,
                    title=title_for(relative),
                    resource_type=resource_type_for(name),
                    byte_size=size,
                    mtime_ns=mtime_ns,
                    content_hash=content_hash,
                    unread_reason=reason,
                )
            )
            if reason == REASON_UNSTABLE:
                stop[0] = REASON_UNSTABLE
                return
            if reason == REASON_READ_BUDGET and budget.exhausted:
                stop[0] = STOP_READ_BUDGET
                return
        if deferred_stop is not None and stop[0] is None:
            stop[0] = deferred_stop


def scan_source_tree(
    locator: str,
    policy: IngestionPolicy,
    limits: IngestionLimits = DEFAULT_LIMITS,
    *,
    expect: str = "dir",
) -> SourceTreeResult:
    """Enumerate one source root and hash what fits into the budget.

    Enumeration never follows a link: directories are opened relative to the
    parent descriptor with ``O_NOFOLLOW`` and every entry is stat'ed with
    ``follow_symlinks=False``. A scan that stopped early -- entry limit, depth
    limit, spent read budget, a directory that cannot be entered, an entry that
    cannot be stat'ed -- reports ``complete=False``, and the caller must not
    conclude absence from it.

    ``max_entries`` bounds the enumeration itself, not only the number of files
    that end up in the result: every directory entry charged against it, and no
    directory is materialised beyond that budget.

    ``expect="file"`` is a document or note source: the scope is exactly that one
    file, so the result is either that file or an empty, incomplete result.
    """
    if expect == "file":
        return _scan_single_file(locator, policy, limits)
    root_fd, root = open_source_root(locator, policy, expect="dir")
    budget = ReadBudget(max_total_bytes=limits.max_total_read_bytes)
    files: list[ObservedFile] = []
    counters = {
        "skipped_unsafe": 0,
        "skipped_excluded": 0,
        "unreadable_directories": 0,
        "entries": 0,
    }
    stop: list[str | None] = [None]
    try:
        _walk(root_fd, (), 1, locator, root, limits, budget, files, counters, stop)
    finally:
        os.close(root_fd)
    return SourceTreeResult(
        files=tuple(files),
        complete=stop[0] is None,
        stop_reason=stop[0],
        skipped_unsafe=counters["skipped_unsafe"],
        skipped_excluded=counters["skipped_excluded"],
        unreadable_directories=counters["unreadable_directories"],
        read_budget_used=budget.used_bytes,
        entries_seen=counters["entries"],
    )


def _scan_single_file(
    locator: str, policy: IngestionPolicy, limits: IngestionLimits
) -> SourceTreeResult:
    """Read one document or note source as the complete scope of that source.

    The label of the observation is the bare file name, because that is the whole
    scope of this kind of source. A label that made it unique would have to be built
    from the absolute address or from the operator's allowlist, and both would make
    the stored label depend on something outside the owning source: the same file
    would be renamed back and forth when the allowlist changed, or when another
    source of the same context also saw it. Identity is the locator, never the
    label.
    """
    root = policy.authorize(locator, expect="file")
    if limits.max_entries < 1:
        # The scope of this source is one entry and there is no budget for it.
        # Reported before anything is read, so an embedding application that
        # lowered the budget to zero cannot make the scope look empty.
        return SourceTreeResult(files=(), complete=False, stop_reason=STOP_ENTRY_LIMIT)
    budget = ReadBudget(max_total_bytes=limits.max_total_read_bytes)
    name = posixpath.basename(locator)
    try:
        file_fd = _open_relative_file(locator, root, what="Quelle")
    except ResourceIngestionError as error:
        reason = str(error.context.get("reason", "file_unreadable"))
        if reason in PERMANENT_REFUSALS:
            # A link or a special file at a registered source path is a
            # configuration error, not a temporarily unreadable file: it is
            # reported as a refusal instead of a quietly incomplete scan.
            raise
        # The one file this source is about cannot be read. The scope is *not*
        # proven empty, so the scan is incomplete and nothing may go missing.
        return SourceTreeResult(
            files=(),
            complete=False,
            stop_reason=reason,
            skipped_unsafe=1,
        )
    try:
        content_hash, size, mtime_ns, reason = _hash_named_file(
            file_fd, locator, root, budget=budget, limits=limits
        )
    finally:
        os.close(file_fd)
    relative = relative_path_for((name,))
    # One observation without a hash is not automatically a proven scope, and this
    # is the one kind of source whose *entire* scope is a single file. Every other
    # reason for a missing hash still describes a file that was seen and located:
    # too large, empty or unreadable are all facts about the file, and the
    # scope was fully covered. An exhausted read budget is different -- the scan
    # never got to look at the bytes at all, so it cannot claim to have seen the
    # whole scope. The folder walk makes exactly the same distinction for exactly
    # the same cause (``STOP_READ_BUDGET``); a document or note source must not use a
    # different answer for the same budget, and `complete=True` here would let the
    # caller draw a missing-decision from an enumeration that stopped short.
    # An unstable observation also fails closed: its locator no longer proves
    # which file was seen, so neither a version nor absence can be concluded.
    incomplete = reason in (REASON_READ_BUDGET, REASON_UNSTABLE)
    return SourceTreeResult(
        files=(
            ObservedFile(
                relative_path=relative,
                locator=locator,
                title=title_for(relative),
                resource_type=resource_type_for(name),
                byte_size=size,
                mtime_ns=mtime_ns,
                content_hash=content_hash,
                unread_reason=reason,
            ),
        ),
        complete=not incomplete,
        stop_reason=reason if incomplete else None,
        read_budget_used=budget.used_bytes,
        entries_seen=1,
    )
