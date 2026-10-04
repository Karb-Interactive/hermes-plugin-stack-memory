"""Stack memory provider for Hermes — v1 (read + deliberate write).

A *decoupled* adapter. It POINTS TO an external stack memory/wiki repo (the dir
containing ``bin/stack.py``) and shells to that CLI; it does NOT live inside the
stack repo, so this directory is its own (publishable) git repo.

Where the stack repo is found (first hit wins):
  1. env ``STACK_REPO``
  2. ``repo_path`` in ``$HERMES_HOME/stack.json`` (written by ``hermes memory setup``)
  3. a legacy default (no-hermes_home callers only)

Scope model: **home + projects.**
  * A page under ``wiki/<org>/<project>/`` is *project* scope (CC-compatible).
  * A top-level ``wiki/*.md`` page is *home* scope — the default, personal, cross-cutting
    base (now.md, preferences, tooling notes already live here).
  * The full ``stack load`` output is injected verbatim into the system prompt (same as
    Claude Code's SessionStart hook) — read-only context, no trimming.
  * Writes: the MAIN agent does not write stack — it reads the injected block and uses
    Hermes' native memory for its own persona. Stack curation is the background saver's
    job (it shells ``stack.py create`` + file tools). The plugin itself does NO file writing.

Design constraints honored here:
  * Decoupled: depends only on the documented ``MemoryProvider`` contract + the ``stack.py``
    CLI. No stack-side code change. ``resolve_agent_cwd`` imported defensively.
  * Coexists with Hermes' builtin memory as a SEPARATE lane: keep builtin ON — it
    auto-curates native persona via the background_review fork; this provider NEVER writes
    there, and that fork (skip_memory=True) never writes stack. Two lanes, no contention.
  * Background curation: ``sync_turn`` buffers turns and fires the saver agent on cadence
    (every N turns), ``on_session_end``, and ``on_pre_compress`` before context is
    discarded. The saver is a
    bounded AIAgent that uses terminal to call ``stack.py create`` for new pages and
    file tools for edits. ``on_memory_write`` is a NO-OP.
  * Affordable on Ollama (no prompt caching): the projection is built ONCE per session;
    per-turn recall uses an in-memory index, no subprocess.
  * Verifiable: set ``STACK_PROVIDER_DEBUG=1`` to log every seam to ``.probe.log``.

The literal ``MemoryProvider`` / ``register_memory_provider`` strings are required by
Hermes' user-plugin discovery scan (plugins/memory/__init__.py::_is_memory_provider_dir).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

# The engine ships with the plugin — the one runtime source. Memory repos hold data only.
_ENGINE = Path(__file__).resolve().parent / "engine.py"
# The write contract travels with the engine, not with the store.
_SCHEMA = Path(__file__).resolve().parent / "SCHEMA.md"

logger = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.memory_provider import MemoryProvider  # absolute import into the host

try:  # decoupled: don't hard-fail if the host internal moves
    from agent.runtime_cwd import resolve_agent_cwd as _resolve_agent_cwd
except Exception:  # pragma: no cover
    _resolve_agent_cwd = None

_DEFAULT_REPO = "~/stack-memory"
# Probe target before initialize() knows hermes_home. Never the plugin checkout:
# debug output must not dirty the installed source or mix across profiles.
_PROBE_LOG: Optional[Path] = None
_DEBUG = os.environ.get("STACK_PROVIDER_DEBUG") == "1"


def _set_probe_log(hermes_home) -> None:
    """Point debug output at the profile's log directory."""
    global _PROBE_LOG
    if not _DEBUG:
        return
    base = Path(hermes_home) if hermes_home else Path(os.path.expanduser("~/.hermes"))
    try:
        (base / "logs").mkdir(parents=True, exist_ok=True)
        _PROBE_LOG = base / "logs" / "stack-provider.log"
    except Exception:
        _PROBE_LOG = None


