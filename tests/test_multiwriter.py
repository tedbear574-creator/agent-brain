"""Multi-writer (shared) scope suite for brain.py — AGENT-BRAIN-49.

A scope shared across machines on a sync folder (OneDrive) can't be a single
append-only file: concurrent appends corrupt it. The shared layout is per-writer
spools (stream/<scope>/<writer>.log) plus a deterministic read-side fold, opt-in
per scope via `brain share`. These tests pin the permanent guarantees:

  * fold determinism under arbitrary arrival order,
  * stable ids when a lagging spool syncs in (never positional renumbering),
  * supersede across writers, incl. the dangling-ref lint WARNING,
  * `brain share` freezes the legacy log and routes new notes to the spool,
  * two writers in two roots merge cleanly by a plain folder copy.

Solo-scope behaviour is asserted UNCHANGED (its own dedicated suite,
test_brain.py, is the fuller regression; here we just confirm the write path
still lands in stream/<scope>.log and mints no spool dir).
"""
import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
BRAIN = os.path.join(ROOT_DIR, "brain.py")

_spec = importlib.util.spec_from_file_location("brain", BRAIN)
brain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brain)


# --------------------------------------------------------------------------- #
# subprocess driver (end-to-end: argparse wiring + exit codes as re-run)       #
# --------------------------------------------------------------------------- #

