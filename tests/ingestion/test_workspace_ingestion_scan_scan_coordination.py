"""Scan coordination contracts for ingestion."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from organiser.db.migrations import upgrade_database
from organiser.db.models import ContextSource, ProcessingRun, Resource
from organiser.domain.resources import (
    SourceAccessError,
    SourceChangedError,
    SourceStaleError,
)
from organiser.services import source_scanner
from organiser.services.domain import update_context_source
from organiser.services.resources import extract_resource, scan_source, verify_resource_version
from support.ingestion import (
    close_boundary,
    make_settings,
    resource_by_locator,
    seed_context,
    seed_source_id,
    session_for,
    snapshots_of,
)


def test_source_change_during_scan_discards_the_result(tmp_path, monkeypatch):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    real_walk = source_scanner.scan_source_tree
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)

        def walk_then_change(*args, **kwargs):
            result = real_walk(*args, **kwargs)
            # A concurrent PATCH of the source between observation and commit.
            with session_for(settings) as other:
                other.execute(
                    ContextSource.__table__.update()
                    .where(ContextSource.id == source_id)
                    .values(path=str(root / "anders"))
                )
                other.commit()
            return result

        monkeypatch.setattr("organiser.services.resources.scan.scan_source_tree", walk_then_change)
        with pytest.raises(SourceChangedError):
            scan_source(session, source_id, settings)
        close_boundary(session)
        assert list(session.scalars(select(Resource))) == []
        run = session.scalar(select(ProcessingRun))
        assert run.status == "failed"
        assert run.output_payload["error_code"] == "source_changed"


# -- stale sources -----------------------------------------------------------


def test_archived_source_invalidates_access(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        close_boundary(session)
        update_context_source(
            session, context_id=context_id, source_id=source_id, status="archived"
        )
        close_boundary(session)
        with pytest.raises(SourceStaleError):
            extract_resource(session, resource_id, settings)
        close_boundary(session)
        check = verify_resource_version(session, resource_id, "0" * 64, settings)
        assert check.ok is False
        assert check.reason == "source_not_active"
        assert len(snapshots_of(session, resource_id)) == 1


def test_deleted_source_invalidates_access_without_deleting_history(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        close_boundary(session)
        session.execute(ContextSource.__table__.delete().where(ContextSource.id == source_id))
        session.commit()
        close_boundary(session)
        assert session.get(Resource, resource_id).source_id is None
        assert len(snapshots_of(session, resource_id)) == 1
        close_boundary(session)
        with pytest.raises(SourceStaleError):
            extract_resource(session, resource_id, settings)
        close_boundary(session)
        check = verify_resource_version(session, resource_id, "0" * 64, settings)
        assert check.ok is False
        assert check.reason == "source_removed"


def test_source_moved_out_of_the_allowlist_invalidates_access(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    narrowed = source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path / "anders"),))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        close_boundary(session)
        with pytest.raises(SourceAccessError) as error:
            extract_resource(session, resource_id, settings, policy=narrowed)
        assert error.value.context["reason"] == "outside_allowed_roots"
        close_boundary(session)
        check = verify_resource_version(session, resource_id, "0" * 64, settings, policy=narrowed)
        assert check.ok is False
        assert check.reason == "source_not_allowed"


# -- originals stay untouched -------------------------------------------------


def test_scan_and_extract_do_not_touch_the_originals(tmp_path):
    root = tmp_path / "kurs"
    (root / "unter").mkdir(parents=True)
    (root / "unter" / "Notiz.md").write_text("unveraendert", encoding="utf-8")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="unter/Notiz.md"
        ).id
        close_boundary(session)
        extract_resource(session, resource_id, settings)
    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    assert before == after
    assert sorted(path.name for path in root.iterdir()) == ["unter"]


def test_services_refuse_a_callers_open_transaction(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        session.add(
            Resource(
                context_id=context_id,
                locator="/x",
                relative_path="x",
                source_path_snapshot="/x",
                title="x",
                resource_type="other",
            )
        )
        session.flush()
        from organiser.domain.resources import ResourceTransactionError

        with pytest.raises(ResourceTransactionError):
            scan_source(session, source_id, settings)
        session.rollback()
        close_boundary(session)
        session.info["defer_domain_commit"] = True
        with pytest.raises(ResourceTransactionError):
            scan_source(session, source_id, settings)
        session.info.pop("defer_domain_commit")


def test_migration_upgrade_creates_the_workspace_schema(tmp_path):
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    upgrade_database(settings)
    with session_for(settings) as session:
        seed_context(session)


# -- regressions for the independent review ----------------------------------


def test_document_source_registers_exactly_that_file(tmp_path):
    """A document source is one file, not a directory to walk.

    Its label is the bare file name, because that is the whole scope of this kind of
    source. A unique label would have to be built from the absolute address or from
    the operator allowlist, and both would let the stored label change without the
    file changing. Identity is the locator.
    """
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("nur diese Datei", encoding="utf-8")
    (root / "Andere.md").write_text("nicht Teil des Dokuments", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(
            session, context_id, path=str(root / "Notiz.md"), source_type="document"
        )
        result = scan_source(session, source_id, settings)
        assert result.complete is True
        assert result.counters.created == 1
        resource = resource_by_locator(session, context_id=context_id, relative_path="Notiz.md")
        assert resource is not None
        assert resource.source_path_snapshot == str(root / "Notiz.md")
        assert str(tmp_path) not in resource.relative_path
        assert (
            resource_by_locator(session, context_id=context_id, relative_path="Andere.md") is None
        )
