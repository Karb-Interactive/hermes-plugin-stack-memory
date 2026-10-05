# hermes-plugin-stack-memory

> Used by me (@vykhovanets) and people in my teams for at least 5 months already. Previously was in a form of Claude Code memory provider, now formulated as a plugin for Hermes. Distinction: memorizing and remembering happens in a background.

> Each profile can have its own memory store.

## Basic installation: a personal wiki

```sh
hermes plugins install Karb-Interactive/hermes-plugin-stack-memory --enable
hermes config set memory.provider stack
```

Start a new chat in Hermes Desktop, or run `hermes chat` in the CLI. Stack creates the wiki
at the profile's default `$HERMES_HOME/memories/stack` path on first use — normally
`~/.hermes/memories/stack` for the default profile, or
`~/.hermes/profiles/<profile>/memories/stack` for a named profile.

**No organization, project, custom store path, or Git remote is required.** The store starts
with personal pages for current priorities, preferences, and user facts. Leave `repo_path`
unset to use this default; organization/project mappings and shared repositories are optional
additions later. A pre-existing `stack.json.repo_path` override still selects that other store.
With no auxiliary model overrides, the components use your configured main model.
Already have a wiki? [Link this profile to an existing store](#using-an-existing-store).

To create a separate named profile with its own store:

```sh
hermes profile create notes
hermes -p notes plugins install Karb-Interactive/hermes-plugin-stack-memory --enable
hermes -p notes config set memory.provider stack
hermes -p notes chat
```

Here `notes` is an example profile name, not an organization. No gateway service is needed
for Desktop or CLI use. `uv`, `git`, and a configured Git name/email are needed for the store's
initial commit.

## Components

| Component | Receives | Produces |
|-----------|----------|----------|
| **Saver** | Buffered user/assistant exchanges, the previous rolling summary, a catalog of memory pages, and the store/engine/schema paths. | Durable facts written to Markdown pages, followed by reconciliation of `now.md`. It is a separate agent with search, read, patch, and terminal tools. |
| **Retriever** | The current user message, a catalog of page paths and descriptions, and the paths injected during the last three turns. | Up to three relevant page paths. The plugin reads those pages from disk and supplies their bodies to the main agent before it answers. The retriever does not receive the rolling summary or the full conversation. |
| **Summariser** | The previous rolling summary and excerpts of the exchanges just processed by the saver. | An updated session summary, capped at 5,000 characters, for the next saver run. It is working context, not a wiki page. |

Each component can use its own model through `auxiliary.saver`, `auxiliary.retriever`, or
`auxiliary.summarizer`. The saver is an agent that can use tools; the retriever and summariser
are single model calls without tools.

## Logic and cadence

1. **At session start**, the plugin loads the store's base/project context into a stable
   system-prompt block and builds the page catalog.
2. **Before an answer**, the retriever selects pages for the current user message. Their
   bodies are added to the main agent's context. Retrieval participates in the answer path;
   it is not a save job that runs after the answer.
3. **After a completed user/assistant exchange**, Hermes queues memory synchronization on
   its background worker. Stack adds that exchange to its buffer.
4. **Every four exchanges by default**, the saver processes the buffered conversation
   excerpts together with the previous rolling summary. It searches existing memory before
   creating or editing pages, then uses a second turn in the same agent session to reconcile
   `now.md`. Its instructions make the conversation excerpts the source of new facts; the
   summary provides context only. A run may legitimately save nothing.
5. **After the saver finishes**, the summariser updates the rolling summary from that same
   batch. The buffer is cleared; the updated summary goes to the next saver run, not to the
   retriever or the main agent.

`stack.json`'s `cadence` changes the interval; it counts completed exchanges, not individual
messages, tool calls, or minutes. Pending exchanges are also processed at session end and
before context compression, so a short session need not reach the normal cadence.
`stack.json`'s `max_iterations` separately limits the saver agent's work; it does not change
when saving starts.

Saving and summarising run on the background synchronization path. On a session switch, the
buffer, rolling summary, cadence counter, and recent-injection history are reset.


---

A [Hermes](https://github.com/NousResearch/hermes-agent) memory-provider plugin for **stack** — a
git-backed markdown memory store that an agent maintains and reads.

The plugin is self-contained: it ships the engine, the schema, and the writer. A memory store
contains only data. That separation is the point — it means one plugin can serve any number of
stores, a store can be created from nothing, and the plugin contains no personal content.

```
plugin (this repo)          ->  engine + schema + saver + prompts   (program, publishable)
<memory store>  (any path)  ->  wiki/ + config.json + git history    (data, private)
```

## Install

```sh
git clone https://github.com/Karb-Interactive/hermes-plugin-stack-memory   # or:
hermes plugins install Karb-Interactive/hermes-plugin-stack-memory --enable

hermes config set memory.provider stack
hermes gateway restart
```

Then use Hermes. **On first run the plugin creates its own store** at
`$HERMES_HOME/memories/stack`, commits it, and reports the path. Nothing else to configure —
a fresh install needs no pre-existing repository.

Each Hermes profile installs the plugin itself (plugins are not copied by `hermes profile
create --clone`), and each profile gets its own store by default.

### Using an existing store

You can point a profile at a compatible Stack wiki you already have instead of creating a
new one. After installing the plugin, run the interactive setup:

```sh
hermes memory setup
```

Choose **stack**, then enter the existing store's absolute path when asked for the store path.
Use the repository root that contains `wiki/`, **not** the `wiki/` directory itself. The wizard
selects Stack as the memory provider and saves `repo_path` in that profile's `stack.json`.

For a named profile with the plugin already installed:

```sh
hermes -p notes memory setup
```

Then start a new chat in that profile. Future memory reads and writes use the existing store;
linking it does not copy or move its pages, or merge a previous profile-local store into it.
Pointing multiple profiles at the same path shares that memory between them.

The binding is per profile: normally `~/.hermes/stack.json` for the default profile or
`~/.hermes/profiles/notes/stack.json` for `notes`. For an explicit `HERMES_HOME`, it is
`$HERMES_HOME/stack.json`. Its store-path setting is:

```json
{"repo_path": "/absolute/path/to/store"}
```

No organization/project mapping is required to link an existing personal store. Resolution
order:

```
real profile  ->  stack.json.repo_path  ->  $HERMES_HOME/memories/stack   (created on first use)
no profile    ->  STACK_REPO            ->  ~/stack-memory            (tests/scripts only)
```

An inherited `STACK_REPO` is deliberately ignored when a real profile exists — otherwise a fresh
profile would silently read and write the default profile's memory.

### Optional folder mapping

The store's `config.json` maps its organization/project keys to local project folders, so
Stack can select project context from the current working directory and resolve source
anchors. It is optional for a personal wiki. To configure it, copy `config.example.json`
to `config.json` inside the store and fill in the project paths; `~` and `$HOME` are supported.
New stores ignore this machine-local file in Git.

**When migrating between machines with different folder layouts, recreate or update those
paths on the destination.** The wiki pages can stay unchanged, but incorrect mappings can
prevent automatic project-context loading; personal memory still works. This is separate
from the profile's `stack.json`, which selects the memory store itself.

### Pin a version

```sh
hermes plugins install Karb-Interactive/hermes-plugin-stack-memory --ref <40-char-sha>
```

## Configuration

Everything the plugin reads. Config keys with no default are inactive until you set them — the
plugin falls back to the behaviour named in the last column.

| Key | Default | Effect |
|-----|---------|--------|
| `memory.provider` | `""` | Selects this provider. Must be `stack`; with `""` the plugin never loads and Hermes uses builtin memory alone. |
| `stack.json` → `repo_path` | profile-local | Which store this profile reads and writes. Absent ⇒ `$HERMES_HOME/memories/stack`; an absolute path shares one store between profiles. Written into `HERMES_HOME`, so it is per profile. |
| `stack.json` → `dataset_enabled` | `false` | When true, writes the local debug/dataset file (see Data and permissions). Off by default; it stores raw conversation turns. Set via `hermes memory setup`. |
| `auxiliary.retriever.{provider,model,api_key,base_url}` | main model | Model for the per-turn recall call (one `PluginLlm` request over the page catalog). Needs the two trust flags below. |
| `auxiliary.saver.{provider,model,api_key,base_url}` | main model | Model for the background curation agent — the hardest job here, so it is worth a strong one. It runs as its own agent rather than through `PluginLlm`, so it needs no trust flag. |
| `auxiliary.summarizer.{…}` | main model | Model that compresses the rolling context the saver receives. Context only; never written to the store. Needs the same two trust flags as the retriever. |
| `stack.json` → `cadence` | `4` | Exchanges between saver runs. Lower = more frequent, more tokens per session. Set via `hermes memory setup`. |
| `stack.json` → `max_iterations` | `10` | Tool-call budget for one saver run. A session with four or more new facts can exhaust the default mid-write, leaving created-but-empty pages and no commit — raise it before blaming the saver. |
| `plugins.entries.stack.llm.allow_provider_override` | `false` | **Required for the retriever and summarizer.** Without it `PluginLlm` denies the request and raises; both callers swallow the error, so recall quietly returns nothing — indistinguishable from "no page was relevant" — and the summary freezes at its previous value. The saver is unaffected. |
| `plugins.entries.stack.llm.allow_model_override` | `false` | The same gate for the model field specifically. Both must be true. |

```sh
hermes config set memory.provider stack

# a cheaper model for recall, a stronger one for curation
hermes config set auxiliary.retriever.provider ollama-cloud
hermes config set auxiliary.retriever.model nemotron-3-super
hermes config set auxiliary.saver.provider ollama-cloud
hermes config set auxiliary.saver.model glm-5.3-flash

# without these two, the retriever and summarizer settings above are ignored
# (the saver does not go through PluginLlm, so it needs no flag)
hermes config set plugins.entries.stack.llm.allow_provider_override true
hermes config set plugins.entries.stack.llm.allow_model_override true
```

Config is read once at agent init, so **a new session is required** — `/reset` or restart.

`cadence` and `max_iterations` are the provider's own settings, not `auxiliary` keys — set them
through `hermes memory setup` (they land in `stack.json`). The remaining `auxiliary.*` dotted
paths above work with `hermes config set`, but it warns they are not recognized config keys:
they are read by this plugin, not by Hermes core. The warning is expected; the value is written
and used.

Not read: the `timeout` field under any of the three `auxiliary.*` blocks. The plugin passes no
timeout to either path, so setting it does nothing — the recall call can block for as long as the
model takes, and the provider's own callers have no ceiling to fall back on.

## Usage

```sh
hermes gateway restart     # after installing, or after any change to the config above
```

Then use Hermes normally. Four things happen without you asking:

- **At session start** the store is located, its context (`now.md`, `user.md`, the index) is injected
  once as a stable system-prompt block, and shared submodules are pulled in the background. The
  block is byte-stable for the whole session, so it is served from the prompt cache instead of
  re-sent every turn.
- **Every turn**, the retriever makes one model call: it classifies the message and picks at most
  three pages from the catalog, and their current bodies are injected. Pages injected in the last
  three turns are filtered out in code, not by asking the model to remember. It fails open — a
  failed or denied call injects nothing and the turn proceeds. That silence is the whole failure
  mode, so check the trust flags above first if recall never seems to fire.
- **Every `cadence` turns** — and also at session end if a shorter session still has buffered turns,
  and before context compression discards messages — a bounded saver agent runs. It searches for an
  existing page before writing, edits what it finds, and commits. Progress shows up as
  `📥 Memory: injected …` and `📝 Memory: saved …` in the desktop app.
- **Nothing is written by the main agent.** `get_tool_schemas()` returns `[]`, so the conversation
  pays no schema cost for a tool it would only misuse — the saver owns every write.

Maintenance, any time — from anywhere, naming the store explicitly:

```sh
uv run --no-project engine.py --repo ~/System/stack check     # dangling links, orphans
uv run --no-project engine.py --repo ~/System/stack doctor    # config, git, submodules
```

An empty store is a healthy store — a freshly created one has no orgs configured and no pages.
It fills in as you work. To back it up, push it: it is an ordinary git repository, and a local-only
one with no remote is legal.

## Data and permissions

- **Model calls:** the retriever sends the current message and page catalog to its configured
  model. The saver receives buffered conversation excerpts, a rolling summary, store paths,
  and the catalog; pages it reads become part of that agent's context. The summariser receives
  conversation excerpts and the previous summary. Selected memory pages also reach the main
  conversation model. With a remote provider, those inputs leave the machine for that provider.
- **Local access:** the plugin reads and writes the selected store and can inspect configured
  project paths for source-anchor verification. The background saver has search, read, patch,
  and terminal tools under the Hermes process's permissions; its instructions are not a
  filesystem sandbox. Memory writes have no additional human-review step. The saver does not
  pass YOLO/skip-approval options or replace Hermes's approval guards.
- **Shell and Git:** the plugin invokes its engine through `uv run`, and runs Git operations.
  Shared submodules are pulled in the background at session start by default. The saver is
  instructed to pull, commit, and push changed memory repositories using configured Git
  credentials and remotes. Configure those remotes for the intended audience; the plugin does
  not make a remote private or approve its contents for publication.
- **Local diagnostics (opt-in):** when `dataset_enabled` is true in this profile's `stack.json`
  (the same file as `repo_path`, set by `hermes memory setup`), the plugin writes
  `$HERMES_HOME/memories/.stack-provider/dataset.jsonl` — a debug/dataset file holding
  query excerpts, full raw conversation turns fed to the saver, rolling summaries, tool calls
  and their results, and project paths/commit references. It is off by default because it
  stores raw transcripts; it has no size cap or rotation. Treat it and probe logs as private
  conversation data. Stack does not upload those logs to a separate analytics service or
  include an outbound telemetry endpoint.
- **Credentials and background work:** model credentials are resolved from Hermes's configured
  runtime; Git uses the user's existing authentication. Stack does not provide a separate
  credential store. Saving and summarising run on Hermes's background worker, not as a separate
  always-on service.

## What it writes

Writes happen only through the saver, and only in the store's own schema:

- **Write-through consolidation:** every saver run gets two turns in one session — save durable
  knowledge, then reconcile `now.md`. Completed outcomes move to their own page; `now.md` keeps
  only unresolved state plus a pointer. DONE/MERGED/SHIPPED history, commits and PR numbers are
  excluded.
- **Two memory lanes:** Hermes builtin memory stays enabled for native persona curation. The stack
  provider writes only stack; the builtin background reviewer never writes stack. Neither
  duplicates the other.

## The engine

`engine.py` is the single runtime and is harness-neutral — no Hermes imports. It takes the store
explicitly and never infers it from its own location:

```sh
uv run --no-project engine.py --repo <store> load    [cwd]   # context + page catalog
uv run --no-project engine.py --repo <store> check   [cwd]   # health: verify-all, dangling links, orphans
uv run --no-project engine.py --repo <store> create  ...     # write a page (stages; does not commit)
uv run --no-project engine.py --repo <store> doctor          # config, git state, submodules
uv run --no-project engine.py --repo <store> init            # create a minimal store at an absent path
```

Any other harness can use it by calling that path — it does not need to be copied into a store.

## Develop

- Debug: `STACK_PROVIDER_DEBUG=1` logs lifecycle seams to `.probe.log`.
- Tests:
  ```sh
  PYTHONPATH=$HOME/.hermes/hermes-agent:$HOME/.hermes/plugins \
    uv run --project $HOME/.hermes/hermes-agent --no-sync \
    python -m unittest discover -s tests -p 'test_*.py'
  ```
  `tests/test_vocab.py` also runs standalone (it asserts prompt vocabulary, not discover-style tests).
- `tests/check_profile_store.py` exercises fresh-profile and explicit-store resolution with real
  objects against temp `HERMES_HOME`s.
- Current behaviour is defined by the code and its tests. There is no separate spec document in the
  tree: an earlier v1 build spec was deleted once it stopped describing this system, and Git holds
  its history.

## Requirements

`uv` and `git` on PATH. No API key, no pip dependencies beyond the engine's PEP 723 `pyyaml`.
