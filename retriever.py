"""Per-turn memory retrieval via a single PluginLlm call.

One LLM call: catalog + user message → classification (CASUAL/SYSTEM/WORK) +
the relevant page paths. No agent loop, no tools, no search_files. ~2-3s on a
fast model. Pattern: Claude Code's "determine which memory files to attach" step.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .common import call_llm, parse_path_response
from .prompts import RETRIEVER as _RETRIEVER_PROMPT

logger = logging.getLogger(__name__)

# Maximum pages to return
_MAX_PAGES = 3


class Retriever:
    """Single-call memory retrieval via PluginLlm.
    
    One LLM call: catalog + user message → classification + relevant paths.
    No agent loop, no tools, no search_files. ~2-3s on a fast model.
    
    Pattern: Claude Code's "Determine which memory files to attach" subagent.
    """
    
    def __init__(
        self,
        wiki_path: Path,
        catalog: str,
        task: Optional[str] = None,
    ):
        self._wiki_path = wiki_path
        self._catalog = catalog
        self._task = task
    
    def retrieve(
        self,
        user_message: str,
        recently_injected: Set[str] = None,
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Classify the message and pick relevant pages from the catalog.
        
        Args:
            user_message: The current user message
            recently_injected: Set of page paths already injected in recent turns
            
        Returns: (paths, trace) where trace is a list with one entry:
                 {"type": "llm_call", "prompt": ..., "response": ...}
        Never raises — returns ([], []) on any failure.
        """
        recently_injected = recently_injected or set()
        
        # Check PluginLlm availability (preserves early-return behavior)
        try:
            from agent.plugin_llm import PluginLlm  # noqa: F401
        except ImportError:
            logger.debug("PluginLlm not available (running outside Hermes)")
            return [], []
        
        prompt = _RETRIEVER_PROMPT.format(
            catalog=self._catalog,
            injected=", ".join(sorted(recently_injected)) if recently_injected else "(none)",
            max_pages=_MAX_PAGES,
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_message},
        ]
        
        try:
            response_text = call_llm(messages=messages, task=self._task)
            paths = parse_path_response(response_text, max_pages=_MAX_PAGES)
            
            # Filter out recently injected
            paths = [p for p in paths if p not in recently_injected]
            
            trace = [{"type": "llm_call", "prompt": prompt[:500], "response": str(response_text)[:500]}]
            
            return paths[:_MAX_PAGES], trace
        except Exception as e:
            logger.warning("Retriever LLM call failed: %s", e)
            return [], [{"type": "error", "error": str(e)[:200]}]