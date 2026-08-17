# Agent Brain — conventions & write protocol

This is the canonical reference for how to read and write an Agent Brain
instance. It describes the protocol, not any one deployment: everything here
holds for a fresh instance on day one.

## The model — memory is a stream, not a document

An instance has one **data root** (resolved from `BRAIN_ROOT`, then a `root` key
in `brain.config.json`, then a `brain/` folder next to `brain.py`). Under it,
memory is organized **per scope** (a scope is a project slug; the default
cross-project scope is `global`):

- **`stream/<scope>.log`** — the source of truth. Append-only typed entries, one
  line each: `TS | type | [meta] | text`. Types: `decision`, `milestone`,
  `gotcha`, `invariant`, `question`, `papercut`. Caps are per type: **800 chars**
  for `decision`/`question`, which carry the reasoning, and **300** for the rest,
  where terseness is the feature. Never edited; corrections are new entries with
  `--supersedes N`.
- **`tree/<scope>/`** — GENERATED caches, never hand-edited: `root.md`
  (watermarked lossy state summary), `b<lo>-<hi>.md` (branch gists, 32
  entries/branch), `hazards.md`/`design.md`/`hazards.tsv` (verbatim standing
  indexes by subsystem + label cache). The tree is a **deterministic** function
  of the stream — no model calls, no scheduler. It is rebuilt inline on every
  write, so it is never stale, and `brain rebuild` can regenerate it from the
  stream at any time.

**Two paths, deliberately:**

- **State** (decision/milestone/question) compresses into `root.md` — lossy by
  design. It orients; it does not carry detail.
- **Standing** (gotcha/`invariant`) is **never summarized** — only indexed
  verbatim by subsystem. Hazards are what must not be re-hit; invariants are what
  must stay TRUE about a subsystem's design. Hazards leave the live index only
  via `brain resolve`; invariants only via `--supersedes`. Pull `brain hazards`
  before touching a subsystem, `brain design` before redesigning one; the
  injected state carries neither.

## Three-class routing — pin / stream / register

There are three homes for a durable fact, and the class decides the home:

- **Pin** — an always-loaded identity fact. A pin is an `invariant` marked
  `--pin`; it renders verbatim at the top of every wake, so a scope's pins share
  a hard character budget. Use it for the handful of facts a session must have in
  front of it every time (`brain note --type invariant --pin --subsystem
  identity "..."`).
- **Stream** — everything else that is memory: a decision and its why, a gotcha,
  an abandoned approach, an open question. It is retrieved on demand (`wake`,
  `hazards`, `design`, `recall`, `grep`), not pushed whole.
- **Register** — repeated records of ONE schema (entities, documents,
  interviews, expenses…). A register is not memory; it is a structured,
  multi-writer table with its own folder, config, and CSV/board faces. Use
  `registers.py` with `--root <dir>`.

## Write rules

0. **Route before you write.** Would you open a work item to fix it today? → put
   it on the **ticket board** (`tickets.py open ... --root <board>`), NOT a note;
   the board is the one home for a fixable defect. Must the next session know it
   to work correctly? → `gotcha`. Neither, but it cost you time? → `papercut`. A
   note describing a defect you intend to fix is misfiled — the CLI's fix-language
   gate catches the obvious cases (`--force-type` overrides), but compressing to
   fit the cap can strip the very words it matches on, so decide the route first.
1. **Capture inline, the moment it lands** — not at session end:
   `python brain.py note --project <scope> --type <T> "one line"`.
   Standing entries (`gotcha`/`invariant`) **require** `--subsystem "label"` — the
   command errors without it, because the label is the verbatim index's only
   retrieval axis. Reuse an existing label for the same part of the system (check
   `brain hazards`), never mint near-synonyms (trivial variants snap
   automatically).
2. **One fact per entry, inside its cap.** Capture what the diff can't tell you.
   Never replay a diff. **Reasoning that will not fit belongs in a deep doc**
   under the data root (e.g. `projects/<scope>/<topic>.md`) — the entry holds the
   ruling and points at it.
3. **Supersede, don't edit.** Stale or wrong entry → `brain note --supersedes N`
   with the corrected fact; it drops out of the summary/index path. Fixed a known
   gotcha → `brain resolve --project <scope> N "how"` (records a milestone +
   retires the hazard). `brain note` rejects a near-duplicate of a live entry and
   shows what it collided with: reconcile with `--supersedes N`, or pass
   `--distinct` when it genuinely is a separate fact. Two live entries that
   disagree is the failure that gate exists to stop.
   **`papercut` inverts this.** A papercut is friction you would NOT open a board
   row for today, and its value is cumulative. Duplicate papercuts never block:
   they COUNT, and `brain note` says when one has recurred enough to promote to
   the board. `brain papercuts` pulls the same signal clustered.
