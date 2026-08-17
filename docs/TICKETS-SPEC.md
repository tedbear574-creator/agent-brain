# tickets — spec (ratified 2026-08-16, global stream #123–#127)

Multi-writer ticket board: append-only per-writer event logs + a deterministic
fold. Formalizes the assistant agenda/dispatch split into one system that also
deploys standalone at work (OneDrive-synced shared folders, non-technical
colleagues driving it through their own Claude sessions). Sibling of `gkb.py`:
same philosophy (typed append-only stream, generated views, no model anywhere
in the read path).

## Constraints (ratified, do not relitigate)

- **One file per writer, forever.** OneDrive conflict-copies corrupt
  multi-writer files; a single-writer file can only sync late, never fork.
  Locally (git-managed roots) concurrent appends to one file are fine —
  append mode + short retry, same as gkb.py's stream write.
- **Writer id is derived, never manually set.** Basename of the user profile
  dir (`C:\Users\jsmith` → `jsmith`). `TICKETS_WRITER` env may override
  (agents), but nothing ever *requires* setting it — manual identifiers were
  skipped in practice and defaults clobbered.
- **Single stdlib-only python file** (`tickets.py`, py3.10+), deployed by
  copying the file. No git dependency in any code path.
- **Closure is never silent about evidence.** `close` hard-requires
  `--source`: free text — a document NAME is fine (no path needed), a commit,
  a url, a conversation description — or the explicit attestation fallback
  `person:<writer>` ("the person closing is the source"). The error message
  on a missing source explains exactly this.
- **Dates are external deadlines** (client, filing, a person waiting), never
  a schedule for agent work. Say so in `--due` help text.
- **Plain words in every human-facing output** (board, page, errors): readers
  are non-technical; no jargon, no internal type names.

## Store

```
<root>/config.json          {"prefix": "ACME-DOC", "name": "Acme TP documentation 2026"}
<root>/events/<writer>.jsonl
<root>/board.md, board.html, board.csv   (generated only — never hand-edited)
```

Root: `--root DIR` or `TICKETS_ROOT` env; error with a plain explanation if
neither set or config.json missing. `tickets init --root DIR --prefix P
--name N` creates it. **Prefix is required and must be meaningful** — the
specific engagement/project (a client with three engagements = three roots,
three prefixes), never generic.

**Per-ticket prefix** (multi-project boards, e.g. the hub): a ticket may
carry its own short code (`open --prefix`, `edit --prefix`; blank reverts to
the board's), stored on the event, uppercased for display. A board whose
tickets all read the same prefix disambiguates nothing (user ruling
2026-08-16). **Each prefix numbers from 1 independently.** A ticket's
effective prefix is its own when set, else the board's; `propose_n` at `open`
= max number among live+closed tickets *sharing that effective prefix*, + 1.
So the first ticket under a new code is `CODE-1` even on a board that already
holds tickets under other codes. `edit --prefix` keeps the ticket's number
when that number is free in the target code's space, otherwise the event
carries the next free number there and the fold applies it (the CLI prints
the resulting key either way). The permanent hex id never changes and is the
reference that always resolves. Resolution: a full key `PREFIX-n` matches
within that prefix's space; a bare `n` matches the board's own prefix first,
then — if unique board-wide — any single ticket carrying it, and errors with
the candidate keys when several prefixes share the number.

## Events

One JSON object per line: `{"ts": ISO-8601 seconds, "writer": str,
"verb": str, "id": hash, ...}` where `id` is the canonical ticket identity —
8 lowercase hex chars, random at `open`, carried by every later event.
(Widened from 6 after review: two writers minting offline must not collide;
if the near-impossible collision happens anyway the fold warns loudly and
keeps the first — silent loss is never allowed.)

Verbs: `open` (title, propose_n, optional owner/due/notes/prefix), `claim`
(owner ← writer unless `--owner`), `block` (reason), `reopen`, `close`
(source REQUIRED, optional note), `edit` (any of
title/owner/due/notes/prefix), `update` (text — the ticket's one-line
"where this stands"; only the latest shows on board/page/csv, every earlier
one stays recoverable in the log because updates are appended events like
everything else), `comment` (text).

## Fold (deterministic — the board is computed, never stored)

Replay all events from all writer files, ordered by `(ts, writer,
line-number)`. Display number: honor `propose_n` (creator writes the next
free number in the ticket's effective-prefix space); if two tickets proposed
the same number **under the same prefix**, the later one (by replay order)
keeps the number **with a letter suffix** (`5` and `5b`) — nothing is ever
renumbered to a different number, so a clash between two tickets can never
move a third. The same number under two different prefixes is not a clash and
takes no suffix. An old board numbered board-wide folds to identical keys:
board-wide allocation left no `(prefix, n)` pair duplicated, so folding its
events under the per-prefix rule yields exactly the same numbers (no
migration, events are never rewritten). (Replaced "loser takes next free" after
review: next-free assignment cascades when a late-syncing earlier-timestamp
ticket arrives — its victim steals the next number, whose owner steals the
next, and so on. Suffixes make the blast radius exactly the colliding
ticket, and even it keeps its numeral.) Residual, documented: a ticket's key
can *gain* a suffix when a colliding earlier ticket syncs in late; mutating
verbs echo the resolved key + title so a mis-typed key is visible. Status
derivation: open → claimed (has owner) → blocked / closed; `block` records
its reason in a dedicated field (never clobbering notes), `reopen` clears
it. Later `edit` wins per field.

## CLI

`open, claim, close, block, reopen, edit, update, comment, show, board,
page, export, log, init`. The served page has full read and write parity
with the CLI: every verb is a button/form, and a per-ticket History view
(GET `/api/log?ticket=`) renders the same lines as `tickets log`. Every ticket argument accepts bare `n`, `PREFIX-n`, or
the hash. `board`: terminal table (key, title, owner, due, status), sorted
due-first with overdue flagged, closed hidden unless `--all`. `page`: fully
regenerate `board.md` + `board.html` (self-contained inline CSS, no external
assets, readable on any machine). `export`: `board.csv`, Excel-friendly
(UTF-8 BOM; cells starting `=`/`+`/`-`/`@` get a leading apostrophe so Excel
never runs them as formulas). `log TICKET`: the event history, human-readable.

## Tests + gate

`tests/test_tickets.py` (pytest): fold determinism (shuffled file read order,
same board); propose_n collision race renumbers only the loser; close without
source rejected with the attestation hint; writer derivation + env override;
id resolution (n / PREFIX-n / hash); page + csv generation; reopen/edit
semantics. Gate: `python -m pytest tests/test_tickets.py -q`.

## Out of scope for the build

gkb.py, agenda.md, dispatch.md, hooks, wake integration — migration and the
doc sweep happen at the hub after merge.
