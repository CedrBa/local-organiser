"""Shared fixtures and builders for test_workspace_context_review contracts; no executable tests."""

from __future__ import annotations

import json
import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from organiser.core.config import Settings
from organiser.db.migrations import upgrade_database
from organiser.services.workspace_context import (
    workspace_context,
)
from support.workspace import workspace_env as workspace_env
from support.workspace import finance as finance, store as store


def build(env, policy=None, **kwargs):
    return workspace_context(env["session"], env["context"].id, env["store"], policy, **kwargs)


def event_entry(packet, event_type):
    """The manifest entry of exactly one event, selected by its recorded event type.

    A context has more than one event, and the payload questions are about a specific
    one; taking the first entry would silently test some other row.
    """
    return next(
        entry
        for entry in packet.manifest.entities
        if entry.entity_type == "event" and entry.selected_fields.get("event_type") == event_type
    )


# ---------------------------------------------------------------------------
# F3 -- the cost of one request
# ---------------------------------------------------------------------------


@pytest.fixture
def counted_session(tmp_path):
    """A session whose JSON layer reports how many characters it was asked to parse."""
    settings = Settings(database_path=tmp_path / "counted.sqlite3")
    upgrade_database(settings)
    seen: list[int] = []

    def deserialiser(value):
        seen.append(len(value))
        return json.loads(value)

    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        json_deserializer=deserialiser,
    )

    @sa_event.listens_for(engine, "connect")
    def _configure(connection, _record):
        connection.execute("PRAGMA foreign_keys = ON")

    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as session:
        yield session, seen


# ---------------------------------------------------------------------------
# E1 -- the redaction has to reach an address wherever it stands
# ---------------------------------------------------------------------------

# Every host address in this module's payload fixtures, in the spelling the packet
# would serialise. The assertion helper checks both the plain and the JSON-escaped
# form, because a Windows path reaches the client as ``C:\\Kurs`` and a check that only
# looked for the plain spelling would pass on a leak.
HOST_ADDRESSES = (
    "/home/operator/GEHEIM/plan.pdf",
    "/home/operator/GEHEIM/zwei.pdf",
    "/home/operator/GEHEIM/drei.pdf",
    "C:\\Kurs\\vorlesung.pdf",
    "\\\\Fileserver\\Kurs\\vorlesung.pdf",
    "/var/lib/GEHEIM/vier.pdf",
    "/geheim",
)


def assert_no_host_address(rendered, extra=()):
    """No address from :data:`HOST_ADDRESSES` may appear anywhere in ``rendered``."""
    for address in (*HOST_ADDRESSES, *extra):
        assert address not in rendered, f"Hostadresse im Paket: {address}"
        assert address.replace("\\", "\\\\") not in rendered, f"Hostadresse im Paket: {address}"
