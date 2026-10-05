"""Tests for the saver agent module.

Tests the testable parts of Saver without requiring a running Hermes:
- _format_turns() with various inputs (normal, empty, long content truncation)
- _build_prompt() contains wiki path, scoped catalog, and extraction signals
- _extract_saves() with mock agent messages (terminal/engine create, no tools, "Nothing to save")
- extract_and_save() returns [] when AIAgent not importable
- extract_and_save() returns [] when agent raises

Uses unittest.mock to patch imports. Does NOT require a running Hermes.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# Make plugin modules importable without Hermes
_here = Path(__file__).resolve().parent
if str(_here.parent) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from stack.saver import Saver
from stack.common import format_turns


STORE = Path("/store")
ENGINE = Path("/plugin/engine.py")
SCHEMA = Path("/plugin/SCHEMA.md")


def _saver(wiki=Path("/wiki"), catalog="test"):
    """Construct a Saver the way the provider does: all paths supplied."""
    return Saver(wiki_path=wiki, store_path=STORE, engine_path=ENGINE,
                 schema_path=SCHEMA, catalog=catalog)


class TestFormatTurns(unittest.TestCase):
    """Tests for format_turns() (used by Saver, default max_chars=2000)."""

    def test_normal_turns(self):
        """Normal turns list produces formatted output with turn numbers and roles."""
        turns = [
            {"role": "user", "content": "Hello, can you help me?"},
            {"role": "assistant", "content": "Of course! What do you need?"},
        ]
        result = format_turns(turns)
        self.assertIn("Turn 1 [user]", result)
        self.assertIn("Hello, can you help me?", result)
        self.assertIn("Turn 2 [assistant]", result)
        self.assertIn("Of course! What do you need?", result)

    def test_empty_list(self):
        """Empty turns list produces empty string."""
        result = format_turns([])
        self.assertEqual(result, "")

    def test_single_turn(self):
        """Single turn produces one formatted block."""
        turns = [{"role": "user", "content": "Just one turn"}]
        result = format_turns(turns)
        self.assertIn("Turn 1 [user]", result)
        self.assertIn("Just one turn", result)

    def test_long_content_truncated(self):
        """Content longer than 2000 chars is truncated with marker."""
        long_content = "A" * 3000
        turns = [{"role": "user", "content": long_content}]
        result = format_turns(turns)
        self.assertIn("…(truncated)", result)
        # The truncated content should be at most 2000 chars + marker
        # Check that the full 3000 chars are NOT present
        self.assertNotIn("A" * 2500, result)
        # But the first 2000 chars ARE present
        self.assertIn("A" * 2000, result)

    def test_content_at_boundary_not_truncated(self):
        """Content exactly 2000 chars is NOT truncated."""
        boundary_content = "B" * 2000
        turns = [{"role": "assistant", "content": boundary_content}]
        result = format_turns(turns)
        self.assertNotIn("…(truncated)", result)
        self.assertIn(boundary_content, result)

    def test_missing_role_defaults_to_question_mark(self):
        """Missing role key defaults to '?'."""
        turns = [{"content": "No role here"}]
        result = format_turns(turns)
        self.assertIn("[?]", result)

    def test_missing_content_defaults_to_empty(self):
        """Missing content key defaults to empty string."""
        turns = [{"role": "user"}]
        result = format_turns(turns)
        self.assertIn("Turn 1 [user]", result)

    def test_turns_separated_by_double_newline(self):
        """Multiple turns are separated by double newlines."""
        turns = [
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "Second"},
        ]
        result = format_turns(turns)
        # Check there's a blank line between turns
        self.assertIn("\n\n--- Turn 2", result)


class TestBuildPrompt(unittest.TestCase):
    """Tests for Saver._build_prompt()."""

    def test_contains_wiki_path(self):
        """Prompt contains the wiki directory path."""
        wiki = Path("/custom/path/to/wiki")
        s = _saver(wiki, "test catalog")
        prompt = s._build_prompt()
        self.assertIn(str(wiki), prompt)

    def test_contains_catalog(self):
        """Prompt contains the scoped catalog text."""
        catalog = (
            "wiki/preferences/dark_mode.md — Prefers dark mode\n"
            "wiki/preferences/short_answers.md — Prefers short answers\n"
        )
        s = _saver(Path("/wiki"), catalog)
        prompt = s._build_prompt()
        self.assertIn(catalog, prompt)

    def test_contains_canonical_kind_taxonomy(self):
        """Prompt contains the SCHEMA.md canonical kind names."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        for kind in (
            "kind=preference",
            "kind=user",
            "kind=decision",
            "kind=architecture",
            "kind=note",
            "kind=learning",
        ):
            self.assertIn(kind, prompt)

    def test_contains_dont_record_verbatim(self):
        """Prompt contains _V_DONT_RECORD text verbatim."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertIn("task progress, session outcomes", prompt)
        self.assertIn("stale in 7 days", prompt)

    def test_contains_exclusion_list(self):
        """Prompt contains the 'what NOT to extract' section."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertIn("commit SHAs", prompt)
        self.assertIn("Environment-dependent failures", prompt)
        self.assertIn("Negative claims about tools or features", prompt)
        self.assertIn("Session-specific transient errors", prompt)
        self.assertIn("One-off task narratives", prompt)

    def test_contains_routing_section(self):
        """Prompt contains the routing section."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertIn("Where to write", prompt)
        self.assertIn("routing", prompt.lower())

    def test_contains_rules(self):
        """Prompt contains the rules section."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertIn("create --name", prompt)
        self.assertIn("Nothing to save", prompt)
        self.assertIn("CONTEXT ONLY", prompt)

    def test_prompt_uses_the_plugin_engine_not_a_repo_local_copy(self):
        """The saver must invoke the plugin's engine, with an explicit --repo.

        Contract: the memory repo holds data only. A prompt that tells the saver
        to run `<repo>/bin/stack.py` reintroduces the two-places problem.
        """
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertNotIn("bin/stack.py", prompt)
        self.assertIn("engine.py", prompt)
        self.assertIn("--repo", prompt)

    def test_contains_search_first_instruction(self):
        """Prompt instructs to search wiki first."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertIn("Search the wiki FIRST", prompt)

    def test_contains_declarative_facts_rule(self):
        """Prompt instructs declarative facts, not instructions."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertIn("declarative facts, not instructions", prompt)

    def test_contains_anchors_rule(self):
        """Prompt contains the anchors rule (file or file::entity, never line numbers)."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertIn("Anchors are file or file::entity", prompt)

    def test_skill_learning_is_outside_saver_scope(self):
        """Saver curates knowledge without requesting unavailable skill tools."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertNotIn("skill_manage", prompt)
        self.assertNotIn("skills", prompt.lower())

    def test_contains_trigger_verbatim(self):
        """Prompt contains _V_TRIGGER text verbatim."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertIn("decided, learned, or corrected", prompt)

    def test_contains_sacred_preferences_rule(self):
        """Prompt states preferences are sacred (additive-only, never auto-regenerated or merged)."""
        s = _saver(Path("/wiki"), "test")
        prompt = s._build_prompt()
        self.assertIn("sacred", prompt)
        self.assertIn("additive-only", prompt)


class TestExtractSaves(unittest.TestCase):
    """Tests for Saver._extract_saves()."""

    def setUp(self):
        self.s = Saver(
            wiki_path=Path("/fake/wiki"),
            store_path=STORE,
            engine_path=ENGINE,
            schema_path=SCHEMA,
            catalog="wiki/preferences/foo.md — Foo",
        )

    def _make_agent(self, messages):
        """Create a mock agent with the given _session_messages."""
        agent = MagicMock()
        agent._session_messages = messages
        return agent

    def _term_call(self, command):
        """Build a terminal tool call with the given command."""
        return {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": command}),
            },
        }

    def test_agent_called_stack_create(self):
        """Agent that called engine create returns extracted info."""
        cmd = ("uv run /fake/engine.py create "
               "--name user-prefers-concise-responses "
               "--description 'User wants short answers' "
               "--content 'The user prefers concise responses.' "
               "--kind preference")
        messages = [
            {"role": "user", "content": "Extract knowledge"},
            {"role": "assistant", "content": "I'll save this preference.",
             "tool_calls": [self._term_call(cmd)]},
        ]
        agent = self._make_agent(messages)
        result = self.s._extract_saves(agent)

        self.assertEqual(len(result), 1)
        saved = result[0]
        self.assertEqual(saved["kind"], "preference")
        self.assertEqual(saved["name"], "user-prefers-concise-responses")
        self.assertEqual(saved["page_written"], "wiki/user-prefers-concise-responses.md")

    def test_agent_called_stack_create_with_dir(self):
        """engine create with --dir produces the correct page_written path."""
        cmd = ("uv run /fake/engine.py create "
               "--name arch-decision "
               "--description 'Architecture decision' "
               "--content 'We decided to use X.' "
               "--kind decision --dir <org>/discussions")
        messages = [
            {"role": "assistant", "content": "",
             "tool_calls": [self._term_call(cmd)]},
        ]
        agent = self._make_agent(messages)
        result = self.s._extract_saves(agent)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "decision")
        self.assertEqual(result[0]["name"], "arch-decision")
        self.assertEqual(result[0]["page_written"], "wiki/<org>/discussions/arch-decision.md")

    def test_agent_no_tool_calls(self):
        """Agent that didn't call any tools returns empty list."""
        messages = [
            {"role": "user", "content": "Extract knowledge"},
            {"role": "assistant", "content": "Nothing to save."},
        ]
        agent = self._make_agent(messages)
        result = self.s._extract_saves(agent)
        self.assertEqual(result, [])

    def test_agent_no_stack_create_calls(self):
        """Agent called other tools but not terminal with engine create returns empty list."""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_files",
                            "arguments": json.dumps({"pattern": "*.md"}),
                        },
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "/some/file.md"}),
                        },
                    },
                ],
            },
        ]
        agent = self._make_agent(messages)
        result = self.s._extract_saves(agent)
        self.assertEqual(result, [])

    def test_agent_said_nothing_to_save(self):
        """Agent that said 'Nothing to save' and made no tool calls returns empty list."""
        messages = [
            {"role": "user", "content": "Extract knowledge"},
            {"role": "assistant", "content": "Nothing to save."},
        ]
        agent = self._make_agent(messages)
        result = self.s._extract_saves(agent)
        self.assertEqual(result, [])

    def test_multiple_create_calls(self):
        """Multiple engine create calls in one agent session are all extracted."""
        cmd1 = ("uv run /fake/engine.py create --name pref-1 "
                "--description 'First' --content 'C1' --kind preference")
        cmd2 = ("uv run /fake/engine.py create --name note-1 "
                "--description 'First note' --content 'C2' --kind note")
        messages = [
            {"role": "assistant", "content": "Saving two pages.",
             "tool_calls": [self._term_call(cmd1), self._term_call(cmd2)]},
        ]
        agent = self._make_agent(messages)
        result = self.s._extract_saves(agent)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "pref-1")
        self.assertEqual(result[0]["kind"], "preference")
        self.assertEqual(result[1]["name"], "note-1")
        self.assertEqual(result[1]["kind"], "note")

    def test_invalid_json_arguments_skipped(self):
        """Terminal calls with invalid JSON arguments are silently skipped."""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": "{broken json",
                        },
                    },
                ],
            },
        ]
        agent = self._make_agent(messages)
        result = self.s._extract_saves(agent)
        self.assertEqual(result, [])

    def test_missing_kind_defaults_to_reference(self):
        """engine create without --kind defaults to 'reference'."""
        cmd = ("uv run /fake/engine.py create --name some-note "
               "--description 'A note' --content 'Content here'")
        messages = [
            {"role": "assistant", "content": "",
             "tool_calls": [self._term_call(cmd)]},
        ]
        agent = self._make_agent(messages)
        result = self.s._extract_saves(agent)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "reference")

    def test_empty_session_messages(self):
        """Agent with no _session_messages returns empty list."""
        agent = MagicMock()
        agent._session_messages = []
        result = self.s._extract_saves(agent)
        self.assertEqual(result, [])

    def test_no_session_messages_attr(self):
        """Agent without _session_messages attribute returns empty list."""
        agent = MagicMock()
        del agent._session_messages
        # getattr returns the Mock's attribute, not [].
        # Actually, MagicMock auto-creates attributes, so _session_messages
        # will be a MagicMock. The isinstance check handles this.
        # Let's use a plain object instead:
        class BareAgent:
            pass
        agent = BareAgent()
        result = self.s._extract_saves(agent)
        self.assertEqual(result, [])

    def test_create_across_multiple_messages(self):
        """engine create calls spread across multiple assistant messages are all captured."""
        messages = [
            {
                "role": "assistant",
                "content": "Saving first.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({
                                "command": "uv run /fake/engine.py create --name pref-a --description A --content A --kind preference",
                            }),
                        },
                    },
                ],
            },
            {"role": "tool", "content": "Saved."},
            {
                "role": "assistant",
                "content": "Saving second.",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({
                                "command": "uv run /fake/engine.py create --name note-b --description B --content B --kind note",
                            }),
                        },
                    },
                ],
            },
        ]
        agent = self._make_agent(messages)
        result = self.s._extract_saves(agent)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "pref-a")
        self.assertEqual(result[1]["name"], "note-b")


