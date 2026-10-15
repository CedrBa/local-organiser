"""Source labels contracts for ingestion."""

from __future__ import annotations

from organiser.core.config import Settings
from organiser.db.models import Resource
from organiser.services import source_scanner
from organiser.services.resources import (
    extract_resource,
    list_resources,
    scan_source,
    verify_resource_version,
)
from support.ingestion import (
    close_boundary,
    make_settings,
    resource_by_locator,
    seed_context,
    seed_source_id,
    session_for,
)
from support.workspace_ingestion_review_fixes import (
    EMPTY_SHA256 as EMPTY_SHA256,
    _CountingScanner as _CountingScanner,
    _scanned_course as _scanned_course,
    _resource_id_of as _resource_id_of,
    _refill_after_stat as _refill_after_stat,
    _orphan_the_source as _orphan_the_source,
    resource_id_of as resource_id_of,
)


# -- F6: the policy is derived once -------------------------------------------


def test_policy_is_derived_once_per_verification(tmp_path, monkeypatch):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        expected = session.get(Resource, resource_id).current_content_hash
        close_boundary(session)

        calls = []
        real_from_settings = source_scanner.IngestionPolicy.from_settings

        def counting_from_settings(given):
            calls.append(given)
            return real_from_settings(given)

        monkeypatch.setattr(source_scanner.IngestionPolicy, "from_settings", counting_from_settings)
        check = verify_resource_version(session, resource_id, expected, settings)
        assert check.ok is True
        assert len(calls) == 1


# -- F7: labels belong to the owning source and are stable ---------------------


def test_document_source_labels_are_the_bare_name_and_may_coincide(tmp_path):
    """Two document sources with the same file name keep the same label.

    A label that made them unique would have to be built from the absolute address
    or from the allowlist. Both are outside the owning source, so the stored label
    would change when the operator changes the allowlist or when a neighbouring
    source also sees the file. Identity is the locator, so the label stays the
    minimal, stable thing it can be.
    """
    first = tmp_path / "bilanzierung"
    second = tmp_path / "rechnungswesen"
    first.mkdir()
    second.mkdir()
    (first / "Notiz.md").write_text("Bilanzierung", encoding="utf-8")
    (second / "Notiz.md").write_text("Rechnungswesen", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        first_id = seed_source_id(
            session, context_id, path=str(first / "Notiz.md"), source_type="document"
        )
        second_id = seed_source_id(
            session, context_id, path=str(second / "Notiz.md"), source_type="note"
        )
        scan_source(session, first_id, settings)
        scan_source(session, second_id, settings)
        close_boundary(session)
        page = list_resources(session, context_id)
        assert page.total == 2
        assert {item.relative_path for item in page.items} == {"Notiz.md"}
        assert {item.title for item in page.items} == {"Notiz"}
        # Two resources, distinguished by their id and not by their label.
        assert len({item.id for item in page.items}) == 2
        for item in page.items:
            assert str(tmp_path) not in item.relative_path
            assert not item.relative_path.startswith("/")
        close_boundary(session)
        # Both are independently extractable, each with its own snapshot.
        texts = {
            item.relative_path: extract_resource(session, item.id, settings).status.value
            for item in page.items
        }
        assert set(texts.values()) == {"ready"}


def test_a_neighbouring_source_never_renames_a_foreign_resource(tmp_path):
    """A label follows the owner, in both scan orders.

    A folder source and a document source can discover the same address. Whoever
    registered the resource owns its label, and the other's observation refreshes
    the version without renaming the row -- otherwise the label would depend on
    scan order.
    """
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "unter" / "Notiz.md"
    target.parent.mkdir()
    target.write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    for folder_first in (True, False):
        with session_for(settings) as session:
            context_id = seed_context(session, title=f"Kurs {folder_first}").id
            close_boundary(session)
            folder_id = seed_source_id(session, context_id, path=str(root))
            close_boundary(session)
            document_id = seed_source_id(
                session, context_id, path=str(target), source_type="document"
            )
            close_boundary(session)
            order = (
                [(folder_id, str(root)), (document_id, str(target))]
                if folder_first
                else [(document_id, str(target)), (folder_id, str(root))]
            )
            for source_id, _path in order:
                scan_source(session, source_id, settings)
                close_boundary(session)
            page = list_resources(session, context_id)
            assert page.total == 1, folder_first
            item = page.items[0]
            expected_label = "unter/Notiz.md" if folder_first else "Notiz.md"
            assert item.relative_path == expected_label
            assert item.title == "Notiz"
            # A second full round in the other order changes nothing either.
            close_boundary(session)
            for source_id, _path in reversed(order):
                scan_source(session, source_id, settings)
                close_boundary(session)
            again = list_resources(session, context_id)
            assert again.items[0].relative_path == expected_label
            assert again.items[0].title == "Notiz"
            assert again.items[0].current_snapshot_id is not None


def test_labels_do_not_depend_on_the_operator_allowlist(tmp_path):
    """Changing the allowed roots must not rename an existing resource."""
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "unter").mkdir()
    (root / "unter" / "Notiz.md").write_text("erste Fassung", encoding="utf-8")
    wide = make_settings(tmp_path / "db-wide", allowed_roots=(tmp_path,))
    with session_for(wide) as session:
        context_id = seed_context(session).id
        folder_id = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        scan_source(session, folder_id, wide)
        close_boundary(session)
        first = resource_by_locator(session, context_id=context_id, relative_path="unter/Notiz.md")
        assert first is not None
        close_boundary(session)
        # Same database, narrower allowlist: still the same source, still readable.
        narrow = Settings(
            database_path=wide.database_path,
            source_allowed_roots=(root,),
            knowledge_root=wide.knowledge_root,
            api_token=wide.api_token,
        )
        scan_source(session, folder_id, narrow)
        close_boundary(session)
        second = session.get(Resource, first.id)
        assert second.relative_path == "unter/Notiz.md"
        assert second.title == "Notiz"
        # A document source in the same context keeps its bare name, too.
        close_boundary(session)
        document_id = seed_source_id(
            session, context_id, path=str(root / "unter" / "Notiz.md"), source_type="document"
        )
        close_boundary(session)
        scan_source(session, document_id, narrow)
        close_boundary(session)
        owned = session.get(Resource, first.id)
        assert owned.source_id == folder_id
        assert owned.relative_path == "unter/Notiz.md"


def test_orphan_rebind_recomputes_the_label_from_the_new_owner(tmp_path):
    """After a re-adoption the label belongs to the new owner, exactly once."""
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "unter" / "Notiz.md"
    target.parent.mkdir()
    target.write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        folder_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, folder_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="unter/Notiz.md"
        ).id
        close_boundary(session)
        _orphan_the_source(session, folder_id)
        close_boundary(session)

        document_id = seed_source_id(session, context_id, path=str(target), source_type="document")
        close_boundary(session)
        result = scan_source(session, document_id, settings)
        assert result.counters.rebound == 1
        close_boundary(session)
        row = session.get(Resource, resource_id)
        assert row.source_id == document_id
        assert row.relative_path == "Notiz.md"
        assert row.title == "Notiz"
        # And it stays that way: a repeated scan of the new owner is a no-op.
        close_boundary(session)
        again = scan_source(session, document_id, settings)
        assert again.counters.unchanged == 1
        close_boundary(session)
        assert session.get(Resource, resource_id).relative_path == "Notiz.md"
