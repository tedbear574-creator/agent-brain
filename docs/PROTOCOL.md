# Agent Brain — conventions & write protocol

The canonical reference for how to read and write an Agent Brain instance. It
describes the protocol, not any one deployment: everything here holds for a
fresh instance on day one.

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
  of the stream — no model calls, no scheduler. Rebuilt inline on every write,
  so never stale; `brain rebuild` regenerates it from the stream at any time,
  ending with a deterministic `lint` pass.

**Two paths, deliberately:**

- **State** (decision/milestone/question) compresses into `root.md` — lossy by
  design. It orients; it does not carry detail.
- **Standing** (gotcha/`invariant`) is **never summarized** — only indexed
  verbatim by subsystem. Hazards are what must not be re-hit; invariants are what
  must stay TRUE about a subsystem's design (its load-bearing constraints).
  Hazards leave the live index only via `brain resolve`; invariants only via
  `--supersedes`. Pull `brain hazards` before touching a subsystem, `brain
  design` before redesigning one; the injected state carries neither.

## Three-class routing — pin / stream / register

There are three homes for a durable fact, and the class decides the home:

- **Pin** — an always-loaded identity fact. A pin is an `invariant` marked
  `--pin`; it renders verbatim at the top of every wake, so a scope's pins share
  a hard character budget. For the handful of facts every session must see
  (`brain note --type invariant --pin --subsystem identity "..."`).
- **Stream** — everything else that is memory: a decision and its why, a gotcha,
  an abandoned approach, an open question. Retrieved on demand (`wake`,
  `hazards`, `design`, `recall`, `grep`), not pushed whole.
- **Register** — repeated records of ONE schema (entities, documents,
  interviews, expenses…). Not memory: a structured multi-writer table with its
  own folder, config, and CSV/board faces (`registers.py --root <dir>`).

## Write rules

0. **Route before you write.** Would you dispatch a fix for it today? → put it on
   the **ticket board** (`tickets.py open ... --root <board>`), NOT a note; the
   board is the one home for a fixable defect. Must the next session know it to
   work correctly? → `gotcha`. Neither, but it cost you time? → `papercut`. A note
   describing a defect you intend to fix is misfiled — the CLI's fix-language gate
   catches the obvious cases (`--force-type` overrides), but compressing to fit the
   cap can strip the very words it matches on: decide the route first, write second.
1. **Capture inline, the moment it lands** — not at session end:
   `python brain.py note --project <scope> --type <T> "one line"`.
   Standing entries (`gotcha`/`invariant`) **require** `--subsystem "label"` — the
   command errors without it, because the label is the verbatim index's only
   retrieval axis. Reuse an existing label for the same part of the system (check
   `brain hazards`), never mint near-synonyms (trivial variants snap
   automatically). A label starting `*` marks the entry CROSS-CUTTING: it injects
   into every scope's edit gate (owner-set only — use for hazards that rhyme
   across projects). An invariant should name the code path or artifact it
   constrains; when a change falsifies one, supersede it — even re-noting
   identical text re-dates it (`brain lint` flags invariants unconfirmed for over
   120 days).
2. **One fact per entry, inside its cap.** Capture what the diff can't tell you:
   the decision and its why, the gotcha, the abandoned approach, the open question.
   Never replay a diff. **Reasoning that will not fit belongs in a deep doc** under
   the data root (e.g. `projects/<scope>/<topic>.md`) — the doc holds the model,
   derivation, ledger; the entry holds the ruling and points at it. Compressing an
   argument into shorthand loses the part worth keeping. `brain wake` names deep docs.
3. **Supersede, don't edit.** Stale or wrong entry → `brain note --supersedes N`
   with the corrected fact; it drops out of the summary/index path. Fixed a known
   gotcha → `brain resolve --project <scope> N "how"` (records a milestone +
   retires the hazard). `brain note` rejects a near-duplicate of a live entry and
   shows what it collided with: reconcile with `--supersedes N`, or pass
   `--distinct` when it genuinely is a separate fact. Two live entries that
   disagree is the failure that gate exists to stop.
   **`papercut` inverts this.** A papercut is friction you would NOT open a board
   row for today, and its value is cumulative — one annoyance is not worth a row,
   the same annoyance three times is, and with nowhere to put the first two you
   never learn it recurs. Duplicate papercuts never block: they COUNT, and
   `brain note` says when one has recurred enough to promote to the board —
   nothing schedules a drain, recurrence raises itself. `brain papercuts` pulls
   the same signal clustered; promote the recurring ones.
4. **What *is*, never planned state** — except the ticket board (below). A
   `question` entry is the sanctioned way to record a genuine unknown.
5. **New scope**: `brain new-project <slug> --dir PATH [--prefix PREFIX]` does the
   whole stand-up in one idempotent call — `git init` + a first commit in PATH, the
   founding stream milestone, the scope's identity card (see *Scope cards &
   lifecycle*), and the ticket prefix (default = slug upper-cased) registered on the
   board. Re-running reports what already exists and changes nothing, so it is also
   the repair tool for a half-built scope. (A bare `brain note --project <slug>` still
   creates a stream on its own, but leaves the card, repo, and prefix for you.)
