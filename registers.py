#!/usr/bin/env python3
"""registers — a shared record store that survives file-sync folders.

A register holds many records of one declared shape: an entity master, an
interview index, an employee list. The shape is set once (a small schema); the
program is the same for every register. Facts live here; judgement lives in
your notes, pointing back at a record's key.

Like the sibling ticket board, every person who uses a register writes only to
their own file, so nothing they type can collide with or overwrite what a
teammate typed. What you read (a table, a web page, a spreadsheet) is never
stored — it is rebuilt from everyone's files each time you ask, in a fixed
order, so two people looking at the same folder always see the same register.

Store on disk:

    <root>/config.json          the register's name, short code, and schema
    <root>/events/<name>.jsonl  one file per person, one action per line
    <root>/register.md          rebuilt on demand (page)
    <root>/register.html        rebuilt on demand (page), opens in any browser
    <root>/register.csv         rebuilt on demand (export), opens in Excel
    <root>/history.csv          the full value series, long format, for pivots

Nothing here needs git, a server, or any outside package — it is one plain
Python file (3.10+). Copy it anywhere and it works.
"""

from __future__ import annotations

import argparse
import csv
import http.server
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import date, datetime

# --------------------------------------------------------------------------- #
# Who am I (the writer id)                                                     #
# --------------------------------------------------------------------------- #
# One file per person, forever. The person's name is taken from their computer
# account (the last part of their home folder: C:\Users\jsmith -> jsmith), so
# nobody ever has to type or remember an identifier. Automated helpers can set
# REGISTERS_WRITER to write under their own name, but it is never required.


def writer_id() -> str:
    """The name of the person (or helper) making a change."""
    override = os.environ.get("REGISTERS_WRITER")
    if override and override.strip():
        return _safe_name(override.strip())
    home = os.path.expanduser("~")
    parts = [p for p in re.split(r"[\\/]+", home) if p]
    return _safe_name(parts[-1] if parts else "") or "unknown"


def _safe_name(name: str) -> str:
    """Keep a writer name usable as a filename (no slashes, spaces, etc.)."""
    keep = [c if (c.isalnum() or c in "-_.") else "-" for c in name]
    return "".join(keep).strip("-") or "unknown"


# --------------------------------------------------------------------------- #
# Where is the register (the root)                                            #
# --------------------------------------------------------------------------- #


def resolve_root(args) -> str:
    """Find the register folder, or explain plainly what is missing."""
    root = getattr(args, "root", None) or os.environ.get("REGISTERS_ROOT")
    if not root:
        _die("I don't know which register to use. Point me at its folder with "
             "--root <folder>, or set the REGISTERS_ROOT environment variable "
             "to that folder.")
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.exists(_config_path(root)):
        _die(f"That folder is not a register yet: {root}\n"
             "There is no config.json inside it. Create the register first "
             "with:\n"
             "  registers init --root <folder> --prefix <SHORT-CODE> --name "
             "\"<full name>\" --schema-file <schema.json>")
    return root


def _config_path(root: str) -> str:
    return os.path.join(root, "config.json")


def _events_dir(root: str) -> str:
    return os.path.join(root, "events")


