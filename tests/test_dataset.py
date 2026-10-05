"""Tests for DatasetLogger — mock data only, no real wiki needed."""

import json
from pathlib import Path
from unittest.mock import patch

from stack.dataset import DatasetLogger


def _make_logger(tmp_path: Path) -> DatasetLogger:
    """Build a logger pointed at tmp_path/eval/dataset.jsonl with a mock wiki repo."""
    wiki = tmp_path / "wiki_repo"
    wiki.mkdir()
    log_path = tmp_path / "eval" / "dataset.jsonl"
    return DatasetLogger(log_path=log_path, wiki_repo=wiki)


def _read_entries(log_path: Path):
    """Read the JSONL file and parse each line."""
    text = log_path.read_text(encoding="utf-8")
    lines = [l for l in text.strip().split("\n") if l]
    return [json.loads(l) for l in lines], lines


def _walk_from(logger: DatasetLogger, cwd: Path):
    """Replicate _project_sha's walk-up logic from a given cwd (no agent import).

    Returns the dict _project_sha would return, or None.
    """
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".git").exists() and parent.resolve() != logger._wiki_repo.resolve():
            sha = logger._git_sha(parent)
            return {"project_repo": str(parent), "project_sha": sha}
    return None


# -- log_retriever -------------------------------------------------------------

def test_log_retriever_writes_valid_jsonl_line(tmp_path: Path):
    logger = _make_logger(tmp_path)
    with patch.object(DatasetLogger, "_git_sha", return_value="abc1234"):
        logger.log_retriever(
            session_id="sess-1",
            turn=0,
            user_message="how do I configure the application?",
            retriever_hits=["wiki/foo.md", "wiki/bar.md"],
            injected_pages=["wiki/foo.md", "wiki/bar.md"],
        )
    entries, lines = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    assert len(entries) == 1
    assert len(lines) == 1  # one JSONL line
    e = entries[0]
    assert e["session_id"] == "sess-1"
    assert e["wiki_sha"] == "abc1234"
    assert e["type"] == "retriever"
    assert e["turn"] == 0
    assert e["retriever_hits"] == ["wiki/foo.md", "wiki/bar.md"]
    assert e["injected_pages"] == ["wiki/foo.md", "wiki/bar.md"]


def test_log_retriever_injected_pages_default_empty(tmp_path: Path):
    logger = _make_logger(tmp_path)
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"):
        logger.log_retriever("s1", 0, "msg", retriever_hits=["wiki/a.md"])
    entries, _ = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    assert entries[0]["injected_pages"] == []  # nothing injected unless passed


def test_log_retriever_truncates_long_message(tmp_path: Path):
    logger = _make_logger(tmp_path)
    long_msg = "x" * 1000
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"):
        logger.log_retriever("s1", 1, long_msg)
    entries, _ = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    # The DATASET stores a 500-char excerpt (a log-size bound, not model context).
    assert len(entries[0]["user_message"]) == 500


def test_log_retriever_uses_injected_pages_when_provided(tmp_path: Path):
    logger = _make_logger(tmp_path)
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"):
        logger.log_retriever(
            "s1", 2, "msg",
            retriever_hits=["wiki/a.md"],
            injected_pages=["wiki/b.md", "wiki/c.md"],
        )
    entries, _ = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    assert entries[0]["injected_pages"] == ["wiki/b.md", "wiki/c.md"]


def test_log_retriever_includes_trace(tmp_path: Path):
    """log_retriever with trace parameter includes it in the JSONL entry."""
    logger = _make_logger(tmp_path)
    trace = [{"type": "llm_call", "prompt": "...", "response": "..."}]
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"):
        logger.log_retriever(
            session_id="sess-trace",
            turn=1,
            user_message="test query",
            retriever_hits=["wiki/foo.md"],
            trace=trace,
        )
    entries, _ = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    assert len(entries) == 1
    assert entries[0]["trace"] == trace


def test_log_retriever_trace_defaults_to_none(tmp_path: Path):
    """log_retriever without trace parameter defaults trace to None in the entry."""
    logger = _make_logger(tmp_path)
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"):
        logger.log_retriever(session_id="s1", turn=0, user_message="msg", retriever_hits=["wiki/a.md"])
    entries, _ = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    assert entries[0]["trace"] is None


