# SCHEMA — the agent contract

> **Audience: you, the agent.** This is not human prose; it is the operating spec you
> follow to maintain `wiki/` and to serve the user from it. Read it before you load,
> write, verify, or consolidate. If a rule here conflicts with a default behavior,
> this wins.

The user never hand-structures knowledge. **You** do all filing, linking, citing, and
upkeep. The user talks and draws; you maintain the wiki and read it to help them.

Org and project names, paths, and per-store exemptions are that store's data — they live
in its `config.json`, never here. This file describes the format, not any one store.

---

## 0. Prime directives (invariants — never violate)

1. **Additive by default — but a page tracks *current* truth.** A page is the single source
   of truth about its thing and is **updated in place when that thing changes** (git history
   is the undo for in-wiki edits). What this directive forbids is **losing meaning that
   lives nowhere else**: automatic/unreviewed edits don't *delete* a fact or a page, and a
   *transform* that removes or relocates substance is the human's call — or a sanctioned,
   provably-lossless distill-then-drop (§3.2). Strict append-only is reserved for the
   preference layer (§5).
2. **Trust = cheap verification, not a timestamp.** A page states what it rests on
   (`sources:` anchors) so the user can spot-check it. The verified/drifted/unknown state
   is computed on demand and is **advisory**. "I can't verify this" is a first-class,
   **loud** answer — never silently downgraded to "fine".
3. **Anchors are `file` or `file::entity`, NEVER line numbers.** Line numbers rot the
   moment anyone edits the file; they turn a trustworthy citation into a false one.
4. **The agent maintains; the human owns.** You may reorganize, link, and summarize. You may
   not silently drop knowledge, and you may not overwrite a preference.

---

## 1. Load (serving the user)

`engine.py load <cwd>` prints what the agent should read at the start of work in a
directory. One command, no branching:

- Always: `now.md` (current intent), `user.md` (who the user is), `index.md` (entry points).
- Plus, when `cwd` resolves through `config.json` to an org/project: that project's pages
  and a drift check over them.

`load` is **read-only** and **bounded**: it names the pages and their verify state, not
their bodies. The agent reads a body on demand. A page missing from the load path is
invisible to the agent — which is why the digests in §5 matter.

## 1.1 Shared orgs (optional — a team-owned submodule)

A store may nest one or more **shared submodules** under `wiki/<org>/` whose remote is a
team repository rather than the user's own. Rules:

- **Resolution** maps `cwd` → `(org, project)` by the store's `config.json`, matching both
  the on-disk name and any configured alias.
- **Shared content is authored for the team.** Never commit private pages into a shared
  submodule; never push someone else's uncommitted work.
- **A shared submodule is its own repository.** Commit and push it explicitly; the outer
  repo records only its pointer. A pull that fails or diverges means **stop on that
  surface** — never force-push, never merge blindly.
- The set of shared orgs is store data (`config.json` + `.gitmodules`), not a format rule.

---

## 2. Page format

Every page is markdown with YAML frontmatter:

```yaml
---
name: <slug>                 # the page's identity; [[wikilinks]] target this
description: "<one line>"    # what this page is about; used in catalogs
metadata:
  node_type: memory
  type: <project|feedback|user|source|reference>
  kind: <see §3>             # required for the typed kinds
  synced_at: <YYYY-MM-DD>    # only on pages with `sources:` anchors (§4)
  originSessionId: <id>      # optional provenance
---
```

`parse_frontmatter` accepts both the flat shape (fields at the top level) and the nested
`metadata:` shape above, promoting children of `metadata:` upward. Pages written by
different tools must remain readable by all of them.

**`sources:`** is a list of `{file: <path>, entity: <name>}` — the anchors a page rests on.
Only pages anchored to code carry them (§4).

## 3. Page kinds

| kind | what it is | where it goes |
|---|---|---|
| `preference` | the user stated how they want to work — faithful, their words, sacred | `wiki/preferences/<id>.md` + digest |
| `user` | an agent-curated fact about who the user is (role, expertise, context) | `wiki/user/<id>.md` + digest |
| `decision` | a multi-session plan (Goal / Plan / Progress / Open questions) | `wiki/<org>/<project>/decisions/` |
| `architecture` | how the code IS — **requires anchors** | `wiki/<org>/<project>/` |
| `learning` | a concept the user understood, in their own words (per-project) | `wiki/<org>/<project>/learnings/` |
| `note` / `reference` | a durable fact or pointer that isn't the above | `wiki/` or `wiki/<org>/<project>/` |
| `source` | a summary of an external document (§5.1) | `wiki/sources/<type>/` |

### 3.1 Decisions & multi-session plans

