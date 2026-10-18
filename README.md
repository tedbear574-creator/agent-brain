# Agent Brain

Durable, greppable memory for agent-driven work — plain text, stdlib-only, and
portable enough to run from a folder copy on a locked-down laptop.

Agent Brain is two small command-line tools over an append-only, plain-text
store:

- **`brain`** — the memory engine: an append-only typed **stream** per scope, a
  generated lossy **tree** (state summary + branch gists), verbatim **pins** and
  a verbatim **hazard index**. The tree is a deterministic function of the stream
  — no model calls, no scheduler — rebuilt inline on every write.
- **`tickets`** — a multi-writer **ticket board**: the one place a fixable defect
  or tracked commitment lives. Per-writer append-only event logs folded into a
  computed board, so it is conflict-free under folder sync.

The distinction that makes it work: **a defect you would fix is a ticket, not a
note; a fact the next session must know is a note.** (`docs/PROTOCOL.md` is the
canonical protocol — routing rules, write rules, retrieval, enforcement.)

## Why it's different

Automatic-write + similarity-recall memory systems bloat and drift. Agent Brain
counters that with **deliberate typed writes**, **deterministic routing** (no
embeddings, no similarity search anywhere), and **explicit lifecycle**
(supersede, resolve, retire). Knowledge lives in plain text; intelligence lives
in the harness; the glue is thin and enumerable — six Claude Code hooks and three
CLIs.

## Two deployment profiles

- **Dev machine (git available)** — every `brain` write commits; git is the
  stream's safety net (the stream is the only non-regenerable artifact).
- **Locked-down / OneDrive machine (no git)** — the engine detects no git and
  degrades explicitly: the append-only stream files plus your folder-sync version
  history are the record, and `brain doctor` says so out loud. Tickets are
  conflict-free under sync by design (per-writer files).

Multi-writer honesty: **tickets are multi-writer**; the **stream is
single-writer per instance** (one machine appends to each scope's stream log).

## Quickstart

### Folder-copy profile (no install, no git, no admin)

```sh
# Copy/unzip the package folder anywhere (e.g. a synced OneDrive path), then:
python brain.py init --no-git          # scaffolds ./brain and reports the profile
python brain.py note -p demo -t decision "use the deterministic tree"
python brain.py wake --scope demo
python tickets.py init --root ./boards/work --prefix WORK --name "Work board"
python tickets.py open "fix the flaky test" --root ./boards/work
```

Point any tool at a different instance with `BRAIN_ROOT` (for `brain`) or
`--root` (for `tickets`). No pip installs, no compiled dependencies —
Python 3.10+ standard library only.

### Full install (data root + Claude Code hooks)

```sh
python install/install.py --root ./brain --name my-deployment
python brain.py doctor        # green when git-backed; explicit when not
```

The installer creates the instance, writes `brain.config.json` naming its data
root, and merges the six hooks into your Claude Code `settings.json` (with a
timestamped backup). Use `--print-only` to get the snippet to paste yourself, or
`--no-git` for the folder-sync profile.

## Instance model

An instance is **one data root** plus a config naming it. The root is resolved in
priority order:

1. `BRAIN_ROOT` environment variable
2. the `root` key in `brain.config.json` next to `brain.py`
3. default: a `brain/` folder next to `brain.py`

Nothing hardcodes a home directory. The repo never contains instance data — a
fresh instance is empty, not a copy of anyone's memory (a structural test
enforces this).

## Updating

The package and the instance are separate on purpose, so updating is safe and
dumb:

- **Git profile**: `git pull` in the package folder. Done.
- **Folder-copy profile** (no git): replace the package folder with the new
  copy. Your `brain.config.json` is the only file of yours inside the package
  folder — keep it (or re-create it: it is two lines).

No update ever touches instance data. The stream format is append-only and
stable — treated as a contract, not an implementation detail. Generated files
(trees, indexes, board pages) are caches: if an update ever changes how they
render, `brain rebuild --all` regenerates everything from the stream. There
are no migrations to run, because the one non-regenerable artifact — the
stream — is never rewritten.

## Layout

```
brain.py            the memory engine CLI
tickets.py          the ticket board CLI
hooks/              Claude Code enforcement layer (instance-relative)
install/install.py  create an instance + wire hooks (stdlib only)
docs/PROTOCOL.md    conventions & write protocol (read this)
docs/TICKETS-SPEC.md
tests/              pytest suites (stdlib + pytest)
```

## Running the tests

```sh
python -m pytest tests -q
```

## License

MIT — see `LICENSE`.
