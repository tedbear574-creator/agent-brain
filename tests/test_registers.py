"""Tests for registers.py — the multi-writer generic record store.

Roots live under tmp_path; the writer id is controlled with the
REGISTERS_WRITER environment variable (monkeypatched) so tests never depend on
the machine's real user account.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registers  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def run(*argv):
    """Run the CLI, return the exit code (raises SystemExit on error paths)."""
    return registers.main(list(argv))


def as_writer(monkeypatch, name):
    monkeypatch.setenv("REGISTERS_WRITER", name)


# A schema that exercises all three kinds and a natural key.
KEYED_SCHEMA = [
    {"name": "code", "label": "Code", "kind": "fixed"},
    {"name": "name", "label": "Name", "kind": "fixed", "required": True},
    {"name": "status", "label": "Status", "kind": "current"},
    {"name": "revenue", "label": "Revenue", "kind": "tracked", "type": "number"},
]

# A schema with no natural key (records get PREFIX-n numbers).
NUMBERED_SCHEMA = [
    {"name": "name", "label": "Name", "kind": "fixed", "required": True},
    {"name": "owner", "label": "Owner", "kind": "current"},
    {"name": "score", "label": "Score", "kind": "tracked", "type": "number"},
]


def make_reg(tmp_path, monkeypatch, writer="alice", prefix="ENT",
             name="Entity master", schema=None, key="code"):
    root = str(tmp_path / "reg")
    as_writer(monkeypatch, writer)
    sfile = str(tmp_path / "schema.json")
    with open(sfile, "w", encoding="utf-8") as f:
        json.dump(schema if schema is not None else KEYED_SCHEMA, f)
    argv = ["init", "--root", root, "--prefix", prefix, "--name", name,
            "--schema-file", sfile]
    if key:
        argv += ["--key", key]
    run(*argv)
    return root


def make_numbered(tmp_path, monkeypatch, writer="alice"):
    return make_reg(tmp_path, monkeypatch, writer=writer, prefix="ROW",
                    name="Interview index", schema=NUMBERED_SCHEMA, key=None)


def write_raw(root, writer, events):
    """Write events straight to a writer's file (bypassing the CLI) so tests
    can stage exact timestamps and orderings."""
    os.makedirs(os.path.join(root, "events"), exist_ok=True)
    path = os.path.join(root, "events", f"{writer}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _events(root, writer):
    path = os.path.join(root, "events", f"{writer}.jsonl")
    return [json.loads(line) for line in open(path, encoding="utf-8")]


def _rec_by_key(root, key):
    cfg = registers.load_config(root)
    recs = registers.register_state(root)
    return registers.resolve_record(cfg, recs, key)


# --------------------------------------------------------------------------- #
# init + root resolution                                                       #
# --------------------------------------------------------------------------- #


def test_init_writes_config_with_schema_and_key(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    cfg = json.load(open(os.path.join(root, "config.json"), encoding="utf-8"))
    assert cfg["name"] == "Entity master"
    assert cfg["prefix"] == "ENT"
    assert cfg["key"] == "code"
    assert cfg["schema"] == KEYED_SCHEMA
    assert os.path.isdir(os.path.join(root, "events"))


def test_init_without_key_omits_key(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch)
    cfg = json.load(open(os.path.join(root, "config.json"), encoding="utf-8"))
    assert "key" not in cfg


def test_init_twice_is_refused(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    sfile = str(tmp_path / "schema.json")
    with pytest.raises(SystemExit):
        run("init", "--root", root, "--prefix", "ENT", "--name", "x",
            "--schema-file", sfile, "--key", "code")


def test_missing_root_is_a_plain_error(tmp_path, monkeypatch):
    monkeypatch.delenv("REGISTERS_ROOT", raising=False)
    as_writer(monkeypatch, "alice")
    with pytest.raises(SystemExit):
        run("board")


def test_root_without_config_is_a_plain_error(tmp_path, monkeypatch, capsys):
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    as_writer(monkeypatch, "alice")
    with pytest.raises(SystemExit):
        run("board", "--root", empty)
    assert "not a register yet" in capsys.readouterr().err


def test_root_from_env(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    monkeypatch.setenv("REGISTERS_ROOT", root)
    run("add", "code=ACME", "name=Acme")
    assert "ACME" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# schema validation                                                            #
# --------------------------------------------------------------------------- #


def _init_with_schema(tmp_path, monkeypatch, schema, key=None, prefix="ENT"):
    as_writer(monkeypatch, "alice")
    root = str(tmp_path / "reg")
    sfile = str(tmp_path / "schema.json")
    with open(sfile, "w", encoding="utf-8") as f:
        json.dump(schema, f)
    argv = ["init", "--root", root, "--prefix", prefix, "--name", "n",
            "--schema-file", sfile]
    if key:
        argv += ["--key", key]
    return run(*argv)


def test_schema_bad_kind_is_a_plain_error(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _init_with_schema(tmp_path, monkeypatch,
                          [{"name": "a", "label": "A", "kind": "weird"}])
    assert "kind" in capsys.readouterr().err


def test_schema_bad_type_is_a_plain_error(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _init_with_schema(tmp_path, monkeypatch,
                          [{"name": "a", "label": "A", "kind": "current",
                            "type": "money"}])
    assert "type" in capsys.readouterr().err


def test_schema_bad_name_is_a_plain_error(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _init_with_schema(tmp_path, monkeypatch,
                          [{"name": "Bad Name", "label": "B", "kind": "fixed"}])
    assert "name" in capsys.readouterr().err.lower()


def test_schema_missing_label_is_a_plain_error(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _init_with_schema(tmp_path, monkeypatch,
                          [{"name": "a", "kind": "fixed"}])
    assert "label" in capsys.readouterr().err.lower()


def test_schema_duplicate_field_is_a_plain_error(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _init_with_schema(tmp_path, monkeypatch, [
            {"name": "a", "label": "A", "kind": "fixed"},
            {"name": "a", "label": "A2", "kind": "current"},
        ])
    assert "twice" in capsys.readouterr().err


def test_schema_empty_is_a_plain_error(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _init_with_schema(tmp_path, monkeypatch, [])
    assert "at least one field" in capsys.readouterr().err


def test_key_must_name_an_existing_field(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _init_with_schema(tmp_path, monkeypatch,
                          [{"name": "a", "label": "A", "kind": "fixed"}],
                          key="missing")
    assert "no field called" in capsys.readouterr().err


def test_key_must_be_a_fixed_field(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _init_with_schema(tmp_path, monkeypatch,
                          [{"name": "a", "label": "A", "kind": "current"}],
                          key="a")
    assert "must be a fixed field" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# writer derivation                                                            #
# --------------------------------------------------------------------------- #


def test_writer_from_home_basename(monkeypatch):
    monkeypatch.delenv("REGISTERS_WRITER", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: r"C:\Users\jsmith")
    assert registers.writer_id() == "jsmith"


def test_writer_env_override(monkeypatch):
    monkeypatch.setenv("REGISTERS_WRITER", "agent-7")
    assert registers.writer_id() == "agent-7"


def test_writer_name_is_filename_safe(monkeypatch):
    monkeypatch.setenv("REGISTERS_WRITER", "j smith/2")
    wid = registers.writer_id()
    assert "/" not in wid and " " not in wid


def test_each_writer_gets_own_file(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    as_writer(monkeypatch, "alice")
    run("add", "--root", root, "code=A", "name=Aye")
    as_writer(monkeypatch, "bob")
    run("add", "--root", root, "code=B", "name=Bee")
    files = sorted(os.listdir(os.path.join(root, "events")))
    assert files == ["alice.jsonl", "bob.jsonl"]


# --------------------------------------------------------------------------- #
# add + required fields + type checks                                          #
# --------------------------------------------------------------------------- #


def test_add_requires_required_fields(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        run("add", "--root", root, "code=ACME")   # name is required
    assert "Name" in capsys.readouterr().err


def test_add_requires_the_natural_key(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        run("add", "--root", root, "name=NoCode")   # code (the key) missing
    assert "Code" in capsys.readouterr().err


def test_add_unknown_field_is_a_plain_error(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        run("add", "--root", root, "code=A", "name=Aye", "colour=blue")
    assert "no field called" in capsys.readouterr().err


def test_add_rejects_tracked_field(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        run("add", "--root", root, "code=A", "name=Aye", "revenue=100")
    assert "tracked" in capsys.readouterr().err


def test_add_number_type_is_validated(tmp_path, monkeypatch, capsys):
    root = make_numbered(tmp_path, monkeypatch)
    run("add", "--root", root, "name=Row1")
    with pytest.raises(SystemExit):
        run("post", "--root", root, "-r", "1", "--field", "score",
            "--value", "not-a-number", "--asof", "2025")
    assert "number" in capsys.readouterr().err


def test_add_date_type_is_validated(tmp_path, monkeypatch, capsys):
    schema = [
        {"name": "code", "label": "Code", "kind": "fixed"},
        {"name": "joined", "label": "Joined", "kind": "current",
         "type": "date"},
    ]
    root = make_reg(tmp_path, monkeypatch, schema=schema, key="code")
    with pytest.raises(SystemExit):
        run("add", "--root", root, "code=A", "joined=not-a-date")
    assert "date" in capsys.readouterr().err
    run("add", "--root", root, "code=B", "joined=2026-09-30")
    assert _rec_by_key(root, "B")["values"]["joined"] == "2026-09-30"


# --------------------------------------------------------------------------- #
# fold determinism                                                             #
# --------------------------------------------------------------------------- #


def test_fold_deterministic_under_shuffled_read_order(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "add",
         "id": "aaaaaa", "values": {"name": "A"}, "propose_n": 1},
        {"ts": "2026-08-16T09:05:00", "writer": "alice", "verb": "add",
         "id": "cccccc", "values": {"name": "C"}, "propose_n": 3},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:02:00", "writer": "bob", "verb": "add",
         "id": "bbbbbb", "values": {"name": "B"}, "propose_n": 2},
    ])
    real_listdir = os.listdir

    def forward(path):
        return sorted(real_listdir(path))

    def reverse(path):
        return sorted(real_listdir(path), reverse=True)

    monkeypatch.setattr(registers.os, "listdir", forward)
    b1 = {r["id"]: r["n"] for r in registers.register_state(root).values()}
    monkeypatch.setattr(registers.os, "listdir", reverse)
    b2 = {r["id"]: r["n"] for r in registers.register_state(root).values()}
    assert b1 == b2 == {"aaaaaa": 1, "bbbbbb": 2, "cccccc": 3}


# --------------------------------------------------------------------------- #
# auto-number letter-suffix (no natural key)                                   #
# --------------------------------------------------------------------------- #


def _keys(root):
    cfg = registers.load_config(root)
    recs = registers.register_state(root)
    return {r["id"]: registers.key_of(cfg, r) for r in recs.values()}


def test_number_collision_gets_letter_suffix_no_renumber(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch)
    write_raw(root, "seed", [
        {"ts": "2026-08-16T08:00:00", "writer": "seed", "verb": "add",
         "id": "111111", "values": {"name": "seed"}, "propose_n": 1},
    ])
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "add",
         "id": "aaaaaa", "values": {"name": "a2"}, "propose_n": 2},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:00:00", "writer": "bob", "verb": "add",
         "id": "bbbbbb", "values": {"name": "b2"}, "propose_n": 2},
    ])
    keys = _keys(root)
    assert keys["111111"] == "ROW-1"
    assert keys["aaaaaa"] == "ROW-2"
    assert keys["bbbbbb"] == "ROW-2b"


def test_late_sync_never_cascades(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "add",
         "id": "aaaaaa", "values": {"name": "A"}, "propose_n": 1},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:05:00", "writer": "bob", "verb": "add",
         "id": "bbbbbb", "values": {"name": "B"}, "propose_n": 2},
    ])
    before = _keys(root)
    assert before["aaaaaa"] == "ROW-1" and before["bbbbbb"] == "ROW-2"
    write_raw(root, "carol", [
        {"ts": "2026-08-15T17:00:00", "writer": "carol", "verb": "add",
         "id": "cccccc", "values": {"name": "C"}, "propose_n": 1},
    ])
    after = _keys(root)
    assert after["cccccc"] == "ROW-1"
    assert after["aaaaaa"] == "ROW-1b"   # only the collider moves
    assert after["bbbbbb"] == "ROW-2"    # bystander never moves


def test_numbered_add_increments(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch)
    run("add", "--root", root, "name=First")
    run("add", "--root", root, "name=Second")
    assert set(_keys(root).values()) == {"ROW-1", "ROW-2"}


# --------------------------------------------------------------------------- #
# natural-key duplicate warns loudly, keeps first                              #
# --------------------------------------------------------------------------- #


def test_duplicate_natural_key_across_writers_warns_keeps_first(
        tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "add",
         "id": "aaaaaaaa", "values": {"code": "ACME", "name": "alice's"}},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:01:00", "writer": "bob", "verb": "add",
         "id": "bbbbbbbb", "values": {"code": "ACME", "name": "bob's"}},
    ])
    recs = registers.register_state(root)
    err = capsys.readouterr().err
    assert "aaaaaaaa" in recs and "bbbbbbbb" not in recs   # first kept
    assert recs["aaaaaaaa"]["values"]["name"] == "alice's"
    assert "same" in err and "bob" in err and "ACME" in err


def test_duplicate_natural_key_is_case_insensitive(tmp_path, monkeypatch,
                                                   capsys):
    root = make_reg(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "add",
         "id": "aaaaaaaa", "values": {"code": "Acme", "name": "first"}},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:01:00", "writer": "bob", "verb": "add",
         "id": "bbbbbbbb", "values": {"code": "ACME", "name": "second"}},
    ])
    recs = registers.register_state(root)
    capsys.readouterr()
    assert len(recs) == 1


def test_add_duplicate_key_via_cli_is_a_plain_error(tmp_path, monkeypatch,
                                                    capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=First")
    with pytest.raises(SystemExit):
        run("add", "--root", root, "code=acme", "name=Second")
    assert "already a record" in capsys.readouterr().err


def test_duplicate_id_across_writers_warns_not_silent(tmp_path, monkeypatch,
                                                      capsys):
    root = make_numbered(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "add",
         "id": "aaaaaaaa", "values": {"name": "alice's"}, "propose_n": 1},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:01:00", "writer": "bob", "verb": "add",
         "id": "aaaaaaaa", "values": {"name": "bob's"}, "propose_n": 2},
    ])
    recs = registers.register_state(root)
    err = capsys.readouterr().err
    assert recs["aaaaaaaa"]["values"]["name"] == "alice's"
    assert "same id" in err and "bob" in err


# --------------------------------------------------------------------------- #
# resolution: key / number / id                                                #
# --------------------------------------------------------------------------- #


def test_resolve_by_key_number_and_id(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch)
    run("add", "--root", root, "name=Resolve me")
    cfg = registers.load_config(root)
    recs = registers.register_state(root)
    rec = list(recs.values())[0]
    rid = rec["id"]
    assert registers.resolve_record(cfg, recs, "1")["id"] == rid
    assert registers.resolve_record(cfg, recs, "ROW-1")["id"] == rid
    assert registers.resolve_record(cfg, recs, rid)["id"] == rid


def test_resolve_by_natural_key_case_insensitive(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    cfg = registers.load_config(root)
    recs = registers.register_state(root)
    assert registers.resolve_record(cfg, recs, "acme")["values"]["code"] == "ACME"


def test_resolve_unknown_key_errors(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    cfg = registers.load_config(root)
    recs = registers.register_state(root)
    with pytest.raises(SystemExit):
        registers.resolve_record(cfg, recs, "NOPE")


# --------------------------------------------------------------------------- #
# current fields: set (later wins)                                             #
# --------------------------------------------------------------------------- #


def test_set_later_wins_per_field(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme", "status=active")
    run("set", "--root", root, "-r", "ACME", "status=dormant")
    run("set", "--root", root, "-r", "ACME", "status=closed")
    rec = _rec_by_key(root, "ACME")
    assert rec["values"]["status"] == "closed"
    assert rec["values"]["name"] == "Acme"   # untouched fixed field survives


def test_set_multiple_current_fields_at_once(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch)
    run("add", "--root", root, "name=Row")
    run("set", "--root", root, "-r", "1", "owner=alice")
    assert _rec_by_key(root, "1")["values"]["owner"] == "alice"


def test_set_on_fixed_field_is_refused(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    with pytest.raises(SystemExit):
        run("set", "--root", root, "-r", "ACME", "name=Nope")
    assert "correct" in capsys.readouterr().err


def test_set_on_tracked_field_is_refused(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    with pytest.raises(SystemExit):
        run("set", "--root", root, "-r", "ACME", "revenue=100")
    assert "post" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# tracked fields: post (series ordering + as-of retrieval)                     #
# --------------------------------------------------------------------------- #


def test_post_series_latest_by_asof(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    # Post out of order; latest by as-of must win regardless of insert order.
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1500", "--asof", "2025")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1000", "--asof", "2024")
    rec = _rec_by_key(root, "ACME")
    latest = registers.latest_tracked(rec, "revenue")
    assert latest["value"] == "1500" and latest["asof"] == "2025"
    # The whole series is retrievable, ordered.
    series = rec["tracked"]["revenue"]
    assert [s["asof"] for s in series] == ["2024", "2025"]


def test_post_requires_asof(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    with pytest.raises(SystemExit):
        run("post", "--root", root, "-r", "ACME", "--field", "revenue",
            "--value", "1000", "--asof", "  ")
    assert "as-of" in capsys.readouterr().err


def test_post_on_non_tracked_field_is_refused(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    with pytest.raises(SystemExit):
        run("post", "--root", root, "-r", "ACME", "--field", "status",
            "--value", "x", "--asof", "2025")
    assert "tracked" in capsys.readouterr().err


def test_post_history_kept_in_log(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1000", "--asof", "2024")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1500", "--asof", "2025")
    capsys.readouterr()
    run("log", "--root", root, "-r", "ACME")
    out = capsys.readouterr().out
    assert "1000" in out and "1500" in out
    assert "2024" in out and "2025" in out


# --------------------------------------------------------------------------- #
# correct (fixed field, requires a note)                                       #
# --------------------------------------------------------------------------- #


def test_correct_requires_a_note(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    with pytest.raises(SystemExit):
        # argparse makes --note required, so omit via a blank value instead.
        run("correct", "--root", root, "-r", "ACME", "--field", "name",
            "--value", "Acme Ltd", "--note", "   ")
    assert "note" in capsys.readouterr().err.lower()


def test_correct_changes_a_fixed_field(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("correct", "--root", root, "-r", "ACME", "--field", "name",
        "--value", "Acme Ltd", "--note", "full legal name")
    assert _rec_by_key(root, "ACME")["values"]["name"] == "Acme Ltd"


def test_correct_the_key_relabels_without_stranding(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    rec = _rec_by_key(root, "ACME")
    rid = rec["id"]
    run("correct", "--root", root, "-r", "ACME", "--field", "code",
        "--value", "ACMX", "--note", "code changed")
    cfg = registers.load_config(root)
    recs = registers.register_state(root)
    # The id is identity: the corrected key finds the same record, by id too.
    assert registers.resolve_record(cfg, recs, "ACMX")["id"] == rid
    assert registers.resolve_record(cfg, recs, rid)["values"]["code"] == "ACMX"


def test_correct_key_to_a_duplicate_is_refused(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("add", "--root", root, "code=BETA", "name=Beta")
    with pytest.raises(SystemExit):
        run("correct", "--root", root, "-r", "BETA", "--field", "code",
            "--value", "ACME", "--note", "oops")
    assert "already has" in capsys.readouterr().err


def test_correct_on_non_fixed_field_is_refused(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    with pytest.raises(SystemExit):
        run("correct", "--root", root, "-r", "ACME", "--field", "status",
            "--value", "x", "--note", "why")
    assert "set" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# retire / restore                                                             #
# --------------------------------------------------------------------------- #


def test_retire_requires_a_reason(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    with pytest.raises(SystemExit):
        run("retire", "--root", root, "-r", "ACME", "   ")
    assert "reason" in capsys.readouterr().err


def test_retire_hides_then_restore_shows(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("retire", "--root", root, "-r", "ACME", "merged into BETA")
    rec = _rec_by_key(root, "ACME")
    assert rec["retired"] and rec["retire_reason"] == "merged into BETA"
    run("restore", "--root", root, "-r", "ACME")
    rec = _rec_by_key(root, "ACME")
    assert not rec["retired"] and rec["retire_reason"] == ""


def test_board_hides_retired_unless_all(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=LIVE", "name=Live one")
    run("add", "--root", root, "code=GONE", "name=Gone one")
    run("retire", "--root", root, "-r", "GONE", "closed")
    capsys.readouterr()
    run("board", "--root", root)
    out = capsys.readouterr().out
    assert "Live one" in out and "Gone one" not in out
    run("board", "--root", root, "--all")
    assert "Gone one" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# comment                                                                      #
# --------------------------------------------------------------------------- #


def test_comment_appends_to_history(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("comment", "--root", root, "-r", "ACME", "spoke to the controller")
    assert _rec_by_key(root, "ACME")["comments"][0]["text"] == \
        "spoke to the controller"


def test_comment_cannot_be_empty(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    with pytest.raises(SystemExit):
        run("comment", "--root", root, "-r", "ACME", "   ")


# --------------------------------------------------------------------------- #
# unknown fields in a raw event are ignored with a warning                     #
# --------------------------------------------------------------------------- #


def test_unknown_field_in_event_is_ignored_with_warning(tmp_path, monkeypatch,
                                                        capsys):
    root = make_reg(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "add",
         "id": "aaaaaaaa",
         "values": {"code": "ACME", "name": "Acme", "ghost": "x"}},
    ])
    recs = registers.register_state(root)
    err = capsys.readouterr().err
    assert "ghost" not in recs["aaaaaaaa"]["values"]
    assert "unknown field" in err and "ghost" in err


# --------------------------------------------------------------------------- #
# show / page / view                                                           #
# --------------------------------------------------------------------------- #


def test_show_prints_tracked_series(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1000", "--asof", "2024")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1500", "--asof", "2025")
    capsys.readouterr()
    run("show", "--root", root, "-r", "ACME")
    out = capsys.readouterr().out
    assert "Revenue" in out
    assert "2024: 1000" in out and "2025: 1500" in out


def test_page_generates_self_contained_html(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=On the page")
    run("page", "--root", root)
    html = open(os.path.join(root, "register.html"), encoding="utf-8").read()
    assert os.path.exists(os.path.join(root, "register.md"))
    assert "On the page" in html and "<style>" in html
    for bad in ("http://", "https://", "src=", "<link"):
        assert bad not in html


def test_page_html_escapes_content(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=X", "name=danger <script> & co")
    run("page", "--root", root)
    html = open(os.path.join(root, "register.html"), encoding="utf-8").read()
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_view_writes_current_page_to_temp_never_the_root(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=fresh record")
    tmp = str(tmp_path / "faketemp")
    os.makedirs(tmp)
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: tmp)
    run("view", "--root", root, "--no-open")
    page = os.path.join(tmp, "register-ENT.html")
    assert os.path.exists(page)
    assert "fresh record" in open(page, encoding="utf-8").read()
    assert not os.path.exists(os.path.join(root, "register.html"))


def test_init_drops_launcher(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    launcher = os.path.join(root, "Open register.cmd")
    assert os.path.exists(launcher)
    text = open(launcher, encoding="utf-8").read()
    assert "serve" in text and "pythonw" in text and "%~dp0" in text


# --------------------------------------------------------------------------- #
# export: register.csv + history.csv shape + formula guard                     #
# --------------------------------------------------------------------------- #


def test_export_register_csv_shape_and_bom(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme", "status=active")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1500", "--asof", "2025")
    run("export", "--root", root)
    path = os.path.join(root, "register.csv")
    raw = open(path, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")   # UTF-8 BOM for Excel
    text = raw.decode("utf-8-sig")
    header = text.splitlines()[0]
    # tracked fields get paired <label> and <label> as of columns
    assert header == "key,Code,Name,Status,Revenue,Revenue as of,state,id"
    assert "ACME,ACME,Acme,active,1500,2025,active," in text


def test_export_history_csv_long_format(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1000", "--asof", "2024")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1500", "--asof", "2025")
    run("export", "--root", root, "--history")
    path = os.path.join(root, "history.csv")
    text = open(path, encoding="utf-8-sig").read()
    lines = text.splitlines()
    assert lines[0] == "key,field,as_of,value,writer,ts"
    assert any(l.startswith("ACME,Revenue,2024,1000,") for l in lines)
    assert any(l.startswith("ACME,Revenue,2025,1500,") for l in lines)


def test_export_history_not_written_without_flag(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("export", "--root", root)
    assert not os.path.exists(os.path.join(root, "history.csv"))


def test_export_guards_against_excel_formulas(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch)
    run("add", "--root", root, "name==2+5|cmd", "owner=@dana")
    run("export", "--root", root)
    text = open(os.path.join(root, "register.csv"),
                encoding="utf-8-sig").read()
    assert "'=2+5|cmd" in text and "'@dana" in text
    assert ",=2+5" not in text


def test_export_history_guards_formulas(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch)
    run("add", "--root", root, "name=Row")
    run("post", "--root", root, "-r", "1", "--field", "score",
        "--value", "5", "--asof", "@2025")
    run("export", "--root", root, "--history")
    text = open(os.path.join(root, "history.csv"),
                encoding="utf-8-sig").read()
    # value column formula guard (the value here is a plain number, but a
    # dangerous value would be defused); check the value cell is guarded when
    # it starts with a formula char.
    run("post", "--root", root, "-r", "1", "--field", "score",
        "--value", "-9", "--asof", "2026")
    run("export", "--root", root, "--history")
    text = open(os.path.join(root, "history.csv"),
                encoding="utf-8-sig").read()
    assert "'-9" in text


# --------------------------------------------------------------------------- #
# log                                                                          #
# --------------------------------------------------------------------------- #


def test_log_shows_full_history(tmp_path, monkeypatch, capsys):
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("set", "--root", root, "-r", "ACME", "status=active")
    run("correct", "--root", root, "-r", "ACME", "--field", "name",
        "--value", "Acme Ltd", "--note", "legal name")
    run("comment", "--root", root, "-r", "ACME", "kickoff call done")
    run("retire", "--root", root, "-r", "ACME", "merged")
    capsys.readouterr()
    run("log", "--root", root, "-r", "ACME")
    out = capsys.readouterr().out
    assert "added it" in out
    assert "set Status" in out
    assert "corrected Name" in out and "legal name" in out
    assert "kickoff call done" in out
    assert "retired it: merged" in out


# --------------------------------------------------------------------------- #
# full life-cycle smoke                                                        #
# --------------------------------------------------------------------------- #


def test_full_lifecycle_smoke(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    run("add", "--root", root, "code=ACME", "name=Acme", "status=active")
    run("add", "--root", root, "code=BETA", "name=Beta")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1000", "--asof", "2024")
    run("set", "--root", root, "-r", "ACME", "status=dormant")
    run("correct", "--root", root, "-r", "BETA", "--field", "code",
        "--value", "BETAX", "--note", "rename")
    run("retire", "--root", root, "-r", "BETAX", "closed")
    run("board", "--root", root, "--all")
    run("page", "--root", root)
    run("export", "--root", root, "--history")
    for fname in ("register.md", "register.html", "register.csv",
                  "history.csv"):
        assert os.path.exists(os.path.join(root, fname))
    html = open(os.path.join(root, "register.html"), encoding="utf-8").read()
    for bad in ("http://", "https://", "<link", "src="):
        assert bad not in html
    acme = _rec_by_key(root, "ACME")
    assert acme["values"]["status"] == "dormant"
    beta = _rec_by_key(root, "BETAX")
    assert beta["retired"]


# --------------------------------------------------------------------------- #
# serve — the interactive local register                                       #
# --------------------------------------------------------------------------- #

import contextlib          # noqa: E402
import http.client         # noqa: E402
import threading           # noqa: E402


@contextlib.contextmanager
def running_register(root):
    server = registers.make_server(root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def api(port, method, path, token="__use__", origin=None, body=None,
        server=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {}
    if token == "__use__":
        token = server.token if server is not None else None
    if token is not None:
        headers["X-Registers-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return resp.status, raw


def test_serve_register_json_roundtrip(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    run("add", "--root", root, "code=ACME", "name=Acme", "status=active")
    with running_register(root) as (server, port):
        status, body = api(port, "GET", "/api/register", server=server)
    assert status == 200
    assert body["name"] == "Entity master"
    assert body["key"] == "code"
    assert [f["name"] for f in body["schema"]] == \
        ["code", "name", "status", "revenue"]
    rec = body["active"][0]
    assert rec["key"] == "ACME"
    assert rec["values"]["name"] == "Acme"
    for field in ("id", "key", "values", "tracked", "retired", "comments"):
        assert field in rec


def test_serve_full_lifecycle_lands_in_acting_writers_file(tmp_path,
                                                           monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="carol")
    with running_register(root) as (server, port):
        # A browser-side "writer" is ignored — the server derives it.
        assert api(port, "POST", "/api/act", server=server,
                   body={"verb": "add",
                         "values": {"code": "ACME", "name": "Acme"},
                         "writer": "mallory"})[0] == 200
        for payload in (
            {"verb": "set", "record": "ACME", "values": {"status": "active"}},
            {"verb": "post", "record": "ACME", "field": "revenue",
             "value": "1000", "asof": "2024"},
            {"verb": "correct", "record": "ACME", "field": "name",
             "value": "Acme Ltd", "note": "legal"},
            {"verb": "comment", "record": "ACME", "text": "started"},
            {"verb": "retire", "record": "ACME", "reason": "merged"},
            {"verb": "restore", "record": "ACME"},
        ):
            assert api(port, "POST", "/api/act", server=server,
                       body=payload)[0] == 200
    assert os.listdir(os.path.join(root, "events")) == ["carol.jsonl"]
    verbs = [e["verb"] for e in _events(root, "carol")]
    assert verbs == ["add", "set", "post", "correct", "comment", "retire",
                     "restore"]
    rec = _rec_by_key(root, "ACME")
    assert rec["values"]["name"] == "Acme Ltd"
    assert not rec["retired"]


def test_serve_add_missing_required_is_400(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    with running_register(root) as (server, port):
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "add", "values": {"code": "ACME"}})
    assert status == 400
    assert "Name" in body["error"]


def test_serve_correct_without_note_is_400(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    with running_register(root) as (server, port):
        api(port, "POST", "/api/act", server=server,
            body={"verb": "add", "values": {"code": "ACME", "name": "Acme"}})
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "correct", "record": "ACME",
                                 "field": "name", "value": "X"})
    assert status == 400
    assert "note" in body["error"].lower()


def test_serve_missing_record_field_is_400_not_500(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    with running_register(root) as (server, port):
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "set", "values": {"status": "x"}})
    assert status == 400
    assert "record" in body["error"].lower()


def test_serve_duplicate_key_is_400(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    with running_register(root) as (server, port):
        api(port, "POST", "/api/act", server=server,
            body={"verb": "add", "values": {"code": "ACME", "name": "A"}})
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "add",
                                 "values": {"code": "ACME", "name": "B"}})
    assert status == 400
    assert "already a record" in body["error"]


def test_serve_concurrent_adds_mint_distinct_records(tmp_path, monkeypatch):
    root = make_numbered(tmp_path, monkeypatch, writer="alice")
    results = []
    with running_register(root) as (server, port):
        def hit(i):
            results.append(api(port, "POST", "/api/act", server=server,
                               body={"verb": "add",
                                     "values": {"name": f"row {i}"}}))
        threads = [threading.Thread(target=hit, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    assert all(status == 200 for status, _ in results)
    recs = registers.register_state(root)
    assert len(recs) == 8
    assert len({r["id"] for r in recs.values()}) == 8
    assert len(_events(root, "alice")) == 8


def test_serve_log_endpoint(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="fred")
    run("add", "--root", root, "code=ACME", "name=Acme")
    run("post", "--root", root, "-r", "ACME", "--field", "revenue",
        "--value", "1000", "--asof", "2024")
    run("comment", "--root", root, "-r", "ACME", "see the shared doc")
    with running_register(root) as (server, port):
        status, body = api(port, "GET", "/api/log?record=ACME", server=server)
        bad, err = api(port, "GET", "/api/log?record=NOPE", server=server)
        noauth, _ = api(port, "GET", "/api/log?record=ACME", token=None)
    assert status == 200
    assert body["key"] == "ACME"
    joined = "\n".join(body["lines"])
    assert "added it" in joined
    assert "posted Revenue" in joined
    assert "see the shared doc" in joined
    assert bad == 400 and "error" in err
    assert noauth == 403


def test_serve_missing_token_is_403(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    with running_register(root) as (server, port):
        reg_status, _ = api(port, "GET", "/api/register", token=None)
        act_status, _ = api(port, "POST", "/api/act", token=None,
                            body={"verb": "add",
                                  "values": {"code": "X", "name": "Y"}})
    assert reg_status == 403 and act_status == 403


def test_serve_foreign_origin_is_403(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    run("add", "--root", root, "code=ACME", "name=Acme")
    with running_register(root) as (server, port):
        status, _ = api(port, "POST", "/api/act", server=server,
                        origin="http://evil.example",
                        body={"verb": "comment", "record": "ACME",
                              "text": "x"})
    assert status == 403
    assert not os.path.exists(os.path.join(root, "events", "evil.jsonl"))


def test_serve_same_origin_is_allowed(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    with running_register(root) as (server, port):
        origin = f"http://127.0.0.1:{port}"
        status, body = api(port, "GET", "/api/register", origin=origin,
                           server=server)
    assert status == 200 and "active" in body


def test_serve_page_is_self_contained(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    with running_register(root) as (server, port):
        status, raw = api(port, "GET", "/", token=None)
        token = server.token
    assert status == 200
    html = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    assert token in html
    for bad in ("http://", "https://", "src=\"", "<link"):
        assert bad not in html


def test_serve_post_and_correct_roundtrip(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="erin")
    with running_register(root) as (server, port):
        api(port, "POST", "/api/act", server=server,
            body={"verb": "add", "values": {"code": "ACME", "name": "Acme"}})
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "post", "record": "ACME",
                                 "field": "revenue", "value": "500",
                                 "asof": "2025"})
    assert status == 200
    rec = body["register"]["active"][0]
    assert rec["tracked"]["revenue"]["latest"]["value"] == "500"
    assert rec["tracked"]["revenue"]["latest"]["writer"] == "erin"


def test_serve_retired_records_in_payload(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    run("add", "--root", root, "code=GONE", "name=Gone")
    run("retire", "--root", root, "-r", "GONE", "closed")
    with running_register(root) as (server, port):
        status, body = api(port, "GET", "/api/register", server=server)
    assert status == 200
    assert body["active"] == []
    assert body["retired"][0]["key"] == "GONE"
    assert body["retired"][0]["retire_reason"] == "closed"


def _serving_refused(port):
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        conn.request("GET", "/")
        conn.getresponse().read()
        conn.close()
        return False
    except (ConnectionRefusedError, OSError):
        return True


def test_serve_exits_when_heartbeats_stop(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    monkeypatch.setenv("REGISTERS_SERVE_IDLE", "1")
    monkeypatch.setenv("REGISTERS_SERVE_GRACE", "0")
    server = registers.make_server(root)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    watchdog = registers.start_idle_watchdog(server)
    try:
        watchdog.join(timeout=10)
        assert not watchdog.is_alive()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert _serving_refused(port)
    finally:
        server.server_close()


def test_serve_heartbeats_keep_it_alive_and_goodbye_ends_it(tmp_path,
                                                            monkeypatch):
    import time as _time
    root = make_reg(tmp_path, monkeypatch, writer="alice")
    monkeypatch.setenv("REGISTERS_SERVE_IDLE", "1")
    monkeypatch.setenv("REGISTERS_SERVE_GRACE", "0")
    server = registers.make_server(root)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    watchdog = registers.start_idle_watchdog(server)
    try:
        for _ in range(6):
            status, _b = api(port, "GET", "/api/ping", server=server)
            assert status == 200
            _time.sleep(0.4)
        assert watchdog.is_alive()
        api(port, "POST", f"/api/goodbye?token={server.token}", token=None)
        watchdog.join(timeout=10)
        assert not watchdog.is_alive()
    finally:
        server.server_close()


def test_launcher_uses_serve_without_a_console(tmp_path, monkeypatch):
    root = make_reg(tmp_path, monkeypatch)
    text = open(os.path.join(root, "Open register.cmd"),
                encoding="utf-8").read()
    assert "serve" in text and "pythonw" in text


def test_export_guards_natural_key_column(tmp_path, monkeypatch):
    # The key column of BOTH csv files is user-controlled on a keyed register
    # (the natural key is a fixed field) — it gets the same formula guard as
    # every data cell.
    root = make_reg(tmp_path, monkeypatch)
    run("add", "--root", root, "code==HYPERLINK(1)", "name=Sneaky")
    run("post", "--root", root, "-r", "=HYPERLINK(1)", "--field", "revenue",
        "--value", "1", "--asof", "2025")
    run("export", "--root", root, "--history")
    reg = open(os.path.join(root, "register.csv"), encoding="utf-8-sig").read()
    hist = open(os.path.join(root, "history.csv"), encoding="utf-8-sig").read()
    for text in (reg, hist):
        assert "'=HYPERLINK(1)" in text
        assert not any(line.startswith("=") for line in text.splitlines())
