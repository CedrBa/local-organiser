"""Contracts contracts for context_knowledge."""

from __future__ import annotations

import pytest
from organiser.domain.knowledge_path import (
    KnowledgePathError,
    validate_context_path,
    validate_knowledge_path,
)
from support.context_knowledge_integration import (
    alembic_config as alembic_config,
    session as session,
    service as service,
    event_types as event_types,
    tree as tree,
    _raise_commit as _raise_commit,
    _raise_filesystem as _raise_filesystem,
    _insert_context as _insert_context,
)


# -- pure path contract -------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "/absolut/pfad",
        "//server/share",
        "C:/pfad",
        "c:\\pfad",
        "bewerbungen\\sap",
        "a//b",
        "a/./b",
        "./a",
        "..",
        "a/../b",
        "a/b/",
        "a\x00b",
        "a\nb",
        " lead/pfad",
        "pfad/lead ",
        "a/ /b",
        "-rf",
        "a/-rf",
        "a" * 513,
        "a/" * 256 + "x",
    ],
)
def test_shared_validator_rejects_unsafe_context_paths(value):
    with pytest.raises(KnowledgePathError):
        validate_context_path(value)


@pytest.mark.parametrize("value", ["1:2", "C:relative", "c:relative", "é:relative"])
@pytest.mark.parametrize("allow_reserved_segments", [False, True])
def test_shared_validator_rejects_single_character_drive_prefix(value, allow_reserved_segments):
    """Die Policy gilt auch fuer Nicht-ASCII-Buchstaben und fuer Store-Dateipfade."""
    with pytest.raises(KnowledgePathError, match="Windows-Laufwerkspfad"):
        validate_knowledge_path(value, allow_reserved_segments=allow_reserved_segments)


@pytest.mark.parametrize("value", ["bewerbungen/sap", "a", "ab:cd", "a/b:c"])
@pytest.mark.parametrize("allow_reserved_segments", [False, True])
def test_shared_validator_preserves_relative_paths_outside_drive_prefix_policy(
    value, allow_reserved_segments
):
    """Normale relative Pfade und Doppelpunkte ausserhalb des Praefixes bleiben erlaubt."""
    assert validate_knowledge_path(value, allow_reserved_segments=allow_reserved_segments) == value