4. **What *is*, never planned state** — except the ticket board. A `question`
   entry is the sanctioned way to record a genuine unknown.
5. **New scope**: the first `brain note --project <slug>` creates the stream.
6. **No secrets, ever.** Env var names OK; values never.

## Retrieval

- `brain wake --scope <s>` — root state + newest raw tail + pull map (what
  session-start injection shows).
- `brain hazards --scope <s> [--grep RX]` — live hazard index; read BEFORE
  touching a subsystem.
- `brain design --scope <s> [--grep RX]` — live invariants; read BEFORE
  redesigning a subsystem.
- `brain lint [--scope <s>]` — deterministic integrity audit: provenance
  citations resolve live, supersession chains acyclic, label near-duplicates,
  view budgets.
- `brain grep RX [--scope <s>]` — raw search across streams.
- `brain zoom <s> <lo>-<hi>` — raw entries for a range.
- `brain decisions|milestones|questions|gotchas --scope <s> [-n N]` — typed lists.
- `brain papercuts [--scope <s>]` — open papercuts clustered by recurrence.
- `brain recall "<text>"` — live rulings across scopes bearing on a topic,
  verbatim (what the conversational hook pushes).
- `brain branches <s>` / `brain status` — tree shape / store health.
- `brain history "<query>"` — the un-curated transcript archive (pull-only).

## Enforcement (hooks)

The `hooks/` directory holds the Claude Code enforcement layer. Every hook
resolves the instance from `BRAIN_ROOT` / config / the default `brain/` folder —
none hardcode a path. All injection hooks honor `BRAIN_NO_INJECT=1`.

- **SessionStart `kb_card_inject.py`** — injects the cwd scope's `[BRAIN WAKE]`
  (root state + newest raw tail + pull map). Scope = the first path component
  under `BRAIN_PROJECTS_ROOT`, or the directory basename when that is unset.
- **PostToolUse `kb_hazard_inject.py`** — on a project-code Write/Edit, injects
  the live standing entries (hazards + invariants) whose subsystem matches the
  edited file's path. Fires for every session — hazards are keyed to the files a
  session actually touches, which no brief can predict.
- **UserPromptSubmit `kb_topic_inject.py`** — when a prompt raises a topic that
  live decisions/questions/invariants already speak to, injects those rulings
  verbatim, once per entry per session.
- **Stop `kb_stop_guard.py`** — on sessions that edited project code, blocks the
  first stop until the session ran `brain note`/`brain resolve` or wrote inside
  the data root. `BRAIN_NO_STOP_GUARD=1` exempts a delegated worker.
- **PreToolUse `kb_write_authority.py`** — denies writes into the data root from
  a delegated worker (`BRAIN_DELEGATE=1`): the stream is single-writer per
  instance.
- **SessionEnd `kb_session_log.py`** — appends one observational audit line per
  session under `_state/`.

Hook command paths in `settings.json` must use **forward slashes** — the hook
runner treats backslashes as escape sequences.

## The ticket board — all tracked work

The **one sanctioned exception** to "the brain records what is". CLI:
`tickets.py`; store: per-writer append-only event logs + a deterministic fold —
the board is computed, never stored, so it is conflict-free under folder sync.
Every ticket's key carries the prefix of the project it belongs to (`WEB-17`,
`API-3`), set at `open --prefix`. Owner set = work only that person can do;
unowned = any session may claim it, and doing it is the response, not scheduling
it. A due date means an external deadline or a genuinely date-gated watch, never
a schedule for doable agent work. Closing requires `--source`: evidence (doc
name, commit, url) or the explicit attestation `person:<writer>` — never silent.

## Portability invariant

Knowledge in plain text (streams + markdown); intelligence in the harness; glue
thin and enumerable (six hooks + three CLIs + a few env markers). The engine has
zero LLM dependency and zero OS-scheduler dependency: the tree is rebuilt
mechanically on write. On a git-backed machine every write commits; on a no-git
folder-sync machine the append-only spool files plus the sync's version history
are the record, and `brain doctor` says which profile is active.