# -- log_saver -----------------------------------------------------------------

def test_log_saver_writes_valid_jsonl_line(tmp_path: Path):
    logger = _make_logger(tmp_path)
    with patch.object(DatasetLogger, "_git_sha", return_value="def5678"):
        logger.log_saver(
            session_id="sess-2",
            trigger="cadence_4",
            turns_covered=[1, 2, 3],
            extracted=[{"kind": "decision", "name": "foo", "page_written": "wiki/foo.md"}],
            reasoning="extracted one decision",
        )
    entries, lines = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    assert len(entries) == 1
    assert len(lines) == 1
    e = entries[0]
    assert e["session_id"] == "sess-2"
    assert e["wiki_sha"] == "def5678"
    assert e["type"] == "save"
    assert e["trigger"] == "cadence_4"
    assert e["turns_covered"] == [1, 2, 3]
    assert e["extracted"] == [{"kind": "decision", "name": "foo", "page_written": "wiki/foo.md"}]


def test_log_saver_includes_trace(tmp_path: Path):
    """log_saver with trace parameter includes it in the JSONL entry."""
    logger = _make_logger(tmp_path)
    trace = [
        {"type": "reasoning", "text": "Searching wiki for overlap."},
        {"type": "tool_call", "tool": "terminal", "args": {"command": "stack.py create ..."}},
    ]
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"):
        logger.log_saver(
            session_id="sess-save-trace",
            trigger="cadence_4",
            turns_covered=[1, 2, 3],
            extracted=[{"kind": "decision", "name": "foo", "page_written": "wiki/foo.md"}],
            trace=trace,
        )
    entries, _ = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    assert len(entries) == 1
    assert entries[0]["trace"] == trace


def test_log_saver_trace_defaults_to_none(tmp_path: Path):
    """log_saver without trace parameter defaults trace to None in the entry."""
    logger = _make_logger(tmp_path)
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"):
        logger.log_saver(
            session_id="s1",
            trigger="cadence_4",
            turns_covered=[1],
            extracted=[],
        )
    entries, _ = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    assert entries[0]["trace"] is None


# -- project_sha ---------------------------------------------------------------

def test_project_sha_absent_when_no_git_repo(tmp_path: Path):
    """When cwd is a non-git dir, project_sha should NOT be in the entry."""
    logger = _make_logger(tmp_path)
    non_git_dir = tmp_path / "non_git"
    non_git_dir.mkdir()
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"), \
         patch.object(DatasetLogger, "_project_sha",
                      side_effect=lambda: _walk_from(logger, non_git_dir)):
        logger.log_retriever("s1", 0, "msg")
    entries, _ = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    e = entries[0]
    assert "project_sha" not in e
    assert "project_repo" not in e


def test_project_sha_present_when_git_repo_found(tmp_path: Path):
    """When cwd resolves to a git repo distinct from wiki, project_sha is captured."""
    logger = _make_logger(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    (project_dir / ".git").mkdir()  # fake .git dir
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"), \
         patch.object(DatasetLogger, "_project_sha",
                      side_effect=lambda: _walk_from(logger, project_dir)):
        logger.log_retriever("s1", 0, "msg")
    entries, _ = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    e = entries[0]
    assert e["project_sha"] == "sha"
    assert "project_repo" in e
    assert str(project_dir) in e["project_repo"]


# -- append + multi-entry -------------------------------------------------------

def test_multiple_entries_append_correctly(tmp_path: Path):
    logger = _make_logger(tmp_path)
    with patch.object(DatasetLogger, "_git_sha", return_value="sha"):
        logger.log_retriever("s1", 0, "first", retriever_hits=["a.md"])
        logger.log_saver("s1", "cadence_4", [0], [], "none")
        logger.log_retriever("s1", 1, "second", retriever_hits=["b.md"])
    entries, lines = _read_entries(tmp_path / "eval" / "dataset.jsonl")
    assert len(entries) == 3
    assert len(lines) == 3
    assert all(isinstance(e, dict) for e in entries)
    assert entries[0]["type"] == "retriever"
    assert entries[1]["type"] == "save"
    assert entries[2]["type"] == "retriever"