@pytest.fixture
def root(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    return {"kb": str(kb), "stream": str(kb / "stream")}


def run(root, *args, writer=None, expect=None):
    env = dict(os.environ, BRAIN_ROOT=root["kb"])
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if writer is not None:
        env["BRAIN_WRITER"] = writer
    else:
        env.pop("BRAIN_WRITER", None)
    r = subprocess.run([sys.executable, BRAIN, *args], env=env,
                       capture_output=True, text=True)
    if expect is not None:
        assert r.returncode == expect, (
            f"args={args} rc={r.returncode}\nOUT:{r.stdout}\nERR:{r.stderr}")
    return r


def stream_file(root, scope):
    return os.path.join(root["stream"], f"{scope}.log")


def spool_file(root, scope, writer):
    return os.path.join(root["stream"], scope, f"{writer}.log")


def read_lines(path):
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


# --------------------------------------------------------------------------- #
# pure-function driver (fold internals against a monkeypatched STREAM_DIR)      #
# --------------------------------------------------------------------------- #

@pytest.fixture
def stream_dir(tmp_path, monkeypatch):
    d = tmp_path / "stream"
    d.mkdir()
    monkeypatch.setattr(brain, "KB_ROOT", str(tmp_path))
    monkeypatch.setattr(brain, "STREAM_DIR", str(d))
    monkeypatch.setattr(brain, "TREE_DIR", str(tmp_path / "tree"))
    return str(d)


def _line(ts, typ, text, meta=None):
    return f"{ts} | {typ} | {meta} | {text}" if meta else f"{ts} | {typ} | {text}"


def write_spool(stream_dir, scope, writer, lines):
    d = os.path.join(stream_dir, scope)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{writer}.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_legacy(stream_dir, scope, lines):
    with open(os.path.join(stream_dir, f"{scope}.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------- fold determinism

def test_fold_orders_by_ts_then_writer_then_seq(stream_dir):
    # Three writers, interleaved timestamps. The fold is a total order on
    # (ts, writer, seq) regardless of which file is read first.
    write_spool(stream_dir, "shared", "bob", [
        _line("2026-08-17T09:00", "decision", "bob first"),
        _line("2026-08-17T11:00", "decision", "bob second"),
    ])
    write_spool(stream_dir, "shared", "alice", [
        _line("2026-08-17T10:00", "decision", "alice first"),
    ])
    lines, ids = brain._fold("shared")
    texts = [brain._parse(ln)[3] for ln in lines]
    assert texts == ["bob first", "alice first", "bob second"]
    assert ids == ["bob:1", "alice:1", "bob:2"]


def test_fold_is_idempotent_and_read_order_independent(stream_dir):
    for w, lines in (("alice", ["a1", "a2"]), ("bob", ["b1"]), ("carol", ["c1"])):
        write_spool(stream_dir, "shared", w,
                    [_line("2026-08-17T09:0%d" % i, "milestone", t)
                     for i, t in enumerate(lines)])
    first = brain._fold("shared")
    second = brain._fold("shared")
    assert first == second                      # deterministic, no hidden state
    # Every id is unique and shaped <writer>:<seq>.
    _, ids = first
    assert len(ids) == len(set(ids)) == 4
    assert all(":" in i for i in ids)


def test_legacy_entries_sort_first_and_keep_numeric_ids(stream_dir):
    write_legacy(stream_dir, "shared", [
        _line("2026-08-10T09:00", "decision", "legacy one"),
        _line("2026-08-11T09:00", "decision", "legacy two"),
    ])
    write_spool(stream_dir, "shared", "alice", [
        _line("2026-08-17T09:00", "decision", "spool one"),
    ])
    lines, ids = brain._fold("shared")
    assert [brain._parse(l)[3] for l in lines] == ["legacy one", "legacy two", "spool one"]
    assert ids == ["1", "2", "alice:1"]         # legacy keeps its numbers


# --------------------------------------------------------------- id stability

def test_id_stable_when_lagging_spool_syncs_in(stream_dir):
    write_spool(stream_dir, "shared", "alice", [
        _line("2026-08-17T12:00", "decision", "alice ruling"),
    ])
    _, ids_before = brain._fold("shared")
    pos_before = ids_before.index("alice:1")
    # A second machine's spool arrives, carrying an EARLIER-timestamped entry
    # that interleaves BEFORE alice's in display order.
    write_spool(stream_dir, "shared", "bob", [
        _line("2026-08-17T08:00", "decision", "bob earlier ruling"),
    ])
    lines_after, ids_after = brain._fold("shared")
    pos_after = ids_after.index("alice:1")
    assert pos_after == pos_before + 1          # alice moved DOWN one row…
    assert "alice:1" in ids_after               # …but its id never changed
    assert ids_after == ["bob:1", "alice:1"]


def test_supersede_ref_survives_a_late_arriving_spool(stream_dir):
    # alice supersedes her own #1 from a later entry; a bob spool then syncs in
    # with an earlier ts. The dead-map must still resolve alice:1 -> alice:2.
    write_spool(stream_dir, "shared", "alice", [
        _line("2026-08-17T09:00", "invariant", "old rule"),
        _line("2026-08-17T12:00", "invariant", "new rule", "sup=alice:1"),
    ])
    write_spool(stream_dir, "shared", "bob", [
        _line("2026-08-17T06:00", "decision", "bob early"),
    ])
    entries, ids = brain._fold("shared")
    dead = brain._superseded(entries, ids)
    dead_ids = {ids[p - 1] for p in dead}
    assert dead_ids == {"alice:1"}              # keyed by identity, not position


# --------------------------------------------------------------- supersede across writers

def test_cross_writer_supersede_drops_from_live_view(stream_dir):
    write_spool(stream_dir, "shared", "alice", [
        _line("2026-08-17T09:00", "gotcha", "the flaky bit", "sub=parser"),
    ])
    write_spool(stream_dir, "shared", "bob", [
        _line("2026-08-17T10:00", "milestone", "fixed it", "sup=alice:1"),
    ])
    entries, ids = brain._fold("shared")
    dead = brain._superseded(entries, ids)
    # alice:1 is superseded by bob's entry -> not live.
    alice_pos = ids.index("alice:1") + 1
    assert alice_pos in dead
    assert ids[dead[alice_pos] - 1] == "bob:1"


def test_dangling_supersede_ref_is_a_lint_warning_not_error(root):
    # bob references an id that isn't present (its spool hasn't synced). Lint
    # reports it as a WARNING and names sync lag — it does not crash the audit.
    os.makedirs(os.path.join(root["stream"], "shared"), exist_ok=True)
    with open(spool_file(root, "shared", "bob"), "w", encoding="utf-8") as f:
        f.write(_line("2026-08-17T10:00", "milestone", "fixed the thing",
                      "sup=alice:9") + "\n")
    r = run(root, "lint", "--scope", "shared")
    combined = r.stdout + r.stderr
    assert "alice:9" in combined
    assert "sync" in combined.lower() or "no such id" in combined.lower()


# --------------------------------------------------------------- share conversion

def test_share_freezes_legacy_and_routes_new_notes_to_spool(root):
    run(root, "note", "-p", "demo", "-t", "decision", "solo ruling one", expect=0)
    run(root, "note", "-p", "demo", "-t", "decision", "solo ruling two", expect=0)
    legacy = stream_file(root, "demo")
    assert len(read_lines(legacy)) == 2
    frozen_before = read_lines(legacy)

    r = run(root, "share", "--scope", "demo", expect=0)
    assert "froze 2" in r.stdout
    assert os.path.isdir(os.path.join(root["stream"], "demo"))

    # A note after sharing lands in THIS writer's spool, never the frozen log.
    run(root, "note", "-p", "demo", "-t", "decision", "shared ruling three",
        writer="alice", expect=0)
    assert read_lines(legacy) == frozen_before          # legacy untouched
    spool = read_lines(spool_file(root, "demo", "alice"))
    assert len(spool) == 1
    assert "shared ruling three" in spool[0]

    # …and the new entry is visible through the fold with a stable spool id.
    r = run(root, "zoom", "demo", "1-10", expect=0)
    assert "#alice:1" in r.stdout
    assert "solo ruling one" in r.stdout and "shared ruling three" in r.stdout


def test_share_is_idempotent(root):
    run(root, "note", "-p", "demo", "-t", "milestone", "x", expect=0)
    run(root, "share", "--scope", "demo", expect=0)
    r = run(root, "share", "--scope", "demo", expect=0)
    assert "already a shared scope" in r.stdout


def test_shared_note_supersede_by_stable_id(root):
    run(root, "share", "--scope", "sh", expect=0)
    run(root, "note", "-p", "sh", "-t", "invariant", "--subsystem", "core",
        "must hold A", writer="alice", expect=0)
    # Supersede alice:1 from the same writer, by its stable id.
    r = run(root, "note", "-p", "sh", "-t", "invariant", "--subsystem", "core",
            "--supersedes", "alice:1", "must hold A revised", writer="alice", expect=0)
    assert "supersedes alice:1" in r.stdout
    # design view shows only the live (revised) invariant.
    r = run(root, "design", "--scope", "sh", expect=0)
    assert "must hold A revised" in r.stdout
    assert "must hold A revised" in r.stdout and r.stdout.count("must hold A") >= 1
    # the superseded original is gone from the live index
    assert "#alice:2" in r.stdout


def test_shared_supersede_unknown_id_rejected(root):
    run(root, "share", "--scope", "sh", expect=0)
    run(root, "note", "-p", "sh", "-t", "decision", "a", writer="alice", expect=0)
    r = run(root, "note", "-p", "sh", "-t", "decision", "--supersedes", "bob:7",
            "b", writer="alice", expect=1)
    assert "bob:7" in r.stderr


# --------------------------------------------------------------- solo unchanged

def test_solo_scope_writes_single_file_no_spool_dir(root):
    run(root, "note", "-p", "solo", "-t", "decision", "one", expect=0)
    run(root, "note", "-p", "solo", "-t", "decision", "two", expect=0)
    assert len(read_lines(stream_file(root, "solo"))) == 2
    assert not os.path.isdir(os.path.join(root["stream"], "solo"))
    # Ids are the classic positional numbers — no <writer>:<seq> anywhere.
    import re
    r = run(root, "zoom", "solo", "1-9", expect=0)
    assert "#1 " in r.stdout and "#2 " in r.stdout
    assert not re.search(r"#\w+:\d", r.stdout)


# --------------------------------------------------------------- folder-copy merge

def two_roots(tmp_path):
    a = {"kb": str(tmp_path / "a")}
    b = {"kb": str(tmp_path / "b")}
    for r in (a, b):
        os.makedirs(r["kb"])
        r["stream"] = os.path.join(r["kb"], "stream")
    return a, b


def test_two_writers_merge_by_folder_copy(tmp_path):
    a, b = two_roots(tmp_path)
    for r, w, txt in ((a, "alice", "alice ruling"), (b, "bob", "bob ruling")):
        run(r, "share", "--scope", "team", expect=0)
        run(r, "note", "-p", "team", "-t", "decision", txt, writer=w, expect=0)

    # Sync the folder: copy bob's spool into alice's shared-scope directory,
    # exactly as a sync client would drop a new per-writer file in.
    shutil.copy(spool_file(b, "team", "bob"), spool_file(a, "team", "bob"))

    r = run(a, "zoom", "team", "1-20", expect=0)
    assert "alice ruling" in r.stdout and "bob ruling" in r.stdout
    assert "#alice:1" in r.stdout and "#bob:1" in r.stdout

    # The merged fold on A is identical to what a from-scratch root with both
    # spools would produce — determinism across machines, no coordination.
    c = {"kb": str(tmp_path / "c")}
    os.makedirs(c["kb"])
    c["stream"] = os.path.join(c["kb"], "stream")
    os.makedirs(os.path.join(c["stream"], "team"))
    for w in ("alice", "bob"):
        shutil.copy(spool_file(a, "team", w), spool_file(c, "team", w))
    ra = run(a, "grep", ".", "--scope", "team", expect=0)
    rc = run(c, "grep", ".", "--scope", "team", expect=0)
    assert ra.stdout == rc.stdout


def test_doctor_reports_shared_scope_and_writers(root):
    run(root, "share", "--scope", "team", expect=0)
    run(root, "note", "-p", "team", "-t", "decision", "x", writer="alice", expect=0)
    run(root, "note", "-p", "team", "-t", "decision", "y", writer="bob", expect=0)
    r = run(root, "doctor", expect=0)
    assert "shared" in r.stdout.lower()
    assert "team" in r.stdout
    assert "alice" in r.stdout and "bob" in r.stdout


# ------------------------------------------------- hazard gate id round-trip

def test_match_path_exclude_round_trips_shared_ids(root):
    # The edit-gate dedup loop: MATCHED-IDS emits display ids (numeric for
    # legacy entries, writer:seq for spool entries) and --exclude must accept
    # the very same tokens back. One legacy hazard + one spool hazard, both
    # keyed to the same path, excluded one at a time.
    run(root, "note", "-p", "acme", "-t", "gotcha",
        "payments parser mangles unicode headers",
        "--subsystem", "payments", expect=0)
    run(root, "share", "--scope", "acme", expect=0)
    run(root, "note", "-p", "acme", "-t", "gotcha",
        "rounding bug in the payments export step",
        "--subsystem", "payments", writer="alice", expect=0)

    r = run(root, "hazards", "--scope", "acme",
            "--match-path", "src/payments/export.py", expect=0)
    assert "MATCHED-IDS:" in r.stdout
    ids_line = [ln for ln in r.stdout.splitlines()
                if ln.startswith("MATCHED-IDS:")][0]
    ids = {t.strip() for t in ids_line.split(":", 1)[1].split(",") if t.strip()}
    assert ids == {"1", "alice:1"}

    r = run(root, "hazards", "--scope", "acme",
            "--match-path", "src/payments/export.py",
            "--exclude", "alice:1", expect=0)
    assert "alice:1" not in r.stdout
    assert "#1" in r.stdout  # the legacy hazard still surfaces

    r = run(root, "hazards", "--scope", "acme",
            "--match-path", "src/payments/export.py",
            "--exclude", "1,alice:1", expect=0)
    assert r.stdout.strip() == ""  # both excluded -> silent


# ----------------------------------------------------------- writer id safety

def test_safe_writer_lowercases_without_hash():
    assert brain._safe_writer("Tod") == "tod"
    assert brain._safe_writer("a-b") == "a-b"


def test_safe_writer_distinct_raw_names_never_share_a_spool():
    a = brain._safe_writer("a/b")
    b = brain._safe_writer("a b")
    assert a != b
    assert a.startswith("a-b-") and b.startswith("a-b-")
