"""Scan policy contracts for ingestion."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from organiser.core.config import parse_source_allowed_roots
from organiser.db.models import Resource
from organiser.domain.resources import (
    ResourceIngestionError,
    SourceAccessError,
    is_excluded_component,
    relative_path_for,
    resource_type_for,
    title_for,
)
from organiser.services import source_scanner
from organiser.services.source_scanner import policy as scanner_policy
from organiser.services.resources import scan_source
from support.ingestion import (
    make_settings,
    seed_context,
    seed_source_id,
    session_for,
)


# -- settings: operator allowlist parsing ------------------------------------


def test_allowed_roots_default_to_deny(monkeypatch, tmp_path):
    monkeypatch.delenv("ORGANISER_SOURCE_ALLOWED_ROOTS", raising=False)
    assert make_settings(tmp_path).source_allowed_roots == ()
    assert parse_source_allowed_roots(None) == ()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json",
        "{}",
        '{"roots": ["/tmp"]}',
        '["relative/path"]',
        '[""]',
        "[null]",
        "[123]",
        '["/tmp", "relative"]',
        '["/tmp/\\u0000x"]',
    ],
)
def test_broken_allowlist_configuration_denies_everything(raw):
    assert parse_source_allowed_roots(raw) == ()


def test_allowlist_accepts_absolute_roots_and_deduplicates(tmp_path):
    assert parse_source_allowed_roots(f'["{tmp_path}", "{tmp_path}/", "{tmp_path}"]') == (
        tmp_path.resolve(),
    )
    policy = source_scanner.IngestionPolicy.from_settings(
        make_settings(tmp_path, allowed_roots=(tmp_path,))
    )
    assert policy.allowed_roots == (str(tmp_path.resolve()),)


def test_allowlist_is_read_from_the_environment(monkeypatch, tmp_path):
    from organiser.core.config import get_settings

    monkeypatch.setenv("ORGANISER_SOURCE_ALLOWED_ROOTS", f'["{tmp_path}"]')
    assert get_settings().source_allowed_roots == (tmp_path.resolve(),)
    monkeypatch.setenv("ORGANISER_SOURCE_ALLOWED_ROOTS", "kaputt")
    assert get_settings().source_allowed_roots == ()


# -- pure classification rules ----------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("skript.pdf", "pdf"),
        ("Notiz.MD", "markdown"),
        ("notiz.markdown", "markdown"),
        ("notiz.txt", "text"),
        ("archiv.zip", "other"),
        ("ohne_endung", "other"),
    ],
)
def test_resource_type_classification(name, expected):
    assert resource_type_for(name) == expected


def test_title_and_relative_label_are_bounded():
    assert title_for("unterordner/Notiz_fuer_Bilanzierung.md") == "Notiz fuer Bilanzierung"
    assert len(title_for("x" * 500 + ".pdf")) <= 200
    assert relative_path_for(("a", "b.txt")) == "a/b.txt"
    with pytest.raises(ValueError):
        relative_path_for(("..",))
    with pytest.raises(ValueError):
        relative_path_for(())


@pytest.mark.parametrize(
    "name", [".git", ".hidden", "node_modules", "__pycache__", ".venv", "venv", "site-packages"]
)
def test_excluded_components(name):
    assert is_excluded_component(name)


@pytest.mark.parametrize("name", ["Skript.pdf", "Notiz.md", "unterordner"])
def test_included_components(name):
    assert not is_excluded_component(name)


# -- policy refusals ---------------------------------------------------------


def test_scan_without_configured_root_is_denied(tmp_path):
    (tmp_path / "kurs").mkdir()
    settings = make_settings(tmp_path)
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(tmp_path / "kurs"))
        with pytest.raises(SourceAccessError) as error:
            scan_source(session, source_id, settings)
        assert error.value.code == "source_not_allowed"
        assert error.value.context["reason"] == "no_allowed_roots_configured"
        assert list(session.scalars(select(Resource))) == []


def test_scan_outside_allowed_root_is_denied(tmp_path):
    allowed = tmp_path / "erlaubt"
    allowed.mkdir()
    elsewhere = tmp_path / "anders"
    elsewhere.mkdir()
    (elsewhere / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(allowed,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(elsewhere))
        with pytest.raises(SourceAccessError) as error:
            scan_source(session, source_id, settings)
        assert error.value.context["reason"] == "outside_allowed_roots"
        assert error.value.status_code == 403


def test_relative_and_unsupported_sources_are_refused(tmp_path):
    (tmp_path / "kurs").mkdir()
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        relative_id = seed_source_id(session, context_id, path="kurs/unterordner")
        with pytest.raises(ResourceIngestionError) as error:
            scan_source(session, relative_id, settings)
        assert error.value.code == "source_path_not_absolute"
        assert error.value.status_code == 422
        for index, source_type in enumerate(("manual", "link", "inbox")):
            unsupported_id = seed_source_id(
                session, context_id, path=str(tmp_path / f"q{index}"), source_type=source_type
            )
            with pytest.raises(ResourceIngestionError) as raised:
                scan_source(session, unsupported_id, settings)
            assert raised.value.code == "source_type_unsupported"
        assert list(session.scalars(select(Resource))) == []


def test_inactive_source_cannot_be_scanned(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root), status="archived")
        with pytest.raises(SourceAccessError) as error:
            scan_source(session, source_id, settings)
        assert error.value.code == "source_inactive"


def test_knowledge_and_hidden_directories_are_not_sources(tmp_path):
    allowed = tmp_path / "erlaubt"
    (allowed / ".git").mkdir(parents=True)
    (allowed / "wissen" / ".versteckt").mkdir(parents=True)
    (allowed / "kurs").mkdir()
    settings = make_settings(tmp_path, allowed_roots=(allowed,), knowledge_root=allowed / "wissen")
    policy = source_scanner.IngestionPolicy.from_settings(settings)
    for candidate in (allowed / "wissen", allowed / ".git", allowed / "wissen" / ".versteckt"):
        with pytest.raises(SourceAccessError) as error:
            policy.authorize(str(candidate), expect="dir")
        assert error.value.code == "source_excluded"
    assert policy.authorize(str(allowed / "kurs"), expect="dir") == str(allowed.resolve())


def test_project_root_is_never_a_source(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_policy, "PROJECT_ROOT", tmp_path)
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    policy = source_scanner.IngestionPolicy.from_settings(settings)
    with pytest.raises(SourceAccessError) as error:
        policy.authorize(str(tmp_path / "src"), expect="dir")
    assert error.value.context["reason"] == "excluded_root"
