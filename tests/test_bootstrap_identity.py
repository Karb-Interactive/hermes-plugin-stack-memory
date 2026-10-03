"""The store bootstrap must report a store whose commit failed as a warning.

`init` creates the store, then its `git commit` fails and it still exits 0 when
the machine has no git identity — reporting that honestly as
``{"git": "not initialized", "git_detail": ...}``. Reading only
``"initialized": true`` announced that as a clean creation: a store that exists
with no history, and history is its undo path.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from stack import StackMemoryProvider  # noqa: E402


def _init_payload(git):
    payload = {"initialized": True, "repo": "x", "format_version": 1, "git": git}
    if git != "initialized":
        payload["git_detail"] = "no git identity configured — set user.name/user.email"
    return payload


def _provider(repo, out, notify=None):
    prov = StackMemoryProvider.__new__(StackMemoryProvider)
    prov._repo = Path(repo)
    prov._notify = notify
    prov._run_stack = lambda sub, timeout: out
    return prov


def _git(repo, *args, identity=False):
    cmd = ["git", "-C", str(repo)]
    if identity:
        cmd += ["-c", "user.name=t", "-c", "user.email=t@t"]
    return subprocess.run(cmd + list(args), capture_output=True, text=True)


def _git_init(repo):
    repo.mkdir(parents=True, exist_ok=True)
    if _git(repo, "init", "-b", "main").returncode != 0:
        _git(repo, "init")


def _commit(repo, name="f.txt"):
    """A real commit needs staged content — an empty tree has nothing to commit."""
    (repo / name).write_text("x")
    _git(repo, "add", "-A", identity=True)
    _git(repo, "commit", "-m", "init", identity=True)


# -- the bootstrap outcome must match what the engine actually reported --------


def test_clean_creation_is_announced(tmp_path):
    msgs = []
    prov = _provider(tmp_path / "store", json.dumps(_init_payload("initialized")), msgs.append)
    prov._ensure_store()
    assert any("created a new stack store" in m for m in msgs)
    assert not any("no commit" in m for m in msgs)


def test_failed_commit_is_a_warning_not_a_creation(tmp_path):
    """The regression: this case used to print the success message."""
    msgs = []
    payload = _init_payload("not initialized")
    prov = _provider(tmp_path / "store", json.dumps(payload), msgs.append)
    prov._ensure_store()
    assert any("no commit" in m for m in msgs)
    assert not any("created a new stack store" in m for m in msgs)
    # the engine's own reason survives into the warning
    assert any("user.name/user.email" in m for m in msgs)


def test_unparseable_engine_output_is_not_a_success(tmp_path):
    msgs = []
    prov = _provider(tmp_path / "store", "Traceback (most recent call last):\nboom", msgs.append)
    prov._ensure_store()
    assert msgs == []


@pytest.mark.parametrize("out,expected", [
    ('{"initialized": true}', {"initialized": True}),
    ("[1, 2]", {}),
    ("null", {}),
    ("", {}),
    ("not json", {}),
])
def test_parse_payload_never_claims_success_on_junk(out, expected):
    assert StackMemoryProvider._parse_payload(out) == expected


# -- a store that already exists is still checked for its commit ---------------


def test_existing_store_without_commit_warns(tmp_path):
    repo = tmp_path / "store"
    (repo / ".stack").mkdir(parents=True)
    (repo / ".stack" / "manifest.json").write_text('{"format_version": 1}')
    _git_init(repo)  # initialized repository, no commit
    msgs = []
    prov = _provider(repo, json.dumps(_init_payload("initialized")), msgs.append)
    prov._ensure_store()
    assert any("no commit" in m for m in msgs)


def test_existing_committed_store_is_left_alone_silently(tmp_path):
    repo = tmp_path / "store"
    (repo / ".stack").mkdir(parents=True)
    (repo / ".stack" / "manifest.json").write_text('{"format_version": 1}')
    _git_init(repo)
    _commit(repo)
    msgs = []
    prov = _provider(repo, json.dumps(_init_payload("initialized")), msgs.append)
    prov._ensure_store()
    assert msgs == []


# -- refs layout is the check itself -------------------------------------------


def test_repo_with_no_commit_is_detected(tmp_path):
    repo = tmp_path / "store"
    _git_init(repo)
    prov = _provider(repo, "{}")
    assert prov._repo_has_no_commits() is True


def test_repo_with_a_commit_is_not_flagged(tmp_path):
    repo = tmp_path / "store"
    _git_init(repo)
    _commit(repo)
    prov = _provider(repo, "{}")
    assert prov._repo_has_no_commits() is False


def test_packed_refs_counts_as_a_commit(tmp_path):
    """A packed repo still has history — never flag it."""
    repo = tmp_path / "store"
    _git_init(repo)
    _commit(repo)
    _git(repo, "pack-refs", "--all")
    prov = _provider(repo, "{}")
    assert prov._repo_has_no_commits() is False


def test_non_repository_is_not_claimed_as_uncommitted(tmp_path):
    repo = tmp_path / "store"
    repo.mkdir()
    prov = _provider(repo, "{}")
    assert prov._repo_has_no_commits() is False
