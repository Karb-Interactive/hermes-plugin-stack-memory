"""Tests for the rolling summarizer module.

These tests do NOT require a running Hermes instance — they mock PluginLlm
or exercise pure-Python methods.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import unittest
from unittest.mock import patch
from stack.summarizer import Summarizer, _SUMMARY_MAX
from stack.common import format_turns


class TestFormatTurns(unittest.TestCase):
    """Tests for format_turns() as used by the summarizer (max_chars=500)."""

    def test_normal_turns(self):
        turns = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = format_turns(turns, max_chars=500)
        self.assertIn("[user]", result)
        self.assertIn("Hello", result)
        self.assertIn("[assistant]", result)
        self.assertIn("Hi there", result)

    def test_empty_list(self):
        result = format_turns([], max_chars=500)
        self.assertEqual(result, "")

    def test_long_content_truncated(self):
        long_content = "x" * 600
        turns = [{"role": "user", "content": long_content}]
        result = format_turns(turns, max_chars=500)
        # Should be truncated to 500 chars + marker
        self.assertIn("…", result)
        # The content portion should be at most 500 chars + truncation marker
        self.assertNotIn("x" * 600, result)
        self.assertIn("x" * 500, result)

    def test_missing_role_defaults_to_question_mark(self):
        turns = [{"content": "no role"}]
        result = format_turns(turns, max_chars=500)
        self.assertIn("[?]", result)

    def test_missing_content_defaults_to_empty(self):
        turns = [{"role": "user"}]
        result = format_turns(turns, max_chars=500)
        self.assertIn("[user]", result)


class TestUpdateEmpty(unittest.TestCase):
    """update() returns '' when both prev_summary and delta_turns are empty."""

    def setUp(self):
        self.s = Summarizer()

    def test_both_empty(self):
        result = self.s.update("", [])
        self.assertEqual(result, "")

    def test_both_none(self):
        result = self.s.update("", [])
        self.assertEqual(result, "")


class TestUpdateLLMFailure(unittest.TestCase):
    """update() returns prev_summary on LLM failure."""

    def setUp(self):
        self.s = Summarizer()

    @patch("stack.summarizer.call_llm", side_effect=Exception("boom"))
    def test_returns_prev_on_exception(self, mock_call):
        prev = "previous summary text"
        turns = [{"role": "user", "content": "hello"}]
        result = self.s.update(prev, turns)
        self.assertEqual(result, prev)

    @patch("stack.summarizer.call_llm", return_value="")
    def test_returns_prev_on_empty_result(self, mock_call):
        prev = "previous summary"
        turns = [{"role": "user", "content": "hello"}]
        result = self.s.update(prev, turns)
        self.assertEqual(result, prev)


class TestUpdatePluginLlmNotImportable(unittest.TestCase):
    """update() returns prev_summary when PluginLlm is not importable."""

    def setUp(self):
        self.s = Summarizer()

    def test_returns_prev_when_not_importable(self):
        # When call_llm returns "" (PluginLlm not available), update returns prev_summary
        with patch("stack.summarizer.call_llm", return_value=""):
            prev = "previous summary"
            turns = [{"role": "user", "content": "hello"}]
            result = self.s.update(prev, turns)
            self.assertEqual(result, prev)


class TestCallLlmNotImportable(unittest.TestCase):
    """call_llm() returns '' when PluginLlm is not importable."""

    def test_returns_empty_string(self):
        # In the Hermes environment PluginLlm is importable — this test only
        # asserts the no-Hermes path. Skip if it is available.
        import importlib.util
        if importlib.util.find_spec("agent.plugin_llm") is not None:
            self.skipTest("PluginLlm is available in this environment")
        from stack.common import call_llm
        result = call_llm("test prompt")
        self.assertEqual(result, "")


class TestUpdateTruncation(unittest.TestCase):
    """update() truncates result to _SUMMARY_MAX (5000) chars."""

    def setUp(self):
        self.s = Summarizer()

    @patch("stack.summarizer.call_llm", return_value="x" * 6000)
    def test_truncates_to_max(self, mock_call):
        turns = [{"role": "user", "content": "hello"}]
        result = self.s.update("", turns)
        self.assertEqual(len(result), _SUMMARY_MAX)
        self.assertEqual(result, "x" * _SUMMARY_MAX)


class TestUpdateNormalFlow(unittest.TestCase):
    """update() returns the LLM result when everything works."""

    def setUp(self):
        self.s = Summarizer()

    @patch("stack.summarizer.call_llm", return_value="A compact summary.")
    def test_returns_llm_result(self, mock_call):
        turns = [{"role": "user", "content": "hello"}]
        result = self.s.update("", turns)
        self.assertEqual(result, "A compact summary.")

    @patch("stack.summarizer.call_llm", return_value="Updated summary.")
    def test_returns_llm_result_with_prev(self, mock_call):
        prev = "old summary"
        turns = [{"role": "user", "content": "more discussion"}]
        result = self.s.update(prev, turns)
        self.assertEqual(result, "Updated summary.")


if __name__ == "__main__":
    unittest.main()