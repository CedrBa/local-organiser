"""Deterministic domain state changes and their audit history.

Public imports remain available here; implementation lives in focused modules.
"""

from .validation import (
    TERMINAL_TASK_STATUSES as TERMINAL_TASK_STATUSES,
    UNSET as UNSET,
    _required_title as _required_title,
    _normalized_time as _normalized_time,
    _validate_schedule as _validate_schedule,
    CONTEXT_STATUSES as CONTEXT_STATUSES,
    CONTEXT_STATUS_TRANSITIONS as CONTEXT_STATUS_TRANSITIONS,
    CONTEXT_SOURCE_TYPES as CONTEXT_SOURCE_TYPES,
    CONTEXT_SOURCE_STATUSES as CONTEXT_SOURCE_STATUSES,
    _required_path as _required_path,
    KNOWLEDGE_PATH_MAX_LENGTH as KNOWLEDGE_PATH_MAX_LENGTH,
)
from .errors import (
    DomainError as DomainError,
    NotFoundError as NotFoundError,
    ConflictError as ConflictError,
    DomainValidationError as DomainValidationError,
)
from .events import (
    EventContext as EventContext,
    _serialize as _serialize,
    _changes as _changes,
    record_event as record_event,
    list_events_for_entity as list_events_for_entity,
    list_events_by_correlation_id as list_events_by_correlation_id,
    list_events_after_id as list_events_after_id,
)
from .transactions import (
    _commit as _commit,
)
from .queries import (
    _project_or_error as _project_or_error,
    _task_or_error as _task_or_error,
    _topic_or_error as _topic_or_error,
    _context_or_error as _context_or_error,
    _context_source_or_error as _context_source_or_error,
)
from .task_bindings import (
    _validate_parent as _validate_parent,
    _resolved_context_id as _resolved_context_id,
    _resolved_topic_id as _resolved_topic_id,
    _assert_task_context_change_allowed as _assert_task_context_change_allowed,
)
from .projects import (
    create_project as create_project,
    update_project as update_project,
    _transition_project as _transition_project,
    pause_project as pause_project,
    resume_project as resume_project,
    complete_project as complete_project,
    cancel_project as cancel_project,
    reopen_project as reopen_project,
    get_project as get_project,
    list_projects as list_projects,
    list_project_tasks as list_project_tasks,
)
from .tasks import (
    create_task as create_task,
    update_task_details as update_task_details,
    _transition_task as _transition_task,
    start_task as start_task,
    mark_task_waiting as mark_task_waiting,
    complete_task as complete_task,
    reopen_task as reopen_task,
    cancel_task as cancel_task,
    reschedule_task as reschedule_task,
    get_task as get_task,
    list_tasks as list_tasks,
    list_child_tasks as list_child_tasks,
)
from .contexts import (
    _required_type as _required_type,
    _normalized_knowledge_path as _normalized_knowledge_path,
    _validate_context_status as _validate_context_status,
    _validate_context_parent as _validate_context_parent,
    _committed_knowledge_path as _committed_knowledge_path,
    _assert_knowledge_path_available as _assert_knowledge_path_available,
    create_context as create_context,
    update_context as update_context,
    set_context_status as set_context_status,
    get_context as get_context,
    list_contexts as list_contexts,
    list_child_contexts as list_child_contexts,
)
from .sources import (
    _validate_source_type as _validate_source_type,
    _validate_source_status as _validate_source_status,
    get_context_source as get_context_source,
    list_context_sources as list_context_sources,
    add_context_source as add_context_source,
    update_context_source as update_context_source,
    remove_context_source as remove_context_source,
)