def load_config(root: str) -> dict:
    with open(_config_path(root), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# The schema (the shape of one record)                                        #
# --------------------------------------------------------------------------- #
# A schema is a small list of fields. Each field has a name the program uses, a
# label people read, and a kind that decides how it is allowed to change:
#
#   fixed    — identity, set when the record is added. Not offered for routine
#              editing; a typo is repaired with `correct`, which needs a note.
#   current  — a value that changes over time; the latest `set` wins.
#   tracked  — a value series; each `post` carries the value and an as-of label
#              (a year, a period, a date). The latest shows; every period stays.

_KINDS = ("fixed", "current", "tracked")
_TYPES = ("text", "number", "date")


def _field_map(cfg: dict) -> dict:
    """name -> field definition, in schema order."""
    return {f["name"]: f for f in cfg.get("schema", [])}


def _visible_fields(cfg: dict) -> list[dict]:
    return [f for f in cfg.get("schema", []) if not f.get("hidden")]


def validate_schema(schema, key) -> None:
    """Check a schema before it is written into a new register. Every problem
    is explained in plain words — the schema outlives the code, so it is worth
    getting right the first time."""
    if not isinstance(schema, list) or not schema:
        _die("The schema must be a list with at least one field. A field looks "
             "like {\"name\": \"code\", \"label\": \"Code\", \"kind\": "
             "\"fixed\"}.")
    names: set[str] = set()
    for f in schema:
        if not isinstance(f, dict):
            _die("Each field in the schema must be an object with a name, a "
                 "label, and a kind.")
        name = f.get("name", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(name)):
            _die(f"Field name \"{name}\" isn't allowed. Use lower-case letters, "
                 "numbers and underscores, starting with a letter, e.g. "
                 "employee_id.")
        if name in names:
            _die(f"The field \"{name}\" appears twice. Each field name must be "
                 "used once.")
        names.add(name)
        if not str(f.get("label", "")).strip():
            _die(f"The field \"{name}\" needs a label — the plain words people "
                 "read at the top of its column.")
        kind = f.get("kind")
        if kind not in _KINDS:
            _die(f"The field \"{name}\" has kind \"{kind}\". It must be one of: "
                 "fixed (identity), current (latest wins), or tracked (a value "
                 "series).")
        ftype = f.get("type", "text")
        if ftype not in _TYPES:
            _die(f"The field \"{name}\" has type \"{ftype}\". It must be one of: "
                 "text, number, or date.")
    if key:
        if key not in names:
            _die(f"The natural key names \"{key}\", but there is no field "
                 "called that in the schema.")
        kf = next(f for f in schema if f["name"] == key)
        if kf.get("kind") != "fixed":
            _die(f"The natural key \"{key}\" must be a fixed field — it is the "
                 "record's identity, not something that changes.")


# --------------------------------------------------------------------------- #
# The event log (one line per action)                                         #
# --------------------------------------------------------------------------- #
# Each action a person takes is one line of JSON in that person's own file.
# Lines are only ever added, never changed or deleted, so a sync folder can
# always merge two people's files cleanly.


def _writer_path(root: str, writer: str) -> str:
    return os.path.join(_events_dir(root), f"{writer}.jsonl")


def _now() -> str:
    """Current time to the second, e.g. 2026-08-16T09:41:07."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def append_event(root: str, event: dict) -> None:
    """Add one action to the current person's file.

    Opening in append mode is atomic per line, and a brief retry rides out the
    rare moment two local processes touch the same file at once.
    """
    writer = event["writer"]
    os.makedirs(_events_dir(root), exist_ok=True)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    path = _writer_path(root, writer)
    for attempt in range(5):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def _iter_raw_events(root: str):
    """Yield (ts, writer, line_number, event) for every action.

    Read order across files does not matter — the fold sorts before it counts,
    so the register is identical no matter which file is read first.
    """
    edir = _events_dir(root)
    if not os.path.isdir(edir):
        return
    for fname in os.listdir(edir):
        if not fname.endswith(".jsonl"):
            continue
        path = os.path.join(edir, fname)
        with open(path, encoding="utf-8") as f:
            for lineno, raw in enumerate(f):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield (ev.get("ts", ""), ev.get("writer", ""), lineno, ev)


def sorted_events(root: str) -> list[dict]:
    """Every action, in the one fixed order the register is built from:
    by time, then by person's name, then by position in that person's file."""
    rows = list(_iter_raw_events(root))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in rows]


# --------------------------------------------------------------------------- #
# The fold (rebuild the register from the actions)                            #
# --------------------------------------------------------------------------- #


def _norm_prefix(s) -> str:
    """A register's short code, tidied for display: trimmed, uppercased."""
    return (s or "").strip().upper()


def _letter_suffix(k: int) -> str:
    """0 -> "" (the first record to propose a number keeps it plain),
    1 -> "b", 2 -> "c", ... — the same way hotel rooms handle an extra door."""
    if k == 0:
        return ""
    if k <= 25:
        return chr(ord("a") + k)
    return "z" + str(k - 24)


def fold(cfg: dict, events: list[dict], warnings: list | None = None) -> dict:
    """Turn the ordered list of actions into the current records.

    Returns a dict keyed by each record's permanent id (8 hex characters minted
    when it was added, and never changed). How a record is labelled for people
    depends on the register:

      * If the schema names a natural key (an entity code, an employee id), the
        label is that field's value. If two people add records with the same
        key before their files sync, nobody's record is silently lost: the
        first in the fixed order is kept and the clash is reported loudly.
      * Otherwise records are numbered PREFIX-1, PREFIX-2, ... If two people
        propose the same number, the later one in the fixed order keeps the
        number with a letter after it (5 and 5b). Numbers never cascade and the
        permanent id underneath never changes.
    """
    keyfield = cfg.get("key")
    fields = _field_map(cfg)
    records: dict[str, dict] = {}
    seen_n: dict[int, int] = {}
    used_keys: dict[str, str] = {}   # lower-cased key value -> id

    def warn(msg: str) -> None:
        if warnings is not None:
            warnings.append(msg)

    for ev in events:
        rid = ev.get("id")
        verb = ev.get("verb")
        if not rid or not verb:
            continue

        if verb == "add":
            if rid in records:
                prev = records[rid]
                same = (ev.get("writer", "") == prev["added_by"]
                        and ev.get("ts", "") == prev["added_at"])
                if not same:
                    warn(f"two different records were created with the same id "
                         f"{rid} (kept {prev['added_by']}'s, ignored "
                         f"{ev.get('writer', '')}'s). Please add the second one "
                         f"again so it gets its own id.")
                continue
            clean = {}
            for fn, v in (ev.get("values") or {}).items():
                if fn not in fields:
                    warn(f"ignoring an unknown field \"{fn}\" in an add action.")
                    continue
                clean[fn] = v
            if keyfield:
                kv = str(clean.get(keyfield, "")).strip()
                low = kv.lower()
                if low in used_keys and used_keys[low] != rid:
                    first = records[used_keys[low]]
                    warn(f"two records were given the same "
                         f"{fields[keyfield]['label']} \"{kv}\" (kept "
                         f"{first['added_by']}'s, ignored "
                         f"{ev.get('writer', '')}'s). One of them needs a "
                         f"different {fields[keyfield]['label']}.")
                    continue
                used_keys[low] = rid
                rec = {"id": rid, "n": None, "nsfx": "", "key_value": kv}
            else:
                proposed = int(ev.get("propose_n") or 1)
                k = seen_n.get(proposed, 0)
                seen_n[proposed] = k + 1
                rec = {"id": rid, "n": proposed, "nsfx": _letter_suffix(k),
                       "key_value": ""}
            rec.update({
                "values": dict(clean),
                "tracked": {},          # field -> [{asof, value, writer, ts}]
                "retired": False,
                "retire_reason": "",
                "comments": [],
                "added_by": ev.get("writer", ""),
                "added_at": ev.get("ts", ""),
            })
            records[rid] = rec
            continue

        rec = records.get(rid)
        if rec is None:
            continue  # an action for a record we never saw added — skip it

        if verb == "set":
            for fn, v in (ev.get("values") or {}).items():
                if fn not in fields:
                    warn(f"ignoring an unknown field \"{fn}\" in a set action.")
                    continue
                rec["values"][fn] = v
        elif verb == "correct":
            fn = ev.get("field")
            if fn not in fields:
                warn(f"ignoring an unknown field \"{fn}\" in a correct action.")
                continue
            rec["values"][fn] = ev.get("value")
            if keyfield and fn == keyfield:
                rec["key_value"] = str(ev.get("value", "")).strip()
        elif verb == "post":
            fn = ev.get("field")
            if fn not in fields:
                warn(f"ignoring an unknown field \"{fn}\" in a post action.")
                continue
            rec["tracked"].setdefault(fn, []).append({
                "asof": ev.get("asof", ""),
                "value": ev.get("value", ""),
                "writer": ev.get("writer", ""),
                "ts": ev.get("ts", ""),
            })
        elif verb == "retire":
            rec["retired"] = True
            rec["retire_reason"] = ev.get("reason", "")
        elif verb == "restore":
            rec["retired"] = False
            rec["retire_reason"] = ""
        elif verb == "comment":
            rec["comments"].append({
                "writer": ev.get("writer", ""),
                "ts": ev.get("ts", ""),
                "text": ev.get("text", ""),
            })

    # Order each tracked series so the latest period (by as-of, then time) is
    # last — that is the value the register shows.
    for rec in records.values():
        for series in rec["tracked"].values():
            series.sort(key=lambda s: (s["asof"], s["ts"]))
    return records


def register_state(root: str) -> dict:
    cfg = load_config(root)
    warnings: list = []
    records = fold(cfg, sorted_events(root), warnings)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return records


def _max_number(records: dict) -> int:
    return max((r["n"] for r in records.values() if r["n"]), default=0)


# --------------------------------------------------------------------------- #
# Labels and lookups                                                          #
# --------------------------------------------------------------------------- #


def key_of(cfg: dict, rec: dict) -> str:
    """The label a person says out loud. It is the natural key's value when the
    register has one, otherwise PREFIX-<number>."""
    if cfg.get("key"):
        return rec.get("key_value", "")
    return f"{_norm_prefix(cfg['prefix'])}-{rec['n']}{rec['nsfx']}"


def latest_tracked(rec: dict, field: str):
    """The most recent posted value for a tracked field, or None."""
    series = rec["tracked"].get(field) or []
    return series[-1] if series else None


def resolve_record(cfg: dict, records: dict, token: str) -> dict:
    """Accept the natural key, a PREFIX-<n> key, or the permanent hex id.
    Returns the record or ends with a plain error."""
    token = (token or "").strip()
    if not token:
        _die("Which record? Give me its key or its id.")
    low = token.lower()

    # Permanent id: a short run of hex characters.
    if 6 <= len(low) <= 12 and all(c in "0123456789abcdef" for c in low):
        if low in records:
            return records[low]

    if cfg.get("key"):
        for rec in records.values():
            if rec.get("key_value", "").strip().lower() == low:
                return rec
        _die(f"There is no record with that key on this register: \"{token}\".")

    # No natural key: a PREFIX-<n> key or a bare number, optionally suffixed.
    prefix = _norm_prefix(cfg["prefix"])
    num = token
    dash = token.rfind("-")
    if dash != -1:
        num = token[dash + 1:]
    m = re.fullmatch(r"(\d+)([a-z](?:\d+)?)?", num.lower())
    if m:
        n, sfx = int(m.group(1)), m.group(2) or ""
        for rec in records.values():
            if rec["n"] == n and rec["nsfx"] == sfx:
                return rec
        _die(f"There is no record {prefix}-{n}{sfx} on this register.")
    _die(f"I don't recognise \"{token}\". Use a key or a record id.")


# --------------------------------------------------------------------------- #
# Value checking                                                              #
# --------------------------------------------------------------------------- #


def _clean_value(field: dict, value) -> str:
    """Tidy and check one value against its field's type. Returns the cleaned
    text, or ends with a plain error."""
    v = "" if value is None else str(value).strip()
    if v == "":
        return ""
    ftype = field.get("type", "text")
    if ftype == "number":
        try:
            float(v)
        except ValueError:
            _die(f"\"{field['label']}\" should be a number, but I got \"{v}\".")
    elif ftype == "date":
        try:
            date.fromisoformat(v)
        except ValueError:
            _die(f"\"{field['label']}\" should be a date like 2026-09-30, but "
                 f"I got \"{v}\".")
    return v


# --------------------------------------------------------------------------- #
# Core actions (the one code path the CLI and the web server both go through)  #
# --------------------------------------------------------------------------- #
# Every change — from a typed command or from a button on the web page — lands
# here. The rules live in exactly one place: the writer is always derived here
# (never handed in from a browser), every action becomes one line in that
# writer's own file, and every rule (required fields, a note on a correction,
# a reason on a retire) is checked here.


def _new_id(records: dict) -> str:
    for _ in range(1000):
        rid = secrets.token_hex(4)
        if rid not in records:
            return rid
    raise RuntimeError("could not mint a free record id")


def act_add(root: str, *, values: dict, writer: str | None = None) -> dict:
    """Add a new record. `values` carries the fixed and current fields."""
    writer = writer or writer_id()
    cfg = load_config(root)
    fields = _field_map(cfg)
    keyfield = cfg.get("key")
    clean = {}
    for fn, v in (values or {}).items():
        if fn not in fields:
            raise RegisterError(_unknown_field(cfg, fn))
        f = fields[fn]
        if f["kind"] == "tracked":
            raise RegisterError(
                f"\"{f['label']}\" is a tracked value. Add the record first, "
                f"then post it with an as-of period.")
        clean[fn] = _clean_value(f, v)
    for fn, f in fields.items():
        required = f.get("required") or (fn == keyfield)
        if required and not clean.get(fn):
            raise RegisterError(f"\"{f['label']}\" is needed to add a record.")
    records = register_state(root)
    if keyfield:
        kv = clean.get(keyfield, "").strip().lower()
        for rec in records.values():
            if rec.get("key_value", "").strip().lower() == kv:
                raise RegisterError(
                    f"There is already a record with "
                    f"{fields[keyfield]['label']} \"{clean[keyfield]}\".")
    rid = _new_id(records)
    event = {"ts": _now(), "writer": writer, "verb": "add", "id": rid,
             "values": clean}
    if not keyfield:
        event["propose_n"] = _max_number(records) + 1
    append_event(root, event)
    return fold(cfg, sorted_events(root))[rid]


def _unknown_field(cfg: dict, fn: str) -> str:
    known = ", ".join(f["name"] for f in cfg.get("schema", []))
    return f"There is no field called \"{fn}\". The fields are: {known}."


def _act_mutate(root: str, record_token: str, verb: str, build,
                writer: str | None = None) -> dict:
    """Shared path for an action on an existing record."""
    writer = writer or writer_id()
    cfg = load_config(root)
    records = register_state(root)
    rec = resolve_record(cfg, records, record_token)
    event = {"ts": _now(), "writer": writer, "verb": verb, "id": rec["id"]}
    build(event, rec, writer, cfg)
    append_event(root, event)
    return fold(cfg, sorted_events(root))[rec["id"]]


def act_set(root: str, record_token: str, *, values: dict,
            writer: str | None = None) -> dict:
    cfg = load_config(root)
    fields = _field_map(cfg)
    clean = {}
    for fn, v in (values or {}).items():
        if fn not in fields:
            raise RegisterError(_unknown_field(cfg, fn))
        f = fields[fn]
        if f["kind"] != "current":
            if f["kind"] == "fixed":
                raise RegisterError(
                    f"\"{f['label']}\" is a fixed field — it can't be changed "
                    f"with set. If it is wrong, repair it with correct (a note "
                    f"is required).")
            raise RegisterError(
                f"\"{f['label']}\" is a tracked value — record it with post, "
                f"which asks for the period it is for.")
        clean[fn] = _clean_value(f, v)
    if not clean:
        raise RegisterError("Nothing to set. Give at least one current field, "
                            "like status=active.")

    def build(ev, rec, w, cfg):
        ev["values"] = clean
    return _act_mutate(root, record_token, "set", build, writer=writer)


def act_post(root: str, record_token: str, *, field: str, value,
             asof: str, writer: str | None = None) -> dict:
    cfg = load_config(root)
    fields = _field_map(cfg)
    if field not in fields:
        raise RegisterError(_unknown_field(cfg, field))
    f = fields[field]
    if f["kind"] != "tracked":
        raise RegisterError(
            f"\"{f['label']}\" isn't a tracked value, so it has no periods to "
            f"post. Change it with " +
            ("correct." if f["kind"] == "fixed" else "set."))
    asof = (asof or "").strip()
    if not asof:
        raise RegisterError(
            "A posted value needs an as-of period — the year or date it is "
            "for, like 2025 or FY25.")
    val = _clean_value(f, value)
    if val == "":
        raise RegisterError(f"Give a value for \"{f['label']}\" to post.")

    def build(ev, rec, w, cfg):
        ev["field"] = field
        ev["value"] = val
        ev["asof"] = asof
    return _act_mutate(root, record_token, "post", build, writer=writer)


def act_correct(root: str, record_token: str, *, field: str, value,
                note: str, writer: str | None = None) -> dict:
    cfg = load_config(root)
    fields = _field_map(cfg)
    if field not in fields:
        raise RegisterError(_unknown_field(cfg, field))
    f = fields[field]
    if f["kind"] != "fixed":
        raise RegisterError(
            f"\"{f['label']}\" isn't a fixed field, so it isn't corrected. "
            f"Change it with " + ("set." if f["kind"] == "current"
                                  else "post."))
    note = (note or "").strip()
    if not note:
        raise RegisterError(
            "A correction always needs a note saying why — a fixed field is "
            "the record's identity, so a change to it is worth a sentence.")
    val = _clean_value(f, value)
    if val == "":
        raise RegisterError(f"Give the corrected value for \"{f['label']}\".")
    if cfg.get("key") == field:
        records = register_state(root)
        me = resolve_record(cfg, records, record_token)
        low = val.strip().lower()
        for rec in records.values():
            if (rec["id"] != me["id"]
                    and rec.get("key_value", "").strip().lower() == low):
                raise RegisterError(
                    f"Another record already has {f['label']} \"{val}\".")

    def build(ev, rec, w, cfg):
        ev["field"] = field
        ev["value"] = val
        ev["note"] = note
    return _act_mutate(root, record_token, "correct", build, writer=writer)


def act_retire(root: str, record_token: str, *, reason: str,
               writer: str | None = None) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise RegisterError("Say why the record is leaving the active list — "
                            "retiring one needs a reason.")

    def build(ev, rec, w, cfg):
        ev["reason"] = reason
    return _act_mutate(root, record_token, "retire", build, writer=writer)


def act_restore(root: str, record_token: str, *,
                writer: str | None = None) -> dict:
    return _act_mutate(root, record_token, "restore",
                       lambda ev, rec, w, cfg: None, writer=writer)


def act_comment(root: str, record_token: str, *, text: str,
                writer: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise RegisterError("Say something — a note can't be empty.")

    def build(ev, rec, w, cfg):
        ev["text"] = text
    return _act_mutate(root, record_token, "comment", build, writer=writer)


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #


def cmd_init(args) -> int:
    """Create a new register folder from a schema."""
    root = os.path.abspath(os.path.expanduser(args.root))
    prefix = (args.prefix or "").strip()
    if not prefix:
        _die("A register needs a short code (--prefix), e.g. STAFF. It labels "
             "records that don't have a natural key.")
    name = (args.name or "").strip()
    if not name:
        _die("A register needs a full name (--name), e.g. \"Staff directory\". "
             "It is what people see at the top.")
    if not args.schema_file:
        _die("A register needs a schema (--schema-file <file.json>): the list "
             "of fields each record has.")
    try:
        with open(os.path.expanduser(args.schema_file), encoding="utf-8") as f:
            schema = json.load(f)
    except FileNotFoundError:
        _die(f"I couldn't find the schema file: {args.schema_file}")
    except json.JSONDecodeError as e:
        _die(f"The schema file isn't valid JSON: {e}")
    key = (args.key or "").strip() or None
    validate_schema(schema, key)
    if os.path.exists(_config_path(root)):
        _die(f"There is already a register in {root}. Nothing changed.")
    os.makedirs(_events_dir(root), exist_ok=True)
    cfg = {"name": name, "prefix": prefix}
    if key:
        cfg["key"] = key
    cfg["schema"] = schema
    with open(_config_path(root), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    _write_launcher(root)
    print(f"Created the register \"{name}\" in {root}")
    if key:
        kf = _field_map(cfg)[key]
        print(f"Records are identified by their {kf['label']}.")
    else:
        print(f"Records will be numbered {prefix}-1, {prefix}-2, and so on.")
    print("Double-clicking \"Open register.cmd\" in that folder shows the "
          "up-to-date register in a browser.")
    return 0


def _write_launcher(root: str) -> None:
    """A double-clickable file so a non-technical teammate can work the register
    from a browser. Prefers a registers.py copied into the register folder
    itself; falls back to wherever this file lives."""
    me = os.path.abspath(__file__)
    body = (
        "@echo off\r\n"
        "set \"R=%~dp0registers.py\"\r\n"
        f"if not exist \"%R%\" set \"R={me}\"\r\n"
        "where pythonw >nul 2>nul\r\n"
        "if %errorlevel%==0 (\r\n"
        "  start \"\" pythonw \"%R%\" serve --root \"%~dp0.\"\r\n"
        ") else (\r\n"
        "  start \"Register\" /min python \"%R%\" serve --root \"%~dp0.\"\r\n"
        ")\r\n")
    with open(os.path.join(root, "Open register.cmd"), "w",
              encoding="utf-8", newline="") as f:
        f.write(body)


def _parse_assignments(items) -> dict:
    out = {}
    for it in items:
        if "=" not in it:
            _die(f"Use field=value, like status=active. I didn't understand "
                 f"\"{it}\".")
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def cmd_add(args) -> int:
    """Add a new record: registers add --root DIR code=ACME name=\"Acme Ltd\"."""
    root = resolve_root(args)
    values = _parse_assignments(args.assignments)
    rec = act_add(root, values=values)
    cfg = load_config(root)
    print(f"Added {key_of(cfg, rec)}")
    print(f"  id {rec['id']}")
    return 0


def cmd_set(args) -> int:
    """Change one or more current fields."""
    root = resolve_root(args)
    values = _parse_assignments(args.assignments)
    rec = act_set(root, args.record, values=values)
    cfg = load_config(root)
    print(f"Updated {key_of(cfg, rec)}.")
    return 0


def cmd_post(args) -> int:
    """Post a value for a tracked field, for a given period."""
    root = resolve_root(args)
    rec = act_post(root, args.record, field=args.field, value=args.value,
                   asof=args.asof)
    cfg = load_config(root)
    print(f"Posted {args.field} = {args.value} (as of {args.asof}) on "
          f"{key_of(cfg, rec)}.")
    return 0


def cmd_correct(args) -> int:
    """Repair a fixed field (a note is required)."""
    root = resolve_root(args)
    rec = act_correct(root, args.record, field=args.field, value=args.value,
                      note=args.note)
    cfg = load_config(root)
    print(f"Corrected {args.field} on {key_of(cfg, rec)}.")
    return 0


def cmd_retire(args) -> int:
    """Move a record off the active list, with a reason."""
    root = resolve_root(args)
    rec = act_retire(root, args.record, reason=args.reason)
    cfg = load_config(root)
    print(f"Retired {key_of(cfg, rec)}: {rec['retire_reason']}")
    return 0


def cmd_restore(args) -> int:
    """Bring a retired record back to the active list."""
    root = resolve_root(args)
    rec = act_restore(root, args.record)
    cfg = load_config(root)
    print(f"Restored {key_of(cfg, rec)}.")
    return 0


def cmd_comment(args) -> int:
    """Add a note to a record's history."""
    root = resolve_root(args)
    rec = act_comment(root, args.record, text=args.text)
    cfg = load_config(root)
    print(f"Added a note to {key_of(cfg, rec)}.")
    return 0


def cmd_show(args) -> int:
    """Show one record in full, with every tracked field's whole series."""
    root = resolve_root(args)
    cfg = load_config(root)
    records = register_state(root)
    rec = resolve_record(cfg, records, args.record)
    print(f"{key_of(cfg, rec)}")
    status = "retired" if rec["retired"] else "active"
    if rec["retired"] and rec["retire_reason"]:
        status = f"retired ({rec['retire_reason']})"
    print(f"  status : {status}")
    for f in _visible_fields(cfg):
        if f["kind"] == "tracked":
            series = rec["tracked"].get(f["name"]) or []
            latest = series[-1] if series else None
            shown = (f"{latest['value']} (as of {latest['asof']})"
                     if latest else "(none yet)")
            print(f"  {f['label']} : {shown}")
            for s in series:
                print(f"      {s['asof']}: {s['value']}  "
                      f"({s['writer']}, {s['ts'][:10]})")
        else:
            print(f"  {f['label']} : {rec['values'].get(f['name'], '') or '-'}")
    print(f"  id     : {rec['id']}")
    print(f"  added  : {rec['added_at']} by {rec['added_by']}")
    if rec["comments"]:
        print("  notes over time:")
        for c in rec["comments"]:
            print(f"    {c['ts']}  {c['writer']}: {c['text']}")
    return 0


def _sorted_records(cfg: dict, records: dict, show_all: bool) -> list[dict]:
    rows = list(records.values())
    if not show_all:
        rows = [r for r in rows if not r["retired"]]
    if cfg.get("key"):
        rows.sort(key=lambda r: r.get("key_value", "").lower())
    else:
        rows.sort(key=lambda r: (r["n"], r["nsfx"]))
    return rows


def _cell_text(cfg: dict, rec: dict, f: dict) -> str:
    """The latest value of a field as plain text (tracked shows its period)."""
    if f["kind"] == "tracked":
        latest = latest_tracked(rec, f["name"])
        if not latest:
            return ""
        return f"{latest['value']} (as of {latest['asof']})"
    return rec["values"].get(f["name"], "")


def cmd_board(args) -> int:
    """Print the register as a table in the terminal."""
    root = resolve_root(args)
    cfg = load_config(root)
    records = register_state(root)
    rows = _sorted_records(cfg, records, args.all)
    print(cfg["name"])
    if not rows:
        print("(no records yet)")
        return 0
    visible = _visible_fields(cfg)
    headers = ["key"] + [f["label"] for f in visible]
    if args.all:
        headers.append("state")
    table = []
    for rec in rows:
        line = [key_of(cfg, rec)]
        for f in visible:
            val = _cell_text(cfg, rec, f)
            if len(val) > 40:
                val = val[:39] + "…"
            line.append(val or "-")
        if args.all:
            line.append("retired" if rec["retired"] else "active")
        table.append(line)
    print(_render_table(headers, table))
    return 0


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    cols = len(headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(r[i])))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * widths[i] for i in range(cols))]
    for r in rows:
        out.append("  ".join(str(r[i]).ljust(widths[i]) for i in range(cols)))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Static pages (markdown + html) and the spreadsheet exports                  #
