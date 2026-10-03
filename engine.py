#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""engine — the harness-neutral core for the memory/wiki system.

This is the ONE maintained runtime. A memory store contains only data, Git
history, local config, and a small ``.stack/`` format marker -- never a copy
of this file.

The store is selected explicitly with a top-level ``--repo PATH``; the engine
never infers the store from its own location, and knows nothing about Hermes
profiles. The Hermes adapter resolves the store and passes it.

  engine.py --repo PATH resolve [cwd]     cwd -> (org, project) + page set
  engine.py --repo PATH verify <page.md>  provenance anchors -> verified|drifted|unknown
  engine.py --repo PATH load   [cwd]      now + org index + project pages + status
  engine.py --repo PATH check  [cwd]      drift check across the page set
  engine.py --repo PATH create --name ..  write a page; stages, does NOT commit
  engine.py --repo PATH doctor            health: config, git state, submodules
  engine.py --repo PATH init              create a minimal store at an absent path

Trust = verifiability. Anchors are file or file::entity, NEVER line numbers.
"unknown" (missing/moved/renamed/unreadable source) is LOUD, never downgraded.
"""
from __future__ import annotations
import sys, os, json, re, shutil, subprocess
from pathlib import Path
import yaml

# The store format this engine writes. A store whose manifest declares a
# different value is not something this engine should silently operate on.
FORMAT_VERSION = 1

# Set by main() from --repo. Every command requires an explicit store path.
STACK_ROOT: Path = None  # type: ignore[assignment]
WIKI: Path = None        # type: ignore[assignment]


def cfg() -> dict:
    """Load config.json. Expands ~ and $HOME in path values so the file
    is portable across machines with different usernames / layouts."""
    raw = json.loads((STACK_ROOT / "config.json").read_text())
    # Expand user-env paths in org/project paths
    for org, ocfg in raw.get("orgs", {}).items():
        if "path" in ocfg:
            ocfg["path"] = os.path.expanduser(os.path.expandvars(ocfg["path"]))
        for proj, pcfg in ocfg.get("projects", {}).items():
            if "path" in pcfg:
                pcfg["path"] = os.path.expanduser(os.path.expandvars(pcfg["path"]))
    if "memory_inbox_glob" in raw:
        raw["memory_inbox_glob"] = os.path.expanduser(os.path.expandvars(raw["memory_inbox_glob"]))
    return raw


def _check_config() -> tuple[bool, str]:
    """Verify config.json exists and has at least one org with a project.
    Returns (ok, message). Called at startup to warn before silent degradation."""
    cfg_path = STACK_ROOT / "config.json"
    if not cfg_path.exists():
        return False, (
            f"config.json NOT FOUND at {cfg_path}\n"
            f"  Project-scoped memory is OFF — only base scope (now/preferences/user) will load.\n"
            f"  Create it from config.example.json (copy + edit paths for this machine)."
        )
    try:
        c = cfg()
    except Exception as e:
        return False, f"config.json is invalid: {e}"
    orgs = c.get("orgs", {})
    if not orgs:
        return False, "config.json has no orgs — project-scoped memory is OFF."
    total_projects = sum(len(o.get("projects", {})) for o in orgs.values())
    if not total_projects:
        return False, "config.json has orgs but no projects — project-scoped memory is OFF."
    # Check that at least one project path exists on disk
    missing = []
    for org, ocfg in orgs.items():
        for proj, pcfg in ocfg.get("projects", {}).items():
            p = pcfg.get("path", "")
            if p and not os.path.isdir(p):
                missing.append(f"  {org}/{proj}: {p}")
    if missing:
        return True, (
            f"config.json loaded ({len(orgs)} orgs, {total_projects} projects)\n"
            f"  ⚠ {len(missing)} project path(s) not found on disk:\n"
            + "\n".join(missing)
            + "\n  Those projects won't load until the repos are cloned there."
        )
    return True, f"config.json OK — {len(orgs)} orgs, {total_projects} projects, all paths exist."


# ---------------------------------------------------------------- resolve ----
def resolve(cwd: str):
    """Map a working directory to (org, project). Bridges on-disk vs memory-key
    names via aliases; matches subdirs and worktrees by prefix or path component.
    Returns (None, None) when config.json is missing — base scope only."""
    real = os.path.realpath(cwd)
    parts = set(real.split(os.sep))
    try:
        c = cfg()
    except Exception:
        return None, None
    # 1) strongest: cwd is the project dir or inside it
    for org, ocfg in c["orgs"].items():
        for proj, pcfg in ocfg.get("projects", {}).items():
            ppath = os.path.realpath(pcfg["path"])
            if real == ppath or real.startswith(ppath + os.sep):
                return org, proj
    # 2) worktree / alias: org name + a project alias both appear as path components
    for org, ocfg in c["orgs"].items():
        if org not in parts:
            continue
        for proj, pcfg in ocfg.get("projects", {}).items():
            aliases = set(pcfg.get("aliases", [])) | {proj}
            if aliases & parts:
                return org, proj
    return None, None


def page_set(org: str, proj: str) -> list[Path]:
    """The pages /stack load surfaces, in order: now, preferences, org index,
    then the project's pages."""
    pages = []
    # now + personal preferences + user profile (global) · then the org's index + shared conventions.
    # conventions.md is the org-scoped, shareable preference layer (lives inside the
    # shared org subtree, e.g. a submodule); personal preferences.md layers on top of it
    # at use time (personal wins). Both guarded by exists() so the page set is forward-
    # compatible before the file lands.
    for p in (WIKI / "now.md", WIKI / "preferences.md", WIKI / "user.md",
              WIKI / org / "index.md", WIKI / org / "conventions.md"):
        if p.exists():
            pages.append(p)
    # Open shared discussions are the agent-to-agent inbox. Surface their catalog entries on
    # every project load for the org so a proposal stays visible instead of hiding in a folder
    # nobody lists. Visibility only — nothing schedules the reconciliation; acting on a surfaced
    # entry is the agent's contract (SCHEMA §1.1). Content still loads on demand only.
    discussions = WIKI / org / "discussions"
    if discussions.is_dir():
        for p in sorted(discussions.glob("*.md")):
            if p.name == "TEMPLATE.md":
                continue
            fm = parse_frontmatter(p.read_text(errors="ignore"))
            if fm.get("status", "open") == "open":
                pages.append(p)
    pdir = WIKI / org / proj
    if pdir.is_dir():
        pages += sorted(pdir.rglob("*.md"))   # incl. decisions/ and other subdirs
    # Private decisions/notes for a SHARED org live in a sibling `<org>-private/<proj>/`
    # that stays in the personal outer repo (never in the shared submodule) — so a page
    # too personal to share (e.g. a wind-down plan) still auto-loads for the owner.
    ppriv = WIKI / f"{org}-private" / proj
    if ppriv.is_dir():
        pages += sorted(ppriv.rglob("*.md"))
    return pages


