"""Tests for the retriever module (single-call memory retrieval via PluginLlm).

Tests the testable parts of Retriever without requiring a running Hermes:
- _parse_response() with various inputs (JSON, plain text, empty, garbage)
- retrieve() returns ([], []) when PluginLlm is not importable
- retrieve() returns ([], []) when PluginLlm raises
- retrieve() filters out recently_injected paths
- retrieve() caps at _MAX_PAGES (3)
- _RETRIEVER_PROMPT contains key Claude Code patterns

Uses unittest.mock to patch PluginLlm. Does NOT require a running Hermes.
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

from stack.retriever import Retriever, _RETRIEVER_PROMPT, _MAX_PAGES
from stack.common import parse_path_response


class TestParseResponse(unittest.TestCase):
    """Tests for parse_path_response()."""

    def test_json_single_page(self):
        """JSON with a single page path."""
        text = json.dumps({"classification": "WORK", "pages": ["wiki/preferences/foo.md"]})
        result = parse_path_response(text)
        self.assertEqual(result, ["wiki/preferences/foo.md"])

    def test_json_embedded_in_prose(self):
        """JSON embedded in surrounding prose."""
        text = (
            "I analyzed the message and here's my response:\n"
            '{"classification": "WORK", "pages": ["wiki/preferences/foo.md"]}\n'
            "Hope that helps!"
        )
        result = parse_path_response(text)
        self.assertEqual(result, ["wiki/preferences/foo.md"])

    def test_plain_text_with_paths(self):
        """Plain text with paths (fallback to regex)."""
        text = (
            "The relevant pages are:\n"
            "wiki/preferences/foo.md\n"
            "wiki/preferences/bar.md\n"
        )
        result = parse_path_response(text)
        self.assertEqual(len(result), 2)
        self.assertIn("wiki/preferences/foo.md", result)
        self.assertIn("wiki/preferences/bar.md", result)

    def test_empty_string(self):
        """Empty string returns empty list."""
        self.assertEqual(parse_path_response(""), [])
        self.assertEqual(parse_path_response("   "), [])

    def test_garbage_no_paths(self):
        """Text with no .md paths returns empty list."""
        text = "I couldn't find any relevant pages for this query."
        result = parse_path_response(text)
        self.assertEqual(result, [])

    def test_json_empty_pages_list(self):
        """JSON with empty pages list returns empty list."""
        text = json.dumps({"classification": "WORK", "pages": []})
        result = parse_path_response(text)
        self.assertEqual(result, [])

    def test_json_casual_classification_still_parses_pages(self):
        """JSON with classification CASUAL should still parse pages — caller decides."""
        text = json.dumps({"classification": "CASUAL", "pages": ["wiki/preferences/foo.md"]})
        result = parse_path_response(text)
        self.assertEqual(result, ["wiki/preferences/foo.md"])

    def test_json_multiple_pages_capped(self):
        """JSON with more than _MAX_PAGES pages — only _MAX_PAGES returned."""
        pages = [f"wiki/page_{i}.md" for i in range(10)]
        text = json.dumps({"classification": "WORK", "pages": pages})
        result = parse_path_response(text, max_pages=_MAX_PAGES)
        self.assertEqual(len(result), _MAX_PAGES)

    def test_plain_text_paths_without_wiki_prefix_normalized(self):
        """Paths without 'wiki/' prefix are normalized in fallback regex."""
        text = "preferences/foo.md\nacme/rocket/bar.md\n"
        result = parse_path_response(text)
        self.assertIn("wiki/preferences/foo.md", result)
        self.assertIn("wiki/acme/rocket/bar.md", result)

    def test_plain_text_dedup_paths(self):
        """Duplicate paths in fallback are deduplicated."""
        text = "wiki/preferences/foo.md\nwiki/preferences/foo.md\nwiki/preferences/bar.md\n"
        result = parse_path_response(text)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], "wiki/preferences/foo.md")

    def test_none_input(self):
        """None input returns empty list (doesn't crash)."""
        self.assertEqual(parse_path_response(None), [])

    def test_json_non_md_filtered(self):
        """JSON with non-.md paths filters them out."""
        text = json.dumps({"classification": "WORK", "pages": ["wiki/preferences/foo.txt", "wiki/preferences/bar.md"]})
        result = parse_path_response(text)
        self.assertEqual(result, ["wiki/preferences/bar.md"])


class TestRetrieve(unittest.TestCase):
    """Tests for Retriever.retrieve() — the LLM call path."""

    def setUp(self):
        self.r = Retriever(
            wiki_path=Path("/fake/wiki"),
            catalog="## Global preferences\n  · wiki/preferences/foo.md — Foo pref",
        )

    def test_returns_empty_when_pluginllm_not_importable(self):
        """Returns ([], []) when PluginLlm can't be imported (running outside Hermes)."""
        import builtins

        original_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "agent.plugin_llm":
                raise ImportError("No module named 'agent.plugin_llm'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_blocked_import):
            paths, trace = self.r.retrieve("help me debug this code")

        # No paths and no raise; the trace may carry an entry, but retrieval stays graceful.
        self.assertEqual(paths, [])
        self.assertIsInstance(trace, list)

    def test_returns_empty_when_pluginllm_raises(self):
        """Returns ([], trace) when PluginLlm.complete() raises an exception."""
        mock_llm_cls = MagicMock(side_effect=RuntimeError("LLM unavailable"))

        mock_module = MagicMock()
        mock_module.PluginLlm = mock_llm_cls

        with patch.dict(sys.modules, {"agent.plugin_llm": mock_module}):
            paths, trace = self.r.retrieve("help me debug this code")

        self.assertEqual(paths, [])
        self.assertIsInstance(trace, list)
        # trace should have an error entry
        self.assertTrue(any(e.get("type") == "error" for e in trace))

    def test_returns_paths_from_llm_response(self):
        """Returns extracted paths from the LLM JSON response."""
        mock_llm = MagicMock()
        mock_llm.complete = MagicMock(return_value='{"classification": "WORK", "pages": ["wiki/preferences/foo.md"]}')

        mock_llm_cls = MagicMock(return_value=mock_llm)
        mock_module = MagicMock()
        mock_module.PluginLlm = mock_llm_cls

        with patch.dict(sys.modules, {"agent.plugin_llm": mock_module}):
            paths, trace = self.r.retrieve("help me with preferences")

        self.assertEqual(paths, ["wiki/preferences/foo.md"])
        self.assertIsInstance(trace, list)
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["type"], "llm_call")
        mock_llm.complete.assert_called_once()

    def test_filters_out_recently_injected(self):
        """Recently injected paths are filtered from the result."""
        mock_llm = MagicMock()
        mock_llm.complete = MagicMock(
            return_value=json.dumps({
                "classification": "WORK",
                "pages": ["wiki/preferences/foo.md", "wiki/preferences/bar.md"],
            })
        )

        mock_llm_cls = MagicMock(return_value=mock_llm)
        mock_module = MagicMock()
        mock_module.PluginLlm = mock_llm_cls

        with patch.dict(sys.modules, {"agent.plugin_llm": mock_module}):
            paths, trace = self.r.retrieve(
                "help me with preferences",
                recently_injected={"wiki/preferences/foo.md"},
            )

        # foo.md was recently injected, so only bar.md should remain
        self.assertEqual(paths, ["wiki/preferences/bar.md"])

    def test_caps_at_max_pages(self):
        """retrieve() caps returned paths at _MAX_PAGES (3)."""
        pages = [f"wiki/page_{i}.md" for i in range(10)]
        mock_llm = MagicMock()
        mock_llm.complete = MagicMock(
            return_value=json.dumps({"classification": "WORK", "pages": pages})
        )

        mock_llm_cls = MagicMock(return_value=mock_llm)
        mock_module = MagicMock()
        mock_module.PluginLlm = mock_llm_cls

        with patch.dict(sys.modules, {"agent.plugin_llm": mock_module}):
            paths, trace = self.r.retrieve("test message")

        self.assertEqual(len(paths), _MAX_PAGES)
        self.assertEqual(_MAX_PAGES, 3)

    def test_dict_response_content_key(self):
        """When llm.complete() returns a dict, the 'content' key is extracted."""
        mock_llm = MagicMock()
        mock_llm.complete = MagicMock(
            return_value={"content": '{"classification": "WORK", "pages": ["wiki/preferences/foo.md"]}'}
        )

        mock_llm_cls = MagicMock(return_value=mock_llm)
        mock_module = MagicMock()
        mock_module.PluginLlm = mock_llm_cls

        with patch.dict(sys.modules, {"agent.plugin_llm": mock_module}):
            paths, trace = self.r.retrieve("test message")

        self.assertEqual(paths, ["wiki/preferences/foo.md"])

    def test_user_message_not_truncated(self):
        """The FULL user message reaches the retriever LLM (no economy truncation)."""
        mock_llm = MagicMock()
        mock_llm.complete = MagicMock(return_value='{"classification": "CASUAL", "pages": []}')

        mock_llm_cls = MagicMock(return_value=mock_llm)
        mock_module = MagicMock()
        mock_module.PluginLlm = mock_llm_cls

        long_msg = "x" * 5000
        with patch.dict(sys.modules, {"agent.plugin_llm": mock_module}):
            self.r.retrieve(long_msg)

        # call_llm passes messages=[{system}, {user}] as the first positional to complete().
        messages = mock_llm.complete.call_args[0][0]
        user_msg = next(m["content"] for m in messages if m["role"] == "user")
        self.assertEqual(user_msg, long_msg)  # full message, untruncated

    def test_trace_contains_prompt_and_response(self):
        """Trace entry contains truncated prompt and response."""
        mock_llm = MagicMock()
        mock_llm.complete = MagicMock(
            return_value='{"classification": "WORK", "pages": ["wiki/preferences/foo.md"]}'
        )

        mock_llm_cls = MagicMock(return_value=mock_llm)
        mock_module = MagicMock()
        mock_module.PluginLlm = mock_llm_cls

        with patch.dict(sys.modules, {"agent.plugin_llm": mock_module}):
            paths, trace = self.r.retrieve("test message")

        self.assertEqual(len(trace), 1)
        entry = trace[0]
        self.assertEqual(entry["type"], "llm_call")
        self.assertIn("prompt", entry)
        self.assertIn("response", entry)
        # Prompt and response are truncated to 500 chars
        self.assertLessEqual(len(entry["prompt"]), 500)
        self.assertLessEqual(len(entry["response"]), 500)


class TestRetrieverPrompt(unittest.TestCase):
    """Tests that _RETRIEVER_PROMPT contains key Claude Code patterns."""

    def test_contains_classification_labels(self):
        """Prompt contains CASUAL, WORK, and SYSTEM classification labels."""
        self.assertIn("CASUAL", _RETRIEVER_PROMPT)
        self.assertIn("WORK", _RETRIEVER_PROMPT)
        self.assertIn("SYSTEM", _RETRIEVER_PROMPT)

    def test_contains_conservative_selection(self):
        """Prompt contains the 'If you are unsure' conservative selection instruction."""
        self.assertIn("If you are unsure", _RETRIEVER_PROMPT)

    def test_contains_anti_keyword_matching(self):
        """Prompt warns against surface keyword matching."""
        self.assertIn("surface keyword overlap", _RETRIEVER_PROMPT)

    def test_contains_no_re_injection(self):
        """Prompt instructs not to re-select already-returned pages."""
        self.assertIn("Do not select pages that were already returned", _RETRIEVER_PROMPT)

    def test_contains_max_pages(self):
        """Prompt contains the max_pages placeholder."""
        self.assertIn("{max_pages}", _RETRIEVER_PROMPT)

    def test_contains_catalog_placeholder(self):
        """Prompt contains the catalog placeholder."""
        self.assertIn("{catalog}", _RETRIEVER_PROMPT)

    def test_contains_injected_placeholder(self):
        """Prompt contains the injected pages placeholder."""
        self.assertIn("{injected}", _RETRIEVER_PROMPT)

    def test_contains_json_output_format(self):
        """Prompt specifies JSON output format with classification and pages."""
        self.assertIn("classification", _RETRIEVER_PROMPT)
        self.assertIn("pages", _RETRIEVER_PROMPT)
        self.assertIn("JSON", _RETRIEVER_PROMPT)

    def test_does_not_mention_search_files(self):
        """Prompt does NOT mention search_files (no agent tools)."""
        self.assertNotIn("search_files", _RETRIEVER_PROMPT)

    def test_does_not_mention_read_file(self):
        """Prompt does NOT mention read_file (no agent tools)."""
        self.assertNotIn("read_file", _RETRIEVER_PROMPT)

    def test_does_not_mention_aiaagent(self):
        """Prompt does NOT mention AIAgent (single LLM call, no agent loop)."""
        self.assertNotIn("AIAgent", _RETRIEVER_PROMPT)
        self.assertNotIn("agent loop", _RETRIEVER_PROMPT.lower())


if __name__ == "__main__":
    unittest.main()