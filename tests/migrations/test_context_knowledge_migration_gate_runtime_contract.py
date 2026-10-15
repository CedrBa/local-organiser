"""Runtime contract contracts for migrations."""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from conftest import make_settings
from organiser.db.migrations import PROJECT_ROOT
from organiser.db.models import ContextSource, Event, Inbox, ProcessingRun
from organiser.db.session import create_session_factory
from organiser.domain.knowledge_path import KnowledgePathError, validate_context_path
from organiser.services.domain import (
    EventContext,
    add_context_source,
    create_context,
    create_project,
    update_context,
)
from support.paths import REPOSITORY_ROOT
from support.context_knowledge_migration_gate import (
    MIGRATION_0009 as MIGRATION_0009,
    REVISION_0007 as REVISION_0007,
    REVISION_0008 as REVISION_0008,
    REVISION_0009 as REVISION_0009,
    LEGACY_TABLES as LEGACY_TABLES,
    STAMP as STAMP,
    load_revision_0009 as load_revision_0009,
    alembic_config as alembic_config,
    enforced_engine as enforced_engine,
    revision_of as revision_of,
    table_names as table_names,
    snapshot as snapshot,
    foreign_key_violations as foreign_key_violations,
    rows as rows,
    index_sql as index_sql,
    insert_context as insert_context,
    insert_source as insert_source,
    create_legacy_world as create_legacy_world,
    assert_no_context_data as assert_no_context_data,
    assert_clean_integrity as assert_clean_integrity,
    assert_unchanged_at_0008 as assert_unchanged_at_0008,
    PATH_MATRIX as PATH_MATRIX,
)


@pytest.mark.parametrize("value", PATH_MATRIX, ids=repr)
def test_migration_preflight_and_runtime_validator_agree(value):
    """Beide Implementierungen muessen fuer dieselbe Eingabe dieselbe Antwort geben.

    Der Preflight in 0009 ist bewusst eingefroren und importiert den
    Laufzeitvalidator nicht. Diese Matrix haelt die beiden Regeln zusammen, ohne
    die historische Migration an wandelbaren Domaincode zu binden.
    """
    revision = load_revision_0009()
    try:
        validate_context_path(value)
        runtime_accepts = True
    except KnowledgePathError:
        runtime_accepts = False

    assert (revision._problem(value) is None) is runtime_accepts


def test_migration_preflight_names_the_drive_path_rule():
    """Die Diagnose benennt den Grund, damit die Bereinigung nichtrateselt."""
    revision = load_revision_0009()
    assert revision._problem("1:2") == "Windows-Laufwerkspfad"
    assert revision._problem("C:relative") == "Windows-Laufwerkspfad"
    assert revision._problem("C:") == "Windows-Laufwerkspfad"
    assert revision._problem("C:/pfad") == "absoluter Pfad"
    assert revision._problem("bewerbungen/sap") is None


def test_migration_preflight_does_not_import_the_domain_validator():
    """Die historische Migration darf den wandelbaren Domaincode nicht einbinden."""
    source = Path(MIGRATION_0009).read_text(encoding="utf-8")
    module = load_revision_0009()

    imported_modules = {
        node.module.split(".")[0] if isinstance(node, ast.ImportFrom) and node.module else None
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    assert "organiser" not in imported_modules
    assert not [name for name in module.__dict__ if name.split(".")[0] == "organiser"]
    # Die Laufwerksregel steht als eigene, festgeschriebene Zeile in der Revision.
    assert 'value[1:2] == ":"' in source
