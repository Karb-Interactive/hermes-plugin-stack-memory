"""Verify the fresh-profile and explicit-store paths, end to end, with real objects.

Not a unit test of a helper — this drives the provider's own resolution and
bootstrap against temp HERMES_HOMEs, which is the behaviour a new profile hits.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Derived from this file's location, never a hardcoded home.
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from stack import StackMemoryProvider  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="stack-profile-check-"))
try:
    # ---- 1. a fresh profile resolves to its OWN profile-local store ----------
    pA = tmp / "profileA"
    pA.mkdir()
    prov = StackMemoryProvider.__new__(StackMemoryProvider)
    repoA = prov._resolve_repo(str(pA))
    check("fresh profile -> profile-local default",
          repoA == pA / "memories" / "stack", str(repoA))

    # ---- 2. a second profile resolves somewhere different -------------------
    pB = tmp / "profileB"
    pB.mkdir()
    repoB = prov._resolve_repo(str(pB))
    check("second profile -> a different store", repoA != repoB, str(repoB))

    # ---- 3. an explicit repo_path overrides the default (sharing) -----------
    pC = tmp / "profileC"
    pC.mkdir()
    shared = tmp / "shared-store"
    (pC / "stack.json").write_text('{"repo_path": "%s"}' % shared)
    repoC = prov._resolve_repo(str(pC))
    check("explicit stack.json.repo_path wins", repoC == shared, str(repoC))

    # ---- 4. an inherited STACK_REPO does NOT hijack a real profile ---------
    os.environ["STACK_REPO"] = str(tmp / "HIJACK")
    repoA2 = prov._resolve_repo(str(pA))
    check("inherited STACK_REPO is ignored for a real profile",
          repoA2 == pA / "memories" / "stack", str(repoA2))
    del os.environ["STACK_REPO"]

    # ---- 5. the provider bootstraps a missing store at initialize ----------
    prov2 = StackMemoryProvider.__new__(StackMemoryProvider)
    prov2._repo = repoA
    prov2._notify = None
    prov2._run_stack = lambda sub, timeout: subprocess.run(
        ["uv", "run", "--no-project", str(PLUGIN_DIR / "engine.py"),
         "--repo", str(prov2._repo), *sub],
        capture_output=True, text=True, timeout=timeout).stdout
    prov2._ensure_store()
    check("missing store is created on initialize", (repoA / "wiki").is_dir(), str(repoA))
    check("the created store has a manifest", (repoA / ".stack" / "manifest.json").exists())
    check("the created store is a git repo", (repoA / ".git").is_dir())
    check("the created store contains no runtime",
          not (repoA / "bin").exists(), "no bin/ directory")

    # ---- 6. an existing store is left alone --------------------------------
    before = sorted(p.name for p in repoA.iterdir())
    prov2._ensure_store()
    after = sorted(p.name for p in repoA.iterdir())
    check("re-initialising an existing store changes nothing", before == after)

    # ---- 7. a non-empty unrelated dir is not clobbered ---------------------
    pD = tmp / "has-content"
    pD.mkdir()
    (pD / "important.txt").write_text("do not touch")
    out = subprocess.run(
        ["uv", "run", "--no-project", str(PLUGIN_DIR / "engine.py"),
         "--repo", str(pD), "init"], capture_output=True, text=True).stdout
    check("init refuses a non-empty unrelated directory",
          "refusing" in out and (pD / "important.txt").read_text() == "do not touch")

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