A `decision` page has four sections: **Goal · Plan · Progress · Open questions**.
`Progress` is **append-only** — it is the record of what actually happened, in order.
`Plan` and `Open questions` are updated in place as reality moves. When the work
completes, the outcome is distilled to its own page and the plan page keeps only what
is still true.

### 3.2 `now` — the current view, not a chronicle

`now.md` is a **current view**: what is active right now, one line per thread, each
pointing at the page that holds the detail. It is **not** a log.

- Done work leaves `now`. The durable outcome goes to its own page first — distill, then
  drop, never drop-and-lose.
- Commits, PR numbers, "DONE/SHIPPED", and session history do **not** belong in `now`.
- Git is the chronicle. If an item is finished and its substance lives nowhere else, it
  gets a page before the line goes away.

### 3.3 `user` — who the user is

Descriptive facts about the user: role, expertise, working context. **Agent-curated and
additive** — refined in place as understanding sharpens, never rewritten wholesale.
These are *descriptive* (what is true), as opposed to `preference` (how to behave).

### 3.4 `learning` — a concept understood, in the user's own words

A per-project page recording that the user understood something, in **their** words:
*In my words · Analogy · Why it mattered · Related*. Not the agent's explanation — the
user's restatement. Auto-loaded with its project; referenced from the project's
`architecture` page.

## 4. Verify (`engine.py verify <page>`)

For each `sources:` anchor: does the `file` exist? does the `entity` still exist in it?
has it changed since `synced_at`?

- **Git-aware** when the repo has commits (last-commit date of the cited file vs
  `synced_at`). Without git history, drift is **not tracked** — the page rests on anchor
  existence.
- Result per page: `verified` · `drifted` (name which anchor) · `unknown`
  (missing/moved/dataless — **surface it loudly**).
- Output is **advisory**. It names what to recheck; it never claims the page is right.

**`engine.py check`** — on-demand read-only health report: verify all anchored pages,
dangling `[[links]]`, orphan pages, and `index.md` coverage. Mechanical and report-only.

## 5. Routing — where each kind is written

- `kind: preference` → per-node page at `wiki/preferences/<id>.md`, **plus a one-line entry
  in the aggregator `wiki/preferences.md`**. The aggregator is what `load` surfaces — the
  per-node files are not in the load path, so **a principle missing from the digest never
  loads**. `check`'s orphan report is the backstop. Preferences are **sacred**: additive
  only, never merged, never regenerated.
- `kind: user` → per-node page at `wiki/user/<id>.md`, **plus an aggregator entry in
  `wiki/user.md`**. Same load-path rule. Not sacred — refined in place.
- `kind: learning` → `wiki/<org>/<project>/learnings/<concept>.md`, one file per concept,
  auto-loaded with its project, linked from that project's architecture page.
- `kind: architecture` → `wiki/<org>/<project>/*.md`, with `file`/`file::entity` anchors
  read from the code.
- `kind: decision` → `wiki/<org>/<project>/decisions/<slug>.md`.
- `kind: note`/`reference` → `wiki/` (home) or `wiki/<org>/<project>/`.

**Dedup is append-with-pointer** (`see also [[other]]`) — never collapse one page into
another and drop it. Nothing is lost, no links break.

### 5.1 Ingest — the sources feed

Pull external reading into `wiki/sources/<type>/<slug>.md` as **summaries**; the originals
stay external and are read on demand. **No vector store** — browse `wiki/sources/index.md`
and search the tree; the agent reads summaries to pick.

- **Split (this is the reliability).** A deterministic step enumerates, fetches, extracts,
  and hashes, diffing against existing source pages: their `source_id` +
  `content_sha256` frontmatter **is** the manifest, so re-runs skip what is unchanged. It
  emits a worklist; the **agent** writes each summary. Trigger is point-at-it — no scan,
  no daemon.
- **Source page:** `kind: source`; frontmatter `{source_type, source_id, title, modified,
  content_sha256, topics:[]}`; body `## Gist · ## Key points · ## Topics · ## Source`.
  Summarize at gist/claims altitude — the original holds the detail.
- **Freshness is ingest-time, not load-time.** Re-running the ingest re-fetches and
  re-summarizes only what changed. `load`/`check` never re-fetch (too slow).
- **Privacy.** Ingest only the pointed-at folder/note/file — never bulk.

## 6. Deferred capabilities

Capabilities that are defined but not active. They are not part of the contract until
built: the scheduled autonomy/lint pass, the recurring native-memory drain, and the rest
of the sources feed (bookmarks, document/PDF, e-book, chat→notes). The sources feed ships
with its first adapter only; each further adapter is added when it is actually needed.
