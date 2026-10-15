"""Snapshot times contracts for workspace."""

from __future__ import annotations

from datetime import timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from organiser.core.config import Settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import ResourceSnapshot
from organiser.domain.context import KNOWLEDGE_CURRENT
from organiser.services.workspace_context import (
    workspace_context,
)
from support.workspace import (
    BASE,
    HASH_A,
    add_context,
    add_resource,
    add_snapshot,
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
# F1 -- aware and naive timestamps in the same snapshot window
# ---------------------------------------------------------------------------


def test_snapshots_of_one_resource_survive_mixed_aware_and_naive_timestamps(tmp_path):
    """SQLite stores no timezone, and the session keeps ``expire_on_commit=False``.

    So one snapshot of a resource arrives as an *aware* ``datetime`` -- the value the
    Python default produced in this session -- while the next one, read back from the
    database, is *naive*. Two extractor versions of one content hash are enough to make
    the builder compare them, and the raw comparison raised ``TypeError``: an HTTP 500
    on ordinary data instead of a packet.
    """
    settings = Settings(database_path=tmp_path / "mixed.sqlite3")
    upgrade_database(settings)
    factory = sessionmaker(bind=create_engine(settings.database_url), expire_on_commit=False)
    session = factory()
    context = add_context(session, title="Gemischte Zeitstempel")
    resource = add_resource(session, context, content_hash=HASH_A)

    # Fresh in this session: aware, and it stays aware because nothing expires it.
    add_snapshot(
        session,
        resource,
        text="frisch",
        content_hash=HASH_A,
        created_at=BASE,
        extractor_version="text-v1",
    )
    # Written by a different session: naive once this session reads it back.
    other = factory()
    with other:
        later = ResourceSnapshot(
            resource_id=resource.id,
            content_hash=HASH_A,
            extractor_version="pdf-v2",
            status="ready",
            text="spaeter",
            truncated=False,
            metadata_payload={},
        )
        later.created_at = BASE + timedelta(hours=1)
        other.add(later)
        other.commit()

    packet = workspace_context(session, context.id, None)

    shown = packet.resources[0]
    assert shown.knowledge_status == KNOWLEDGE_CURRENT
    # The newer snapshot wins, in whichever order the identity map and SQLite hand the
    # two rows over -- that is what the normalisation is for.
    assert shown.snapshot_id == later.id
    assert [excerpt.text for excerpt in packet.knowledge_excerpts] == ["spaeter"]

    # Reversed insertion order, same result: the selection does not depend on which row
    # happened to be loaded from Python and which from disk.
    session2 = factory()
    context2 = add_context(session2, title="Umgekehrt")
    resource2 = add_resource(session2, context2, content_hash=HASH_A)
    newer = ResourceSnapshot(
        resource_id=resource2.id,
        content_hash=HASH_A,
        extractor_version="pdf-v2",
        status="ready",
        text="spaeter",
        truncated=False,
        metadata_payload={},
    )
    newer.created_at = BASE + timedelta(hours=1)
    session2.add(newer)
    session2.commit()
    other2 = factory()
    with other2:
        add_snapshot(
            other2,
            resource2,
            text="frisch",
            content_hash=HASH_A,
            created_at=BASE,
            extractor_version="text-v1",
        )

    packet2 = workspace_context(session2, context2.id, None)

    assert packet2.resources[0].snapshot_id == newer.id
