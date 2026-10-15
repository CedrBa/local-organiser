"""Resource registry, controlled intake and bounded extraction.

Five public functions form the contract with the other components:

``list_resources``
    Bounded, deterministic inventory of one context. Read only.
``get_resource_snapshot``
    One resource with its snapshot history and an explicit *staleness* verdict.
``scan_source``
    Register the files of one source and read bounded text from them.
``extract_resource``
    Re-read one original and store one immutable snapshot of it.
``verify_resource_version``
    Fresh permission, fresh address, fresh hash. Read only, writes nothing.

Boundaries that the rest of the system may rely on
--------------------------------------------------

*Observation is separate from persistence.* No file is read while a write
transaction is open. A scan reads the source identity, ends that transaction,
walks the filesystem, then re-reads the source identity and only then writes. If
the source changed in between, the observation is discarded instead of being
attached to a source that no longer describes it.

*Registration is not permission.* Every read re-checks the operator allowlist, the
source status, the source identity and the address. A stored locator is a hint
for the database, never a capability.

*Absence requires completeness.* ``missing`` is only ever written after an
enumeration that saw the whole scope of the same source without hitting a budget,
a permission failure or an unreadable directory. Extraction problems never make a
file missing, and a file that could not be read keeps its previous state.

*Nothing is created by reading.* No file is written, moved, chmod'ed or deleted;
no context.md, no knowledge file, no temporary file next to the source. The only
temporary artefact is the parser's private working directory, which is removed
unconditionally.

*No LLM, no network, no shell.* A scan is deterministic: same filesystem, same
database, same rows.

Public imports remain available here; implementation lives in focused modules.
"""

from .constants import (
    RUN_TYPE_SCAN as RUN_TYPE_SCAN,
    RUN_TYPE_EXTRACT as RUN_TYPE_EXTRACT,
    STATUS_ACTIVE as STATUS_ACTIVE,
    STATUS_MISSING as STATUS_MISSING,
    STATUS_ARCHIVED as STATUS_ARCHIVED,
    SCANNABLE_SOURCE_TYPES as SCANNABLE_SOURCE_TYPES,
    EVENT_RESOURCE_REGISTERED as EVENT_RESOURCE_REGISTERED,
    EVENT_RESOURCE_REFRESHED as EVENT_RESOURCE_REFRESHED,
    EVENT_RESOURCE_MISSING as EVENT_RESOURCE_MISSING,
    EVENT_RESOURCE_EXTRACTED as EVENT_RESOURCE_EXTRACTED,
    EVENT_SOURCE_SCANNED as EVENT_SOURCE_SCANNED,
    EVENT_SOURCE_REJECTED as EVENT_SOURCE_REJECTED,
    REASON_SOURCE_REMOVED as REASON_SOURCE_REMOVED,
    REASON_SOURCE_STATUS as REASON_SOURCE_STATUS,
    REASON_RESOURCE_STATUS_MISSING as REASON_RESOURCE_STATUS_MISSING,
    REASON_RESOURCE_STATUS_ARCHIVED as REASON_RESOURCE_STATUS_ARCHIVED,
    REASON_RESOURCE_HASH_MOVED as REASON_RESOURCE_HASH_MOVED,
    REASON_STALE_MISSING as REASON_STALE_MISSING,
    REASON_STALE_ARCHIVED as REASON_STALE_ARCHIVED,
    REASON_STALE_NO_SNAPSHOT as REASON_STALE_NO_SNAPSHOT,
    REASON_FILE_UNREADABLE as REASON_FILE_UNREADABLE,
    REASON_RESOURCE_EMPTY as REASON_RESOURCE_EMPTY,
    CURRENT_SNAPSHOT_STATUSES as CURRENT_SNAPSHOT_STATUSES,
    _LOCATOR_QUERY_CHUNK as _LOCATOR_QUERY_CHUNK,
    _SCAN_LOG as _SCAN_LOG,
    _REGISTRY_UNIQUE_MESSAGES as _REGISTRY_UNIQUE_MESSAGES,
)
from .transactions import (
    _ResultTransaction as _ResultTransaction,
    _require_application_boundary as _require_application_boundary,
    _end_read_transaction as _end_read_transaction,
)
from .identity import (
    SourceIdentity as SourceIdentity,
    ResourceIdentity as ResourceIdentity,
    _ScanState as _ScanState,
    _load_source_identity as _load_source_identity,
    _load_resource_identity as _load_resource_identity,
)
from .runs import (
    _audit as _audit,
    _open_run as _open_run,
    _finish_run as _finish_run,
    _record_rejected_run as _record_rejected_run,
    _record_rejection_without_run as _record_rejection_without_run,
)
from .access import (
    _load_source_for_access as _load_source_for_access,
    _authorize_resource_address as _authorize_resource_address,
    _address_within_source as _address_within_source,
    _os_error_to_refusal as _os_error_to_refusal,
    _read_refusal_reason as _read_refusal_reason,
)
from .queries import (
    _current_snapshot_ids as _current_snapshot_ids,
    _summarize as _summarize,
    list_resources as list_resources,
    _snapshot_summary as _snapshot_summary,
    get_resource_snapshot as get_resource_snapshot,
)
from .observations import (
    _existing_by_locator as _existing_by_locator,
    _refresh_resource as _refresh_resource,
    _rebind_orphan_resource as _rebind_orphan_resource,
    _observation_payload as _observation_payload,
    _plan_observations as _plan_observations,
    _apply_observations as _apply_observations,
    _mark_missing as _mark_missing,
    _mark_nothing_missing as _mark_nothing_missing,
)
from .snapshots import (
    _plan_extractions as _plan_extractions,
    _run_extractions as _run_extractions,
    _store_snapshot as _store_snapshot,
    _find_or_create_snapshot as _find_or_create_snapshot,
)
from .failures import (
    _scan_result_commit_outcome as _scan_result_commit_outcome,
    _classify_scan_failure as _classify_scan_failure,
    _record_scan_failure as _record_scan_failure,
)
from .scan import (
    scan_source as scan_source,
)
from .extract import (
    _update_observed_version as _update_observed_version,
    extract_resource as extract_resource,
)
from .verification import (
    _failed_check as _failed_check,
    verify_resource_version as verify_resource_version,
    assert_resource_version as assert_resource_version,
)
