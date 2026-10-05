"""Store-jail for the saver's terminal: a destructive verb naming a path outside the
store is blocked; nothing else is inspected.

A guard, not a sandbox — it stops the common slip (an `rm`/`mv`/`cp`/redirect aimed
outside the store) and nothing more. Relative paths, git targets, `cd` state, and
in-process mutations (`python -c`, `find -delete`) all fail open. The rule is
deliberately shape-independent: it does not grow to cover every command the saver
runs. Reads/executions stay open (the saver inspects project repos for anchors).
Scope is per-thread: `arm()` confines the arming thread (the saver's).
"""

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Temp dirs a saver may legitimately write to (POSIX /tmp, macOS /private/tmp,
# Windows %TEMP%); the plugin's own scratch lives here too.
SCRATCH: Tuple[str, ...] = tuple(dict.fromkeys(
    p for p in (os.environ.get("TMPDIR", ""), tempfile.gettempdir(), "/tmp", "/private/tmp")
    if p))

# Verbs that can destroy or overwrite; `sed` joins only with -i. All else is a read.
DESTRUCTIVE = frozenset((
    "rm", "rmdir", "unlink", "shred", "mv", "cp", "truncate", "dd", "tee", "install",
    "rsync", "mkdir", "touch", "ln", "chmod", "chown", "kill", "pkill", "tar", "curl", "wget",
))
SPLIT = re.compile(r"&&|\|\||;|\|")


class _Jail(threading.local):
    """Per-thread confine roots; ``None`` means 'not armed here'."""

    roots: Optional[List[str]] = None


_jail = _Jail()


def arm(stores, scratch=(), **_ignored) -> None:
    """Confine THIS thread: destructive paths must stay inside *stores* or *scratch*."""
    _jail.roots = [str(p) for p in stores] + [str(p) for p in scratch]


def disarm() -> None:
    """Stop confining THIS thread (no-op on the common unarmed path)."""
    _jail.roots = None


def check_terminal(tool_name: str = "", args: Any = None, **_: Any) -> Optional[Dict[str, str]]:
    """``pre_tool_call`` body: block a destructive command that names a path outside the store."""
    why = None
    if _jail.roots is not None and tool_name == "terminal" and isinstance(args, dict):
        why = _outside(args.get("command"), _jail.roots)
    if not why:
        return None
    return {"action": "block", "message": (
        f"Saver terminal blocked: {why}. Mutations must stay inside the memory store "
        f"({', '.join(_jail.roots)}) — git history is the undo. Reads are unaffected.")}


def _outside(cmd: Any, roots: List[str]) -> Optional[str]:
    """A destructive verb (or any ``>``/``>>`` redirect) naming a `~`/`/`-absolute token
    outside *roots* -> a reason. Relative paths and unknown verbs fail open."""
    for chunk in SPLIT.split(cmd if isinstance(cmd, str) else ""):
        parts = chunk.split()
        if not parts:
            continue
        verb = Path(parts[0]).name.lower()
        write = verb in DESTRUCTIVE or (verb == "sed" and any(a.startswith("-i") for a in parts))
        targets = ([*parts[1:]] if write else []) + [
            parts[j + 1] for j, t in enumerate(parts[:-1]) if t in (">", ">>")]
        for tok in targets:
            tok = tok.strip("'\"")
            if tok.startswith(("~", "/")) and "://" not in tok:
                path = Path.home() / tok[2:] if tok.startswith("~") else Path(tok)
                if not _inside(path, roots):
                    return f"{tok} is outside the store"
    return None


def _inside(path: Path, roots: List[str]) -> bool:
    return any(str(path) == b or _rel(path, b) for b in roots)


def _rel(path: Path, base: str) -> bool:
    try:
        return path.resolve().is_relative_to(Path(base).resolve())
    except Exception:
        return False
