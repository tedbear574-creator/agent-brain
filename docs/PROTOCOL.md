---
type: Reference
title: "GKB Conventions & Write Protocol"
description: "GKB v3 protocol: typed append-only streams + generated trees, capture/retrieval rules, assistant layer, enforcement hooks."
tags: [meta, conventions, protocol, gkb]
timestamp: "2026-07-27T00:00:00Z"
---

This file is the canonical reference for how to read and write this **GKB (Global Knowledge Bank)** bundle — protocol **v3** (full cutover 2026-07-27; v0.2 card protocol retired, its cards distilled into streams and tombstoned; pre-cutover content lives in this repo's git history). It is **not** a project or dependency record — do not add project-specific content here.

## The model — memory is a stream, not a document

Per scope (≈ project slug; assistant-home uses scope `global`):

- **`stream/<scope>.log`** — the source of truth. Append-only typed entries, one line each: `TS | type | [meta] | text`. Types: `decision`, `milestone`, `gotcha`, `invariant`, `question`, `papercut`. Caps are per type: **800 chars** for `decision`/`question`, which carry the reasoning, and **300** for the rest, where terseness is the feature. Never edited; corrections are new entries with `--supersedes N`.
- **`tree/<scope>/`** — GENERATED caches, never hand-edited: `root.md` (watermarked lossy state summary), `b<lo>-<hi>.md` (branch summaries, 32 entries/branch), `hazards.md`/`design.md`/`hazards.tsv` (verbatim standing indexes by subsystem + label cache). Rebuilt by `gkb rebuild` (haiku branches/labels, sonnet roots; also the daily 08:05 `gkb-rebuild` task; every rebuild ends with a deterministic `lint` pass).

**Two paths, deliberately:**

- **State** (decision/milestone/question) compresses into `root.md` — lossy by design. It orients; it does not carry detail.
- **Standing** (gotcha/`invariant`) is **never summarized** — only indexed verbatim by subsystem. Hazards are what must not be re-hit; invariants are what must stay TRUE about a subsystem's design (its load-bearing constraints — the design layer). Hazards leave the live index only via `gkb resolve`; invariants only via `--supersedes`. Pull `gkb hazards` before touching a subsystem, `gkb design` before redesigning one; the injected state carries neither.

## Write rules

0. **Route before you write.** Would you dispatch a fix for it today? → open a ticket (`tickets.py open ... --root ~/.claude/global-kb/tickets`), NOT a note; the board is the one home for a fixable defect. Must the next session know it to work correctly? → `gotcha`. Neither, but it cost you time? → `papercut`. A note that describes a defect you intend to fix is misfiled — the CLI's fix-language gate catches the obvious cases (`--force-type` overrides), but compressing to fit the cap can strip the very words it matches on, so decide the route first and write second.
1. **Capture inline, the moment it lands** — not at session end:
   `python ~/.claude/okf-tools/gkb.py note --project <scope> --type <T> "one line"`
   Standing entries (`gotcha`) **require** `--subsystem "label"` — the command errors without it, because the label is the verbatim index's only retrieval axis. Reuse an existing label for the same part of the system (check `gkb hazards`), never mint near-synonyms (trivial variants snap automatically). A label starting `*` marks the entry CROSS-CUTTING: it injects into every scope's edit gate (owner-set only; use for hazards that rhyme across projects — Windows, claude-CLI, Shopify). An invariant should name the code path/artifact it constrains; when a change falsifies one, supersede it — even re-noting identical text re-dates it (lint flags invariants unconfirmed >120d).
2. **One fact per entry, inside its cap.** Capture what the diff can't tell you: the decision and its why, the gotcha, the abandoned approach, the open question. Never replay a diff. **Reasoning that will not fit belongs in `projects/<scope>/<topic>.md`** — the deep doc holds the model, the derivation, the ledger; the entry holds the ruling and points at it. Compressing an argument down to one line until it reads as shorthand loses the part worth keeping. `gkb wake` names a scope's deep docs, so they are found without a routing table.
3. **Supersede, don't edit.** Stale or wrong entry → `gkb note --supersedes N` with the corrected fact; it drops out of the summary/index path. Fixed a known gotcha → `gkb resolve --project <scope> N "how"` (records a milestone + retires the hazard). `gkb note` rejects a near-duplicate of a live entry and shows what it collided with: reconcile with `--supersedes N`, or pass `--distinct` when it genuinely is a separate fact. Two live entries that disagree is the failure that gate exists to stop.
   **`papercut` inverts this.** A papercut is friction you would NOT open a board row for today, and its value is cumulative — one annoyance is not worth a row, the same annoyance three times is, and with nowhere to put the first two you never learn it recurs. So duplicate papercuts never block: they COUNT, and `gkb note` says when one has recurred enough to promote. `gkb papercuts` pulls the same signal clustered. Nothing schedules a drain — recurrence raises itself.
4. **What *is*, never planned state** — except the ticket board (below). A `question` entry is the sanctioned way to record a genuine unknown.
5. **New scope**: the first `gkb note --project <slug>` creates the stream. Add a tombstone-style card in `projects/<slug>.md` (frontmatter + pointer body — copy an existing tombstone) so indexes and `affects:` routing see it, then run the index generator.
6. **No secrets, ever.** Env var names OK; values never.

## Retrieval

- `gkb wake --scope <s>` — root state + newest raw tail + pull map (what session-start injection shows).
- `gkb hazards --scope <s> [--grep RX]` — live hazard index; read BEFORE touching a subsystem.
- `gkb design --scope <s> [--grep RX]` — live invariants; read BEFORE redesigning a subsystem.
- `gkb lint [--scope <s>]` — deterministic integrity audit: provenance citations resolve live, supersession chains acyclic, label near-duplicates, view budgets.
- `gkb grep RX [--scope <s>]` — raw search across streams (all scopes when unscoped).
- `gkb zoom <s> <lo>-<hi>` — raw entries for a range (drill below a branch summary).
- `gkb decisions|milestones|questions|gotchas --scope <s> [-n N]` — typed listings.
- `gkb papercuts [--scope <s>]` — open papercuts clustered by recurrence; promote the recurring ones.
- `gkb recall "<text>"` — live rulings across all scopes bearing on a topic, verbatim. What the conversational gate pushes; run it by hand when you want more than the gate surfaced.
- `gkb branches <s>` / `gkb status` — tree shape / store health.
- Cross-project coupling: grep frontmatter `affects: <slug>` across the bundle (tombstones keep `affects:`).

## Enforcement (hooks)

- **SessionStart `kb_card_inject.py`** (interactive sessions only; `CCDESK_ROLE` sessions get nothing): injects `user.md` + the cwd scope's `[KB WAKE]` (falls back to the card body for scopes with no stream) + due-or-overdue tickets from the HUB board. New project dirs whose name ≠ scope slug need one line in `DIR_TO_SLUG`.
- **PostToolUse `kb_hazard_inject.py`**: on the first project-code Write/Edit in a scope, injects the live standing index (hazards + invariants) once per session — budget-bounded: whole groups ordered by relevance to the edited path, then recency; overflow degrades to a one-line digest naming the labels + entry numbers.
- **UserPromptSubmit `kb_topic_inject.py`**: the conversational counterpart. When a prompt raises a topic that live decisions/questions/invariants already speak to, injects those rulings verbatim — once per entry per session, silent unless the lexical match is strong. Skipped for `CCDESK_ROLE`: a topic match on the dispatcher's prose is exactly the ambient identity the role gate keeps out (unlike file-keyed hazards, which delegates DO get).
- All injection hooks honor `GKB_NO_INJECT=1` (a control-arm session for the seeded-trap replay provably receives no KB context).
- **Stop `kb_stop_guard.py`**: fires only on sessions that edited project code (any Write/Edit outside `~/.claude/`). Blocks the first stop until the session ran `gkb note`/`gkb resolve` or wrote under `global-kb/`. Even a trivial change is one line — `[kb-skip]` is retired. Files created via shell heredocs are a known blind spot.
- Hook command paths in `settings.json` must use **forward slashes** — the hook runner treats backslashes as escape sequences.

## What lives where (v3)

| Knowledge type | Home |
|---|---|
| Decisions, gotchas, milestones, open questions, invariants, papercuts | `stream/<scope>.log` via `gkb note` |
| Orientation state (generated) | `tree/<scope>/root.md` — never hand-edit |
| Project identity + routing pointer | `projects/<slug>.md` tombstone (frontmatter + pointers only) |
| Advisory reasoning, models, derivations, ledgers — anything that does not survive a one-line cap | `projects/<slug>/<topic>.md`; the ruling goes in the stream and points here. `gkb wake` lists them |
| Interface contract for a shared dep | `deps/` |
| Generic reusable how-to | `playbooks/` |
| Durable cross-cutting facts | `reference/` |
| Tracked work — Tod's dated commitments AND agent-doable items | the ticket board: `tickets/` (CLI `okf-tools/tickets.py`; per-ticket project prefixes); owner `tod` = his alone, unowned = take it; dates only for external deadlines or date-gated watches; close needs `--source` |
| User model | `user.md` |
| Local build/run/test, env/secrets, code-coupled constraints | in-repo `CLAUDE.md` only |

Contracts, playbooks, and reference/deep docs stay as hand-written documents with the v0.2 frontmatter (`type`, `title`, `description`, `tags`, `timestamp`, `status`, `affects`, `last-verified`) — they're reference material, not memory, and `affects:` remains the cross-project routing key. Factor generic learnings into `playbooks/` so other projects find them.

## Tombstone card convention

Every scope keeps a minimal `projects/<slug>.md`: full frontmatter (for `index.md` generation and `affects:` grep) + a body of pointers only — wake/hazards/capture commands, surviving deep docs, repo location, and the archive note (`git log -- projects/<slug>.md`). No state in the body; the stream owns state.

## Project lifecycle

Lifecycle is carried in the tombstone's `status:` and recorded as a `decision`/`milestone` entry in the stream:

| State | `status:` | Filesystem |
|---|---|---|
| Live | `active` / `partial` / `broken` | `Documents\Claude\<dir>\` |
| Parked | `parked` | stays in place |
| Superseded / absorbed / dead | `superseded` | move dir to `Documents\Claude\_archive\<dir>\` |

Archiving is `mv` only — never rewrite the archived repo. The stream + tombstone stay forever. A killed experiment gets a `gotcha` entry stating why (so it doesn't come back).

## Assistant Layer

**Core invariant: push context by role, pull context by graph.** Interactive sessions get identity + scoped injection; delegated workers get their plan only; everything else is reachable on demand and injected never. No store is injected whole into every context; no similarity-based retrieval, anywhere. (Anti-bloat: the claude.ai memory failure was automatic writes + similarity recall + no lifecycle. GKB counters: deliberate typed writes + deterministic routing + lifecycle.)

### `user.md` (bundle root) — the user model

- Declarative model of the user: role, preferences, working style, active threads at ONE-LINE altitude. No project detail (streams own that), no behavioral instructions (CLAUDE.md owns those).
- **Hard budget: 40 lines body.** A new fact must evict a weaker one. Character sheet, not diary.
- Agent-curated: any session may update it; hygiene review enforces the budget.
- Injected into every **interactive** session regardless of cwd. Never into executor-role sessions.

### The ticket board (`tickets/`) — all tracked work

Every ticket's key carries the prefix of the project it belongs to (`CCDESK-17`, `WEALTH-PLAN-7`), set at `open --prefix` or moved later with `edit --prefix`; `HUB` is reserved for genuinely hub-level items. Numbers are board-wide, so a re-label never changes a ticket's number and bare-number lookup stays unambiguous.

The **one sanctioned exception** to "GKB records what is". CLI: `okf-tools/tickets.py` (spec: `okf-tools/TICKETS-SPEC.md`); store: per-writer append-only event logs + a deterministic fold — the board is computed, never stored. Owner `tod` = work only Tod can do (a decision, an external login, his own body — every such ticket states the exact next action); unowned = any session may claim it, and doing it is the response, not scheduling it. A due date means an external deadline or a genuinely date-gated watch (a policy that resumes, a scheduled review), never a schedule for doable agent work. Closing requires `--source`: evidence (doc name, commit, url), or the explicit attestation `person:<writer>` — never silent. Ticket titles follow plain-words; legacy agenda/dispatch row ids live in migrated tickets' notes.

### Role-gated injection

Delegated workers (ccdesk owner runs, fusion panelists, verify turns) run with env `CCDESK_ROLE` set. SessionStart injection and the Stop capture obligation skip them: a delegate's context is its brief, curated by the dispatcher — never ambient identity. The PostToolUse hazard gate is the exception and DOES fire for them: hazards are keyed to the files a session actually touches, which no brief author can predict, and delegated sessions do the bulky edits most likely to re-hit one.

### Assistant-home project

`Documents\Claude\assistant\` (scope `global`) — the hub whose role is the aggregate view: ticket-board triage and drain, cross-project overlap scan, hygiene. Project sessions stay narrow specialists; consistent presence comes from `user.md`.

### Hygiene (standing duty)

Weekly, in assistant-home: work the ticket board — overdue tickets re-dated or surfaced to Tod, unowned tickets dispatched (an open ticket a session could do IS the staleness), claimed tickets with dead sessions reopened, blocked tickets without a named blocker resolved; compress `user.md` back under budget; run `gkb status` and rebuild stale trees; supersede entries found wrong in passing.

### Portability invariant

Knowledge in plain text (streams + markdown); intelligence in the harness; glue thin and enumerable (three hooks + one CLI + one env marker). Provider switch = rewrite the hooks, lose nothing.

## Generated views & tools

- **`~/.claude/okf-tools/gkb.py`** — the v3 CLI (note/resolve/wake/hazards/grep/zoom/branches/typed listings/rebuild/status).
- **`~/.claude/okf-tools/gen-index.ps1`** (→ `gen_index.py`) — regenerates root + sub-indexes from card frontmatter (tombstones included) + lints `affects:` slugs.
