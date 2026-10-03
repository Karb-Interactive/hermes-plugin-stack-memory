"""Append-only JSONL logger for the stack memory plugin.

Records what the retriever and saver decided each turn, as lean metadata +
references (NOT content): every entry carries session_id + wiki_sha (+ the
project repo sha when cwd is a project), so any decision can be reconstructed
later by checking out the wiki at that sha. Standalone so it can be tested on
its own; the plugin just calls log_retriever() / log_saver().
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class DatasetLogger:
    """Append-only JSONL logger for retriever + saver decisions (metadata, not content)."""

    def __init__(self, log_path: Path, wiki_repo: Path):
        self._log_path = log_path
        self._wiki_repo = wiki_repo

    def _git_sha(self, repo: Path) -> str:
        """Short HEAD sha of a git repo, or 'unknown'."""
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(repo), capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            return r.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    def _project_sha(self) -> Optional[Dict[str, str]]:
        """If cwd resolves to a git repo (that isn't the wiki repo), capture its sha."""
        try:
            from agent.runtime_cwd import resolve_agent_cwd
            cwd = Path(resolve_agent_cwd())
        except Exception:
            cwd = Path.cwd()
        for parent in [cwd] + list(cwd.parents):
            if (parent / ".git").exists() and parent.resolve() != self._wiki_repo.resolve():
                return {"project_repo": str(parent), "project_sha": self._git_sha(parent)}
        return None

    def log_retriever(
        self,
        session_id: str,
        turn: int,
        user_message: str,
        retriever_hits: Optional[List[str]] = None,   # pages the retriever chose
        injected_pages: Optional[List[str]] = None,   # what was actually injected this turn
        reasoning: str = "",
        trace: Optional[List[Dict]] = None,           # the retriever LLM call (prompt+response)
    ) -> None:
        """Log one retriever decision (per turn)."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "wiki_sha": self._git_sha(self._wiki_repo),
            "type": "retriever",
            "turn": turn,
            "user_message": user_message[:500],
            "retriever_hits": retriever_hits,
            "injected_pages": injected_pages or [],
            "reasoning": reasoning,
            "trace": trace,
        }
        proj = self._project_sha()
        if proj:
            entry.update(proj)
        self._append(entry)

    def log_saver(
        self,
        session_id: str,
        trigger: str,                      # "cadence_<n>" or "on_pre_compress"
        turns_covered: List[int],
        extracted: List[Dict[str, str]],   # [{kind, name, page_written}]
        reasoning: str = "",
        trace: Optional[List[Dict]] = None,  # the saver agent's full tool-call chain
        status: str = "saved",               # "saved" | "nothing_to_save" | "error"
        error: str = "",                     # error message if status="error"
        input_turns: Optional[List[Dict[str, str]]] = None,  # raw turns fed to the saver
        rolling_summary: str = "",           # rolling summary fed to the saver
    ) -> None:
        """Log one saver run (its writes + the steps it took)."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "wiki_sha": self._git_sha(self._wiki_repo),
            "type": "save",
            "trigger": trigger,
            "turns_covered": turns_covered,
            "extracted": extracted,
            "reasoning": reasoning,
            "trace": trace,
            "status": status,
        }
        if input_turns is not None:
            entry["input_turns"] = input_turns
        if rolling_summary:
            entry["rolling_summary"] = rolling_summary
        if error:
            entry["error"] = error
        proj = self._project_sha()
        if proj:
            entry.update(proj)
        self._append(entry)

    def _append(self, entry: Dict[str, Any]) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