6. **No secrets, ever.** Env var names OK; values never.

## Retrieval

- `brain wake --scope <s>` — pins + computed state + pull map + board (human / cross-scope pull); `--inject` prints only the header line + pins + computed state, the two sections the SessionStart hook composes into the injection.
- `brain hazards --scope <s> [--grep RX]` — live hazard index; BEFORE touching a subsystem.
- `brain design --scope <s> [--grep RX]` — live invariants; BEFORE redesigning one.
- `brain lint [--scope <s>]` — deterministic integrity audit: provenance resolves live, supersession acyclic, label near-duplicates, view budgets.
- `brain grep RX [--scope <s>]` — raw search across streams.
- `brain zoom <s> <lo>-<hi>` — raw entries for a range (drill below a branch gist).
- `brain decisions|milestones|questions|gotchas --scope <s> [-n N]` — typed lists.
- `brain papercuts [--scope <s>]` — open papercuts clustered by recurrence.
- `brain recall "<text>"` — live rulings across scopes bearing on a topic, verbatim (what the conversational hook pushes; run it by hand for more than it surfaced).
- `brain branches <s>` / `brain status` — tree shape / store health.
- `brain history "<query>"` — the un-curated transcript archive (pull-only).
- Cross-scope coupling: a scope card's `affects:` key names coupled scopes — grep card frontmatter.

## Enforcement (two layers)

Enforcement lives in the ENGINE so every surface gets the discipline; the hooks
are accelerators for one surface, not the mechanism. It is NEVER a wall: no write
is blocked, no session refused service — enforcement is notices and obligations.
Both layers run the SAME capture-or-attest contract: capture what the diff can't
tell, or attest that there is genuinely nothing.

**Engine-side (every MCP surface).** The MCP server (`brain_mcp.py`) is a
per-session process, so it carries session state: each mutating board/register
call (`tickets_open` / `tickets_update` / `tickets_close`, `registers_add` /
`registers_post`) accrues one unit of *capture debt*; a `brain_note`/
`brain_resolve` clears it, and `brain_attest` — the sanctioned "nothing to
capture" exit — clears it and records a one-line reason to
`<root>/_state/attest.log`. From two units of debt on, every tool result carries
an advisory line to capture or attest — never blocking.

**Hook-side (Claude Code).** The `hooks/` directory holds the Claude Code
enforcement layer. Every hook resolves the instance from `BRAIN_ROOT` / config /
the default `brain/` folder — none hardcode a path. All injection hooks honor
`BRAIN_NO_INJECT=1`. The Stop hook gates on session substance (below).

