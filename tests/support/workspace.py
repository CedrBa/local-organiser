"""Shared fixtures for the bounded workspace read model.

Every fixture builds real ORM rows through the real schema revision and writes only
into ``tmp_path``. No production database, no production knowledge root and no real
source file is ever opened. The ``finance`` fixture is the acceptance scenario from
the architecture: one course context, one registered folder source, two extracted
documents, topics, open tasks with a deadline and resource links, and an
initialised markdown knowledge store.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from organiser.core.config import Settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import (
    ContextSource,
    Event,
    Resource,
    ResourceSnapshot,
    ResourceTopic,
    Task,
    TaskResource,
    Topic,
)
from organiser.db.session import create_session_factory
from organiser.services.domain import create_context
from organiser.services.knowledge import KnowledgeStore


BASE = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
EXTRACTOR = "text-v1"


@pytest.fixture
def workspace_env(tmp_path):
    """A migrated temporary database plus a matching temporary knowledge root."""
    settings = Settings(database_path=tmp_path / "workspace.sqlite3")
    upgrade_database(settings)
    factory = create_session_factory(settings)
    with factory() as session:
        yield session, tmp_path
        session.rollback()


@pytest.fixture
def store(tmp_path) -> KnowledgeStore:
    root = tmp_path / "knowledge"
    root.mkdir()
    return KnowledgeStore(root)


# ---------------------------------------------------------------------------
# Row builders -- all through the real ORM, all auditable the same way
# ---------------------------------------------------------------------------


def add_context(
    session, *, title="Finanzierung", type="course", description=None, parent=None, path=None
):
    context = create_context(
        session,
        title=title,
        type=type,
        description=description,
        parent_context_id=parent,
        knowledge_path=path,
    )
    return context


def add_source(
    session, context, *, path="/erlaubt/finanzierung", source_type="folder", status="active"
):
    source = ContextSource(context_id=context.id, source_type=source_type, path=path, status=status)
    session.add(source)
    session.commit()
    return source


def add_resource(
    session,
    context,
    *,
    title="Skript",
    relative_path="skript.md",
    source=None,
    status="active",
    content_hash=HASH_A,
    resource_type="markdown",
    last_seen_at=BASE,
    locator=None,
):
    resource = Resource(
        context_id=context.id,
        source_id=source.id if source is not None else None,
        locator=locator or f"file:///kurs/{relative_path}",
        relative_path=relative_path,
        source_path_snapshot=f"/erlaubt/finanzierung/{relative_path}",
        title=title,
        resource_type=resource_type,
        status=status,
        current_content_hash=content_hash,
        byte_size=1024,
        mtime_ns=1,
        last_seen_at=last_seen_at,
    )
    session.add(resource)
    session.commit()
    return resource


def add_snapshot(
    session,
    resource,
    *,
    text="Bilanzierung Grundlagen",
    content_hash=HASH_A,
    extractor_version=EXTRACTOR,
    status="ready",
    truncated=False,
    metadata=None,
    created_at=BASE,
):
    snapshot = ResourceSnapshot(
        resource_id=resource.id,
        content_hash=content_hash,
        extractor_version=extractor_version,
        status=status,
        text=text if status == "ready" else None,
        truncated=truncated,
        metadata_payload=metadata if metadata is not None else {},
    )
    snapshot.created_at = created_at
    session.add(snapshot)
    session.commit()
    return snapshot


def add_topic(session, context, *, title="Bilanzierung", status="in_progress", created_at=BASE):
    # The schema enforces ``knowledge_key = 'topic-' || id`` with a CHECK, so the id
    # has to exist before the INSERT. The key stays immutable from then on, exactly
    # as Worker A's contract requires.
    topic = Topic(
        id=str(uuid.uuid4()),
        context_id=context.id,
        title=title,
        title_key=title.casefold(),
        status=status,
        knowledge_key="",
    )
    topic.knowledge_key = f"topic-{topic.id}"
    topic.created_at = created_at
    topic.updated_at = created_at
    session.add(topic)
    session.commit()
    return topic


def add_open_task(
    session,
    *,
    title="Uebungsblatt 1 loesen",
    context=None,
    topic=None,
    project_id=None,
    due_at=None,
    created_at=BASE,
    status="open",
    description=None,
    parent_task_id=None,
):
    task = Task(
        title=title,
        status=status,
        context_id=context.id if context is not None else None,
        topic_id=topic.id if topic is not None else None,
        project_id=project_id,
        parent_task_id=parent_task_id,
        due_at=due_at,
        description=description,
    )
    task.created_at = created_at
    task.updated_at = created_at
    session.add(task)
    session.commit()
    return task


def link_resource_topic(session, resource, topic):
    session.add(ResourceTopic(resource_id=resource.id, topic_id=topic.id))
    session.commit()


def link_task_resource(session, task, resource):
    session.add(TaskResource(task_id=task.id, resource_id=resource.id))
    session.commit()


def add_event(
    session, *, entity_type, entity_id, event_type="TEST", payload=None, occurred_at=BASE
):
    event = Event(
        occurred_at=occurred_at,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload if payload is not None else {"note": "ok"},
    )
    session.add(event)
    session.commit()
    return event


# ---------------------------------------------------------------------------
# Knowledge files
# ---------------------------------------------------------------------------


def write_knowledge(store: KnowledgeStore, knowledge_path: str, name: str, body: str) -> Path:
    target = store.root / knowledge_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def initialise_knowledge(store: KnowledgeStore, knowledge_path: str, title="Finanzierung") -> None:
    directory = store.root / knowledge_path
    (directory / "topics").mkdir(parents=True, exist_ok=True)
    (directory / "overview.md").write_text(
        f"# {title}\n\n## Ueberblick\n\nKursnotiz Rechnungswesen und Rechnungslegungsbilanz\n",
        encoding="utf-8",
    )
    (directory / "progress.md").write_text(
        "# Fortschritt\n\n## Status\n\n- Status: in Arbeit\n\n## Fortschritt\n\nErstes Kapitel gelesen.\n",
        encoding="utf-8",
    )
    (directory / "notes.md").write_text(
        "# Notizen\n\n## 2026-09-30 08:00 UTC\n\nSkript gelesen.\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The acceptance fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def finance(workspace_env, store):
    """One course context with sources, extracted documents, topics, tasks, knowledge."""
    session, tmp_path = workspace_env
    context = add_context(session, title="Finanzierung und Rechnungswesen", type="course")
    source = add_source(session, context)
    lecture = add_resource(
        session,
        context,
        title="Vorlesung 1",
        relative_path="vorlesung1.pdf",
        source=source,
        content_hash=HASH_A,
        resource_type="pdf",
    )
    add_snapshot(
        session,
        lecture,
        text="Kapitel 1: Bilanzierung. "
        + "Die Bewertung von Vermoegensgegenstaenden ist massgeblich. " * 40,
        content_hash=HASH_A,
        metadata={
            "page_spans": [{"char_start": 0, "char_end": 100000, "page_start": 1, "page_end": 12}]
        },
    )
    worksheet = add_resource(
        session,
        context,
        title="Uebungsblatt 1",
        relative_path="uebungsblatt1.pdf",
        source=source,
        content_hash=HASH_B,
        resource_type="pdf",
        last_seen_at=BASE + timedelta(hours=1),
    )
    add_snapshot(
        session,
        worksheet,
        text="Aufgabe 1: Bilanz erstellen.",
        content_hash=HASH_B,
        created_at=BASE + timedelta(hours=1),
    )
    balance = add_topic(session, context, title="Bilanzierung")
    blocked = add_topic(session, context, title="Abschreibung", status="blocked")
    link_resource_topic(session, lecture, balance)
    link_resource_topic(session, worksheet, balance)
    task = add_open_task(
        session,
        context=context,
        topic=balance,
        due_at=BASE + timedelta(days=2),
        description="Blatt 1 und 2 loesen",
    )
    later = add_open_task(
        session,
        context=context,
        topic=balance,
        due_at=BASE + timedelta(days=9),
        created_at=BASE + timedelta(hours=3),
    )
    link_task_resource(session, task, worksheet)
    add_event(session, entity_type="context", entity_id=context.id, event_type="CONTEXT_CREATED")
    add_event(
        session,
        entity_type="task",
        entity_id=task.id,
        event_type="TASK_CREATED",
        payload={"title": "Uebungsblatt"},
    )

    knowledge_path = f"contexts/{context.id}"
    # Bind through the ORM attribute so the session's identity map stays consistent;
    # a raw table update would leave a stale NULL in front of the builder.
    context.knowledge_path = knowledge_path
    session.commit()
    initialise_knowledge(store, knowledge_path)
    write_knowledge(
        store,
        knowledge_path,
        f"topics/{balance.knowledge_key}.md",
        "# Bilanzierung\n\n## Inhalt\n\nAktiva und Passiva.\n",
    )
    write_knowledge(
        store,
        knowledge_path,
        f"topics/{blocked.knowledge_key}.md",
        "# Abschreibung\n\n## Inhalt\n\nNoch offen.\n",
    )
    return {
        "session": session,
        "store": store,
        "tmp_path": tmp_path,
        "context": context,
        "source": source,
        "lecture": lecture,
        "worksheet": worksheet,
        "balance": balance,
        "blocked": blocked,
        "task": task,
        "later": later,
        "knowledge_path": knowledge_path,
    }


# ---------------------------------------------------------------------------
# Read-only observation helpers
# ---------------------------------------------------------------------------


def tree_fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
    """Path -> (size, mtime_ns, content hash) for every file below ``root``.

    Used to prove that a read never touches the knowledge tree, including that it
    creates no directory and modifies no byte.
    """
    fingerprint: dict[str, tuple[int, int, str]] = {}
    if not root.exists():
        return fingerprint
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        status = path.lstat()
        if path.is_symlink():
            fingerprint[relative] = ("symlink", 0, os.readlink(path))
        elif path.is_dir():
            fingerprint[relative] = ("dir", 0, "")
        else:
            fingerprint[relative] = (
                "file",
                status.st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return fingerprint


def database_fingerprint(path: Path) -> str:
    """Hash of the database file itself; a read-only run must not change a byte."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()