# ----------------------------------------------------------- frontmatter ----
def parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter into a flat dict, tolerant of BOTH page shapes the system
    produces:
      - flat — fields at the top level (shell/editor/agent-written per SCHEMA §2);
      - nested — the same fields under a `metadata:` block, which is what the harness'
        Write-tool / autoMemory path emits (it also injects node_type/originSessionId,
        and nests the `sources:` list inside `metadata:` too).
    Children of `metadata:` are promoted to the top level (via setdefault, so an explicit
    top-level key always wins), so callers read `fm['kind']`, `fm['synced_at']`,
    `fm['sources']`, … no matter which path wrote the page. Uses PyYAML (declared as an
    inline script dependency) instead of a hand parser — the harness' real output nests
    lists under `metadata:`, which the earlier minimal parser silently mis-read.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        fm = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    if not isinstance(fm, dict):
        return {}
    meta = fm.get("metadata")
    if isinstance(meta, dict):
        for k, v in meta.items():
            fm.setdefault(k, v)
    # YAML reads an unquoted ISO date (`synced_at: 2026-05-31`) as a datetime.date; the
    # drift check compares it to git's --date=short *string*, so keep synced_at a string.
    if fm.get("synced_at") is not None:
        fm["synced_at"] = str(fm["synced_at"])
    return fm


def body_of(path):
    """Page text minus the YAML frontmatter block."""
    t = path.read_text(errors="ignore")
    if t.startswith("---"):
        e = t.find("\n---", 3)
        if e != -1:
            return t[e + 4:].lstrip("\n")
    return t


# ---------------------------------------------------------------- verify -----
def _repo_path(repo: str) -> Path | None:
    """'<org>/<project>' -> absolute repo path from config."""
    c = cfg()
    if "/" in repo:
        org, proj = repo.split("/", 1)
        pcfg = c["orgs"].get(org, {}).get("projects", {}).get(proj)
        if pcfg:
            return Path(pcfg["path"])
    return None


