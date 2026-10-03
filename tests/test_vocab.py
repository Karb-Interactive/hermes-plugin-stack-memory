"""Anti-drift guard for the stack plugin's write-vocabulary.

The write instructions are single-sourced in prompts.py (WRITE_INSTRUCTIONS) and
embedded by the saver's SAVER_SYSTEM (the only agent that writes stack; the main
agent reads only). This test fails loudly if anyone re-hardcodes a divergent copy,
drops a canonical term, or skews the kind enum.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


def _stub_host():
    """Stub the Hermes host imports the plugin needs."""
    if "agent.memory_provider" not in sys.modules:
        agent_pkg = sys.modules.setdefault("agent", types.ModuleType("agent"))
        agent_pkg.__path__ = []
        mp = types.ModuleType("agent.memory_provider")
        class MemoryProvider: pass
        mp.MemoryProvider = MemoryProvider
        sys.modules["agent.memory_provider"] = mp


_stub_host()

import stack
from stack.prompts import (
    DONT_RECORD,
    SAVER_PRUNE_USER,
    TRIGGER,
    WRITE_INSTRUCTIONS as _GUIDANCE,
)

_M = stack


def test_shared_phrases_in_guidance():
    """The canonical phrases must appear in the guidance block."""
    for const in (TRIGGER, DONT_RECORD):
        assert const in _GUIDANCE, f"missing from guidance: {const!r}"


def test_canonical_terms_in_guidance():
    """Key terms must be present in the guidance."""
    for term in ("session_search", "line numbers"):
        assert term in _GUIDANCE.lower(), f"missing from guidance: {term!r}"


def test_guidance_has_all_kinds():
    """All 7 kinds must appear in the guidance."""
    for kind in ("preference", "user", "decision", "architecture", "note", "reference", "learning"):
        assert kind in _GUIDANCE, f"missing kind: {kind!r}"


def test_guidance_has_routing():
    """Routing section must mention wiki/preferences/."""
    assert "wiki/preferences/" in _GUIDANCE


def test_guidance_has_additive_rule():
    """The additive rule must be present."""
    assert "additive" in _GUIDANCE.lower()


def test_prune_prompt_enforces_write_through_task_completion():
    """Completed work belongs on its durable page, never in now.md."""
    prompt = SAVER_PRUNE_USER.lower()
    assert "never add a completion" in prompt
    assert "rewrite the entry to only the unresolved current state" in prompt
    assert "dedicated page first" in prompt


def test_session_end_flushes_pending_turns():
    """A session shorter than the cadence still runs saver curation once."""
    provider = _M.StackMemoryProvider()
    provider._saver = object()
    provider._agent_context = "primary"
    provider._session_id = "session-old"
    provider._turn_buffer = [
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": "done"},
    ]
    provider._run_saver = MagicMock()

    provider.on_session_end([])

    provider._run_saver.assert_called_once_with("session-old", trigger="on_session_end")


def test_saver_cadence_reads_positive_auxiliary_config():
    """The live saver cadence can be tuned without changing plugin source."""
    config_pkg = types.ModuleType("hermes_cli")
    config_pkg.__path__ = []
    config_mod = types.ModuleType("hermes_cli.config")
    setattr(config_mod, "load_config", lambda: {"auxiliary": {"saver": {"cadence": 3}}})

    with patch.dict(sys.modules, {
        "hermes_cli": config_pkg,
        "hermes_cli.config": config_mod,
    }):
        provider = _M.StackMemoryProvider()
        cadence = provider._resolve_aux_positive_int(
            "saver", "cadence", default=4
        )

    assert cadence == 3


def test_saver_uses_shared_instructions():
    """The saver prompt must contain the same canonical phrases as the guidance block."""
    from stack.saver import Saver
    from pathlib import Path
    prompt = Saver.__new__(Saver)
    prompt._wiki_path = Path("/tmp/wiki")
    prompt._store_path = Path("/tmp/store")
    prompt._engine_path = Path("/tmp/plugin/engine.py")
    prompt._schema_path = Path("/tmp/plugin/SCHEMA.md")
    prompt._catalog = "(test catalog)"
    text = Saver._build_prompt(prompt)
    for phrase in ("preference", "architecture", "declarative", "line numbers",
                   "session_search"):
        assert phrase in text, f"saver prompt missing: {phrase!r}"
    assert "skill_manage" not in text, "saver must not request an unavailable skill tool"
    assert "skills" not in text.lower(), "skill instructions do not belong in the saver prompt"


def test_saver_requires_private_outer_remote_propagation():
    """Personal memory must pull/commit/push its private outer remote."""
    from stack.prompts import SAVER_SYSTEM, SAVER_USER
    for phrase in ("PRIVATE remote", "pull --ff-only BEFORE editing",
                   "git -C {stack_repo} push", "don't force-push or merge"):
        assert phrase in SAVER_SYSTEM, f"saver system prompt missing: {phrase!r}"
    assert "commit and push the outer private stack repo" in SAVER_USER


def test_no_custom_tool_schemas():
    """The plugin exposes NO custom tools — get_tool_schemas returns [].

    get_tool_schemas IS implemented because it's an @abstractmethod on the host
    MemoryProvider: without it StackMemoryProvider can't be instantiated, so
    register() raises right after logging and initialize() is never called
    (the "register CALLED but no initialize" symptom). The lean adapter
    satisfies the contract by returning an empty list — the model calls
    stack.py via terminal, not via provider tools. handle_tool_call is not
    abstract, so it stays un-overridden (inherits the base NotImplementedError).
    """
    assert _M.StackMemoryProvider().get_tool_schemas() == [], \
        "lean adapter exposes no tools — get_tool_schemas must return []"
    assert "handle_tool_call" not in _M.StackMemoryProvider.__dict__, \
        "handle_tool_call should not be overridden"


def test_no_write_methods():
    """No write/save/git methods should exist on the provider."""
    for method in ("_save_page", "_save_preference", "_save_user", "_save_in_submodule",
                   "_git_commit", "_file_issue", "_publish_shared", "_build_frontmatter"):
        assert not hasattr(_M.StackMemoryProvider, method), \
            f"{method} should be removed — write logic lives in bin/stack.py"


if __name__ == "__main__":
    checks = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    for fn in checks:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(checks)} checks passed")