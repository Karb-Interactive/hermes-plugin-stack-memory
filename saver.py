"""Bounded, write-capable AIAgent that extracts durable knowledge from transcript turns.

Fires in sync_turn every N turns (cadence) and on_pre_compress.
Gets raw delta (turns since last save) + rolling summary (context, never written to memory).
Writes ONLY from raw delta — summary is context, never an extraction source.
Writes autonomously — no approval gate. "Nothing to save" is a valid outcome.

Two turns per run, one agent session:
  1. extract & save (SAVER_USER)
  2. prune now.md (SAVER_PRUNE_USER) — self-healing pass with the save context
     still loaded: distill done/stale entries to their pages, drop obsolete ones.
     "Nothing to prune" is a valid outcome; a prune failure never discards saves.

Standalone module: the plugin wires it in, but all agent-spawning logic lives
here so it can be tested and evolved on its own.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import format_turns
from .prompts import SAVER_PRUNE_USER, SAVER_SYSTEM, SAVER_USER

logger = logging.getLogger(__name__)


class Saver:
    """Bounded, write-capable AIAgent that extracts durable knowledge from transcript turns.

    Fires in sync_turn every N turns (cadence) and on_pre_compress.
    Gets raw delta (turns since last save) + rolling summary (context, never written to memory).
    Writes ONLY from raw delta — summary is context, never an extraction source.
    Writes autonomously — no approval gate. "Nothing to save" is a valid outcome.
    """

    DEFAULT_CADENCE = 4  # turns between saver runs
    DEFAULT_MAX_ITERATIONS = 10

    def __init__(
        self,
        wiki_path: Path,
        store_path: Path,
        engine_path: Path,
        schema_path: Path,
        catalog: str,
        provider: str = None,
        model: str = None,
        api_key: str = None,
        base_url: str = None,
        api_mode: str = None,
        max_iterations: int = None,
    ):
        self._wiki_path = wiki_path
        self._store_path = store_path
        self._engine_path = engine_path
        self._schema_path = schema_path
        self._catalog = catalog
        self._provider = provider
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._api_mode = api_mode
        self._max_iterations = max_iterations or self.DEFAULT_MAX_ITERATIONS

    def extract_and_save(
        self,
        turns: List[Dict[str, str]],
        rolling_summary: str = "",
    ) -> tuple:
        """Extract durable knowledge from raw turns and write to wiki.

        Args:
            turns: List of {role, content} dicts — the raw transcript delta
            rolling_summary: Operational context ("what is this session about").
                            Shapes judgment, NEVER written to wiki.

        Returns: (saved, trace, reasoning) — saved is a list of {kind, name,
                 page_written} for each page saved (both turns: save + prune),
                 trace is the full agent chain, reasoning the agent's final
                 text (save turn, plus "[prune] …" if the prune turn ran).
                 ([], [], "") = "nothing to save" (valid outcome).
        """
        try:
            from run_agent import AIAgent
            from hermes_cli.plugins import (
                set_thread_tool_whitelist,
                clear_thread_tool_whitelist,
            )
        except ImportError:
            logger.debug("AIAgent not available (running outside Hermes)")
            return [], [], ""

        system_prompt = self._build_prompt()
        whitelist = {"search_files", "read_file", "patch", "terminal"}

        turns_text = format_turns(turns)
        user_message = SAVER_USER.format(
            rolling_summary=rolling_summary,
            turns_text=turns_text,
            stack_repo=self._store_path,
            engine=self._engine_path,
            schema=self._schema_path,
        )

        try:
            agent = AIAgent(
                model=self._model,  # Pass None to inherit default model, don't send ""
                max_iterations=self._max_iterations,
                quiet_mode=True,
                provider=self._provider,
                api_key=self._api_key or None,
                base_url=self._base_url or None,
                api_mode=self._api_mode,
                enabled_toolsets=["file", "terminal"],  # Need terminal for the engine's create
                skip_memory=True,
                ephemeral_system_prompt=system_prompt,
            )
            agent.suppress_status_output = True
            agent._skip_mcp_refresh = True
            agent._end_session_on_close = False
            agent.compression_enabled = False

            # Remove write_file from the tool schema entirely so the model
            # never sees it (the whitelist would still let it attempt the call
            # and waste iterations on denials).
            agent.tools = [t for t in agent.tools if t["function"]["name"] != "write_file"]
            agent.valid_tool_names.discard("write_file")

            set_thread_tool_whitelist(
                whitelist,
                deny_msg_fmt="Saver denied non-whitelisted tool: {tool_name}. "
                             "Only search_files, read_file, patch, and terminal are allowed.",
            )
            try:
                result = agent.run_conversation(user_message=user_message)
                save_reasoning = self._get_final_response(agent)

                # Second turn, SAME session (history passed back): prune now.md
                # while the saver still has the catalog, the delta, and its own
                # writes in context. Self-healing pass — failure here must not
                # discard the save results.
                prune_reasoning = ""
                try:
                    agent.run_conversation(
                        user_message=SAVER_PRUNE_USER.format(wiki_path=self._wiki_path),
                        conversation_history=(result or {}).get("messages") or None,
                    )
                    prune_reasoning = self._get_final_response(agent)
                except Exception as e:
                    logger.warning("Saver prune pass failed: %s", e)
            finally:
                clear_thread_tool_whitelist()

            # Extract what was saved from the agent's tool calls (both turns)
            saved = self._extract_saves(agent)
            trace = self._extract_trace(agent)
            reasoning = save_reasoning
            if prune_reasoning and prune_reasoning != save_reasoning:
                reasoning = f"{save_reasoning}\n\n[prune] {prune_reasoning}"

            try:
                agent.close()
            except Exception:
                pass

            return saved, trace, reasoning
        except Exception as e:
            logger.warning("Saver agent failed: %s", e)
            return [], [], ""

    def _build_prompt(self) -> str:
        return SAVER_SYSTEM.format(
            wiki_path=self._wiki_path,
            stack_repo=self._store_path,
            engine=self._engine_path,
            schema=self._schema_path,
            catalog=self._catalog,
        )

    def _extract_saves(self, agent) -> List[Dict[str, str]]:
        """Extract saved pages from the agent's tool calls.

        Scans for terminal calls to the engine's create (the new write path)
        and for patch calls (edits to existing pages).
        Deduplicates by page_written path — prefers 'decision' kind over 'edit'.
        """
        saved = []
        seen_paths = {}  # normalized_path -> index in saved list
        messages = getattr(agent, "_session_messages", [])
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls", []) or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {}) or {}
                tool_name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    continue

                if tool_name == "terminal":
                    # Look for the engine's create call in the command. The engine
                    # ships with the plugin, so match on "engine.py" — matching the
                    # retired "stack.py" name silently missed every real save.
                    cmd = args.get("command", "")
                    if "engine.py" in cmd and "create" in cmd:
                        # Parse --name, --kind, --dir from the command
                        import re
                        name_m = re.search(r"--name\s+(\S+)", cmd)
                        kind_m = re.search(r"--kind\s+(\S+)", cmd)
                        dir_m = re.search(r"--dir\s+(\S+)", cmd)
                        name = name_m.group(1) if name_m else ""
                        kind = kind_m.group(1) if kind_m else "reference"
                        if dir_m:
                            page = f"wiki/{dir_m.group(1)}/{name}.md"
                        else:
                            page = f"wiki/{name}.md" if name else ""
                        if name:
                            entry = {"kind": kind, "name": name, "page_written": page}
                            # Normalize path for dedup
                            norm_path = str(Path(page).resolve()) if page else ""
                            if norm_path and norm_path not in seen_paths:
                                seen_paths[norm_path] = len(saved)
                                saved.append(entry)
                            elif norm_path in seen_paths:
                                # Prefer 'decision' kind over 'edit' or repeated 'terminal'
                                idx = seen_paths[norm_path]
                                if kind == "decision" and saved[idx].get("kind") != "decision":
                                    saved[idx] = entry
                elif tool_name == "patch":
                    # Edit to existing page — extract path from the patch
                    path = args.get("path", "")
                    if path:
                        entry = {"kind": "edit", "name": "", "page_written": path}
                        norm_path = str(Path(path).resolve())
                        if norm_path not in seen_paths:
                            seen_paths[norm_path] = len(saved)
                            saved.append(entry)
                        # Don't overwrite terminal creates with patch edits
        return saved

    def _get_final_response(self, agent) -> str:
        """Extract the final assistant text from the agent's session messages."""
        messages = getattr(agent, "_session_messages", [])
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    texts = [b.get("text", "") for b in content if isinstance(b, dict)]
                    joined = " ".join(texts).strip()
                    if joined:
                        return joined
        return ""

    def _extract_trace(self, agent) -> List[Dict[str, Any]]:
        """Extract the full tool-call chain from the agent's session for debugging.

        Returns a compact list of: {tool, args, result_preview} for each tool call.
        Also includes assistant text messages (reasoning) between tool calls.
        """
        trace = []
        messages = getattr(agent, "_session_messages", [])
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")

            if role == "assistant":
                # Capture reasoning text
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    trace.append({"type": "reasoning", "text": content[:500]})
                elif isinstance(content, list):
                    texts = [b.get("text", "") for b in content if isinstance(b, dict)]
                    joined = " ".join(texts).strip()
                    if joined:
                        trace.append({"type": "reasoning", "text": joined[:500]})

                # Capture tool calls
                for tc in msg.get("tool_calls", []) or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    trace.append({
                        "type": "tool_call",
                        "tool": name,
                        "args": {k: (str(v)[:200] if not isinstance(v, (list, dict)) else v) for k, v in args.items()},
                    })

            elif role == "tool":
                # Capture tool result (truncated)
                content = msg.get("content", "")
                if isinstance(content, str):
                    trace.append({"type": "tool_result", "text": content[:500]})

        return trace