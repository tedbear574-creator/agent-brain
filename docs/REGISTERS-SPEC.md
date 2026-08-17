# registers.py — generic record store (ratified spec)

A register holds repeated records of one declared shape: an entity master, an
interview index, an employee list. **The engine is code; the shape is data** —
standing up a new register is `init` with a schema, zero new code. Facts live
in registers; judgment lives in the stream, citing record keys. Sibling of
tickets.py on the same proven chassis (per-writer spool, deterministic fold,
unstyled local web board); shared code is extracted only when maintaining both
actually hurts — never rebuild the live ticket board for symmetry.

## Constraints (inherited from tickets, non-negotiable)

- **Multi-writer sync-folder safe**: one append-only JSONL file per writer
  (`events/<writer>.jsonl`); nobody ever writes another person's file.
- **Writer identity is derived** (profile-path basename), never typed;
  `REGISTERS_WRITER` env override for agents/tests.
- **The register is computed, never stored**: deterministic fold over all
  events ordered `(ts, writer, line-number)`; generated views only.
- **Plain words in every human-facing output**; readers are non-technical.
- **Unstyled**: structural CSS only; deployments restyle on top.
- **Nothing is ever overwritten**: every change is an appended event; any
  prior value is recoverable from the log.

## Store

```
<root>/config.json   {"name", "prefix", "key"?, "schema": [field, ...]}
<root>/events/<writer>.jsonl
<root>/register.md, register.html, register.csv, history.csv  (generated only)
```

Field: `{"name": snake_case, "label": plain words, "kind":
"fixed"|"current"|"tracked", "type"?: "text"|"number"|"date" (default text),
"required"?: bool}`. Kinds are the change semantics:

- **fixed** — identity, set at `add`. Not offered for routine editing;
  a typo is repaired with `correct`, which REQUIRES a note saying why.
- **current** — later `set` wins per field; history stays in the log.
- **tracked** — a value series: each `post` carries the value and an
  **as-of label** (`2024`, `FY25`, a date). The latest (by as-of, then ts)
  shows on the register; any prior period is retrievable (`show`, `log`,
  `history.csv`). This is the ticket latest-status mechanism, generalized.

`key` (optional) names a required fixed field used as the natural key
(entity code, employee ID) — unique, matched case-insensitively; the fold
warns loudly on a cross-writer duplicate and keeps the first. Without `key`,
records get `PREFIX-n` numbers with the tickets letter-suffix collision rule
(nothing ever renumbers).

**Schema evolution**: appending a new field is always safe (old records show
blank). Changing a field's kind or type, or removing a field, is forbidden —
mark `"hidden": true` to retire one from forms and views. Labels may be
reworded freely. Get the schema right first; the data outlives the code.

## Events

`{"ts": ISO-8601 seconds, "writer", "verb", "id", ...}` — `id` is 8 random
hex chars minted at `add`, carried by every later event (natural keys are
display/lookup; the id is identity, so a corrected key strands nothing).

Verbs: `add` (required fixed fields + any initial values), `set` (current
fields), `post` (one tracked field: value + as-of), `correct` (fixed field +
required note), `retire` (record leaves the active view; required reason),
`restore`, `comment` (text).

## Fold

Records keyed by id. current: later wins per field. tracked: full series
kept per field `[{asof, value, writer, ts}]`, latest = max(asof, ts).
Retired records hidden unless `--all`. Unknown fields in an event are
ignored with a stderr warning (forward compatibility), never a crash.

## CLI

`init --root DIR --prefix P --name N --schema-file schema.json`, then
`add, set, post, correct, retire, restore, comment, show, board, page,
export, log, view, serve`. Record arguments accept the natural key, a
`PREFIX-n` key, or the hex id. `show` prints the record with each tracked
field's full series. `export` writes `register.csv` (one row per record,
latest values; tracked fields get paired `<field>` and `<field> as of`
columns) and `--history` adds `history.csv` in long format —
`key, field, as_of, value, writer, ts` — deliberately pivot-table-ready,
because Excel is the v1 summarization engine. Excel formula guard
(leading-apostrophe on `=+-@`) and UTF-8 BOM as in tickets.

## Serve (the interactive register)

Same chassis and guarantees as the ticket board: 127.0.0.1 only, per-run
token + Origin allowlist, writer derived server-side, all writes through the
same `act_*` core as the CLI, heartbeat + goodbye + idle watchdog so closing
the last tab shuts it down, `Open register.cmd` launcher (pythonw). Forms
are **schema-driven**: labels from config; fixed fields read-only after add
with `correct` behind an explicit control; each tracked field gets a
"Post <label>" form asking value + as-of; per-record History via
`GET /api/log`. Full read/write parity with the CLI.

## Out of scope for v1

In-tool summarization/pivots (that's `history.csv` + Excel; revisit on
verbalized wishes), cross-register joins, schema-migration tooling, any
unification refactor with tickets.py.

## Tests + gate

`tests/test_registers.py` (pytest): fold determinism under shuffled read
order; schema validation (unknown field, missing required, bad kind — plain
errors); natural-key duplicate warns loudly, keeps first; auto-number
letter-suffix no-cascade; tracked series ordering + as-of retrieval;
correct-requires-note; retire/restore; CSV + history.csv shape and formula
guard; serve suite (lifecycle in acting writer's file, 400/403s, forged
writer ignored, self-contained page, heartbeat/goodbye/idle shutdown);
launcher. Gate: `python -m pytest tests/test_registers.py -q`.