# --------------------------------------------------------------------------- #


def _esc(text) -> str:
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_STRUCTURAL_CSS = (
    "body{margin:1rem auto;max-width:64rem;padding:0 1rem;"
    "font-family:sans-serif;}"
    "table{border-collapse:collapse;width:100%;margin-top:1rem;}"
    "th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #999;"
    "vertical-align:top;}"
    "th.fit,td.fit{white-space:nowrap;width:1%;}"
    "tr.closed td{opacity:.55;}"
)


def _md_cell(text) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _render_markdown(cfg: dict, records: dict) -> str:
    visible = _visible_fields(cfg)
    active = _sorted_records(cfg, records, show_all=False)
    retired = [r for r in _sorted_records(cfg, records, show_all=True)
               if r["retired"]]
    head = ["key"] + [f["label"] for f in visible]
    lines = [f"# {cfg['name']}", ""]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("| " + " | ".join("---" for _ in head) + " |")
    if not active:
        lines.append("| " + " | ".join([""] + ["_no records_"] +
                                        [""] * (len(head) - 2)) + " |")
    for rec in active:
        cells = [key_of(cfg, rec)] + [_md_cell(_cell_text(cfg, rec, f))
                                      for f in visible]
        lines.append("| " + " | ".join(cells) + " |")
    if retired:
        lines += ["", "## Retired", "",
                  "| " + " | ".join(head) + " |",
                  "| " + " | ".join("---" for _ in head) + " |"]
        for rec in retired:
            cells = [key_of(cfg, rec)] + [_md_cell(_cell_text(cfg, rec, f))
                                          for f in visible]
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def _render_html(cfg: dict, records: dict) -> str:
    visible = _visible_fields(cfg)
    active = _sorted_records(cfg, records, show_all=False)
    retired = [r for r in _sorted_records(cfg, records, show_all=True)
               if r["retired"]]

    def header():
        cells = ["<th class=\"fit\">Key</th>"]
        cells += [f"<th>{_esc(f['label'])}</th>" for f in visible]
        return "<tr>" + "".join(cells) + "</tr>"

    def row(rec, done=False):
        cls = " class=\"closed\"" if done else ""
        cells = [f"<td class=\"fit\">{_esc(key_of(cfg, rec))}</td>"]
        cells += [f"<td>{_esc(_cell_text(cfg, rec, f))}</td>" for f in visible]
        return "<tr" + cls + ">" + "".join(cells) + "</tr>"

    span = len(visible) + 1
    active_rows = "\n".join(row(r) for r in active) or \
        f"<tr><td colspan=\"{span}\"><em>no records</em></td></tr>"
    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{_esc(cfg['name'])}</title>",
        "<style>",
        _STRUCTURAL_CSS,
        "</style></head><body>",
        f"<h1>{_esc(cfg['name'])}</h1>",
        "<table><thead>" + header() + "</thead><tbody>",
        active_rows,
        "</tbody></table>",
    ]
    if retired:
        parts += [
            "<h2>Retired</h2>",
            "<table><thead>" + header() + "</thead><tbody>",
            "\n".join(row(r, done=True) for r in retired),
            "</tbody></table>",
        ]
    parts.append("<p><small>Rebuilt from everyone's entries. "
                 "This page is generated — don't edit it by hand.</small></p>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def cmd_page(args) -> int:
    """Rebuild register.md and register.html from scratch."""
    root = resolve_root(args)
    cfg = load_config(root)
    records = register_state(root)
    md = _render_markdown(cfg, records)
    html = _render_html(cfg, records)
    with open(os.path.join(root, "register.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(root, "register.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {os.path.join(root, 'register.md')}")
    print(f"Wrote {os.path.join(root, 'register.html')}")
    return 0


def cmd_view(args) -> int:
    """Show the current register in a browser (written to a temp file, never
    into the shared folder, so viewing can't cause a sync conflict)."""
    import tempfile
    import webbrowser
    root = resolve_root(args)
    cfg = load_config(root)
    records = register_state(root)
    html = _render_html(cfg, records)
    safe = "".join(c if c.isalnum() or c in "-_" else "-"
                   for c in cfg["prefix"])
    path = os.path.join(tempfile.gettempdir(), f"register-{safe}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Register is current as of the latest synced actions: {path}")
    if getattr(args, "no_open", False):
        return 0
    if hasattr(os, "startfile"):
        os.startfile(path)
    else:
        webbrowser.open("file://" + path)
    return 0


def _excel_safe(value: str) -> str:
    """Excel treats a cell starting with = + - or @ as a formula and runs it.
    A leading apostrophe makes Excel show the text as-is instead."""
    value = str(value or "")
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def cmd_export(args) -> int:
    """Write register.csv (latest values, one row per record) and, with
    --history, history.csv (the full value series in long format)."""
    root = resolve_root(args)
    cfg = load_config(root)
    records = register_state(root)
    rows = _sorted_records(cfg, records, show_all=True)
    visible = _visible_fields(cfg)

    header = ["key"]
    for f in visible:
        if f["kind"] == "tracked":
            header += [f["label"], f"{f['label']} as of"]
        else:
            header += [f["label"]]
    header += ["state", "id"]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for rec in rows:
        line = [_excel_safe(key_of(cfg, rec))]
        for f in visible:
            if f["kind"] == "tracked":
                latest = latest_tracked(rec, f["name"])
                line.append(_excel_safe(latest["value"]) if latest else "")
                line.append(_excel_safe(latest["asof"]) if latest else "")
            else:
                line.append(_excel_safe(rec["values"].get(f["name"], "")))
        line.append("retired" if rec["retired"] else "active")
        line.append(rec["id"])
        w.writerow(line)
    path = os.path.join(root, "register.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
    print(f"Wrote {path}")

    if getattr(args, "history", False):
        hbuf = io.StringIO()
        hw = csv.writer(hbuf)
        hw.writerow(["key", "field", "as_of", "value", "writer", "ts"])
        tracked = [f for f in visible if f["kind"] == "tracked"]
        for rec in rows:
            for f in tracked:
                for s in rec["tracked"].get(f["name"], []):
                    hw.writerow([_excel_safe(key_of(cfg, rec)),
                                 _excel_safe(f["label"]),
                                 _excel_safe(s["asof"]),
                                 _excel_safe(s["value"]), s["writer"],
                                 s["ts"]])
        hpath = os.path.join(root, "history.csv")
        with open(hpath, "w", encoding="utf-8-sig", newline="") as f:
            f.write(hbuf.getvalue())
        print(f"Wrote {hpath}")
    return 0


def cmd_log(args) -> int:
    """Show one record's whole history, oldest first, in plain language."""
    root = resolve_root(args)
    cfg = load_config(root)
    events = sorted_events(root)
    records = fold(cfg, events)
    rec = resolve_record(cfg, records, args.record)
    print(f"History of {key_of(cfg, rec)}")
    for ev in events:
        if ev.get("id") != rec["id"]:
            continue
        print("  " + _describe_event(cfg, ev))
    return 0


def _describe_event(cfg: dict, ev: dict) -> str:
    ts = ev.get("ts", "")
    who = ev.get("writer", "someone")
    verb = ev.get("verb")
    fields = _field_map(cfg)

    def label(fn):
        f = fields.get(fn)
        return f["label"] if f else fn

    if verb == "add":
        vals = ev.get("values") or {}
        shown = ", ".join(f"{label(k)} = {v}" for k, v in vals.items())
        return f"{ts}  {who} added it" + (f": {shown}" if shown else "")
    if verb == "set":
        vals = ev.get("values") or {}
        shown = ", ".join(f"{label(k)} = {v}" for k, v in vals.items())
        return f"{ts}  {who} set {shown or 'a field'}"
    if verb == "correct":
        return (f"{ts}  {who} corrected {label(ev.get('field'))} to "
                f"{ev.get('value', '')} — {ev.get('note', '')}")
    if verb == "post":
        return (f"{ts}  {who} posted {label(ev.get('field'))} = "
                f"{ev.get('value', '')} (as of {ev.get('asof', '')})")
    if verb == "retire":
        return f"{ts}  {who} retired it: {ev.get('reason', '')}"
    if verb == "restore":
        return f"{ts}  {who} brought it back"
    if verb == "comment":
        return f"{ts}  {who} noted: {ev.get('text', '')}"
    return f"{ts}  {who} did {verb}"


# --------------------------------------------------------------------------- #
# The interactive register (a small local web page)                           #
# --------------------------------------------------------------------------- #
# `registers serve` runs a tiny web server on this machine only (127.0.0.1) so
# a non-technical teammate can add, edit, post, correct, retire, and comment on
# records from a browser. Every change goes through the exact same core the
# typed commands use: the writer is derived here, one line is appended to that
# writer's own file, and every rule is checked here. The browser never touches
# a file directly.


def _payload_schema(cfg: dict) -> list[dict]:
    out = []
    for f in cfg.get("schema", []):
        out.append({
            "name": f["name"],
            "label": f["label"],
            "kind": f["kind"],
            "type": f.get("type", "text"),
            "required": bool(f.get("required")) or (f["name"] == cfg.get("key")),
            "hidden": bool(f.get("hidden")),
        })
    return out


def _register_payload(root: str) -> dict:
    cfg = load_config(root)
    records = register_state(root)
    active = _sorted_records(cfg, records, show_all=False)
    retired = [r for r in _sorted_records(cfg, records, show_all=True)
               if r["retired"]]

    def item(rec: dict) -> dict:
        tracked = {}
        for f in cfg["schema"]:
            if f["kind"] == "tracked":
                series = rec["tracked"].get(f["name"], [])
                tracked[f["name"]] = {
                    "latest": series[-1] if series else None,
                    "series": series,
                }
        return {
            "id": rec["id"],
            "key": key_of(cfg, rec),
            "values": {f["name"]: rec["values"].get(f["name"], "")
                       for f in cfg["schema"]},
            "tracked": tracked,
            "retired": rec["retired"],
            "retire_reason": rec["retire_reason"],
            "comments": rec["comments"],
        }

    return {
        "name": cfg["name"],
        "prefix": _norm_prefix(cfg["prefix"]),
        "key": cfg.get("key"),
        "schema": _payload_schema(cfg),
        "active": [item(r) for r in active],
        "retired": [item(r) for r in retired],
    }


_ACT_LOCK = threading.Lock()


def _dispatch_act(root: str, data: dict) -> dict:
    """Turn one message from the page into a real action through the shared
    core. Raises RegisterError (→ a plain 400) on any bad request."""
    with _ACT_LOCK:
        return _dispatch_act_locked(root, data)


def _dispatch_act_locked(root: str, data: dict) -> dict:
    verb = (data.get("verb") or "").strip()
    record = data.get("record")
    needs_record = ("set", "post", "correct", "retire", "restore", "comment")
    if verb in needs_record and not (isinstance(record, str) and record.strip()):
        raise RegisterError("Which record? The request needs its key or id.")
    if verb == "add":
        return act_add(root, values=data.get("values") or {})
    if verb == "set":
        return act_set(root, record, values=data.get("values") or {})
    if verb == "post":
        return act_post(root, record, field=data.get("field", ""),
                        value=data.get("value", ""), asof=data.get("asof", ""))
    if verb == "correct":
        return act_correct(root, record, field=data.get("field", ""),
                           value=data.get("value", ""),
                           note=data.get("note", ""))
    if verb == "retire":
        return act_retire(root, record, reason=data.get("reason", ""))
    if verb == "restore":
        return act_restore(root, record)
    if verb == "comment":
        return act_comment(root, record, text=data.get("text", ""))
    raise RegisterError(f"I don't know the action \"{verb}\".")


class _RegisterServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    root: str
    token: str
    writer: str
    allowed_origins: set


def make_server(root: str) -> _RegisterServer:
    """Build (but don't start) the local register server on a free port. A
    fresh random token is minted for this run; the page carries it and every
    action request must present it."""
    server = _RegisterServer(("127.0.0.1", 0), _RegisterHandler)
    server.root = root
    server.token = secrets.token_urlsafe(24)
    server.writer = writer_id()
    server.idle_limit = float(os.environ.get("REGISTERS_SERVE_IDLE", "15"))
    server.last_seen = time.monotonic() + float(
        os.environ.get("REGISTERS_SERVE_GRACE", "45"))
    port = server.server_address[1]
    server.allowed_origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }
    return server


class _RegisterHandler(http.server.BaseHTTPRequestHandler):
    """One request. GET / serves the page; the /api/* calls read and change the
    register. Everything under /api requires this run's token and must come
    from the page's own origin."""

    server_version = "registers-board"

    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, html: str, code: int = 200) -> None:
        self._send(code, html.encode("utf-8"), "text/html; charset=utf-8")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin in self.server.allowed_origins

    def _token_ok(self) -> bool:
        tok = self.headers.get("X-Registers-Token")
        if not tok:
            query = urllib.parse.urlparse(self.path).query
            tok = (urllib.parse.parse_qs(query).get("token") or [None])[0]
        return bool(tok) and secrets.compare_digest(str(tok),
                                                    self.server.token)

    def _guard(self) -> bool:
        if not self._origin_ok():
            self._json({"error": "This request came from another website, so "
                                 "it was refused."}, 403)
            return False
        if not self._token_ok():
            self._json({"error": "This request was missing the register's "
                                 "access code. Open the register from its own "
                                 "window."}, 403)
            return False
        self.server.last_seen = time.monotonic()
        return True

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", ""):
            self._html(_render_app(load_config(self.server.root),
                                   self.server.token, self.server.writer))
            return
        if path == "/api/register":
            if not self._guard():
                return
            self._json(_register_payload(self.server.root))
            return
        if path == "/api/ping":
            if not self._guard():
                return
            self._json({"ok": True})
            return
        if path == "/api/log":
            if not self._guard():
                return
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            token = (query.get("record") or [""])[0]
            try:
                events = sorted_events(self.server.root)
                cfg = load_config(self.server.root)
                folded = fold(cfg, events)
                rec = resolve_record(cfg, folded, token)
            except RegisterError as e:
                self._json({"error": str(e)}, 400)
                return
            lines = [_describe_event(cfg, ev) for ev in events
                     if ev.get("id") == rec["id"]]
            self._json({"key": key_of(cfg, rec), "lines": lines})
            return
        self._json({"error": "There is nothing at that address."}, 404)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/goodbye":
            if not self._guard():
                return
            self.server.last_seen = (time.monotonic()
                                     - self.server.idle_limit + 3.0)
            self._json({"ok": True})
            return
        if path != "/api/act":
            self._json({"error": "There is nothing at that address."}, 404)
            return
        if not self._guard():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            self._json({"error": "That request didn't make sense."}, 400)
            return
        try:
            _dispatch_act(self.server.root, data)
        except RegisterError as e:
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:
            self._json({"error": f"Something went wrong: {e}"}, 500)
            return
        self._json({"ok": True, "register": _register_payload(self.server.root)})


def start_idle_watchdog(server: _RegisterServer) -> threading.Thread:
    """Shut the server down once the page's heartbeats stop — closing the last
    browser tab is the off switch."""
    def watch():
        while True:
            time.sleep(0.25)
            if time.monotonic() - server.last_seen > server.idle_limit:
                server.shutdown()
                return
    t = threading.Thread(target=watch, daemon=True)
    t.start()
    return t


def cmd_serve(args) -> int:
    """Run the interactive register in a browser (this machine only)."""
    import webbrowser
    root = resolve_root(args)
    cfg = load_config(root)
    server = make_server(root)
    start_idle_watchdog(server)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"The register \"{cfg['name']}\" is open at:")
    print(f"  {url}")
    print("Anyone at this computer can add, edit, and update records there.")
    print("It closes itself a few seconds after its last browser tab closes.")
    if not getattr(args, "no_open", False):
        try:
            if hasattr(os, "startfile"):
                os.startfile(url)
            else:
                webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _render_app(cfg: dict, token: str, writer: str) -> str:
    """The interactive page: one self-contained HTML file, all styling and
    behaviour inline, nothing loaded from the internet."""
    boot = json.dumps({
        "token": token,
        "writer": writer,
        "name": cfg["name"],
        "prefix": _norm_prefix(cfg["prefix"]),
        "key": cfg.get("key"),
        "schema": _payload_schema(cfg),
    })
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(cfg['name'])}</title>\n"
        "<style>\n" + _APP_CSS + "</style></head><body>\n"
        "<div id=\"app\"><p class=\"muted\">Loading the register…</p></div>\n"
        "<script>window.__REG__ = " + boot.replace("</", "<\\/") + ";</script>\n"
        "<script>\n" + _APP_JS + "</script>\n"
        "</body></html>\n"
    )


