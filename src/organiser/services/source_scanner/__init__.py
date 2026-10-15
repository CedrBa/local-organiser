"""Operator-side source policy and the descriptor based local filesystem layer.

Nothing in this module knows about contexts, resources or the database. It answers
two questions and refuses everything else:

* May this local path be read at all (:class:`IngestionPolicy`)?
* What does a bounded, link-free read of it look like
  (:func:`scan_source_tree`, :func:`read_local_file`)?

Why descriptors instead of paths
--------------------------------

``open(path)`` re-resolves every component of the path at the moment of the call.
A registry that stores a canonical locator and reads it later can therefore be
made to read a different object than the one it validated. Every access here
instead walks the path one component at a time with ``O_NOFOLLOW`` relative to an
already open directory descriptor, so a link anywhere -- including in an
intermediate directory -- fails the access instead of being followed. Enumeration
uses the same mechanism: :func:`os.scandir` is given a descriptor, never a path.

Every opened object is additionally checked against the operator-approved root
through ``/proc/self/fd``. That is a second, independent line of defence: it also
catches mount tricks and any future refactor that reintroduces a path based open.
It is a *check*, not the guarantee -- the guarantee is the per-component
``O_NOFOLLOW`` walk. If procfs is missing, the module says so through
:data:`PROCFS_AVAILABLE` and the walk alone carries the property. A procfs that
exists but fails to answer for a descriptor is a refusal, never a pass.

Files are never written, moved, chmod'ed or created here. A scan only reads.

Public imports remain available here; implementation lives in focused modules.
"""

from .types import (
    CHUNK_SIZE as CHUNK_SIZE,
    PROCFS_AVAILABLE as PROCFS_AVAILABLE,
    REASON_FILE_TOO_LARGE as REASON_FILE_TOO_LARGE,
    REASON_READ_BUDGET as REASON_READ_BUDGET,
    REASON_UNREADABLE as REASON_UNREADABLE,
    REASON_UNSTABLE as REASON_UNSTABLE,
    REASON_DIRECTORY as REASON_DIRECTORY,
    REASON_EMPTY as REASON_EMPTY,
    REASON_SHORT_READ as REASON_SHORT_READ,
    STOP_ENTRY_LIMIT as STOP_ENTRY_LIMIT,
    STOP_DEPTH_LIMIT as STOP_DEPTH_LIMIT,
    STOP_READ_BUDGET as STOP_READ_BUDGET,
    STOP_UNREADABLE_DIRECTORY as STOP_UNREADABLE_DIRECTORY,
    STOP_UNSAFE_DIRECTORY as STOP_UNSAFE_DIRECTORY,
    PERMANENT_REFUSALS as PERMANENT_REFUSALS,
    ObservedFile as ObservedFile,
    ReadBudget as ReadBudget,
    SourceTreeResult as SourceTreeResult,
    FileRead as FileRead,
)
from .policy import (
    IngestionPolicy as IngestionPolicy,
)
from .access import (
    _directory_flags as _directory_flags,
    _file_flags as _file_flags,
    _assert_contained as _assert_contained,
    _fingerprint as _fingerprint,
    _reject_special as _reject_special,
    open_source_root as open_source_root,
    _oserror_to_refusal as _oserror_to_refusal,
    _open_relative_file as _open_relative_file,
    _relative_parts as _relative_parts,
    _open_root_itself as _open_root_itself,
    _open_below_root as _open_below_root,
    _name_still_refers_to as _name_still_refers_to,
)
from .reading import (
    _hash_open_file as _hash_open_file,
    _hash_named_file as _hash_named_file,
    read_local_file as read_local_file,
)
from .walk import (
    _walk as _walk,
    scan_source_tree as scan_source_tree,
    _scan_single_file as _scan_single_file,
)