class TestExtractAndSave(unittest.TestCase):
    """Tests for Saver.extract_and_save() — the agent spawning path."""

    def setUp(self):
        self.s = Saver(
            wiki_path=Path("/fake/wiki"),
            store_path=STORE,
            engine_path=ENGINE,
            schema_path=SCHEMA,
            catalog="wiki/preferences/foo.md — Foo",
        )

    def test_returns_empty_when_aiagent_not_importable(self):
        """Returns ([], [], "") when AIAgent can't be imported (running outside Hermes).

        We simulate this by patching builtins.__import__ to raise ImportError
        for the 'run_agent' module, which is the first import in the try block.
        """
        import builtins

        original_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "run_agent":
                raise ImportError("No module named 'run_agent'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_blocked_import):
            result = self.s.extract_and_save(
                [{"role": "user", "content": "test"}],
                rolling_summary="test context",
            )
        self.assertEqual(result, ([], [], ""))

    def test_returns_empty_when_agent_raises(self):
        """Returns ([], [], "") when the AIAgent constructor raises an exception."""
        mock_agent_cls = MagicMock(side_effect=RuntimeError("boom"))
        mock_module = MagicMock()
        mock_module.AIAgent = mock_agent_cls

        mock_plugins = MagicMock()
        mock_plugins.set_thread_tool_whitelist = MagicMock()
        mock_plugins.clear_thread_tool_whitelist = MagicMock()

        with patch.dict(sys.modules, {
            "run_agent": mock_module,
            "hermes_cli": MagicMock(),
            "hermes_cli.plugins": mock_plugins,
        }):
            result = self.s.extract_and_save(
                [{"role": "user", "content": "test"}],
                rolling_summary="test context",
            )

        self.assertEqual(result, ([], [], ""))
        mock_agent_cls.assert_called_once()

    def test_returns_saves_from_agent_tool_calls(self):
        """Returns extracted saves when the agent called engine create via terminal.

        Mocks AIAgent so that _session_messages contains an assistant message
        with a terminal tool call running engine create.
        """
        mock_agent = MagicMock()
        mock_agent._session_messages = [
            {"role": "user", "content": "Extract knowledge"},
            {
                "role": "assistant",
                "content": "I'll save this.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": "uv run /fake/engine.py create --name test-extraction --description Test --content Content --kind note"}),
                        },
                    }
                ],
            },
        ]
        mock_agent.close = MagicMock()
        mock_agent.run_conversation = MagicMock(return_value={"messages": []})

        mock_module = MagicMock()
        mock_module.AIAgent = MagicMock(return_value=mock_agent)

        mock_plugins = MagicMock()
        mock_plugins.set_thread_tool_whitelist = MagicMock()
        mock_plugins.clear_thread_tool_whitelist = MagicMock()

        with patch.dict(sys.modules, {
            "run_agent": mock_module,
            "hermes_cli": MagicMock(),
            "hermes_cli.plugins": mock_plugins,
        }):
            saved, trace, reasoning = self.s.extract_and_save(
                [{"role": "user", "content": "I prefer concise responses"}],
                rolling_summary="Session about preferences",
            )

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["name"], "test-extraction")
        self.assertEqual(saved[0]["kind"], "note")
        self.assertIsInstance(trace, list)
        mock_agent.close.assert_called_once()
        # Two turns: save, then the now.md prune pass on the same session
        self.assertEqual(mock_agent.run_conversation.call_count, 2)
        prune_call = mock_agent.run_conversation.call_args_list[1]
        self.assertIn("now.md", prune_call.kwargs["user_message"])

    def test_prune_failure_keeps_saves(self):
        """A crash in the prune turn must not discard the save turn's results."""
        mock_agent = MagicMock()
        mock_agent._session_messages = [
            {"role": "user", "content": "Extract knowledge"},
            {
                "role": "assistant",
                "content": "Saved.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": "uv run /fake/engine.py create --name kept-page --description Test --kind note"}),
                        },
                    }
                ],
            },
        ]
        mock_agent.close = MagicMock()
        mock_agent.run_conversation = MagicMock(
            side_effect=[{"messages": []}, RuntimeError("prune crashed")]
        )

        mock_module = MagicMock()
        mock_module.AIAgent = MagicMock(return_value=mock_agent)

        mock_plugins = MagicMock()
        mock_plugins.set_thread_tool_whitelist = MagicMock()
        mock_plugins.clear_thread_tool_whitelist = MagicMock()

        with patch.dict(sys.modules, {
            "run_agent": mock_module,
            "hermes_cli": MagicMock(),
            "hermes_cli.plugins": mock_plugins,
        }):
            saved, trace, reasoning = self.s.extract_and_save(
                [{"role": "user", "content": "test"}],
            )

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["name"], "kept-page")
        self.assertEqual(mock_agent.run_conversation.call_count, 2)
        mock_plugins.clear_thread_tool_whitelist.assert_called_once()

    def test_returns_empty_when_agent_says_nothing_to_save(self):
        """Returns ([], trace, reasoning) when the agent says 'Nothing to save'.

        The saved list is empty but the trace captures the agent's reasoning.
        """
        mock_agent = MagicMock()
        mock_agent._session_messages = [
            {"role": "user", "content": "Extract knowledge"},
            {"role": "assistant", "content": "Nothing to save."},
        ]
        mock_agent.close = MagicMock()
        mock_agent.run_conversation = MagicMock(return_value={"messages": []})

        mock_module = MagicMock()
        mock_module.AIAgent = MagicMock(return_value=mock_agent)

        mock_plugins = MagicMock()
        mock_plugins.set_thread_tool_whitelist = MagicMock()
        mock_plugins.clear_thread_tool_whitelist = MagicMock()

        with patch.dict(sys.modules, {
            "run_agent": mock_module,
            "hermes_cli": MagicMock(),
            "hermes_cli.plugins": mock_plugins,
        }):
            saved, trace, reasoning = self.s.extract_and_save(
                [{"role": "user", "content": "Hello"}],
            )

        self.assertEqual(saved, [])
        # The trace captures the agent's "Nothing to save" reasoning
        self.assertIsInstance(trace, list)
        reasoning_entries = [e for e in trace if e["type"] == "reasoning"]
        self.assertEqual(len(reasoning_entries), 1)
        self.assertIn("Nothing to save", reasoning_entries[0]["text"])
        self.assertIn("Nothing to save", reasoning)

    def test_clears_whitelist_even_on_run_failure(self):
        """The tool whitelist is cleared even if run_conversation raises."""
        mock_agent = MagicMock()
        mock_agent._session_messages = []
        mock_agent.close = MagicMock()
        mock_agent.run_conversation = MagicMock(side_effect=RuntimeError("agent crashed"))

        mock_module = MagicMock()
        mock_module.AIAgent = MagicMock(return_value=mock_agent)

        mock_plugins = MagicMock()
        mock_plugins.set_thread_tool_whitelist = MagicMock()
        mock_plugins.clear_thread_tool_whitelist = MagicMock()

        with patch.dict(sys.modules, {
            "run_agent": mock_module,
            "hermes_cli": MagicMock(),
            "hermes_cli.plugins": mock_plugins,
        }):
            result = self.s.extract_and_save(
                [{"role": "user", "content": "test"}],
            )

        self.assertEqual(result, ([], [], ""))
        mock_plugins.clear_thread_tool_whitelist.assert_called_once()

    def test_default_cadence_is_4(self):
        """DEFAULT_CADENCE class attribute is 4."""
        self.assertEqual(Saver.DEFAULT_CADENCE, 4)


