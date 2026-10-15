"""Reader races contracts for workspace."""

from __future__ import annotations

import os
import stat
from pathlib import Path
import pytest
from organiser.services.knowledge import (
    KnowledgeError,
)
from organiser.services.workspace_context import (
    WorkspaceSelectionError,
    workspace_context,
)
from support.workspace import workspace_env as workspace_env
from support.workspace import finance as finance, store as store
from support.paths import REPOSITORY_ROOT
from support.workspace_context_audit import (
    build as build,
)


def test_an_unknown_focus_id_list_is_refused_as_a_whole(finance):
    with pytest.raises(WorkspaceSelectionError):
        workspace_context(
            finance["session"], finance["context"].id, finance["store"], topic_ids=["a", "", "b"]
        )


# ---------------------------------------------------------------------------
# read_excerpt: component-wise descriptor walk
# ---------------------------------------------------------------------------


def test_read_excerpt_refuses_an_intermediate_symlink_swapped_in_after_the_check(
    store, monkeypatch
):
    """``O_NOFOLLOW`` on the final path says nothing about intermediate segments.

    ``is_symlink()`` on the assembled path describes the moment of the *check*. This
    installs the symlink right after that check, so only a component-wise descriptor
    walk can refuse it.
    """
    outside = store.root.parent / "outside"
    (outside / "contexts" / "c1").mkdir(parents=True)
    (outside / "contexts" / "c1" / "overview.md").write_text("geheim", encoding="utf-8")
    (store.root / "contexts").mkdir(parents=True)

    state = {"swapped": False}
    real_is_symlink = Path.is_symlink

    def swapping_is_symlink(self):
        result = real_is_symlink(self)
        if not result and not state["swapped"] and self.name == "c1":
            state["swapped"] = True
            (store.root / "contexts" / "c1").symlink_to(outside / "contexts" / "c1")
        return result

    monkeypatch.setattr(Path, "is_symlink", swapping_is_symlink)

    with pytest.raises(KnowledgeError):
        store.read_excerpt("contexts/c1/overview.md", 100)

    assert state["swapped"] is True, "der Test hat den Swap nicht ausgeloest"


def test_read_excerpt_does_not_block_on_a_fifo_installed_during_the_window(store, monkeypatch):
    """``O_RDONLY`` on a FIFO blocks forever; ``O_NONBLOCK`` is what prevents that."""
    directory = store.root / "contexts" / "c1"
    directory.mkdir(parents=True)
    target = directory / "overview.md"
    target.write_text("x" * 10, encoding="utf-8")

    real_lstat = os.lstat

    def replacing_lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if (
            not replacing_lstat.done
            and stat.S_ISREG(result.st_mode)
            and str(path).endswith("overview.md")
        ):
            replacing_lstat.done = True
            target.unlink()
            os.mkfifo(target)
        return result

    replacing_lstat.done = False
    monkeypatch.setattr(os, "lstat", replacing_lstat)

    with pytest.raises(KnowledgeError):
        store.read_excerpt("contexts/c1/overview.md", 100)

    assert replacing_lstat.done is True, "der Test hat den FIFO nicht installiert"


def test_read_excerpt_still_works_for_a_nested_path(store):
    directory = store.root / "contexts" / "c1" / "topics"
    directory.mkdir(parents=True)
    (directory / "topic-x.md").write_text("Inhalt", encoding="utf-8")

    read = store.read_excerpt("contexts/c1/topics/topic-x.md", 100)

    assert read.text == "Inhalt"
    assert read.relative_path == "contexts/c1/topics/topic-x.md"


# ---------------------------------------------------------------------------
# Second round: defects the first round of fixes introduced or left partial
# ---------------------------------------------------------------------------


def test_reading_many_excerpts_leaks_no_file_descriptors(store):
    """A descriptor leaked per path segment exhausted the process after ~64 requests."""
    directory = store.root / "contexts" / "c1" / "topics"
    directory.mkdir(parents=True)
    (directory / "topic-x.md").write_text("Inhalt", encoding="utf-8")

    def open_descriptors() -> int:
        return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else -1

    before = open_descriptors()
    for _ in range(200):
        assert store.read_excerpt("contexts/c1/topics/topic-x.md", 100).text == "Inhalt"
    after = open_descriptors()

    if before >= 0:
        assert after - before <= 2, f"{after - before} Deskriptoren nach 200 Reads"


def test_reading_many_excerpts_survives_a_low_descriptor_limit(tmp_path):
    """The strongest form: a worker with RLIMIT_NOFILE 64 must still be readable.

    Run in a subprocess: lowering the descriptor limit is process-global and would
    otherwise leak into the rest of the suite.
    """
    import subprocess
    import sys

    root = tmp_path / "knowledge"
    (root / "contexts" / "c1" / "topics").mkdir(parents=True)
    (root / "contexts" / "c1" / "topics" / "topic-x.md").write_text("Inhalt", encoding="utf-8")
    script = tmp_path / "probe.py"
    source_root = REPOSITORY_ROOT / "src"
    script.write_text(
        "\n".join(
            [
                "import resource, sys",
                f"sys.path.insert(0, {str(source_root)!r})",
                "from organiser.services.knowledge import KnowledgeStore",
                "previous = resource.getrlimit(resource.RLIMIT_NOFILE)",
                "resource.setrlimit(resource.RLIMIT_NOFILE, (64, previous[1]))",
                f"store = KnowledgeStore({str(root)!r})",
                "for _ in range(200):",
                "    assert store.read_excerpt('contexts/c1/topics/topic-x.md', 100).text == 'Inhalt'",
                "print('ok')",
            ]
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=180
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