_APP_CSS = (
    _STRUCTURAL_CSS +
    ".muted{opacity:.6;} .err{font-weight:bold;}"
    ".actions button,.newbtn,.panel button{font:inherit;cursor:pointer;"
    "margin:0 .25rem .25rem 0;}"
    ".panel{margin:.4rem 0 .2rem;padding:.5rem .6rem;border:1px solid #999;}"
    ".panel label{display:block;margin:.3rem 0 .15rem;}"
    ".panel input,.panel textarea,.panel select{width:100%;box-sizing:border-box;"
    "font:inherit;}"
    ".panel textarea{min-height:3rem;}"
    ".panel .cancel{background:none;border:none;text-decoration:underline;}"
    ".new{margin:1rem 0;padding:.5rem .6rem;border:1px solid #999;}"
    ".new .row{display:flex;gap:.6rem;flex-wrap:wrap;}"
    ".new .row>div{flex:1;min-width:9rem;}"
    ".new input{width:100%;box-sizing:border-box;font:inherit;margin-top:.15rem;}"
    ".newbtn{margin-top:.7rem;}"
    ".reason,.cmts{opacity:.75;font-size:.9em;}"
    ".cmts div{margin-top:.15rem;}"
    ".flash{margin:.6rem 0;font-weight:bold;}"
    ".hist div{margin-top:.15rem;opacity:.8;font-size:.9em;}"
)