class TestGetFinalResponse(unittest.TestCase):
    """Tests for Saver._get_final_response()."""

    def setUp(self):
        self.s = Saver(
            wiki_path=Path("/fake/wiki"),
            store_path=STORE,
            engine_path=ENGINE,
            schema_path=SCHEMA,
            catalog="test",
        )

    def test_extracts_last_assistant_text(self):
        """Returns the last assistant message content as string."""
        agent = MagicMock()
        agent._session_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "First response"},
            {"role": "user", "content": "Again"},
            {"role": "assistant", "content": "Final response"},
        ]
        result = self.s._get_final_response(agent)
        self.assertEqual(result, "Final response")

    def test_returns_empty_when_no_assistant_messages(self):
        """Returns empty string when there are no assistant messages."""
        agent = MagicMock()
        agent._session_messages = [
            {"role": "user", "content": "Hello"},
        ]
        result = self.s._get_final_response(agent)
        self.assertEqual(result, "")

    def test_handles_list_content(self):
        """Handles content that is a list of text blocks (OpenAI format)."""
        agent = MagicMock()
        agent._session_messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "Part 1"}, {"type": "text", "text": "Part 2"}]},
        ]
        result = self.s._get_final_response(agent)
        self.assertEqual(result, "Part 1 Part 2")

    def test_skips_empty_assistant_messages(self):
        """Skips assistant messages with empty content and finds the last non-empty one."""
        agent = MagicMock()
        agent._session_messages = [
            {"role": "assistant", "content": "Real response"},
            {"role": "assistant", "content": ""},
        ]
        result = self.s._get_final_response(agent)
        self.assertEqual(result, "Real response")

    def test_returns_empty_when_no_messages(self):
        """Returns empty string when _session_messages is empty."""
        agent = MagicMock()
        agent._session_messages = []
        result = self.s._get_final_response(agent)
        self.assertEqual(result, "")