def _git_last_change(repo: Path, relfile: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%cd", "--date=short", "--", relfile],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _entity_present(path: Path, entity: str) -> bool:
    try:
        blob = path.read_text(errors="ignore")
    except Exception:
        return False
    if entity in blob:
        return True
    if "::" in entity:                       # Class::method -> try the method name
        return entity.split("::")[-1] in blob
    return False


def verify_page(page: Path) -> dict:
    """Return {status, anchors:[{file,entity,state,note}]} for a page.
    status = unknown (any anchor unverifiable) > drifted (any changed) > verified."""
    fm = parse_frontmatter(page.read_text(errors="ignore"))
    synced = fm.get("synced_at")
    sources = fm.get("sources", [])
    if fm.get("kind") in ("preference", "learning") or not sources:
        return {"status": "n/a", "anchors": []}        # preference sacred; learning has no decay tracking — unverified
    anchors, worst = [], "verified"
    for s in sources:
        repo, rel, ent = s.get("repo", ""), s.get("file", ""), s.get("entity")
        rp = _repo_path(repo)
        rec = {"file": f"{repo}/{rel}", "entity": ent, "state": "verified", "note": ""}
        if not rp or not (rp / rel).exists():
            rec.update(state="unknown", note="file missing/moved")
        elif ent and not _entity_present(rp / rel, ent):
            rec.update(state="unknown", note=f"entity '{ent}' not found (renamed?)")
        else:
            changed = _git_last_change(rp, rel)
            if changed and synced and changed > synced:
                rec.update(state="drifted", note=f"changed {changed} > synced {synced}")
        anchors.append(rec)
        if rec["state"] == "unknown":
            worst = "unknown"
        elif rec["state"] == "drifted" and worst != "unknown":
            worst = "drifted"
    return {"status": worst, "anchors": anchors}


# ------------------------------------------- shared-layer health (submodule) -----
def _gitmodule_orgs() -> set:
    """Orgs whose wiki/<org> subtree is a git submodule (the shared layer), read
    from .gitmodules. Empty set when there are no submodules."""
    gm = STACK_ROOT / ".gitmodules"
    if not gm.exists():
        return set()
    return set(re.findall(r"path\s*=\s*wiki/([^/\s]+)", gm.read_text()))


def _git_out(args, cwd=None):
    """Run git and return stripped stdout, or None when it fails.

    `cwd` defaults to the store *at call time*, not at def time: STACK_ROOT is
    assigned by main() from --repo, so a default of `cwd=STACK_ROOT` would freeze
    the pre-init `None` and every call would run `git -C None`.
    """
    target = STACK_ROOT if cwd is None else cwd
    try:
        r = subprocess.run(["git", "-C", str(target), *args],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _is_ancestor(a: str, b: str, cwd) -> bool:
    try:
        return subprocess.run(["git", "-C", str(cwd), "merge-base", "--is-ancestor", a, b],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def shared_layer_status(org: str) -> dict | None:
    """Health of a shared submodule org — None when `org` has no shared submodule.
    Offline only (NEVER fetches, so it's safe in the SessionStart hook): detects an
    uninitialized/empty submodule, and a working tree that is BEHIND (stale) or AHEAD
    of the commit pinned in the outer repo's HEAD. This is the LOUD signal for a
    MISSING/STALE shared layer that page_set()'s exists() guard would otherwise skip
    in silence — same 'unknown is LOUD' rule the drift check already follows."""
    if org not in _gitmodule_orgs():
        return None
    sub = WIKI / org
    if not sub.exists() or not any(sub.glob("*.md")):
        return {"state": "missing",
                "msg": f"shared '{org}' wiki is NOT initialized — its pages are absent",
                "fix": f"git -C {STACK_ROOT} submodule update --init wiki/{org}"}
    recorded = _git_out(["rev-parse", f"HEAD:wiki/{org}"])   # commit pinned in outer HEAD
    actual = _git_out(["rev-parse", "HEAD"], cwd=sub)        # submodule working-tree HEAD
    if recorded and actual and recorded != actual:
        if _is_ancestor(actual, recorded, sub):
            n = _git_out(["rev-list", "--count", f"{actual}..{recorded}"], cwd=sub) or "?"
            return {"state": "stale",
                    "msg": f"shared '{org}' wiki is {n} commit(s) BEHIND the pinned pointer",
                    "fix": f"git -C {STACK_ROOT} submodule update wiki/{org}"}
        if _is_ancestor(recorded, actual, sub):
            n = _git_out(["rev-list", "--count", f"{recorded}..{actual}"], cwd=sub) or "?"
            return {"state": "ahead",
                    "msg": f"shared '{org}' wiki has {n} local commit(s) ahead of the recorded pointer",
                    "fix": f"bump the pointer in {STACK_ROOT} (git add wiki/{org} && commit), then push wiki/{org}"}
        return {"state": "diverged",
                "msg": f"shared '{org}' wiki has DIVERGED from the pinned pointer",
                "fix": f"reconcile wiki/{org} against the pointer recorded in {STACK_ROOT}"}
    return {"state": "ok", "msg": "", "fix": ""}


# ------------------------------------------------------------------ cmds -----
SIGIL = {"verified": "✓", "drifted": "~", "unknown": "⚠", "n/a": "·"}


def cmd_resolve(argv):
    org, proj = resolve(argv[0] if argv else os.getcwd())
    if not org:
        print("resolve: no org/project for this cwd")
        return 1
    print(f"org={org} project={proj}")
    for p in page_set(org, proj):
        print(f"  {p.relative_to(STACK_ROOT)}")
    return 0


def cmd_verify(argv):
    if not argv:
        print("usage: stack.py verify <page.md>")
        return 2
    r = verify_page(Path(argv[0]))
    print(f"status: {r['status']}")
    for a in r["anchors"]:
        print(f"  {SIGIL.get(a['state'],'?')} {a['state']:8} {a['file']}"
              + (f"::{a['entity']}" if a.get('entity') else "")
              + (f"  — {a['note']}" if a['note'] else ""))
    return 0


def cmd_load(argv):
    hook = "--hook" in argv
    pos = [a for a in argv if not a.startswith("--")]
    cwd = pos[0] if pos else os.getcwd()
    org, proj = resolve(cwd)

    out = []
    # Config health check — warn before silent degradation to base-only scope
    ok, msg = _check_config()
    if not ok:
        out.append(f"## ⚠ CONFIG — {msg}")
        out.append("")
    elif "⚠" in msg:
        # Config loaded but some project paths missing on disk
        out.append(f"## · CONFIG — {msg}")
        out.append("")
    if org:
        out.append(f"# stack memory: {org}/{proj}")
        # LOUD: shared submodule health
        sls = shared_layer_status(org)
        if sls and sls["state"] in ("missing", "stale", "diverged"):
            out += [f"## ⚠ SHARED LAYER {sls['state'].upper()} — {sls['msg']}",
                    f"   the shared '{org}' pages below may be ABSENT or OUT OF DATE until you:",
                    f"   {sls['fix']}", ""]
        elif sls and sls["state"] == "ahead":
            out += [f"## · shared '{org}' wiki: {sls['msg']} — {sls['fix']}", ""]
    else:
        out.append("# stack memory")

    # Base: always now + user + index
    now = WIKI / "now.md"
    if now.exists():
        out += ["## Now — active threads (already loaded):", body_of(now).rstrip(), ""]
    if org:
        pnow = WIKI / org / proj / "now.md"
        if pnow.exists():
            out += [f"## Now — {proj} focus (already loaded):", body_of(pnow).rstrip(), ""]
    for name in ("user.md", "index.md"):
        p = WIKI / name
        if p.exists():
            out += [f"## {p.stem.title()} (already loaded):", body_of(p).rstrip(), ""]

    # Project pages (only when cwd resolves)
    if org:
        out.append("## Pages — open ONE only when your task touches it "
                   "(don't preload all, don't re-read, don't re-explore the code):")
        drift = []
        inboxed = False
        for p in page_set(org, proj):
            if p.name == "now.md":
                continue
            fm = parse_frontmatter(p.read_text(errors="ignore"))
            rel = p.relative_to(STACK_ROOT)
            sig = "·"
            if str(p).startswith(str(WIKI / org / proj) + os.sep) and fm.get("sources"):
                v = verify_page(p)
                sig = SIGIL.get(v["status"], "·")
                drift += [(rel, a) for a in v["anchors"] if a["state"] in ("drifted", "unknown")]
            summary = fm.get("description") or fm.get("title") or ""
            target = fm.get("target_author")
            inbox = ""
            if "discussions" in p.parts:
                inboxed = True
                inbox = f" [open → {target}]" if target else " [open]"
            out.append(f"  {sig} {rel}{inbox} — {summary}")
        if inboxed:
            out.append("\n[open …] = an OPEN shared discussion (agent-to-agent inbox). If one is"
                       " addressed to you, or states objective code truth you can verify, and its"
                       " topic touches your task: reconcile it (verify → apply or append why not"
                       " → mark resolved → commit+push). See SCHEMA §1.1.")
        if drift:
            out.append("\n## Recheck — cited anchors that moved; verify these claims before trusting them:")
            for rel, a in drift:
                out.append(f"  {SIGIL[a['state']]} {a['file']}"
                           + (f"::{a['entity']}" if a.get('entity') else "") + f"  ({a['note']})  in {rel}")

    out.append("\nHonor preferences (sacred). These pages ARE your memory — drill in on "
               "demand; don't re-derive what's already here.")
    text = "\n".join(out)
    if hook:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": text}}))
    else:
        print(text)
    return 0


# Wikilinks that point at a skill/command rather than a wiki page, so `check`
# must not report them as dangling. Generic names only — a store adds its own
# through config.json's "skill_refs", because they are data about that store.
SKILL_REFS = {"update-config", "stacked-pr", "teaching-session",
              "stack-ingest", "html-report", "stack-consolidate"}


def _config_list(key):
    """A list-valued config key, or []. Config is the store's data, not code."""
    try:
        val = cfg().get(key)
    except Exception:
        return []
    return [str(v) for v in val] if isinstance(val, list) else []


def _entry_names():
    """Page stems that are entry points, so they are never reported as orphans.

    The structural names are the format's; a store's org/project names are its own
    data, so they come from config.json rather than being hardcoded here.
    """
    names = {"index", "now", "preferences", "user", "conventions",
             "MEMORY", "README", "TEMPLATE"}
    for org, ocfg in (cfg().get("orgs", {}) or {}).items():
        names.add(org)
        names.update((ocfg.get("projects", {}) or {}).keys())
    names.update(_config_list("entry_pages"))
    return names


def _skill_refs():
    """Skill/command wikilink targets: generic defaults plus the store's own."""
    return SKILL_REFS | set(_config_list("skill_refs"))


def _wikilinks(text):
    return set(re.findall(r"\[\[([^\]|#]+)", text))


def _page_map():
    m = {}
    for p in WIKI.rglob("*.md"):
        if "_inbox_archive" in p.parts:
            continue
        m[p.stem] = p
        nm = parse_frontmatter(p.read_text(errors="ignore")).get("name")
        if nm:
            m[nm] = p
    return m


def cmd_check(argv):
    """Read-only health report: verify all anchored pages, dangling links, orphans,
    index coverage. Mechanical + additive — reports, never deletes."""
    pages = [p for p in WIKI.rglob("*.md") if "_inbox_archive" not in p.parts]
    name_map = _page_map()
    inbound, dangling = {}, set()

    print("## verify (anchored pages)")
    counts = {}
    for p in sorted(pages):
        fm = parse_frontmatter(p.read_text(errors="ignore"))
        if fm.get("kind") in ("architecture", "source", "decision") and fm.get("sources"):
            v = verify_page(p)
            counts[v["status"]] = counts.get(v["status"], 0) + 1
            if v["status"] != "verified":
                print(f"  {SIGIL.get(v['status'],'?')} {p.relative_to(STACK_ROOT)} [{v['status']}]")
    print("  totals: " + (", ".join(f"{k}={n}" for k, n in sorted(counts.items())) or "none"))

    for p in pages:
        for ln in _wikilinks(p.read_text(errors="ignore")):
            ln = ln.strip()
            inbound.setdefault(ln, set()).add(p.stem)
            if ln not in name_map and ln not in _skill_refs():
                dangling.add((ln, str(p.relative_to(STACK_ROOT))))

    print("## dangling links (excl. skill/command refs)")
    print("\n".join(f"  ⚠ [[{ln}]]  in {src}" for ln, src in sorted(dangling)) or "  none")

    print("## orphan pages (no inbound [[links]])")
    ENTRY = _entry_names()
    orphans = []
    for p in pages:
        if p.stem in ENTRY:
            continue
        keys = {p.stem, parse_frontmatter(p.read_text(errors="ignore")).get("name")}
        if not (keys & set(inbound)):
            orphans.append(str(p.relative_to(STACK_ROOT)))
    print("\n".join(f"  · {o}" for o in sorted(orphans)) or "  none")

    print("## index.md coverage")
    idx = WIKI / "index.md"
    idx_links = _wikilinks(idx.read_text(errors="ignore")) if idx.exists() else set()
    missing = []
    for p in pages:
        if (p.stem in ENTRY or p.parent.name in ("preferences", "user", "conventions")
                or "sources" in p.parts):
            continue  # per-node preference/conventions files are cataloged in their digest,
                      # and sources in wiki/sources/index.md — not the main index
        keys = {p.stem, parse_frontmatter(p.read_text(errors="ignore")).get("name")}
        if not (keys & idx_links):
            missing.append(p.stem)
    print("  not in index: " + (", ".join(sorted(set(missing))) or "none"))

    print("## shared layer (submodules)")
    sorgs = sorted(_gitmodule_orgs())
    if not sorgs:
        print("  none")
    for org in sorgs:
        s = shared_layer_status(org) or {"state": "n/a", "msg": ""}
        sig = "✓" if s["state"] == "ok" else ("·" if s["state"] == "ahead" else "⚠")
        print(f"  {sig} {org}: {s['state']}" + (f" — {s['msg']}" if s.get("msg") else ""))

    # Open shared discussions age silently otherwise: load surfaces them but nothing measures
    # how long they've sat. Report age so a stale inbox is a visible finding, not a surprise.
    print("## open shared discussions (agent-to-agent inbox)")
    import datetime as _dt
    today = _dt.date.today()
    rows = []
    for org in sorgs:
        ddir = WIKI / org / "discussions"
        if not ddir.is_dir():
            continue
        for p in sorted(ddir.glob("*.md")):
            if p.name == "TEMPLATE.md":
                continue
            fm = parse_frontmatter(p.read_text(errors="ignore"))
            if fm.get("status", "open") != "open":
                continue
            d = fm.get("date")
            if isinstance(d, _dt.date):
                age = f"{(today - d).days}d"
            else:
                try:
                    age = f"{(today - _dt.date.fromisoformat(str(d))).days}d"
                except (TypeError, ValueError):
                    age = "?"
            target = fm.get("target_author")
            rows.append(f"  · {age:>5}  {p.relative_to(STACK_ROOT)}"
                        + (f"  [→ {target}]" if target else ""))
    print("\n".join(rows) or "  none")
    return 0


def cmd_create(argv):
    """Save a page to the wiki with correct frontmatter, routing, and git commit.

    Usage:
      stack.py create --name <slug> --description <text> [--content <text|@file>]
                    [--kind note|reference|decision|architecture|preference|user|learning]
                    [--dir <wiki-folder>] [--anchor file[::entity]]...
                    [--commit-message <text>] [--session-id <id>]

    Routing (SCHEMA §5):
      preference → wiki/preferences/<name>.md + digest link in preferences.md
      user       → wiki/user/<name>.md + digest link in user.md
      learning   → wiki/<dir>/<name>.md (dir must include learnings/)
      decision   → wiki/<dir>/<name>.md
      architecture → wiki/<dir>/<name>.md (anchors REQUIRED)
      note/reference → wiki/<name>.md (home, default) or wiki/<dir>/<name>.md

    Additive-only: if the page exists, returns an error — edit the file directly to update.
    Stages (git add) but does NOT commit — the model commits with its own message.
    """
    import argparse, time as _time
    p = argparse.ArgumentParser(prog="stack.py create", add_help=False)
    p.add_argument("--name", required=True)
    p.add_argument("--description", required=True)
    p.add_argument("--content", default="", help="page body, or @file to read from a file (optional — if omitted, a stub is created; patch it afterward)")
    p.add_argument("--kind", default="reference",
                   choices=["note", "reference", "decision", "architecture",
                            "preference", "user", "learning"])
    p.add_argument("--dir", default="", help="wiki-relative folder (must exist for non-preference/user)")
    p.add_argument("--anchor", action="append", default=[], help="file[::entity] provenance anchor")
    p.add_argument("--commit-message", default="")
    p.add_argument("--session-id", default="")
    args = p.parse_args(argv)

    name = args.name.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return _save_err("name must be kebab-case [a-z0-9-]")
    if not args.description.strip():
        return _save_err("description required")

    content = args.content
    if content.startswith("@"):
        try:
            content = Path(content[1:]).read_text(encoding="utf-8")
        except Exception as e:
            return _save_err(f"cannot read content file: {e}")
    if not content.strip():
        content = f"# {args.description}\n\n<!-- Add content via patch -->\n"

    anchors = []
    for a in args.anchor:
        if "::" in a:
            f, e = a.split("::", 1)
            anchors.append({"file": f.strip(), "entity": e.strip()})
        else:
            anchors.append({"file": a.strip()})

    if args.kind == "architecture" and not [a for a in anchors if a.get("file")]:
        return _save_err("architecture pages REQUIRE anchors: --anchor file[::entity]")

    if args.kind == "preference":
        return _save_preference(name, args.description, content, args.commit_message, args.session_id)
    if args.kind == "user":
        return _save_user(name, args.description, content, args.commit_message, args.session_id)

    dir_arg = args.dir.strip().strip("/")
    if dir_arg:
        target_dir = WIKI / dir_arg
        must_exist = target_dir.parent if args.kind == "learning" else target_dir
        if not must_exist.is_dir():
            return _save_err(f"folder not found: wiki/{dir_arg} — must be an existing folder")
        if args.kind == "learning":
            target_dir.mkdir(exist_ok=True)
        scope = dir_arg
    else:
        target_dir = WIKI
        scope = "home"
    target = target_dir / f"{name}.md"
    if target.exists():
        return _save_err(f"page exists: {target.relative_to(STACK_ROOT)} — additive-only; edit the file directly to update")

    type_ = {"decision": "project", "architecture": "project", "learning": "project"}.get(args.kind, "reference")
    fm_kind = args.kind if args.kind in {"decision", "architecture", "learning"} else None
    sid = args.session_id or os.environ.get("HERMES_SESSION_ID", "")

    frontmatter = _render_page(name, args.description, type_, fm_kind, sid, anchors)
    err = _write_page(target, frontmatter, content)
    if err:
        return _save_err(err)
    page_text = frontmatter + "\n\n" + content.rstrip() + "\n"

    _append_now_pointer(name, args.description, scope)
    _git_stage([target, WIKI / "now.md"])
    print(json.dumps({"saved": str(target.relative_to(STACK_ROOT)), "scope": scope,
                      "content": page_text, "now_pointer": True, "staged": True}))
    return 0


def _save_preference(name, description, content, commit_message, sid):
    prefs_dir = WIKI / "preferences"
    if not prefs_dir.is_dir():
        return _save_err("wiki/preferences/ not found")
    target = prefs_dir / f"{name}.md"
    if target.exists():
        return _save_err(f"preference exists: preferences/{name}.md — additive-only; edit the file directly to update")
    frontmatter = _render_page(name, description, "feedback", "preference", sid,
                               synced_at=False, kind_first=True)
    err = _write_page(target, frontmatter, content)
    if err:
        return _save_err(err)
    page_text = frontmatter + "\n\n" + content.rstrip() + "\n"
    _append_digest_link(WIKI / "preferences.md", name, description,
                        "## Captured via Hermes (review & categorize)")
    _git_stage([target, WIKI / "preferences.md"])
    print(json.dumps({"saved": f"wiki/preferences/{name}.md", "scope": "preference",
                      "content": page_text, "digest_linked": True, "staged": True}))
    return 0


def _save_user(name, description, content, commit_message, sid):
    user_dir = WIKI / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / f"{name}.md"
    if target.exists():
        return _save_err(f"user node exists: user/{name}.md — additive-only; edit the file directly to update")
    frontmatter = _render_page(name, description, "user", "user", sid,
                               synced_at=False, kind_first=True)
    err = _write_page(target, frontmatter, content)
    if err:
        return _save_err(err)
    page_text = frontmatter + "\n\n" + content.rstrip() + "\n"
    _append_digest_link(WIKI / "user.md", name, description,
                        "## Captured via Hermes (review & categorize)")
    _git_stage([target, WIKI / "user.md"])
    print(json.dumps({"saved": f"wiki/user/{name}.md", "scope": "user",
                      "content": page_text, "digest_linked": True, "staged": True}))
    return 0


def _append_digest_link(aggregator_path, name, description, header):
    """Add a one-line [[link]] entry to the aggregator (preferences.md or user.md)."""
    line = f"- {description} [[{name}]]"
    try:
        txt = aggregator_path.read_text(encoding="utf-8", errors="ignore") if aggregator_path.exists() else f"# {aggregator_path.stem.title()}\n"
        if header in txt:
            i = txt.index(header)
            nl = txt.find("\n", i + len(header))
            at = nl + 1 if nl != -1 else len(txt)
            txt = txt[:at] + line + "\n" + txt[at:]
        else:
            txt = txt.rstrip() + f"\n\n{header}\n{line}\n"
        aggregator_path.write_text(txt, encoding="utf-8")
    except Exception:
        pass


_RECENT_HEADER = "## Recently captured (Hermes — regroup or prune)"


def _append_now_pointer(name, description, scope):
    """Add a one-line pointer to now.md under the recent-captures header."""
    now = WIKI / "now.md"
    line = f"- [[{name}]] — {description}  ({scope}; captured via Hermes — regroup or prune)"
    try:
        txt = now.read_text(encoding="utf-8", errors="ignore") if now.exists() else "# Now\n"
        if _RECENT_HEADER in txt:
            i = txt.index(_RECENT_HEADER)
            nl = txt.find("\n", i + len(_RECENT_HEADER))
            at = nl + 1 if nl != -1 else len(txt)
            txt = txt[:at] + line + "\n" + txt[at:]
        else:
            txt = txt.rstrip() + f"\n\n{_RECENT_HEADER}\n{line}\n"
        now.write_text(txt, encoding="utf-8")
    except Exception:
        pass


def _git_stage(paths):
    """Stage exactly the files this command wrote. Does NOT commit.

    A blanket `git add -A` swept unrelated files into the index — anything a human
    happened to have lying in the store got committed under a saver's message. The
    index is the command's own output, nothing else.
    """
    rel = []
    for path in paths:
        try:
            rel.append(str(path.relative_to(STACK_ROOT)))
        except ValueError:
            continue          # outside the store: never stage it
    if not rel:
        return
    try:
        subprocess.run(["git", "add", "--", *rel], cwd=str(STACK_ROOT),
                       capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    except Exception:
        pass  # non-fatal — model can git add manually


def _render_page(name, description, type_, kind, sid, anchors=None,
                 synced_at=True, kind_first=False):
    """Build a page's frontmatter + body prefix. ONE emitter for every kind.

    `synced_at` is opt-in because the kinds genuinely differ: create-routed pages
    (note/decision/architecture/learning) carry it, while preference and user
    pages do not — the field drives drift tracking, which applies only to pages
    anchored to code. Emitting it for every kind would silently change the format.
    """
    lines = ["---", f"name: {name}", f"description: {json.dumps(description)}",
             "metadata:", "  node_type: memory"]
    # Field order is part of the on-disk format and the kinds genuinely differ:
    # preference/user write `kind` before `type`; create-routed pages write it
    # after. Reproducing the old engine's exact order keeps pages byte-identical.
    if kind and kind_first:
        lines.append(f"  kind: {kind}")
    lines.append(f"  type: {type_}")
    if kind and not kind_first:
        lines.append(f"  kind: {kind}")
    if synced_at:
        import time as _t
        lines.append(f"  synced_at: {_t.strftime('%Y-%m-%d')}")
    if sid:
        lines.append(f"  originSessionId: {sid}")
    valid = [a for a in (anchors or []) if (a.get("file") or "").strip()]
    if valid:
        lines.append("  sources:")
        for a in valid:
            lines.append(f"    - file: {a['file']}")
            if a.get("entity"):
                lines.append(f"      entity: {a['entity']}")
    lines.append("---")
    return "\n".join(lines)


def _write_page(target, frontmatter, content):
    """Write a page, or return an error. Every emitter writes through here, so
    no kind can silently skip the guard."""
    try:
        target.write_text(frontmatter + "\n\n" + content.rstrip() + "\n",
                          encoding="utf-8")
        return None
    except Exception as e:
        return f"write failed: {type(e).__name__}: {e}"


def _require_writable_store():
    """Fail closed when the store's declared format is not one we can write.

    Called before any mutation. Without this, `create` happily wrote into a store
    whose manifest declared a newer format — the exact case the format marker
    exists to prevent.
    """
    manifest = STACK_ROOT / ".stack" / "manifest.json"
    if not manifest.exists():
        return None            # a store with no manifest predates versioning
    ok, detail = _manifest_compatible(manifest)
    return None if ok else detail



def _save_err(msg):
    print(json.dumps({"error": msg}))
    return 1


def _outer_remote_health():
    """Return (ok, messages) for the private outer-repo propagation contract.

    This is intentionally read-only: it reports whether the current branch has an
    upstream and whether local state is clean/synchronized, but never fetches,
    pulls, merges, or changes refs.
    """
    messages = []
    branch = _git_out(["branch", "--show-current"], cwd=STACK_ROOT)
    if not branch:
        return False, ["  ⚠ outer repo: detached HEAD — check out the tracked branch before writing"]

    upstream = _git_out(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd=STACK_ROOT)
    if not upstream:
        return False, [f"  ⚠ outer repo: {branch} has no upstream — configure the private remote before writing"]

    dirty = _git_out(["status", "--porcelain"], cwd=STACK_ROOT)
    if dirty:
        messages.append("  ⚠ outer repo: working tree has uncommitted changes")

    counts = _git_out(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], cwd=STACK_ROOT)
    try:
        ahead, behind = (int(value) for value in (counts or "").split())
    except (TypeError, ValueError):
        messages.append(f"  ⚠ outer repo: could not compare {branch} with {upstream}")
        return False, messages

    if ahead or behind:
        messages.append(f"  ⚠ outer repo: {ahead} commit(s) ahead, {behind} behind {upstream}")
    else:
        messages.append(f"  ✓ outer private remote: {branch} tracks {upstream} and is synchronized")

    return not dirty and not ahead and not behind, messages


def cmd_doctor(argv):
    """Health check: config, outer private remote state, and submodules."""
    ok, msg = _check_config()
    print(f"config: {'OK' if ok else 'FAIL'}")
    print(msg)
    print()
    outer_ok, outer_messages = _outer_remote_health()
    print("outer repo:")
    print("\n".join(outer_messages))
    print()
    # Submodule status
    gm = STACK_ROOT / ".gitmodules"
    if gm.exists():
        shared_orgs = _gitmodule_orgs()
        for org in sorted(shared_orgs):
            sub = WIKI / org
            if not (sub / ".git").exists():
                print(f"  ⚠ submodule wiki/{org}: NOT INITIALIZED — run: git -C {STACK_ROOT} submodule update --init wiki/{org}")
            else:
                actual = _git_out(["rev-parse", "HEAD"], cwd=sub)
                ls_tree = _git_out(["ls-tree", "HEAD", f"wiki/{org}"], cwd=STACK_ROOT)
                recorded = ls_tree.split()[-2] if ls_tree and len(ls_tree.split()) >= 2 else None
                if recorded and actual != recorded:
                    n = _git_out(["rev-list", "--count", f"{actual}..{recorded}"], cwd=sub) or "?"
                    if n != "0":
                        print(f"  · submodule wiki/{org}: {n} commit(s) behind recorded pointer")
                    else:
                        n2 = _git_out(["rev-list", "--count", f"{recorded}..{actual}"], cwd=sub) or "?"
                        print(f"  · submodule wiki/{org}: {n2} commit(s) ahead (needs push + bump)")
                else:
                    print(f"  ✓ submodule wiki/{org}: up to date")
    else:
        print("  No submodules (.gitmodules not found)")
    return 0 if ok and outer_ok else 1


def cmd_init(argv):
    """Create a minimal valid store at --repo.

    Usage:
      engine.py --repo PATH init

    The bootstrap circularity is why this lives in the engine rather than in a
    store: the program that creates a store cannot be inside the store it is
    creating. The engine is installed by the plugin, so it always exists.

    Creates, and only creates:
      .git/                 an initialised repository with one commit
      .stack/manifest.json  the format version this store was made for
      .gitignore            ignores config.json (per-machine) and DS_Store
      config.example.json   the empty mapping schema, with $HOME-relative paths
      wiki/                 now.md, user.md, index.md, preferences.md + folders

    A store with no orgs configured is healthy — that is the first-run state.
    An existing non-empty target is never modified: pass nothing and it errors.
    """
    root = STACK_ROOT

    # A path that exists as a file can never become a store. Say so instead of
    # letting iterdir() raise.
    if root.exists() and not root.is_dir():
        print(json.dumps({
            "initialized": False, "repo": str(root),
            "error": "target exists and is a file, not a directory",
        }))
        return 1

    # Refuse to touch a directory that already has content, unless it is already
    # a store we recognise (idempotent re-init).
    if root.exists():
        manifest = root / ".stack" / "manifest.json"
        already = manifest.exists() or (root / "wiki").is_dir()
        entries = [p for p in root.iterdir() if p.name not in (".DS_Store",)]
        if already:
            # An existing store is only "fine" if we can actually operate on it.
            # Writing with an engine that does not know the store's format is how
            # a silent incompatibility turns into corrupted memory.
            if manifest.exists():
                ok, detail = _manifest_compatible(manifest)
                if not ok:
                    print(json.dumps({"initialized": False, "repo": str(root),
                                      "error": detail}))
                    return 1
            print(json.dumps({"initialized": False, "repo": str(root),
                              "reason": "already a store — use its pages, or adopt explicitly"}))
            return 0
        if entries:
            print(json.dumps({
                "initialized": False, "repo": str(root),
                "error": f"target exists and is not empty ({len(entries)} entries); "
                         f"refusing to write into it",
            }))
            return 1

    root.mkdir(parents=True, exist_ok=True)

    # --- manifest -----------------------------------------------------------
    (root / ".stack").mkdir(exist_ok=True)
    (root / ".stack" / "manifest.json").write_text(
        json.dumps({"format_version": FORMAT_VERSION}, indent=2) + "\n", encoding="utf-8")

    # --- ignores ------------------------------------------------------------
    (root / ".gitignore").write_text(
        ".DS_Store\n*.pyc\n__pycache__/\n\n"
        "# Per-machine config — paths differ across machines\n"
        "# Create it from config.example.json and edit the paths.\n"
        "config.json\n",
        encoding="utf-8")

    # --- the mapping schema (empty: no orgs yet) ----------------------------
    (root / "config.example.json").write_text(
        json.dumps({
            "stack_root": "",
            "orgs": {},
            "memory_inbox_glob": "",
        }, indent=2) + "\n", encoding="utf-8")

    # --- the wiki skeleton --------------------------------------------------
    wiki = root / "wiki"
    for sub in ("preferences", "user", "sources"):
        (wiki / sub).mkdir(parents=True, exist_ok=True)

    (wiki / "now.md").write_text(
        "# Now\n\n> A **current view**, not a chronicle — done items are distilled to their page\n"
        "> and dropped from here. Agent-maintained; you don't hand-edit it.\n",
        encoding="utf-8")
    (wiki / "user.md").write_text(
        "# User\n\n> Descriptive facts about *who the user is* — role, expertise, context.\n",
        encoding="utf-8")
    (wiki / "index.md").write_text(
        "# Index\n\n> Entry points into this store. Add a line as pages earn one.\n",
        encoding="utf-8")
    (wiki / "preferences.md").write_text(
        "# Preferences\n\n> Durable principles. Sacred: additive-only, never merged or deleted.\n",
        encoding="utf-8")

    # --- git ----------------------------------------------------------------
    git_ok, git_msg = _git_init(root)
    if not git_ok:
        print(json.dumps({
            "initialized": True, "repo": str(root), "format_version": FORMAT_VERSION,
            "git": "not initialized", "git_detail": git_msg,
        }))
        return 0

    print(json.dumps({
        "initialized": True, "repo": str(root), "format_version": FORMAT_VERSION,
        "git": "initialized",
    }, ensure_ascii=False))
    return 0


def _manifest_compatible(manifest: Path):
    """Is this store's declared format one this engine can operate on?

    Returns (ok, detail). A missing or unreadable manifest is *not* silently
    accepted: an unknown store is exactly the case a writer must refuse rather
    than proceed into.
    """
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"store manifest is unreadable: {type(e).__name__}: {e}"
    raw = data.get("format_version")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return False, (f"store manifest format_version must be an integer, got {raw!r} "
                       f"— refusing to operate on an unknown format")
    if raw > FORMAT_VERSION:
        return False, (f"store format_version {raw} is newer than this engine "
                       f"({FORMAT_VERSION}) — upgrade the plugin; refusing to write")
    return True, f"format_version={raw}"


def _git_init(root: Path):
    """Initialise the store's repository with one commit. Returns (ok, detail)."""
    def run(*args):
        return subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True)

    if not shutil.which("git"):
        return False, "git not found on PATH"
    if not (root / ".git").exists():
        r = run("init", "-b", "main")
        if r.returncode != 0:
            r = run("init")          # older git without -b
            if r.returncode != 0:
                return False, (r.stderr or "").strip()

    # A commit needs an identity. Use the configured one; never invent one,
    # and never write a repo-wide config from here.
    ident = {}
    for key, var in (("user.name", "GIT_AUTHOR_NAME"), ("user.email", "GIT_AUTHOR_EMAIL")):
        r = run("config", "--get", key)
        if r.returncode == 0 and r.stdout.strip():
            ident[var] = r.stdout.strip()
    if not ident:
        return False, ("no git identity configured — set user.name/user.email, "
                       "or commit the empty store yourself")

    run("add", "-A")
    env = dict(os.environ, **ident)
    r = subprocess.run(
        ["git", "-C", str(root), "-c", "commit.gpgsign=false",
         "commit", "-m", "init: empty stack memory store"],
        capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()
    return True, "committed"


def main():
    global STACK_ROOT, WIKI
    cmds = {"resolve": cmd_resolve, "verify": cmd_verify, "load": cmd_load, "check": cmd_check,
            "create": cmd_create, "doctor": cmd_doctor, "init": cmd_init}
    argv = sys.argv[1:]

    # --repo is required and may appear anywhere; it is stripped before dispatch.
    repo = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--repo":
            if i + 1 >= len(argv):
                print("error: --repo requires a PATH", file=sys.stderr)
                return 2
            repo = argv[i + 1]
            i += 2
            continue
        if argv[i].startswith("--repo="):
            repo = argv[i].split("=", 1)[1]
            i += 1
            continue
        rest.append(argv[i])
        i += 1

    if not repo:
        print("error: --repo PATH is required (the memory store to operate on)",
              file=sys.stderr)
        return 2

    STACK_ROOT = Path(os.path.expanduser(os.path.expandvars(repo))).resolve()
    WIKI = STACK_ROOT / "wiki"

    if not rest or rest[0] not in cmds:
        print(__doc__)
        return 2

    command = rest[0]
    # Mutating commands fail closed on an incompatible store. Checked here rather
    # than in each handler so a new write path cannot forget it.
    if command in ("create", "init"):
        problem = _require_writable_store()
        if problem:
            print(json.dumps({"error": problem, "repo": str(STACK_ROOT),
                              "command": command}))
            return 1

    return cmds[command](rest[1:])


if __name__ == "__main__":
    sys.exit(main())
