"""Reader descriptors contracts for workspace."""

from __future__ import annotations

import os
import subprocess
import sys
import pytest
from organiser.services.knowledge import (
    KnowledgeError,
    KnowledgeStore,
    UnsafeKnowledgePathError,
)
from support.workspace import workspace_env as workspace_env
from support.workspace import finance as finance, store as store
from support.paths import REPOSITORY_ROOT
from support.workspace_context_review import (
    build as build,
    event_entry as event_entry,
    counted_session as counted_session,
    HOST_ADDRESSES as HOST_ADDRESSES,
    assert_no_host_address as assert_no_host_address,
)


# ---------------------------------------------------------------------------
# F7 -- the reader must stay inside the KnowledgeError family
# ---------------------------------------------------------------------------


def test_a_symlinked_knowledge_root_is_a_knowledge_error_not_a_raw_os_error(tmp_path):
    """``is_dir()`` follows a link; the descriptor open below it does not.

    A root that turned into a symlink between the two calls used to leave
    ``_open_below_root`` with a raw ``OSError``. Every caller catches
    ``KnowledgeError`` -- the workspace builder's response is a bounded skip with a
    warning -- so the exception escaped the whole reader family and turned one GET into
    an HTTP 500.
    """
    target = tmp_path / "ziel"
    (target / "contexts" / "c1").mkdir(parents=True)
    (target / "contexts" / "c1" / "overview.md").write_text("Inhalt", encoding="utf-8")
    root = tmp_path / "root"
    store = KnowledgeStore(root)
    root.symlink_to(target)

    with pytest.raises(KnowledgeError) as failure:
        store.read_excerpt("contexts/c1/overview.md", 10)

    assert isinstance(failure.value, UnsafeKnowledgePathError)


def test_a_symlinked_topics_directory_stays_a_knowledge_error(tmp_path):
    store = KnowledgeStore(tmp_path)
    outside = tmp_path / "aussen"
    (outside / "fremd").mkdir(parents=True)
    (outside / "fremd" / "x.md").write_text("fremd", encoding="utf-8")
    (store.root / "contexts" / "c1").mkdir(parents=True)
    (store.root / "contexts" / "c1" / "topics").symlink_to(outside / "fremd")

    with pytest.raises(KnowledgeError):
        store.list_topic_files("contexts/c1")


def test_list_topic_files_does_not_follow_a_directory_swapped_after_the_check(
    tmp_path, monkeypatch
):
    """The check was by path and the scan was by path, so a swap in between won.

    ``_resolve_for_read`` proved the directory was not a symlink and then handed the
    same path to ``scandir``. Replacing it in that window made the method enumerate --
    and name -- a directory outside the knowledge tree, which is exactly the policy it
    advertises ("symlinks are skipped, not followed"). The scan now goes through the
    descriptor the check opened.
    """
    root = tmp_path / "knowledge"
    (root / "c" / "topics").mkdir(parents=True)
    (root / "c" / "topics" / "echt.md").write_text("echt", encoding="utf-8")
    outside = tmp_path / "aussen"
    outside.mkdir()
    (outside / "fremd.md").write_text("fremd", encoding="utf-8")
    store = KnowledgeStore(root)

    real_open = os.open
    swapped = {"done": False}

    def swapping_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped["done"] and path == "topics" and kwargs.get("dir_fd") is not None:
            # The check has just proved this component is a directory and opened it;
            # now replace the name behind the descriptor we hold.
            swapped["done"] = True
            os.rename(root / "c" / "topics", root / "c" / "tausche")
            (root / "c" / "topics").symlink_to(outside)
        return descriptor

    monkeypatch.setattr(os, "open", swapping_open)
    stems = store.list_topic_files("c")

    assert swapped["done"], "der Test hat das Verzeichnis nicht getauscht"
    assert stems == ("echt",), "die Aufzaehlung folgte dem ausgetauschten Verzeichnis"


def test_list_topic_files_closes_its_descriptor(tmp_path):
    """A long-lived process reading many packets must not run out of descriptors.

    ``os.scandir(fd)`` documents that it does not close what it was handed, and this
    method opened that descriptor itself, so every exit path has to.
    """
    script = f"""
import os, resource, sys
sys.path.insert(0, {str((REPOSITORY_ROOT / "tests"))!r})
from pathlib import Path
from organiser.services.knowledge import KnowledgeStore

resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
root = Path(sys.argv[1])
(root / "c" / "topics").mkdir(parents=True, exist_ok=True)
(root / "c" / "topics" / "a.md").write_text("a", encoding="utf-8")
store = KnowledgeStore(root)
for _ in range(400):
    assert store.list_topic_files("c") == ("a",)
open_fds = len(os.listdir("/proc/self/fd"))
assert open_fds < 32, f"{{open_fds}} offene Deskriptoren nach 400 Aufrufen"
print(open_fds)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "knowledge")],
        capture_output=True,
        text=True,
        cwd=str(REPOSITORY_ROOT),
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert int(completed.stdout.strip()) < 32