class TestExtractTrace(unittest.TestCase):
    """Tests for Saver._extract_trace()."""

    def setUp(self):
        self.s = Saver(
            wiki_path=Path("/fake/wiki"),
            store_path=STORE,
            engine_path=ENGINE,
            schema_path=SCHEMA,
            catalog="wiki/preferences/foo.md — Foo",
        )

    def _make_agent(self, messages):
        """Create a mock agent with the given _session_messages."""
        agent = MagicMock()
        agent._session_messages = messages
        return agent

    def test_agent_with_tool_calls(self):
        """Agent with tool calls → trace contains tool_call entries."""
        messages = [
            {"role": "user", "content": "Extract knowledge"},
            {
                "role": "assistant",
                "content": "I'll search and save.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_files",
                            "arguments": json.dumps({"pattern": "*.md"}),
                        },
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({
                                "command": "uv run /fake/engine.py create --name test-pref --description Test --content Content --kind preference",
                            }),
                        },
                    },
                ],
            },
        ]
        agent = self._make_agent(messages)
        trace = self.s._extract_trace(agent)

        tool_calls = [e for e in trace if e["type"] == "tool_call"]
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0]["tool"], "search_files")
        self.assertEqual(tool_calls[1]["tool"], "terminal")

    def test_agent_with_reasoning_text(self):
        """Agent with reasoning text → trace contains reasoning entries."""
        messages = [
            {"role": "assistant", "content": "I need to search the wiki first."},
        ]
        agent = self._make_agent(messages)
        trace = self.s._extract_trace(agent)

        reasoning = [e for e in trace if e["type"] == "reasoning"]
        self.assertEqual(len(reasoning), 1)
        self.assertIn("search", reasoning[0]["text"])

    def test_agent_no_messages(self):
        """Agent with no messages → empty trace."""
        agent = self._make_agent([])
        trace = self.s._extract_trace(agent)
        self.assertEqual(trace, [])

    def test_agent_with_tool_results(self):
        """Agent with tool results → trace contains tool_result entries."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "search_files",
                        "arguments": json.dumps({"pattern": "*.md"}),
                    },
                },
            ]},
            {"role": "tool", "content": "wiki/foo.md\nwiki/bar.md"},
        ]
        agent = self._make_agent(messages)
        trace = self.s._extract_trace(agent)

        tool_results = [e for e in trace if e["type"] == "tool_result"]
        self.assertEqual(len(tool_results), 1)
        self.assertIn("wiki/foo.md", tool_results[0]["text"])

    def test_trace_truncates_long_text(self):
        """Long reasoning text is truncated to 500 chars."""
        long_text = "B" * 1000
        messages = [
            {"role": "assistant", "content": long_text},
        ]
        agent = self._make_agent(messages)
        trace = self.s._extract_trace(agent)

        reasoning = [e for e in trace if e["type"] == "reasoning"]
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(len(reasoning[0]["text"]), 500)

    def test_agent_with_list_content(self):
        """Agent with list-format content (OpenAI style) → reasoning extracted."""
        messages = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Part A"},
                {"type": "text", "text": "Part B"},
            ]},
        ]
        agent = self._make_agent(messages)
        trace = self.s._extract_trace(agent)

        reasoning = [e for e in trace if e["type"] == "reasoning"]
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(reasoning[0]["text"], "Part A Part B")

    def test_agent_no_session_messages_attr(self):
        """Agent without _session_messages attribute returns empty trace."""
        class BareAgent:
            pass
        agent = BareAgent()
        trace = self.s._extract_trace(agent)
        self.assertEqual(trace, [])


if __name__ == "__main__":
    unittest.main()