_APP_JS = r"""
(function(){
  var BOOT = window.__REG__;
  var app = document.getElementById('app');
  var reg = null;
  var panel = null;   // {id, kind, field?, lines?}
  var flash = null;

  function esc(s){
    return String(s==null?'':s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }
  function h(a){ return a.join(''); }

  function api(path, opts){
    opts = opts || {};
    opts.headers = Object.assign({'X-Registers-Token': BOOT.token},
                                 opts.headers || {});
    return fetch(path, opts).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(body){
        if(!r.ok){ throw new Error(body.error || ('Error ' + r.status)); }
        return body;
      });
    });
  }

  function load(){
    api('/api/register').then(function(b){ reg = b; render(); })
      .catch(function(e){
        app.innerHTML = '<p class="err">' + esc(e.message) + '</p>';
      });
  }

  var gone = false;
  function regGone(){
    if(gone){ return; }
    gone = true;
    app.innerHTML = '<p class="err">The register closed. Double-click ' +
      '"Open register.cmd" in the register folder to open it again.</p>';
  }
  setInterval(function(){
    if(gone){ return; }
    api('/api/ping').catch(regGone);
  }, 2000);
  window.addEventListener('pagehide', function(){
    if(navigator.sendBeacon){
      navigator.sendBeacon('/api/goodbye?token=' +
                           encodeURIComponent(BOOT.token));
    }
  });

  function act(payload, okText){
    api('/api/act', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    }).then(function(res){
      reg = res.register;
      panel = null;
      flash = {ok:true, text: okText || 'Done.'};
      render();
    }).catch(function(e){
      flash = {ok:false, text: e.message};
      render();
    });
  }

  function visibleFields(){
    return reg.schema.filter(function(f){ return !f.hidden; });
  }
  function fieldsByKind(kind){
    return reg.schema.filter(function(f){ return f.kind===kind && !f.hidden; });
  }
  function fieldDef(name){
    var found = null;
    reg.schema.forEach(function(f){ if(f.name===name){ found = f; } });
    return found;
  }

  function openPanel(id, kind){ panel = {id:id, kind:kind}; flash = null; render(); }
  function closePanel(){ panel = null; render(); }

  function render(){
    if(!reg){ return; }
    var out = [];
    out.push('<h1>' + esc(reg.name) + '</h1>');
    out.push('<p class="muted">You are signed in as <b>' + esc(BOOT.writer) +
             '</b>. Every change is saved under your name.</p>');
    if(flash){
      out.push('<div class="flash">' + esc(flash.text) + '</div>');
    }
    out.push(newBlock());
    out.push('<h2>Records</h2>');
    out.push(tableFor(reg.active, false));
    if(reg.retired.length){
      out.push('<h2>Retired</h2>');
      out.push(tableFor(reg.retired, true));
    }
    out.push('<p><small>The register rebuilds from everyone\'s entries each ' +
             'time. It closes itself when the last tab closes.</small></p>');
    app.innerHTML = out.join('');
    wire();
  }

  function headerRow(){
    var cells = ['<th class="fit">Key</th>'];
    visibleFields().forEach(function(f){
      cells.push('<th>' + esc(f.label) + '</th>');
    });
    cells.push('<th></th>');
    return '<tr>' + cells.join('') + '</tr>';
  }

  function cellFor(t, f){
    if(f.kind === 'tracked'){
      var tr = t.tracked[f.name];
      if(tr && tr.latest){
        return esc(tr.latest.value) + ' <span class="muted">(as of ' +
               esc(tr.latest.asof) + ')</span>';
      }
      return '<span class="muted">—</span>';
    }
    var v = t.values[f.name];
    return v ? esc(v) : '<span class="muted">—</span>';
  }

  function tableFor(rows, retired){
    if(!rows.length){
      return '<p class="muted">' +
        (retired ? 'None retired.' : 'No records yet.') + '</p>';
    }
    var out = ['<table><thead>' + headerRow() + '</thead><tbody>'];
    rows.forEach(function(t){
      var cells = ['<td class="fit">' + esc(t.key) + '</td>'];
      visibleFields().forEach(function(f){
        cells.push('<td>' + cellFor(t, f) + '</td>');
      });
      cells.push('<td class="fit">' + actionBar(t, retired) +
                 panelFor(t) + '</td>');
      out.push('<tr' + (retired ? ' class="closed"' : '') + '>' +
               cells.join('') + '</tr>');
    });
    out.push('</tbody></table>');
    return out.join('');
  }

  function btn(t, kind, label){
    return '<button data-open="' + kind + '" data-id="' + esc(t.id) + '">' +
           esc(label) + '</button>';
  }

  function actionBar(t, retired){
    var b = ['<div class="actions">'];
    if(retired){
      b.push(btn(t, 'restore', 'Restore'));
      b.push(btn(t, 'history', 'History'));
    } else {
      if(fieldsByKind('current').length){ b.push(btn(t, 'set', 'Edit')); }
      fieldsByKind('tracked').forEach(function(f){
        b.push('<button data-open="post" data-id="' + esc(t.id) +
               '" data-field="' + esc(f.name) + '">Post ' + esc(f.label) +
               '</button>');
      });
      if(fieldsByKind('fixed').length){ b.push(btn(t, 'correct', 'Correct')); }
      b.push(btn(t, 'comment', 'Comment'));
      b.push(btn(t, 'retire', 'Retire'));
      b.push(btn(t, 'history', 'History'));
    }
    b.push('</div>');
    var ex = '';
    if(retired && t.retire_reason){
      ex += '<div class="reason">Retired: ' + esc(t.retire_reason) + '</div>';
    }
    if(t.comments && t.comments.length){
      ex += '<div class="cmts">';
      t.comments.forEach(function(c){
        ex += '<div>' + esc(c.writer) + ': ' + esc(c.text) + '</div>';
      });
      ex += '</div>';
    }
    return b.join('') + ex;
  }

  function field(name, label, val){
    return '<label>' + esc(label) + '</label>' +
           '<input data-f="' + esc(name) + '" type="text" value="' +
           esc(val) + '">';
  }
  function area(name, label, val){
    return '<label>' + esc(label) + '</label>' +
           '<textarea data-f="' + esc(name) + '">' + esc(val) + '</textarea>';
  }

  function formPanel(title, fields, goLabel, id, verb, extra){
    return h(['<div class="panel" data-panel="' + esc(id) + '">',
      '<div class="muted">' + esc(title) + '</div>',
      fields.join(''),
      '<button class="go" data-go="' + verb + '" data-id="' + esc(id) + '" ' +
        (extra || '') + '>' + esc(goLabel) + '</button>',
      '<button class="cancel" data-cancel="1">cancel</button>',
      '</div>']);
  }

  function panelFor(t){
    if(!panel || panel.id !== t.id){ return ''; }
    var k = panel.kind;
    if(k === 'set'){
      var fields = fieldsByKind('current').map(function(f){
        return field(f.name, f.label, t.values[f.name] || '');
      });
      return formPanel('Edit this record', fields, 'Save', t.id, 'set');
    }
    if(k === 'post'){
      var f = fieldDef(panel.field);
      return formPanel('Post ' + f.label, [
        field('value', 'Value', ''),
        field('asof', 'As of (a year or date, like 2025 or FY25)', '')
      ], 'Post', t.id, 'post', 'data-field="' + esc(panel.field) + '"');
    }
    if(k === 'correct'){
      var opts = fieldsByKind('fixed').map(function(f){
        return '<option value="' + esc(f.name) + '">' + esc(f.label) +
               '</option>';
      }).join('');
      return h(['<div class="panel" data-panel="' + esc(t.id) + '">',
        '<div class="muted">Fix a typo in a fixed field. Say why — a ' +
          'correction always needs a note.</div>',
        '<label>Which field to fix</label>' +
          '<select data-f="field">' + opts + '</select>',
        field('value', 'Correct value', ''),
        area('note', 'Why (required)', ''),
        '<button class="go" data-go="correct" data-id="' + esc(t.id) + '">' +
          'Save correction</button>',
        '<button class="cancel" data-cancel="1">cancel</button>',
        '</div>']);
    }
    if(k === 'comment'){
      return formPanel('Add a note to this record', [
        area('text', 'Your note', '')
      ], 'Add note', t.id, 'comment');
    }
    if(k === 'retire'){
      return formPanel('Move this record off the active list', [
        field('reason', 'Reason (required)', '')
      ], 'Retire', t.id, 'retire');
    }
    if(k === 'history'){
      var rows = (panel.lines || []).map(function(l){
        return '<div>' + esc(l) + '</div>';
      }).join('');
      return h(['<div class="panel hist" data-panel="' + esc(t.id) + '">',
        '<div class="muted">Everything that has happened to this record:</div>',
        rows || '<div>(nothing yet)</div>',
        '<button class="cancel" data-cancel="1">close</button>',
        '</div>']);
    }
    return '';
  }

  function newBlock(){
    var open = panel && panel.id === '_new';
    if(!open){
      return '<div class="new"><button class="newbtn" data-open="new" ' +
             'data-id="_new">+ New record</button></div>';
    }
    var rows = [];
    reg.schema.forEach(function(f){
      if(f.hidden || f.kind === 'tracked'){ return; }
      var req = f.required ? ' *' : '';
      rows.push('<div><label>' + esc(f.label) + req + '</label>' +
                '<input data-f="' + esc(f.name) + '" type="text"></div>');
    });
    return h(['<div class="new">',
      '<div class="row">', rows.join(''), '</div>',
      '<button class="newbtn" data-go="add" data-id="_new">Add record</button> ',
      '<button class="cancel" data-cancel="1">cancel</button>',
      '</div>']);
  }

  function readFields(id){
    var scope = document.querySelector('[data-panel="' + cssq(id) + '"]') ||
                document.querySelector('.new');
    var vals = {};
    if(!scope){ return vals; }
    scope.querySelectorAll('[data-f]').forEach(function(el){
      vals[el.getAttribute('data-f')] = el.value;
    });
    return vals;
  }
  function cssq(s){ return String(s).replace(/["\\]/g, '\\$&'); }

  function wire(){
    app.querySelectorAll('[data-open]').forEach(function(el){
      el.addEventListener('click', function(){
        var kind = el.getAttribute('data-open');
        var id = el.getAttribute('data-id');
        if(kind === 'new'){ openPanel('_new', 'new'); }
        else if(kind === 'restore'){ act({verb:'restore', record:id}, 'Restored.'); }
        else if(kind === 'post'){
          panel = {id:id, kind:'post', field: el.getAttribute('data-field')};
          flash = null; render();
        }
        else if(kind === 'history'){
          api('/api/log?record=' + encodeURIComponent(id))
            .then(function(res){
              panel = {id:id, kind:'history', lines:res.lines};
              flash = null; render();
            })
            .catch(function(e){ flash = {ok:false, text:e.message}; render(); });
        }
        else { openPanel(id, kind); }
      });
    });
    app.querySelectorAll('[data-cancel]').forEach(function(el){
      el.addEventListener('click', closePanel);
    });
    app.querySelectorAll('[data-go]').forEach(function(el){
      el.addEventListener('click', function(){
        var verb = el.getAttribute('data-go');
        var id = el.getAttribute('data-id');
        var f = readFields(id);
        if(verb === 'add'){
          act({verb:'add', values:f}, 'Record added.');
        } else if(verb === 'set'){
          act({verb:'set', record:id, values:f}, 'Saved.');
        } else if(verb === 'post'){
          act({verb:'post', record:id, field: el.getAttribute('data-field'),
               value:f.value, asof:f.asof}, 'Value posted.');
        } else if(verb === 'correct'){
          act({verb:'correct', record:id, field:f.field, value:f.value,
               note:f.note}, 'Corrected.');
        } else if(verb === 'comment'){
          act({verb:'comment', record:id, text:f.text}, 'Note added.');
        } else if(verb === 'retire'){
          act({verb:'retire', record:id, reason:f.reason}, 'Retired.');
        }
      });
    });
  }

  load();
})();
"""


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #


class RegisterError(SystemExit):
    """A plain, person-facing problem with a request. Subclasses SystemExit so
    the command line exits non-zero; the web server catches it and turns it
    into a 400 with the same plain message instead of crashing the request."""


def _die(msg: str) -> None:
    raise RegisterError(msg)


# --------------------------------------------------------------------------- #
# Command line                                                                 #
# --------------------------------------------------------------------------- #


def _add_root(p) -> None:
    p.add_argument("--root", help="the register's folder (or set REGISTERS_ROOT)")


def _add_record(p) -> None:
    p.add_argument("--record", "-r", required=True,
                   help="which record: its key, a PREFIX-3 key, or its id")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="registers", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a new register folder from a schema")
    p.add_argument("--root", required=True,
                   help="folder to create the register in")
    p.add_argument("--prefix", required=True,
                   help="short code for records without a natural key, e.g. STAFF")
    p.add_argument("--name", required=True,
                   help="full name shown at the top of the register")
    p.add_argument("--schema-file", dest="schema_file", required=True,
                   help="a JSON file: the list of fields each record has")
    p.add_argument("--key",
                   help="name of the fixed field to use as the natural key "
                        "(an entity code, an employee id); optional")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add", help="add a new record (field=value pairs)")
    _add_root(p)
    p.add_argument("assignments", nargs="+",
                   help="field=value pairs, e.g. code=ACME name=\"Acme Ltd\"")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("set", help="change one or more current fields")
    _add_root(p)
    _add_record(p)
    p.add_argument("assignments", nargs="+",
                   help="field=value pairs, e.g. status=active")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("post", help="post a tracked value for a period")
    _add_root(p)
    _add_record(p)
    p.add_argument("--field", required=True, help="which tracked field")
    p.add_argument("--value", required=True, help="the value for this period")
    p.add_argument("--asof", required=True,
                   help="the period it is for: a year or date, like 2025 or FY25")
    p.set_defaults(fn=cmd_post)

    p = sub.add_parser("correct", help="repair a fixed field (a note is required)")
    _add_root(p)
    _add_record(p)
    p.add_argument("--field", required=True, help="which fixed field")
    p.add_argument("--value", required=True, help="the corrected value")
    p.add_argument("--note", required=True, help="why the change is being made")
    p.set_defaults(fn=cmd_correct)

    p = sub.add_parser("retire", help="move a record off the active list")
    _add_root(p)
    _add_record(p)
    p.add_argument("reason", help="why it is being retired")
    p.set_defaults(fn=cmd_retire)

    p = sub.add_parser("restore", help="bring a retired record back")
    _add_root(p)
    _add_record(p)
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("comment", help="add a note to a record's history")
    _add_root(p)
    _add_record(p)
    p.add_argument("text", help="your note")
    p.set_defaults(fn=cmd_comment)

    p = sub.add_parser("show", help="show one record in full")
    _add_root(p)
    _add_record(p)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("board", help="print the register as a table")
    _add_root(p)
    p.add_argument("--all", action="store_true", help="include retired records")
    p.set_defaults(fn=cmd_board)

    p = sub.add_parser("view", help="open the up-to-date register in a browser")
    _add_root(p)
    p.add_argument("--no-open", dest="no_open", action="store_true",
                   help="write the page but don't open a browser")
    p.set_defaults(fn=cmd_view)

    p = sub.add_parser("serve", help="open the interactive register in a browser")
    _add_root(p)
    p.add_argument("--no-open", dest="no_open", action="store_true",
                   help="start the register but don't open a browser")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("page", help="rebuild register.md and register.html")
    _add_root(p)
    p.set_defaults(fn=cmd_page)

    p = sub.add_parser("export",
                       help="write register.csv (and history.csv with --history)")
    _add_root(p)
    p.add_argument("--history", action="store_true",
                   help="also write history.csv, the full value series")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("log", help="show one record's whole history")
    _add_root(p)
    _add_record(p)
    p.set_defaults(fn=cmd_log)

    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except RegisterError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())
