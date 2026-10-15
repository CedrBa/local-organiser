"""Identity map verification contracts for workspace."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from organiser.db.models import Context, Resource, ResourceSnapshot, Task, Topic
from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    verify_workspace_manifest,
    workspace_context,
)
from support.workspace import (
    HASH_A,
    add_context,
    add_open_task,
    add_resource,
    add_snapshot,
    add_topic,
    workspace_env as workspace_env,
)
from support.workspace import finance as finance, store as store
from support.workspace_context_review import (
    build as build,
    event_entry as event_entry,
    counted_session as counted_session,
    HOST_ADDRESSES as HOST_ADDRESSES,
    assert_no_host_address as assert_no_host_address,
)


# ---------------------------------------------------------------------------
# Identity-Map Regressions: canonical DB reads, no stale unchanged, no autoflush
# ---------------------------------------------------------------------------


def test_identity_map_stale_objects_with_second_connection_modification_fail_verification(
    workspace_env,
):
    """Manifest verification for Context, Task, Topic, Resource and ResourceSnapshot
    must not derive unchanged=True from stale ORM identity-map instances when a second
    connection modified the database rows.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)

    context = add_context(session, title="Context Original")
    task = add_open_task(session, context=context, title="Task Original")
    topic = add_topic(session, context=context, title="Topic Original")
    resource = add_resource(session, context, title="Resource Original", content_hash=HASH_A)
    snapshot = add_snapshot(session, resource, content_hash=HASH_A)
    session.commit()

    # Preload entities into the session's ORM identity map
    c_orm = session.get(Context, context.id)
    t_orm = session.get(Task, task.id)
    top_orm = session.get(Topic, topic.id)
    r_orm = session.get(Resource, resource.id)
    s_orm = session.get(ResourceSnapshot, snapshot.id)
    assert all(obj is not None for obj in (c_orm, t_orm, top_orm, r_orm, s_orm))

    packet = workspace_context(session, context.id, store)
    clean_ver = verify_workspace_manifest(session, store, packet.manifest)
    assert clean_ver.unchanged is True, clean_ver.changes

    engine2 = create_engine(f"sqlite:///{tmp_path}/workspace.sqlite3")

    # 1. Modify Context via connection 2
    with engine2.connect() as conn:
        conn.execute(
            text("UPDATE contexts SET title = 'Context Modifiziert' WHERE id = :id"),
            {"id": context.id},
        )
        conn.commit()
    # ORM identity map in session still holds the stale value:
    assert c_orm.title == "Context Original"
    v_ctx = verify_workspace_manifest(session, store, packet.manifest)
    assert v_ctx.unchanged is False
    assert any(c.reference == f"context:{context.id}" and c.field == "title" for c in v_ctx.changes)

    # Revert Context
    with engine2.connect() as conn:
        conn.execute(
            text("UPDATE contexts SET title = 'Context Original' WHERE id = :id"),
            {"id": context.id},
        )
        conn.commit()

    # 2. Modify Task via connection 2
    with engine2.connect() as conn:
        conn.execute(text("UPDATE tasks SET status = 'done' WHERE id = :id"), {"id": task.id})
        conn.commit()
    assert t_orm.status == "open"
    v_task = verify_workspace_manifest(session, store, packet.manifest)
    assert v_task.unchanged is False
    assert any(c.reference == f"task:{task.id}" and c.field == "status" for c in v_task.changes)

    # Revert Task
    with engine2.connect() as conn:
        conn.execute(text("UPDATE tasks SET status = 'open' WHERE id = :id"), {"id": task.id})
        conn.commit()

    # 3. Modify Topic via connection 2
    with engine2.connect() as conn:
        conn.execute(
            text("UPDATE topics SET title = 'Topic Geaendert' WHERE id = :id"), {"id": topic.id}
        )
        conn.commit()
    assert top_orm.title == "Topic Original"
    v_topic = verify_workspace_manifest(session, store, packet.manifest)
    assert v_topic.unchanged is False
    assert any(c.reference == f"topic:{topic.id}" and c.field == "title" for c in v_topic.changes)

    # Revert Topic
    with engine2.connect() as conn:
        conn.execute(
            text("UPDATE topics SET title = 'Topic Original' WHERE id = :id"), {"id": topic.id}
        )
        conn.commit()

    # 4. Modify Resource via connection 2
    with engine2.connect() as conn:
        conn.execute(
            text("UPDATE resources SET status = 'archived' WHERE id = :id"), {"id": resource.id}
        )
        conn.commit()
    assert r_orm.status == "active"
    v_res = verify_workspace_manifest(session, store, packet.manifest)
    assert v_res.unchanged is False
    assert any(
        c.reference == f"resource:{resource.id}" and c.field == "status" for c in v_res.changes
    )

    # Revert Resource
    with engine2.connect() as conn:
        conn.execute(
            text("UPDATE resources SET status = 'active' WHERE id = :id"), {"id": resource.id}
        )
        conn.commit()

    # 5. Modify ResourceSnapshot via connection 2
    with engine2.connect() as conn:
        conn.execute(
            text("UPDATE resource_snapshots SET extractor_version = 'v2-new' WHERE id = :id"),
            {"id": snapshot.id},
        )
        conn.commit()
    assert s_orm.extractor_version == "text-v1"
    v_snap = verify_workspace_manifest(session, store, packet.manifest)
    assert v_snap.unchanged is False
    assert any(
        c.reference == f"snapshot:{snapshot.id}" and c.field == "extractor_version"
        for c in v_snap.changes
    )