- **SessionStart `kb_card_inject.py`** — injects only what is COMPUTED per
  session, in a fixed order under ONE hard total budget (7000 chars): the cwd
  scope's header + pins (`brain wake --inject` §1), `[BRAIN USER]`, `[BRAIN
  BOARD]` (due tickets once + a summary line), `[BRAIN INBOX]`, then `[BRAIN
  STATE]` (§5, trimmed first when over). No instructional prose — that lives in
  the deployment's session rules. Scope = first path component under
  `BRAIN_PROJECTS_ROOT`, else the directory basename (`BRAIN_SCOPE_ALIASES`,
  `name=scope,...`, remaps names); nothing is injected for a stream-less scope.
- **PostToolUse `kb_hazard_inject.py`** — on a project-code Write/Edit, injects
  the live standing entries keyed to the edited file's path, once per HAZARD per
  session; a first edit matching nothing falls back once to the full standing
  index, budget-bounded — whole subsystem groups ordered by relevance to the edited
  path then recency, overflow degraded to a one-line digest naming labels + entry
  numbers. Fires for every session (why: *Role-gated injection* below).
- **UserPromptSubmit `kb_topic_inject.py`** — when a prompt raises a topic that
  live decisions/questions/invariants already speak to, injects those rulings
  verbatim, once per entry per session, silent unless the lexical match is strong
  (it fires on every prompt — precision beats recall).
- **Stop `kb_stop_guard.py`** — on sessions with substance — a write to any project
  file outside the data root and the config dir, OR a mutating tickets/registers
  command — blocks the first stop until the session ran `brain note`/`brain
  resolve`/`brain attest` or wrote inside the data root; even a trivial change is
  one line. Files created via shell heredocs are a known blind spot: substance is
  read from Write/Edit tool calls. `BRAIN_NO_STOP_GUARD=1` exempts a delegated worker.
- **PreToolUse `kb_write_authority.py`** — denies data-root writes by a delegated
  worker (`BRAIN_DELEGATE=1`): the stream is single-writer per instance.
- **SessionEnd `kb_session_log.py`** — appends one observational audit line per
  session under `_state/`.

Hook command paths in `settings.json` must use **forward slashes** — the hook
runner treats backslashes as escape sequences.

## Scope cards & lifecycle

Every scope keeps an identity card, `projects/<scope>.md`: full frontmatter (the
bundle index generator reads it; `affects:` — the cross-project routing key — is
grepped there) plus a body of pointers only: wake/hazards/capture commands,
surviving deep docs, repo location — no state; the stream owns state. `brain
new-project` writes the card from the engine's `templates/project-card.md`; fill in
its `description`/`tags`/`affects` afterwards. The lifecycle rides on the card's `status:`
(`active`, `partial`, `broken`, `parked`, `superseded`), recorded in the stream as
a decision/milestone. Retiring a project moves its repo directory into the
deployment's archive location — a plain move, never a rewrite; stream and card stay
forever, and a killed experiment gets a `gotcha` saying why, so it does not come back.

## The ticket board — all tracked work

The **one sanctioned exception** to "the brain records what is". CLI:
`tickets.py`; store: per-writer append-only event logs + a deterministic fold —
the board is computed, never stored, so it is conflict-free under folder sync.
Every ticket's key carries the prefix of its project (`WEB-17`, `API-3`), set at
`open --prefix`, movable later with `edit --prefix`; each prefix numbers
independently and the permanent hex id never changes. Owner set = work only that
person can do; unowned = any session may claim it, and doing it is the response,
not scheduling it. A due date means an external deadline or a date-gated watch,
never a schedule for doable agent work. Closing requires `--source`: evidence (doc
name, commit, url) or the attestation `person:<writer>` — never silent.

## The assistant layer

Core invariant: **push context by role, pull context by graph.** Interactive
sessions get identity + scoped injection; delegated workers get their brief only —
curated by the dispatcher, never ambient identity; everything else is reachable on
demand and injected never. No store is injected whole into every context, no
similarity-based retrieval anywhere; countered anti-pattern: automatic writes +
similarity recall + no lifecycle.

**Role-gated injection.** Delegates announce themselves with env markers and the
gates split accordingly: `BRAIN_DELEGATE=1` denies them the data root;
`BRAIN_NO_STOP_GUARD=1` lifts the capture duty — the dispatching session carries
it. The hazard gate is the exception, firing for delegates too: hazards are keyed
to files no brief author can predict, and delegates do the bulky edits most likely
to re-hit one.

### `user.md` — the user model

The data root may carry `user.md`: a declarative model of the user — role,
preferences, working style, active threads — at ONE-LINE altitude; no project
detail (streams own that), no behavioral instructions. Hard budget, enforced by
`brain lint`: a new fact evicts a weaker one — character sheet, not diary.
Agent-curated; how it is injected is deployment wiring.

## Portability invariant

Knowledge in plain text (streams + markdown); intelligence in the harness; glue
thin and enumerable (six hooks + three CLIs + a few env markers). Zero LLM
dependency, zero OS-scheduler dependency: the tree rebuilds mechanically on
write. On a git-backed machine every write commits; on a no-git folder-sync
machine the append-only spool files plus the sync's version history are the
record, and `brain doctor` says which profile is active.

## Shared scopes — one scope, many machines

A solo scope is a single append-only file written by one machine; it corrupts if two
machines on a sync folder (OneDrive, Dropbox) append at once. A **shared scope**
removes that hazard the way the ticket board does: one spool per writer plus a
deterministic read-side fold.

**Layout.** A scope is shared iff `stream/<scope>/` exists (a directory):
`stream/<scope>.log` is the FROZEN legacy history — read forever, appended never —
and `stream/<scope>/<writer>.log` is one append-only spool per writer. A machine
writes ONLY its own spool, so no two machines ever touch the same file and nothing
conflicts on a sync folder. The writer id comes from the `writer` key in
`brain.config.json`, defaulting to the derivation `tickets.py` uses (last segment
of the home path); `BRAIN_WRITER` overrides it.

**Opt-in, one-way.** `brain share --scope X` freezes `X.log` and creates
`stream/X/`. Everything else works unchanged — numbering, supersede refs, and every
read/write path. Sharing cannot be undone: the frozen log guarantees legacy id stability.

**Ids never move.** A legacy entry keeps its numeric id (`7`) — the frozen file makes
it permanent; a spool entry's id is `<writer>:<seq>`, its 1-based line number in that
writer's own spool — append-only, so stable forever. Display order is the fold: entries
sorted by `(write-ts, writer, seq)`, legacy first on a tie. A late-arriving synced spool
may interleave a row earlier or later, but no id ever changes — order is display, id is
identity. Everything referencing an entry (`--supersedes`, `resolve`, pins, the hazard
index) accepts both id forms; cross-writer refs are stored verbatim (`sup=alice:3,7`).

**Fold is deterministic and read-side.** The same set of files produces the same
fold on every machine with no coordination. All read paths (wake, hazards, design,
recall, grep, zoom, decisions/…, rebuild, lint, status) consume the fold; all
write paths append to the current writer's spool only. Caps, the duplicate gate,
and supersede semantics are unchanged — the dup gate compares against the folded
live view; cross-writer supersession is allowed mechanically. `lint` reports a
supersede ref pointing at an id not present yet (a spool not synced) as a WARNING:
sync lag is normal. `brain doctor` names each shared scope's writer set.
