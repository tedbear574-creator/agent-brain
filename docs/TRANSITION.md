# Transitioning an existing documentation system into the brain

This is the seeding guide: how to move a project that already documents itself
some other way — README stacks, a wiki, a `docs/` folder, agent instruction
files, scattered notes — onto Agent Brain without importing garbage or faking
a history.

## The one rule that shapes everything

**Do not bulk-import.** The stream is a record of judgment: decisions with
their reasons, hazards that actually bit, questions actually open. A prose
dump converted to entries produces a fake history — hundreds of "decisions"
nobody decided at a moment nobody remembers, at a volume that buries the
entries that matter. Seeding is a *selection* pass, not a conversion pass.
Most of an existing documentation pile should NOT become memory.

## Step 1 — inventory and sort

Walk the existing docs once and sort every fact you find into one of five
buckets. The routing test for each bucket is the question in bold.

| Bucket | Test | Destination |
|---|---|---|
| Identity facts | **Would every future session need this loaded before it does anything?** (what the project is, the one-line architecture, the non-negotiable constraint) | Pin: `brain note --type invariant --pin --subsystem <label>` |
| Live rulings | **Was this decided, for reasons, and does it still govern?** | `brain note --type decision`, with `--source` citing the doc it came from |
| Hazards | **Did this bite someone, and would it bite again?** (the workaround, the trap, the "never do X here") | `brain note --type gotcha --subsystem <label>` |
| Design invariants | **Must this stay true about a subsystem or the design breaks?** | `brain note --type invariant --subsystem <label>` |
| Open questions | **Is this genuinely undecided?** | `brain note --type question` |

Everything that fails all five tests stays OUT of the brain:

- **How-to and reference material** (build steps, API references, runbooks)
  stays as documents. Documents are reference; the brain is judgment. The
  brain can point at them (`--source docs/deploy.md`), not swallow them.
- **History and changelogs** stay in version control. Git is the archive.
- **Stale content** — anything you wouldn't rewrite today — is dropped, not
  imported. Seeding is the cheapest moment you will ever have to shed it.

## Step 2 — registers, only if clearly sensible

Registers are **not part of a standard transition**. Most projects seed pins
and stream entries and are done. A register earns its place only when all
three hold:

1. There are MANY records of ONE shape (an entity master, an interview index,
   an employee list — dozens plus, not five).
2. The records have fields where you need *the current value* AND *how it
   changed* (headcount superseded yearly, status by review round).
3. Sessions will actually query them ("what is X's value for Y as of Z").

If you are unsure, you do not need one — a pin covers small always-needed
identity, the stream covers judgment, and a plain reference document covers
the rest. A register created for data nobody queries is maintenance debt with
a schema. When one IS clearly sensible: one folder per register,
`registers init --root <dir>`, and load the existing records with
`registers add` — that is a data load, not memory seeding, so bulk is fine
there.

## Step 3 — the seeding session

Do the seeding in one sitting, as a real session:

- Every seeded entry cites its origin: `--source <old-doc-path>`. The entry
  becomes the index; the old doc's git history remains the archaeology.
- Date honesty: seeded entries are true *as of seeding*. The stream starts
  now; it does not pretend to reach back. If the original decision date
  matters, put it in the entry text ("ruled 2024-03: ...").
- Expect the write gates to push back — caps force compression, the
  duplicate gate forces reconciliation. That is the system working. Reasoning
  that will not fit under a cap goes in a `projects/<scope>/<topic>.md` doc
  with the ruling in the stream pointing at it.

## Step 4 — retire the old home

**One home per rule.** After a fact is seeded, the old document either:

- **retires** — delete it; version control keeps it recoverable; or
- **demotes to reference** — it keeps the how-to material the brain
  deliberately excludes, and every ruling that moved to the stream is deleted
  from it in the same session. No "now tracked in the brain" tombstones, no
  pointers to the dead copy.

A doc that still states a rule the stream also states will drift from it, and
the next reader cannot tell which copy is live.

## Done when

A fresh session, given only the session-start wake injection (and the hook
wiring from `install.py`), orients correctly without opening the old docs.
If it still needs the old pile to work safely, the wrong things were seeded —
usually hazards are missing, because prose docs rarely mark what actually
bites. Capture going forward is enforced by the hooks; the transition is a
one-time cost.
