"""Topic lifecycle and the two context-bound link tables. Database only.

This module owns the workspace's topic rules. It is deliberately **database
only**: not one function here touches the filesystem. Creating a topic row does
not create a knowledge file, and a knowledge file is never created implicitly by
a link. Keeping the two apart is what makes both sides repairable -- see
:mod:`organiser.services.topic_knowledge`.

What the database does *not* promise
------------------------------------

The two-part link tables and the plain foreign keys on ``tasks`` cannot express
"same context". ``resource_topics(resource_id, topic_id)`` happily accepts a
resource from one context and a topic from another, and ``tasks.topic_id`` only
requires that some ``context_id`` is set. Every such rule is therefore enforced
here, in the domain transaction, and is covered by adversarial tests rather than
by an empty database promise:

* ``resource_topics`` must join two entities of the *same* context.
* ``task_resources`` must join a task and a resource of the *same* context.
* a task without a context can reference no resource at all.

Deliberate omissions
--------------------

* **No ``delete_topic`` / no hard delete.** A topic is a persistent business
  subject; removing it would orphan task, resource and knowledge references.
  Existing resource archiving (``Resource.status = 'archived'``) stays the way
  to retire a document, and a topic is retired by its status.
* **No topic archive column.** The schema contract defines exactly four status
  values and no archive flag; adding one here would be an out-of-contract column.
* **No automatic cascade reassign.** Reparenting or re-contexting is refused with
  :class:`ConflictError` when it would strand existing references, rather than
  silently moving children, topics or resources along.
* **No status coupling.** A done task does not complete its topic, a done topic
  does not complete a task, and neither touches the context status. The three
  lifecycles are confirmed separately.

Public imports remain available here; implementation lives in focused modules.
"""

from .links import (
    TOPIC_CREATED_EVENT as TOPIC_CREATED_EVENT,
    TOPIC_UPDATED_EVENT as TOPIC_UPDATED_EVENT,
    TOPIC_STATUS_CHANGED_EVENT as TOPIC_STATUS_CHANGED_EVENT,
    RESOURCE_TOPIC_LINKED_EVENT as RESOURCE_TOPIC_LINKED_EVENT,
    RESOURCE_TOPIC_UNLINKED_EVENT as RESOURCE_TOPIC_UNLINKED_EVENT,
    TASK_RESOURCE_LINKED_EVENT as TASK_RESOURCE_LINKED_EVENT,
    TASK_RESOURCE_UNLINKED_EVENT as TASK_RESOURCE_UNLINKED_EVENT,
    DEFAULT_TOPIC_STATUS as DEFAULT_TOPIC_STATUS,
    link_resource_topic as link_resource_topic,
    unlink_resource_topic as unlink_resource_topic,
    link_task_resource as link_task_resource,
    unlink_task_resource as unlink_task_resource,
)
from .bindings import (
    _topic_input as _topic_input,
    get_topic as get_topic,
    _topic_or_error as _topic_or_error,
    _context_or_error as _context_or_error,
    topic_knowledge_binding as topic_knowledge_binding,
    _resource_or_error as _resource_or_error,
    _task_or_error as _task_or_error,
)
from .constraints import (
    _real_transaction_is_open as _real_transaction_is_open,
    _flush_caller_state as _flush_caller_state,
    _title_uniqueness_boundary as _title_uniqueness_boundary,
    _constraint_boundary as _constraint_boundary,
    _title_key_uniqueness_violated as _title_key_uniqueness_violated,
    _link_key_uniqueness_violated as _link_key_uniqueness_violated,
)
from .lifecycle import (
    create_topic as create_topic,
    update_topic as update_topic,
    set_topic_status as set_topic_status,
)
from .queries import (
    list_topics as list_topics,
    list_topic_resources as list_topic_resources,
    list_resources_for_topic as list_resources_for_topic,
    list_task_resources as list_task_resources,
    list_resources_for_task as list_resources_for_task,
)

__all__ = [
    "TOPIC_STATUSES",
    "DEFAULT_TOPIC_STATUS",
    "create_topic",
    "get_topic",
    "list_topics",
    "update_topic",
    "set_topic_status",
    "list_topic_resources",
    "list_resources_for_topic",
    "link_resource_topic",
    "unlink_resource_topic",
    "list_task_resources",
    "list_resources_for_task",
    "link_task_resource",
    "unlink_task_resource",
    "topic_knowledge_binding",
]
