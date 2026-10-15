"""Entry budgets contracts for ingestion."""

from __future__ import annotations

import os
from organiser.domain.resources import (
    DEFAULT_LIMITS,
)
from organiser.services import source_scanner
from organiser.services.resources import (
    scan_source,
)
from support.ingestion import (
    make_settings,
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


# -- F5: the entry budget bounds the enumeration --------------------------------


def test_enumeration_budget_counts_hidden_and_unsafe_entries(tmp_path):
    """Hidden names, links and special files cost budget like any other entry."""
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(30):
        (root / f".versteckt{index}").write_text("x", encoding="utf-8")
    os.mkfifo(root / "pipe")
    os.symlink("/etc/passwd", root / "Verweis.md")
    (root / "echt.md").write_text("echt", encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path),))
    limits = DEFAULT_LIMITS.narrowed(max_entries=4)

    result = source_scanner.scan_source_tree(str(root), policy, limits)
    assert result.complete is False
    assert result.stop_reason == "entry_limit_reached"
    assert result.entries_seen == limits.max_entries
    assert len(result.files) <= limits.max_entries


def test_enumeration_does_not_materialise_more_than_the_budget(tmp_path, monkeypatch):
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(60):
        (root / f"Notiz{index}.md").write_text("x", encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path),))
    limits = DEFAULT_LIMITS.narrowed(max_entries=3)
    pulled = [0]
    real_scandir = os.scandir

    def counting_scandir(*args, **kwargs):
        return _CountingScanner(real_scandir(*args, **kwargs), pulled)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    result = source_scanner.scan_source_tree(str(root), policy, limits)
    # Three charged entries plus the one name that proved there was a fourth.
    assert pulled[0] == limits.max_entries + 1
    assert result.entries_seen == limits.max_entries
    assert result.complete is False


def test_truncated_enumeration_is_stable_and_never_marks_missing(tmp_path):
    """The selection is stable for an unchanged directory and stays bounded.

    Which entries a truncated enumeration picks is the directory's own order --
    name-ordering the whole directory first is exactly the unbounded
    materialisation the budget has to prevent. Two runs over the same directory
    therefore agree, which is the determinism that is actually achievable here.
    """
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(10):
        (root / f"Notiz{index:02d}.md").write_text(f"text {index}", encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path),))
    limits = DEFAULT_LIMITS.narrowed(max_entries=4)
    first = source_scanner.scan_source_tree(str(root), policy, limits)
    second = source_scanner.scan_source_tree(str(root), policy, limits)
    assert first.complete is False
    assert [item.locator for item in first.files] == [item.locator for item in second.files]
    assert 0 < len(first.files) <= limits.max_entries


def test_budget_exactly_matching_the_directory_is_complete(tmp_path):
    """The budget is an upper bound, not an off-by-one refusal."""
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(4):
        (root / f"Notiz{index}.md").write_text("x", encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path),))
    result = source_scanner.scan_source_tree(
        str(root), policy, DEFAULT_LIMITS.narrowed(max_entries=4)
    )
    assert result.complete is True
    assert result.entries_seen == 4
    assert len(result.files) == 4


def test_subdirectory_the_budget_cannot_pay_for_stops_the_whole_run(tmp_path):
    """The deferred stop is the only thing that makes this run incomplete.

    The root directory fits the budget exactly, so the overflow is discovered
    *inside* the subdirectory and only reaches the result through the deferred
    stop. Without it the run would look complete, and a complete run may mark
    absent resources ``missing`` -- so this is the path where a missing
    ``deferred_stop`` would do real damage, which is why it is pinned here.
    """
    root = tmp_path / "kurs"
    (root / "unter").mkdir(parents=True)
    (root / "Notiz.md").write_text("a", encoding="utf-8")
    (root / "Zusatz.md").write_text("b", encoding="utf-8")
    (root / "unter" / "Kapitel.md").write_text("c", encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path),))

    limited = DEFAULT_LIMITS.narrowed(max_entries=3)
    result = source_scanner.scan_source_tree(str(root), policy, limited)
    assert result.complete is False
    assert result.stop_reason == "entry_limit_reached"
    # Three root entries paid for, not one name from inside the subdirectory.
    assert result.entries_seen == limited.max_entries
    assert {item.relative_path for item in result.files} == {"Notiz.md", "Zusatz.md"}

    # It is the budget, not a hole in the walk: one more entry sees the chapter.
    generous = DEFAULT_LIMITS.narrowed(max_entries=4)
    whole = source_scanner.scan_source_tree(str(root), policy, generous)
    assert whole.complete is True
    assert whole.entries_seen == 4
    assert {item.relative_path for item in whole.files} == {
        "Notiz.md",
        "Zusatz.md",
        "unter/Kapitel.md",
    }


def test_entry_and_read_budget_together_still_stop_the_run_incomplete(tmp_path):
    """Two budgets hit at once: incomplete, and both are visible in the result.

    The read budget is the immediate cause of the stop, so it is the reason that
    is reported. The truncated enumeration is not lost -- ``entries_seen`` sits at
    the entry budget -- because a client needs to know that raising only the read
    budget will not make this source whole.
    """
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(5):
        (root / f"Notiz{index}.md").write_text("x" * 50, encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path),))
    limits = DEFAULT_LIMITS.narrowed(max_entries=3, max_total_read_bytes=60, max_file_bytes=50)

    result = source_scanner.scan_source_tree(str(root), policy, limits)
    assert result.complete is False
    assert result.stop_reason == "read_budget_exhausted"
    assert result.entries_seen == limits.max_entries
    assert result.read_budget_used == limits.max_total_read_bytes
    assert [item.relative_path for item in result.files if item.unread_reason] == ["Notiz3.md"]
    assert all(
        item.unread_reason == "read_budget_exhausted" for item in result.files if item.unread_reason
    )


def test_hidden_entries_alone_can_exhaust_the_entry_budget(tmp_path):
    """No file is registered and nothing is claimed to be missing."""
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(20):
        (root / f".versteckt{index}").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(
            session, source_id, settings, limits=DEFAULT_LIMITS.narrowed(max_entries=5)
        )
        assert result.complete is False
        assert result.error_code == "entry_limit_reached"
        assert result.counters.created == 0
        assert result.counters.entries == 5
        assert result.counters.marked_missing == 0
        assert any(
            warning.code == "scan_incomplete_no_missing_decision" for warning in result.warnings
        )
