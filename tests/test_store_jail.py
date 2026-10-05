"""Store-jail tests: a destructive verb naming a path outside the store is blocked;
reads pass; unarmed threads pass through."""

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from stack import store_jail  # noqa: E402

STORE = Path("/home/user/memory-store")


@pytest.fixture(autouse=True)
def _disarm():
    store_jail.disarm()
    yield
    store_jail.disarm()


def _block(cmd):
    store_jail.arm([STORE])
    return store_jail.check_terminal("terminal", {"command": cmd})


# -- destructive commands naming an outside path: BLOCK ------------------------

@pytest.mark.parametrize("cmd", [
    "rm -rf /important/data",
    "rm /etc/hosts",
    "mv /home/user/notes/report.pdf /home/user/trash/",
    "cp /etc/passwd /home/user/memory-store/wiki/leak.md",
    "chmod 777 /etc/hosts",
    "echo pwned > /home/user/.bashrc",
    "sed -i '' 's/x/y/' /home/user/.bashrc",
    "cd /home/user && rm /home/user/victim.txt",
])
def test_blocks_outside_destruction(cmd):
    assert _block(cmd) is not None


# -- the saver's real shapes: ALLOW --------------------------------------------

@pytest.mark.parametrize("cmd", [
    "rm /home/user/memory-store/wiki/old.md",
    "mkdir -p /home/user/memory-store/wiki/new",
    "git -C /home/user/memory-store/wiki/shared push",
    "cd /home/user/memory-store && git add -A && git commit -m x && git push",
    "grep -rn foo /home/user/projects/some-repo",
    "sed -n '380,400p' /home/user/projects/some-repo/src/x.cpp",   # sed without -i reads
    "find /home/user/projects -name publish.py",
    "curl -sL https://example.com/page.html | shasum -a 256",      # URL, not a path
    "uv run --no-project /opt/plugin/engine.py --repo /home/user/memory-store create --name x",
    "echo hi > /home/user/memory-store/wiki/note.md",
])
def test_allows_reads_and_in_store_writes(cmd):
    assert _block(cmd) is None


# -- the hook: armed thread confines, unarmed thread passes --------------------

def test_unarmed_thread_passes():
    assert store_jail.check_terminal("terminal", {"command": "rm -rf /important/data"}) is None


def test_ignores_non_terminal_tools():
    store_jail.arm([STORE])
    assert store_jail.check_terminal("read_file", {"path": "/etc/hosts"}) is None


def test_arm_disarm_roundtrip():
    assert _block("rm /etc/hosts") is not None
    store_jail.disarm()
    assert store_jail.check_terminal("terminal", {"command": "rm /etc/hosts"}) is None
