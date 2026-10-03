import logging
from typing import List, Dict, Optional

from .common import call_llm, format_turns

logger = logging.getLogger(__name__)

# Target summary size (chars)
_SUMMARY_MIN = 2000
_SUMMARY_MAX = 5000

_SUMMARY_PROMPT = """\
Compress the following into a compact operational summary (~2-5K chars).

This summary is operational context for a knowledge-curation agent (the "saver").
It helps the saver distinguish durable knowledge from session chatter:
  - "this is a <project> session about rendering" → a remark about env vars is a real preference
  - "this is a C++ session in <project>" → a remark about a compiler flag is a real convention

Rules:
  1. Capture: what project/session is this, what was discussed, what was decided, what was saved
  2. Drop: transient details, tool outputs, error traces, code snippets
  3. Keep it under 5000 chars — this is context, not a transcript
  4. Never include content that should be in the wiki — this is operational, not memory
  5. If the previous summary + delta are empty, return "No context yet."

Previous summary:
{prev_summary}

Delta just processed (raw turns):
{delta_text}
"""


class Summarizer:
    """Rolling context summarizer for the saver agent.
    
    Runs after each saver run. Produces a compact summary that helps the next
    saver run understand the session context. NEVER written to the wiki.
    Uses a cheap model via PluginLlm.complete_structured (no tools, no agent loop).
    """
    
    def __init__(
        self,
        provider: str = None,
        model: str = None,
    ):
        self._provider = provider
        self._model = model
    
    def update(
        self,
        prev_summary: str,
        delta_turns: List[Dict[str, str]],
    ) -> str:
        """Produce a new rolling summary from the previous summary + delta.
        
        Args:
            prev_summary: The previous rolling summary ("" on first run)
            delta_turns: The raw turns that were just processed by the saver
            
        Returns: New compact summary string (~2-5K). Never raises.
        """
        if not delta_turns and not prev_summary:
            return ""
        
        # Format the delta
        delta_text = format_turns(delta_turns, max_chars=500)
        
        prompt = _SUMMARY_PROMPT.format(
            prev_summary=prev_summary or "(none — first run)",
            delta_text=delta_text[:4000],  # cap input
        )
        
        try:
            result = call_llm(prompt, self._provider, self._model)
            if result and len(result) > _SUMMARY_MAX:
                result = result[:_SUMMARY_MAX]
            return result or prev_summary  # fall back to prev on empty result
        except Exception as e:
            logger.warning("Summarizer LLM call failed: %s", e)
            return prev_summary  # never break the saver pipeline