def test_identity_map_stale_objects_with_second_connection_deletion_fail_verification(
    workspace_env,
):
    """Manifest verification must report missing entities/snapshots when rows are deleted
    via a second connection, even if the primary session still caches the ORM instances.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)

    context = add_context(session, title="Deletion Context")
    task = add_open_task(session, context=context, title="Deletion Task")
    topic = add_topic(session, context=context, title="Deletion Topic")
    resource = add_resource(session, context, title="Deletion Resource", content_hash=HASH_A)
    snapshot = add_snapshot(session, resource, content_hash=HASH_A)
    session.commit()

    # Preload entities into identity map
    session.get(Context, context.id)
    session.get(Task, task.id)
    session.get(Topic, topic.id)
    session.get(Resource, resource.id)
    session.get(ResourceSnapshot, snapshot.id)

    packet = workspace_context(session, context.id, store)
    engine2 = create_engine(f"sqlite:///{tmp_path}/workspace.sqlite3")

    # Delete task via second connection
    with engine2.connect() as conn:
        conn.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task.id})
        conn.commit()

    ver = verify_workspace_manifest(session, store, packet.manifest)
    assert ver.unchanged is False
    assert any(c.kind == "missing" and c.reference == f"task:{task.id}" for c in ver.changes)


def test_manifest_verification_is_read_only_and_does_not_autoflush_caller_changes(workspace_env):
    """Manifest verification must not autoflush pending caller mutations in the session,
    must not commit, and must perform zero database writes.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)

    context = add_context(session, title="Clean Context")
    session.commit()

    packet = workspace_context(session, context.id, store)

    # Caller mutates an ORM object in session without committing
    context.title = "Ungespeicherte Aenderung im Caller"
    assert session.is_modified(context)

    # Run verification -- must not autoflush context.title to DB
    ver = verify_workspace_manifest(session, store, packet.manifest)
    # The recorded title in manifest matches the DB row since it wasn't flushed
    assert ver.unchanged is True
    # Caller object in session must STILL be dirty and uncommitted
    assert session.is_modified(context)
    assert context.title == "Ungespeicherte Aenderung im Caller"

    # Verify directly via second connection that DB row was NOT updated
    engine2 = create_engine(f"sqlite:///{tmp_path}/workspace.sqlite3")
    with engine2.connect() as conn:
        row_title = conn.execute(
            text("SELECT title FROM contexts WHERE id = :id"), {"id": context.id}
        ).scalar()
        assert row_title == "Clean Context", (
            "Autoflush darf die Caller-Aenderung nicht in die DB schreiben"
        )