def _probe(event: str) -> None:
    if not _DEBUG or _PROBE_LOG is None:
        return
    try:
        with open(_PROBE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {event}\n")
    except Exception:
        pass


def _agent_cwd() -> str:
    if _resolve_agent_cwd is not None:
        try:
            return str(_resolve_agent_cwd())
        except Exception:
            pass
    return os.getcwd()


def _strip_frontmatter(text: str) -> str:
    """Return the body after a leading ``---`` frontmatter block (if any)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            return text[nl + 1:] if nl != -1 else ""
    return text


def _parse_frontmatter_min(text: str) -> Dict[str, str]:
    """Minimal name/description extraction — no YAML dependency."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm = text[3:end]
    out: Dict[str, str] = {}
    for key in ("name", "description"):
        m = re.search(rf"^{key}:\s*(.+)$", fm, re.MULTILINE)
        if m:
            out[key] = m.group(1).strip().strip('"').strip("'")
    return out


class StackMemoryProvider(MemoryProvider):
    """Adapter exposing the stack wiki to Hermes (read + deliberate write)."""

    def __init__(self) -> None:
        self._repo = self._resolve_repo(None)
        self._hermes_home: Optional[str] = None
        self._agent_context = "primary"
        self._session_id = ""
        self._prompt_block = ""
        # in-memory recall index: (name, description, relpath, abspath, body)
        self._index: List[Tuple[str, str, str, Path, str]] = []
        self._dataset: Optional[Any] = None  # DatasetLogger (lazy-imported in initialize)
        self._retriever: Optional[Any] = None  # per-turn page selection (one LLM call)
        self._injected_history: List[Set[str]] = []  # pages injected in the last 3 turns (no re-injection)
        self._saver: Optional[Any] = None     # background curation agent (the only writer)
        self._summarizer: Optional[Any] = None  # rolling context for the saver
        self._turn_buffer: List[Dict[str, str]] = []
        self._turn_count = 0
        self._rolling_summary = ""
        _probe(f"__init__ repo={self._repo}")

    # -- config / point-to ---------------------------------------------------

    @property
    def name(self) -> str:
        return "stack"

    def _resolve_repo(self, hermes_home: Optional[str]) -> Path:
        """Where is this profile's store?

        With a real hermes_home the store is derived from the profile alone:
        an explicit ``stack.json.repo_path`` (how a store is shared between
        profiles) or an isolated profile-local default. A globally inherited
        ``STACK_REPO`` is deliberately ignored here — otherwise every fresh
        profile silently joins the default profile's store.

        With no hermes_home (unit tests, legacy scripts) ``STACK_REPO`` remains
        the escape hatch, then the profile-local default.
        """
        if hermes_home:
            cfg = Path(hermes_home) / "stack.json"
            try:
                if cfg.exists():
                    rp = json.loads(cfg.read_text(encoding="utf-8")).get("repo_path")
                    if rp:
                        return Path(os.path.expandvars(rp)).expanduser()
            except Exception:
                pass
            return Path(hermes_home) / "memories" / "stack"

        env = os.environ.get("STACK_REPO")
        if env:
            return Path(os.path.expandvars(env)).expanduser()
        return Path(os.path.expandvars(_DEFAULT_REPO)).expanduser()

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "repo_path",
                "description": (
                    "Path to this profile's stack memory store. Leave blank for an "
                    "isolated profile-local store ($HERMES_HOME/memories/stack), created "
                    "on first use. Set an absolute path to share one store between profiles."
                ),
                "secret": False,
                "required": False,
                "default": "",
            }
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        rp = values.get("repo_path")
        if not rp:
            return
        try:
            (Path(hermes_home) / "stack.json").write_text(
                json.dumps({"repo_path": rp}, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # -- core lifecycle ------------------------------------------------------

    def _ensure_store(self) -> None:
        """Create this profile's store if it does not exist yet.

        A fresh profile has a profile-local path but nothing at it. The plugin
        ships the engine, so it can bootstrap the store it is about to read —
        which is only possible because the runtime moved out of the store.

        Anything that is not an initialized store is handed to `init` rather than
        assumed usable: an existing directory with no manifest (empty or not) is
        created-or-refused by `init`, and a path that exists as a FILE is reported.
        A bare `.exists()` check here would accept an empty or unrelated directory
        and then read nothing from it.

        Never destructive: `init` refuses a non-empty unrelated target, and an
        initialized store is reported as already initialized.
        """
        if self._repo.is_file():
            logger.warning("stack store path is a file, not a directory: %s", self._repo)
            return
        if (self._repo / ".stack" / "manifest.json").exists():
            # An initialized store: leave it entirely alone — except to notice a
            # store whose repository never got its init commit. `init` creates the
            # store, then `git commit` fails and it still exits 0 when the machine
            # has no git identity; the store then looks created while having no
            # history, which is its undo path. Silence here is how that stays
            # undiscovered for every later session too.
            if self._repo_has_no_commits():
                self._warn_uncommitted_store("no commit ever landed in its repository")
            return  # an initialized store: leave it entirely alone
        out = self._run_stack(["init"], timeout=60)
        payload = self._parse_payload(out)
        if payload.get("initialized"):
            if payload.get("git") == "initialized":
                _probe(f"bootstrapped new store at {self._repo}")
                if self._notify:
                    self._notify(f"🗂 Memory: created a new stack store at {self._repo}")
            else:
                # `init` exits 0 with the store created but its commit failed, and
                # reports that honestly as {"git": "not initialized", "git_detail":
                # ...}. Reading only `"initialized": true` turned exactly that case
                # into a success message — the store exists, its history does not.
                self._warn_uncommitted_store(payload.get("git_detail") or "unknown reason")
        else:
            # Silence here means nothing happened AND nothing was reported —
            # exactly the case a user must not have to discover later.
            _probe(f"store bootstrap did not initialize: {out.strip()[:400]}")
            logger.warning("stack store bootstrap failed at %s: %s",
                           self._repo, (out.strip()[:400] or "no output"))

    @staticmethod
    def _parse_payload(out: str) -> Dict[str, Any]:
        """Parse an engine reply; anything unparseable is not a success signal."""
        try:
            payload = json.loads(out)
        except (ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _repo_has_no_commits(self) -> bool:
        """Do a plain repository's refs show that no commit ever landed?

        Reads git's own layout rather than spawning `git` on every session start.
        The first commit is what writes `.git/refs/heads/<branch>` or
        `.git/packed-refs`; both absent means `git init` ran and the commit did
        not. Unreadable layout reports False — never claim a failure we can't prove.
        """
        git_dir = self._repo / ".git"
        if not git_dir.is_dir():
            return False  # not a repository at all: a different report
        try:
            if (git_dir / "packed-refs").exists():
                return False
            heads = git_dir / "refs" / "heads"
            return not any(heads.iterdir()) if heads.is_dir() else True
        except OSError:
            return False

    def _warn_uncommitted_store(self, detail: str) -> None:
        """Report a store that exists without a commit, and say what to fix."""
        msg = (f"store at {self._repo} has no commit ({detail}). Its git history is "
               f"the undo path — set git user.name/user.email, then commit the store.")
        _probe(f"uncommitted store: {detail}")
        logger.warning("stack store has no commit at %s: %s", self._repo, detail)
        if self._notify:
            self._notify(f"⚠️ Memory: {msg}")

    def is_available(self) -> bool:
        """The plugin ships its own engine, so availability depends on the plugin
        and the local prerequisites — never on the target store existing.

        Requiring `<store>/bin/stack.py` here was the bootstrap circularity: the
        program that creates a store cannot be inside the store it is creating.
        """
        ok = _ENGINE.exists() and bool(shutil.which("uv"))
        _probe(f"is_available -> {ok} (engine={_ENGINE})")
        return ok

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or ""
        self._hermes_home = kwargs.get("hermes_home")
        self._agent_context = kwargs.get("agent_context", "primary")
        self._notify = kwargs.get("status_callback")  # agent._emit_status → TUI + gateway
        _set_probe_log(self._hermes_home)
        self._repo = self._resolve_repo(self._hermes_home)
        self._ensure_store()
        cwd = _agent_cwd()
        _probe(
            f"initialize session={session_id} platform={kwargs.get('platform')!r} "
            f"agent_context={self._agent_context!r} cwd={cwd}"
        )
        # One subprocess per session: stack.py load handles everything.
        raw = self._run_stack(["load", cwd], timeout=25)
        # Inject the full `stack load` output verbatim — same as Claude Code's
        # SessionStart hook. No trimming/compaction; a now.md cap + consolidation
        # will keep it bounded instead (separate concern). Read-only context: the
        # main agent does NOT write stack and gets no write guidance here — stack
        # curation is the background saver's job (SAVER_SYSTEM), and the main agent
        # uses Hermes' native memory for its own persona.
        self._prompt_block = raw.strip() if (raw and raw.strip()) else ""
        # Build the in-memory recall index once (cheap frontmatter scan).
        self._build_index()
        # Auxiliary components below are constructed eagerly and MUST succeed.
        # A failure here is a bug (bad packaging, broken submodule), not a
        # degraded mode — let it propagate. The host (memory_manager) logs it
        # and disables the provider, rather than running half-initialized and
        # silently dropping retrieval or curation.

        # Dataset logger — records retriever/saver decisions for inspection.
        # Runtime state belongs to the profile, never the plugin checkout:
        # writing it here would share one file across profiles sharing this
        # install, and dirty the installed source.
        from .dataset import DatasetLogger
        _state_dir = Path(self._hermes_home or os.path.expanduser("~/.hermes"))
        self._dataset = DatasetLogger(_state_dir / "memories" / ".stack-provider" / "dataset.jsonl",
                                      self._repo)
        # Retriever: per-turn page selection (one PluginLlm call).
        self._retriever = None
        self._injected_history = []
        if self._agent_context == "primary":
            from .retriever import Retriever
            provider, model, *_ = self._resolve_aux_runtime("retriever")
            catalog = self._build_catalog()
            self._retriever = Retriever(
                wiki_path=self._wiki_dir(),
                catalog=catalog,
                provider=provider,
                model=model,
            )
        # Saver: background curation agent (the only writer).
        from .saver import Saver
        self._saver = None
        self._turn_buffer = []
        self._turn_count = 0
        self._rolling_summary = ""
        self._saver_cadence = self._resolve_aux_positive_int(
            "saver", "cadence", default=Saver.DEFAULT_CADENCE
        )
        if self._agent_context == "primary":
            provider, model, api_key, base_url, api_mode = self._resolve_aux_runtime("saver")
            # Read max_iterations from config (default: 10, saver.py:DEFAULT_MAX_ITERATIONS)
            max_iterations = None
            try:
                from hermes_cli.config import load_config
                _cfg = load_config()
                _aux = _cfg.get("auxiliary", {}) if isinstance(_cfg.get("auxiliary"), dict) else {}
                _task = _aux.get("saver", {}) if isinstance(_aux.get("saver"), dict) else {}
                _mi = _task.get("max_iterations")
                if isinstance(_mi, (int, float)) and _mi > 0:
                    max_iterations = int(_mi)
            except Exception:
                pass
            catalog = self._build_catalog()
            self._saver = Saver(
                wiki_path=self._wiki_dir(),
                store_path=self._repo,
                engine_path=_ENGINE,
                schema_path=_SCHEMA,
                catalog=catalog,
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                api_mode=api_mode,
                max_iterations=max_iterations,
            )
        # Summarizer: rolling context for the saver (never written to the wiki).
        self._summarizer = None
        if self._agent_context == "primary":
            from .summarizer import Summarizer
            provider, model, *_ = self._resolve_aux_runtime("summarizer")
            self._summarizer = Summarizer(
                provider=provider, model=model,
            )
        # Shared-submodule freshness: gateway-free background pull at session start.
        self._pull_submodules_async()
        _probe(f"initialize block_chars={len(self._prompt_block)}")

    def system_prompt_block(self) -> str:
        # Static per session — return the cached compact projection.
        _probe(f"system_prompt_block CALLED (chars={len(self._prompt_block)})")
        return self._prompt_block

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Lean adapter: context-only, no tools. Required by the MemoryProvider
        # abstract contract even when empty — without it the class can't be
        # instantiated, so register() raises and initialize() is never called.
        _probe("get_tool_schemas CALLED")
        return []

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Per-turn recall: ask the retriever which pages are relevant, inject their
        full bodies. One LLM call (the retriever); the plugin does the file I/O."""
        from .retriever import _MAX_PAGES
        _probe(f"prefetch query={query[:80]!r}")

        # Pages injected in the last 3 turns — don't re-inject them.
        recently_injected: Set[str] = set()
        for past in self._injected_history[-3:]:
            recently_injected |= past

        # Retriever: one LLM call → (relevant page paths, trace).
        retriever_paths: List[str] = []
        retriever_trace: List[Dict[str, Any]] = []
        if self._retriever:
            try:
                retriever_paths, retriever_trace = self._retriever.retrieve(query, recently_injected)
                _probe(f"retriever: returned {len(retriever_paths)} paths: {retriever_paths}")
            except Exception as e:
                _probe(f"retriever failed: {e}")
        else:
            _probe("retriever: disabled (non-primary context)")

        # Deterministic backstop: drop anything injected in the last 3 turns, cap the count.
        retriever_paths = [p for p in retriever_paths if p not in recently_injected][:_MAX_PAGES]

        # Map paths → index entries (name, desc, rel, abspath, body).
        merged = []
        for wpath in retriever_paths:
            for entry in self._index:
                if entry[2] == wpath:
                    merged.append(entry)
                    break

        # Remember what we injected this turn (keep last 3 turns).
        self._injected_history.append(set(e[2] for e in merged))
        self._injected_history = self._injected_history[-3:]

        if self._dataset:
            try:
                self._dataset.log_retriever(
                    session_id or self._session_id, turn=0,
                    user_message=query,
                    retriever_hits=retriever_paths or None,
                    injected_pages=[e[2] for e in merged],
                    trace=retriever_trace,
                )
            except Exception as e:
                _probe(f"dataset log_retriever failed: {e}")

        if not merged:
            return ""

        # Inject the FULL page body for each selected page, read fresh from disk
        # (no truncation — relevance is controlled by page COUNT, not by clipping).
        lines = ["## Relevant pages from memory:"]
        for name, desc, rel, abspath, _ in merged:
            lines.append(f"  · {rel}")
            try:
                body = _strip_frontmatter(
                    Path(abspath).read_text(encoding="utf-8", errors="ignore")
                ).strip()
            except Exception:
                body = ""
            if body:
                lines.append(body)
        out = "\n".join(lines)

        if self._notify:
            names = ", ".join(e[2].split("/")[-1].replace(".md", "") for e in merged[:5])
            self._notify(f"📥 Memory: injected {len(merged)} page(s): {names}")

        return out

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages=None) -> None:
        """Buffer turns and fire saver on cadence."""
        if not self._saver or self._agent_context != "primary":
            return
        self._turn_count += 1
        self._turn_buffer.append({"role": "user", "content": user_content})
        self._turn_buffer.append({"role": "assistant", "content": assistant_content})
        # Cadence check
        if self._turn_count % self._saver_cadence == 0:
            self._run_saver(session_id or self._session_id, trigger=f"cadence_{self._saver_cadence}")

    def _run_saver(self, session_id: str, trigger: str) -> None:
        """Spawn the saver agent with accumulated turns."""
        if not self._saver or not self._turn_buffer:
            return
        turns = list(self._turn_buffer)
        try:
            result = self._saver.extract_and_save(turns, self._rolling_summary)
            if isinstance(result, tuple):
                saved, saver_trace, reasoning = result
            else:
                saved, saver_trace, reasoning = result, [], ""
            
            # Log ALL runs to dataset (including failures/empty) so problems are visible
            if self._dataset:
                turn_nums = list(range(self._turn_count - len(turns) // 2 + 1, self._turn_count + 1))
                status = "saved" if saved else "nothing_to_save"
                self._dataset.log_saver(
                    session_id=session_id,
                    trigger=trigger,
                    turns_covered=turn_nums,
                    extracted=saved,
                    trace=saver_trace,
                    reasoning=reasoning,
                    status=status,
                    input_turns=turns,
                    rolling_summary=self._rolling_summary,
                )

            # Notify the user what was saved (same channel as background_review)
            if self._notify and saved:
                names = []
                for s in saved[:5]:
                    page = s.get("page_written") or s.get("name") or "?"
                    names.append(page.split("/")[-1].replace(".md", ""))
                self._notify(f"📝 Memory: saved {len(saved)} page(s): {', '.join(names)}")
        except Exception as e:
            logger.warning("Saver run failed: %s", e)
            if self._dataset:
                self._dataset.log_saver(
                    session_id=session_id,
                    trigger=trigger,
                    turns_covered=[],
                    extracted=[],
                    trace=[],
                    reasoning="",
                    status="error",
                    error=str(e),
                    input_turns=turns,
                    rolling_summary=self._rolling_summary,
                )
        finally:
            # Update the rolling summary after the saver runs.
            if self._summarizer and turns:
                try:
                    self._rolling_summary = self._summarizer.update(self._rolling_summary, turns)
                except Exception as e:
                    logger.debug("Summarizer update failed: %s", e)
            self._turn_buffer = []  # reset buffer after each run

    def on_pre_compress(self, messages) -> None:
        """Fire saver before context is discarded."""
        if not self._saver or self._agent_context != "primary":
            return
        # Convert messages to the format the saver expects
        turns = []
        for m in (messages or []):
            if isinstance(m, dict):
                role = m.get("role", "")
                content = m.get("content", "")
                if isinstance(content, list):
                    content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
                if role in ("user", "assistant") and content:
                    turns.append({"role": role, "content": str(content)})
        if turns:
            self._run_saver(self._session_id, trigger="on_pre_compress")

    def on_session_end(self, messages) -> None:
        """Flush a short session that ended before the normal saver cadence."""
        if not self._saver or self._agent_context != "primary" or not self._turn_buffer:
            return
        self._run_saver(self._session_id, trigger="on_session_end")

    def on_session_switch(self, new_session_id: str, *, reset: bool = False, **kwargs) -> None:
        """Reset accumulated turn buffer + rolling summary + injection history on session switch."""
        self._turn_buffer = []
        self._turn_count = 0
        self._rolling_summary = ""
        self._injected_history = []

    def _run_stack(self, sub_args: List[str], timeout: int) -> str:
        """Run the plugin's embedded engine against this provider's store.

        The engine ships with the plugin, so the memory repo holds no runtime.
        The store is passed explicitly with --repo; the engine never infers it.

        A failing engine must not look like a silent success: a non-zero exit with
        no stdout is surfaced as its stderr, so the caller warns instead of reading
        an empty string as "nothing to do".
        """
        try:
            proc = subprocess.run(
                ["uv", "run", "--no-project", str(_ENGINE), "--repo", str(self._repo), *sub_args],
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            if proc.returncode != 0 and not (proc.stdout or "").strip():
                detail = (proc.stderr or "").strip()
                _probe(f"_run_stack {sub_args} exit={proc.returncode}: {detail[:400]}")
                return json.dumps({"error": detail or f"exit {proc.returncode}",
                                   "command": sub_args[0] if sub_args else ""})
            return proc.stdout or ""
        except Exception as e:
            _probe(f"_run_stack error {sub_args}: {e}")
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    def _wiki_dir(self) -> Path:
        return self._repo / "wiki"

    def _pull_submodules_async(self) -> None:
        """Gateway-free freshness (the default): pull EVERY submodule in a background daemon thread —
        best-effort, timeout-guarded, non-blocking (session start isn't delayed). Fully contained in
        the agent process; no separate service. Primary context only."""
        if self._agent_context != "primary":
            return
        repo = self._repo
        if not (repo / ".gitmodules").exists():
            return

        def _run():
            try:
                subprocess.run(
                    ["git", "-C", str(repo), "submodule", "foreach", "git pull --ff-only || true"],
                    capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL,
                )
                _probe("pull-on-start: submodule pull done")
            except Exception as e:
                _probe(f"pull-on-start error: {e}")

        try:
            threading.Thread(target=_run, daemon=True, name="stack-submodule-pull").start()
        except Exception as e:
            _probe(f"_pull_submodules_async spawn error: {e}")

    def _build_index(self) -> None:
        self._index = []
        wiki = self._wiki_dir()
        if not wiki.is_dir():
            return
        try:
            for p in sorted(wiki.rglob("*.md")):
                parts = p.parts
                if "_inbox_archive" in parts or ".git" in parts:
                    continue
                try:
                    head = p.read_text(encoding="utf-8", errors="ignore")[:1500]
                except Exception:
                    continue
                fm = _parse_frontmatter_min(head)
                name = fm.get("name") or p.stem
                desc = fm.get("description", "")
                body = _strip_frontmatter(head)
                rel = str(p.relative_to(self._repo))
                self._index.append((name, desc, rel, p, body))
        except Exception as e:
            _probe(f"_build_index error: {e}")

    def _build_catalog(self) -> str:
        """Build a flat catalog of wiki pages for the retriever.

        No scope branching — all pages are included. The retriever (LLM)
        decides what's relevant based on the user message + page descriptions.
        """
        _SKIP_NAMES = {"now.md", "index.md", "preferences.md", "user.md",
                       "MEMORY.md", "README.md", "TEMPLATE.md", "conventions.md"}

        parts = []
        for name, desc, rel, _, _ in self._index:
            if Path(rel).name in _SKIP_NAMES:
                continue
            parts.append(f"  · {rel} — {desc.strip()}")
        return "\n".join(parts)

    def _resolve_aux_runtime(self, config_key: str):
        """Resolve provider/model for an auxiliary component (retriever / saver / summarizer).
        Reads auxiliary.<config_key> from the Hermes config; if unset or "auto", returns
        all-None so the component inherits the main model.
        """
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            return (None, None, None, None, None)
        aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}
        task = aux.get(config_key, {}) if isinstance(aux.get(config_key), dict) else {}
        provider = (str(task.get("provider", "")).strip() or None)
        model = (str(task.get("model", "")).strip() or None)
        api_key = (str(task.get("api_key", "")).strip() or None)
        base_url = (str(task.get("base_url", "")).strip() or None)
        if provider and model and provider != "auto":
            try:
                from hermes_cli.runtime_provider import resolve_runtime_provider
                rp = resolve_runtime_provider(
                    requested=provider, target_model=model,
                    explicit_api_key=api_key, explicit_base_url=base_url,
                )
                return (rp.get("provider") or provider, model, rp.get("api_key"),
                        rp.get("base_url"), rp.get("api_mode"))
            except Exception:
                pass
        return (None, None, None, None, None)

    @staticmethod
    def _resolve_aux_positive_int(config_key: str, field: str, *, default: int) -> int:
        """Read a positive integer from ``auxiliary.<config_key>.<field>``."""
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}
            task = aux.get(config_key, {}) if isinstance(aux.get(config_key), dict) else {}
            value = task.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return int(value)
        except Exception:
            pass
        return default


def register(ctx) -> None:
    """Preferred load path: the loader calls register() with a collector ctx."""
    _probe("register CALLED")
    ctx.register_memory_provider(StackMemoryProvider())
