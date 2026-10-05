"""Shared helpers used by the retriever, saver, and summarizer."""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def format_turns(turns: List[Dict[str, str]], max_chars: int = 2000) -> str:
    """Format conversation turns for inclusion in an LLM prompt.

    Used by Saver and Summarizer.
    """
    lines = []
    for i, t in enumerate(turns, 1):
        role = t.get("role", "?")
        content = t.get("content", "")
        if len(content) > max_chars:
            content = content[:max_chars] + "…(truncated)"
        lines.append(f"--- Turn {i} [{role}] ---\n{content}")
    return "\n\n".join(lines)


def call_llm(
    prompt: str = "",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    messages: Optional[List[Dict[str, str]]] = None,
    task: Optional[str] = None,
) -> str:
    """Make a single PluginLlm call. Returns response text or empty string.

    Used by Retriever and Summarizer.

    If *messages* is provided, it is sent as-is (system + user format).
    Otherwise, *prompt* is sent as a single user message.

    *task* routes provider/model through the plugin-registered ``auxiliary.<task>``
    slot (no override flags needed); explicit *provider*/*model* still win if set.

    Returns "" if PluginLlm is not importable (running outside Hermes).
    Raises on LLM call failure — callers catch as needed.
    """
    try:
        from agent.plugin_llm import PluginLlm
    except ImportError:
        logger.debug("PluginLlm not available (running outside Hermes)")
        return ""

    llm = PluginLlm(plugin_id="stack")
    if messages is None:
        messages = [{"role": "user", "content": prompt}]
    kwargs = {}
    if task:
        kwargs["task"] = task
    if provider:
        kwargs["provider"] = provider
    if model:
        kwargs["model"] = model
    result = llm.complete(messages, **kwargs)
    return getattr(result, "text", "") or str(result) if result else ""


def parse_path_response(text: str, max_pages: int = 3) -> List[str]:
    """Parse LLM response for wiki page paths.

    Accepts JSON {"pages": [...]} or plain text with paths. Used by the Retriever.
    """
    import re

    if not text or not text.strip():
        return []

    # Try JSON first
    try:
        json_match = re.search(r'\{[^}]*"pages"[^}]*\}', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            pages = data.get("pages", [])
            if isinstance(pages, list):
                return [p for p in pages if isinstance(p, str) and p.endswith(".md")][:max_pages]
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: regex
    path_pattern = re.compile(r'(?:wiki/)?[a-z0-9_/-]+\.md', re.IGNORECASE)
    matches = path_pattern.findall(text)
    seen = set()
    result = []
    for m in matches:
        path = m if m.startswith("wiki/") else f"wiki/{m}"
        if path not in seen:
            seen.add(path)
            result.append(path)
            if len(result) >= max_pages:
                break
    return result