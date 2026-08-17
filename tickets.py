#!/usr/bin/env python3
"""tickets — a shared ticket board that survives file-sync folders.

Every person who uses the board writes only to their own file, so nothing they
type can ever collide with or overwrite what a teammate typed. The board you
read (a table, a web page, a spreadsheet) is never stored — it is rebuilt from
everyone's files each time you ask for it, in a fixed order, so two people
looking at the same folder always see exactly the same board.

Store on disk:

    <root>/config.json          the board's short code and full name
    <root>/events/<name>.jsonl  one file per person, one action per line
    <root>/board.md             rebuilt on demand (page)
    <root>/board.html           rebuilt on demand (page), opens in any browser
    <root>/board.csv            rebuilt on demand (export), opens in Excel

Nothing in this file needs git, a server, or any outside package — it is one
plain Python file (3.10+). Copy it anywhere and it works.
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
# TICKETS_WRITER to write under their own name, but it is never required.


def writer_id() -> str:
    """The name of the person (or helper) making a change."""
    override = os.environ.get("TICKETS_WRITER")
    if override and override.strip():
        return _safe_name(override.strip())
    home = os.path.expanduser("~")
    # Split on both slash styles so a Windows-style home path gives the same
    # answer on any platform (os.path.basename would not).
    parts = [p for p in re.split(r"[\\/]+", home) if p]
    return _safe_name(parts[-1] if parts else "") or "unknown"


def _safe_name(name: str) -> str:
    """Keep a writer name usable as a filename (no slashes, spaces, etc.)."""
    keep = [c if (c.isalnum() or c in "-_.") else "-" for c in name]
    return "".join(keep).strip("-") or "unknown"


# --------------------------------------------------------------------------- #
# Where is the board (the root)                                               #
# --------------------------------------------------------------------------- #


def resolve_root(args) -> str:
    """Find the board folder, or explain plainly what is missing."""
    root = getattr(args, "root", None) or os.environ.get("TICKETS_ROOT")
    if not root:
        _die("I don't know which board to use. Point me at its folder with "
             "--root <folder>, or set the TICKETS_ROOT environment variable to "
             "that folder.")
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.exists(_config_path(root)):
        _die(f"That folder is not a board yet: {root}\n"
             "There is no config.json inside it. Create the board first with:\n"
             "  tickets init --root <folder> --prefix <SHORT-CODE> --name "
             "\"<full name>\"")
    return root


def _config_path(root: str) -> str:
    return os.path.join(root, "config.json")


def _events_dir(root: str) -> str:
    return os.path.join(root, "events")


def load_config(root: str) -> dict:
    with open(_config_path(root), encoding="utf-8") as f:
        return json.load(f)


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

    Uses the same append-and-retry the sibling stream tool uses: opening in
    append mode is atomic per line, and a brief retry rides out the rare moment
    two local processes touch the same file at once.
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
    """Yield (ts, writer, line_number, event) for every action on the board.

    Read order across files does not matter — the fold sorts before it counts,
    so the board is identical no matter which file is read first.
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
    """Every action, in the one fixed order the board is built from:
    by time, then by person's name, then by position in that person's file."""
    rows = list(_iter_raw_events(root))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in rows]


# --------------------------------------------------------------------------- #
# The fold (rebuild the board from the actions)                               #
# --------------------------------------------------------------------------- #

# What each status means in plain words.
STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_BLOCKED = "blocked"
STATUS_CLOSED = "closed"


def _norm_prefix(s) -> str:
    """A ticket's short code, tidied for display: trimmed, uppercased."""
    return (s or "").strip().upper()


def _letter_suffix(k: int) -> str:
    """0 -> "" (the first ticket to propose a number keeps it plain),
    1 -> "b", 2 -> "c", ... — the same way hotel rooms handle an extra door."""
    if k == 0:
        return ""
    if k <= 25:
        return chr(ord("a") + k)
    return "z" + str(k - 24)


def fold(events: list[dict], warnings: list | None = None) -> dict:
    """Turn the ordered list of actions into the current tickets.

    Returns a dict keyed by the ticket's permanent id (the short code that
    never changes). Each ticket also carries a display number. If two people
    happened to propose the same number (both working before their files had
    synced), nobody's number is taken away: the one whose action comes later
    in the fixed order keeps the same number with a letter after it (5 and
    5b). Numbers never cascade — a clash between two tickets can never move
    a third — and the permanent id underneath never changes, so no earlier
    action is ever left pointing at the wrong ticket.
    """
    seen_n: dict[int, int] = {}
    tickets: dict[str, dict] = {}

    for ev in events:
        tid = ev.get("id")
        verb = ev.get("verb")
        if not tid or not verb:
            continue

        if verb == "open":
            if tid in tickets:
                prev = tickets[tid]
                same = (ev.get("writer", "") == prev["opened_by"]
                        and ev.get("ts", "") == prev["opened_at"])
                if not same and warnings is not None:
                    warnings.append(
                        f"two different tickets were created with the same id "
                        f"{tid} (\"{prev['title']}\" by {prev['opened_by']} and "
                        f"\"{ev.get('title', '')}\" by {ev.get('writer', '')}). "
                        f"Keeping the first; please re-open the second one so "
                        f"it gets its own id.")
                continue
            proposed = int(ev.get("propose_n") or 1)
            k = seen_n.get(proposed, 0)
            seen_n[proposed] = k + 1
            tickets[tid] = {
                "id": tid,
                "n": proposed,
                "nsfx": _letter_suffix(k),
                "prefix": _norm_prefix(ev.get("prefix")),
                "title": ev.get("title", ""),
                "owner": ev.get("owner") or "",
                "due": ev.get("due") or "",
                "notes": ev.get("notes") or "",
                "blocked_reason": "",
                "update": None,        # latest status line: {ts, writer, text}
                "base": "active",      # active | blocked | closed
                "source": "",          # evidence recorded at close
                "close_note": "",
                "opened_by": ev.get("writer", ""),
                "opened_at": ev.get("ts", ""),
                "comments": [],
            }
            continue

        t = tickets.get(tid)
        if t is None:
            continue  # an action for a ticket we never saw opened — skip it

        if verb == "claim":
            t["owner"] = ev.get("owner") or ev.get("writer") or t["owner"]
        elif verb == "block":
            t["base"] = "blocked"
            if ev.get("reason"):
                t["blocked_reason"] = ev["reason"]
        elif verb == "reopen":
            t["base"] = "active"
            t["blocked_reason"] = ""
        elif verb == "close":
            t["base"] = "closed"
            t["source"] = ev.get("source", "")
            t["close_note"] = ev.get("note") or ""
        elif verb == "edit":
            for field in ("title", "owner", "due", "notes", "prefix"):
                if field in ev and ev[field] is not None:
                    t[field] = (_norm_prefix(ev[field]) if field == "prefix"
                                else ev[field])
        elif verb == "update":
            # The ticket's one-line "where this stands" field. Only the
            # latest shows on the board; every earlier one stays in the log.
            t["update"] = {
                "ts": ev.get("ts", ""),
                "writer": ev.get("writer", ""),
                "text": ev.get("text", ""),
            }
        elif verb == "comment":
            t["comments"].append({
                "writer": ev.get("writer", ""),
                "ts": ev.get("ts", ""),
                "text": ev.get("text", ""),
            })

    for t in tickets.values():
        t["status"] = _status_of(t)
    return tickets


def _status_of(t: dict) -> str:
    if t["base"] == "closed":
        return STATUS_CLOSED
    if t["base"] == "blocked":
        return STATUS_BLOCKED
    return STATUS_CLAIMED if t["owner"] else STATUS_OPEN


def board_state(root: str) -> dict:
    warnings: list = []
    tickets = fold(sorted_events(root), warnings)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return tickets


# --------------------------------------------------------------------------- #
# Finding a ticket by what the person typed                                   #
# --------------------------------------------------------------------------- #


def resolve_ticket(tickets: dict, prefix: str, token: str) -> dict:
    """Accept a bare number (3), the full key (ACME-DOC-3), or the permanent
    6-character id. Returns the ticket or ends with a plain error."""
    token = (token or "").strip()
    if not token:
        _die("Which ticket? Give me its number, its key like "
             f"{prefix}-3, or its id.")

    # Permanent id: a short run of hex characters.
    low = token.lower()
    if 6 <= len(low) <= 12 and all(c in "0123456789abcdef" for c in low):
        if low in tickets:
            return tickets[low]

    # Full key: PREFIX-<n> (case-insensitive on the prefix), or a bare
    # number, either optionally carrying a letter (5, 5b, ACME-5b).
    num = token
    dash = token.rfind("-")
    if dash != -1:
        num = token[dash + 1:]

    m = re.fullmatch(r"(\d+)([a-z](?:\d+)?)?", num.lower())
    if m:
        n, sfx = int(m.group(1)), m.group(2) or ""
        for t in tickets.values():
            if t["n"] == n and t["nsfx"] == sfx:
                return t
        others = sorted((t for t in tickets.values() if t["n"] == n),
                        key=lambda t: t["nsfx"])
        if others:
            hint = ", ".join(f"{key_of(prefix, t)} ({t['title']})"
                             for t in others)
            _die(f"There is no ticket {prefix}-{n}{sfx}, but there is: {hint}")
        _die(f"There is no ticket {prefix}-{n}{sfx} on this board.")

    _die(f"I don't recognise \"{token}\". Use a number, a key like "
         f"{prefix}-3, or a ticket id.")


def key_of(prefix: str, t: dict) -> str:
    """The key people say out loud. The short code in front is the ticket's
    own (the project or engagement it really belongs to) when it has one,
    otherwise the board's. The number after it is the ticket's identity: it
    is unique across the whole board, so changing the code in front never
    renumbers anything and old references keep working."""
    return f"{t.get('prefix') or prefix}-{t['n']}{t['nsfx']}"


# --------------------------------------------------------------------------- #
# Dates                                                                        #
# --------------------------------------------------------------------------- #


def _today() -> str:
    return date.today().isoformat()


def _is_overdue(due: str, status: str) -> bool:
    if not due or status == STATUS_CLOSED:
        return False
    return due < _today()


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #


def cmd_init(args) -> int:
    """Create a new board folder."""
    root = os.path.abspath(os.path.expanduser(args.root))
    prefix = (args.prefix or "").strip()
    if not prefix:
        _die("A board needs a short code (--prefix), e.g. ACME-TP. Make it "
             "specific to this piece of work — one client engagement, one "
             "project. It shows up in every ticket key.")
    name = (args.name or "").strip()
    if not name:
        _die("A board needs a full name (--name), e.g. \"Acme transfer-pricing "
             "documentation 2026\". It is what people see at the top of the board.")
    if os.path.exists(_config_path(root)):
        _die(f"There is already a board in {root}. Nothing changed.")
    os.makedirs(_events_dir(root), exist_ok=True)
    with open(_config_path(root), "w", encoding="utf-8") as f:
        json.dump({"prefix": prefix, "name": name}, f, indent=2)
        f.write("\n")
    _write_launcher(root)
    print(f"Created the board \"{name}\" in {root}")
    print(f"Tickets will be numbered {prefix}-1, {prefix}-2, and so on.")
    print("Double-clicking \"Open board.cmd\" in that folder shows the "
          "up-to-date board in a browser.")
    return 0


def _write_launcher(root: str) -> None:
    """A double-clickable file so a non-technical teammate can always work the
    board from a browser: it starts the small local board program, which opens
    the up-to-date board and lets them take, edit, and close tickets — every
    change rebuilt live from the latest synced actions, so there is never a
    stale tracker to regenerate. Prefers a tickets.py copied into the board
    folder itself (the normal shared-folder deployment); falls back to wherever
    this file lives."""
    me = os.path.abspath(__file__)
    # pythonw runs with no console window, so the browser is the only thing
    # the person ever sees (the server closes itself when the tab closes);
    # plain python in a minimized window is the fallback.
    body = (
        "@echo off\r\n"
        "set \"T=%~dp0tickets.py\"\r\n"
        f"if not exist \"%T%\" set \"T={me}\"\r\n"
        "where pythonw >nul 2>nul\r\n"
        "if %errorlevel%==0 (\r\n"
        "  start \"\" pythonw \"%T%\" serve --root \"%~dp0.\"\r\n"
        ") else (\r\n"
        "  start \"Ticket board\" /min python \"%T%\" serve --root \"%~dp0.\"\r\n"
        ")\r\n")
    with open(os.path.join(root, "Open board.cmd"), "w",
              encoding="utf-8", newline="") as f:
        f.write(body)


def _max_number(tickets: dict) -> int:
    return max((t["n"] for t in tickets.values()), default=0)


def cmd_open(args) -> int:
    """Add a new ticket."""
    root = resolve_root(args)
    t = act_open(root, title=args.title, owner=args.owner,
                 due=args.due, notes=args.notes, prefix=args.prefix)
    cfg = load_config(root)
    print(f"Opened {key_of(cfg['prefix'], t)}  ({t['title']})")
    print(f"  id {t['id']}")
    return 0


def _new_id(tickets: dict) -> str:
    # 8 hex characters: wide enough that two people minting ids offline at
    # the same time will not collide in practice (and the fold warns loudly
    # if the near-impossible happens anyway).
    for _ in range(1000):
        tid = secrets.token_hex(4)
        if tid not in tickets:
            return tid
    raise RuntimeError("could not mint a free ticket id")


# --------------------------------------------------------------------------- #
# Core actions (the one code path the CLI and the web server both go through)  #
# --------------------------------------------------------------------------- #
# Every change to the board — from a typed command or from a button on the web
# page — lands here. That means the rules live in exactly one place: the writer
# is always derived here (never handed in from a browser), every action becomes
# one line in that writer's own file, and closing always demands a source.


def act_open(root: str, *, title: str, owner: str | None = None,
             due: str | None = None, notes: str | None = None,
             prefix: str | None = None, writer: str | None = None) -> dict:
    """Add a new ticket. Returns the freshly-folded ticket."""
    writer = writer or writer_id()
    title = (title or "").strip()
    if not title:
        raise TicketError("A new ticket needs a title — a short line saying "
                          "what it is about.")
    tickets = board_state(root)
    tid = _new_id(tickets)
    event = {
        "ts": _now(),
        "writer": writer,
        "verb": "open",
        "id": tid,
        "title": title,
        "propose_n": _max_number(tickets) + 1,
    }
    if owner and owner.strip():
        event["owner"] = owner.strip()
    if due and due.strip():
        event["due"] = due.strip()
    if notes and notes.strip():
        event["notes"] = notes.strip()
    if prefix and prefix.strip():
        event["prefix"] = _norm_prefix(prefix)
    append_event(root, event)
    # Re-fold so the number we report is the one the board will actually show.
    return fold(sorted_events(root))[tid]


def _act_mutate(root: str, ticket_token: str, verb: str, build,
                writer: str | None = None) -> dict:
    """Shared path for an action on an existing ticket."""
    writer = writer or writer_id()
    cfg = load_config(root)
    tickets = board_state(root)
    t = resolve_ticket(tickets, cfg["prefix"], ticket_token)
    event = {"ts": _now(), "writer": writer, "verb": verb, "id": t["id"]}
    build(event, t, writer, cfg)
    append_event(root, event)
    # Re-fold so the returned ticket reflects the change just made — the CLI
    # prints from it and the web server hands it straight back to the page.
    return fold(sorted_events(root))[t["id"]]


def act_claim(root: str, ticket_token: str, *, owner: str | None = None,
              writer: str | None = None) -> dict:
    def build(ev, t, w, cfg):
        ev["owner"] = (owner.strip() if owner and owner.strip() else w)
    return _act_mutate(root, ticket_token, "claim", build, writer=writer)


def act_block(root: str, ticket_token: str, *, reason: str,
              writer: str | None = None) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise TicketError("Say what is in the way — blocking a ticket needs a "
                          "reason.")

    def build(ev, t, w, cfg):
        ev["reason"] = reason
    return _act_mutate(root, ticket_token, "block", build, writer=writer)


def act_reopen(root: str, ticket_token: str, *,
               writer: str | None = None) -> dict:
    return _act_mutate(root, ticket_token, "reopen",
                       lambda ev, t, w, cfg: None, writer=writer)


def _close_source_hint(writer: str) -> str:
    return (
        "Closing a ticket always needs a source — a short note of where "
        "the proof that it's done can be found. Any of these is fine:\n"
        "  a document name   (e.g. Acme master file v3)\n"
        "  a link            (e.g. https://...)\n"
        "  what happened     (e.g. emailed client 8 Aug)\n"
        "If there is nothing to point to and you are personally vouching "
        f"that it's done, say so on the record: person:{writer}")


def act_close(root: str, ticket_token: str, *, source: str,
              note: str | None = None, writer: str | None = None) -> dict:
    writer = writer or writer_id()
    source = (source or "").strip()
    if not source:
        raise TicketError(_close_source_hint(writer))

    def build(ev, t, w, cfg):
        ev["source"] = source
        if note and note.strip():
            ev["note"] = note.strip()
    return _act_mutate(root, ticket_token, "close", build, writer=writer)


def act_edit(root: str, ticket_token: str, *, fields: dict,
             writer: str | None = None) -> dict:
    clean = {}
    for f in ("title", "owner", "due", "notes", "prefix"):
        if f in fields and fields[f] is not None:
            clean[f] = str(fields[f]).strip()
    if "title" in clean and not clean["title"]:
        raise TicketError("A ticket needs a title — it can be changed, "
                          "but not blanked.")
    if not clean:
        raise TicketError("Nothing to change. Give at least one of title, "
                          "owner, due, or notes.")

    def build(ev, t, w, cfg):
        ev.update(clean)
    return _act_mutate(root, ticket_token, "edit", build, writer=writer)


def act_update(root: str, ticket_token: str, *, text: str,
               writer: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise TicketError("Say where the ticket stands — an update can't "
                          "be empty.")

    def build(ev, t, w, cfg):
        ev["text"] = text
    return _act_mutate(root, ticket_token, "update", build, writer=writer)


def act_comment(root: str, ticket_token: str, *, text: str,
                writer: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise TicketError("Say something — a comment can't be empty.")

    def build(ev, t, w, cfg):
        ev["text"] = text
    return _act_mutate(root, ticket_token, "comment", build, writer=writer)


def cmd_claim(args) -> int:
    """Take ownership of a ticket (or hand it to someone with --owner)."""
    root = resolve_root(args)
    t = act_claim(root, args.ticket, owner=args.owner)
    cfg = load_config(root)
    print(f"{key_of(cfg['prefix'], t)} is now with {t['owner']}.")
    return 0


def cmd_block(args) -> int:
    """Mark a ticket stuck, with the reason."""
    root = resolve_root(args)
    t = act_block(root, args.ticket, reason=args.reason)
    cfg = load_config(root)
    print(f"{key_of(cfg['prefix'], t)} is blocked: {t['blocked_reason']}")
    return 0


def cmd_reopen(args) -> int:
    """Move a closed or blocked ticket back to active."""
    root = resolve_root(args)
    t = act_reopen(root, args.ticket)
    cfg = load_config(root)
    print(f"{key_of(cfg['prefix'], t)} is active again.")
    return 0


def cmd_close(args) -> int:
    """Finish a ticket. A source is always required — see where the proof is."""
    root = resolve_root(args)
    t = act_close(root, args.ticket, source=args.source, note=args.note)
    cfg = load_config(root)
    print(f"Closed {key_of(cfg['prefix'], t)}.")
    print(f"  proof: {t['source']}")
    return 0


def cmd_edit(args) -> int:
    """Change a ticket's title, owner, due date, or notes."""
    root = resolve_root(args)
    fields = {}
    for f in ("title", "owner", "due", "notes", "prefix"):
        v = getattr(args, f)
        if v is not None:
            fields[f] = v
    t = act_edit(root, args.ticket, fields=fields)
    cfg = load_config(root)
    print(f"Updated {key_of(cfg['prefix'], t)}.")
    return 0


def cmd_update(args) -> int:
    """Post the latest one-line status of a ticket."""
    root = resolve_root(args)
    t = act_update(root, args.ticket, text=args.text)
    cfg = load_config(root)
    print(f"{key_of(cfg['prefix'], t)} latest: {t['update']['text']}")
    return 0


def cmd_comment(args) -> int:
    """Add a note to a ticket's history."""
    root = resolve_root(args)
    t = act_comment(root, args.ticket, text=args.text)
    cfg = load_config(root)
    print(f"Added a note to {key_of(cfg['prefix'], t)}.")
    return 0


def cmd_show(args) -> int:
    """Show one ticket in full."""
    root = resolve_root(args)
    cfg = load_config(root)
    tickets = board_state(root)
    t = resolve_ticket(tickets, cfg["prefix"], args.ticket)
    key = key_of(cfg["prefix"], t)
    print(f"{key}  —  {t['title']}")
    print(f"  status : {_status_word(t)}")
    print(f"  owner  : {t['owner'] or '(nobody yet)'}")
    print(f"  due    : {_due_word(t)}")
    if t["update"]:
        u = t["update"]
        print(f"  latest : {u['text']}  ({u['writer']}, {u['ts'][:10]})")
    if t["notes"]:
        print(f"  notes  : {t['notes']}")
    if t["status"] == STATUS_CLOSED and t["source"]:
        print(f"  proof  : {t['source']}")
        if t["close_note"]:
            print(f"  closing note: {t['close_note']}")
    print(f"  id     : {t['id']}")
    print(f"  opened : {t['opened_at']} by {t['opened_by']}")
    if t["comments"]:
        print("  notes over time:")
        for c in t["comments"]:
            print(f"    {c['ts']}  {c['writer']}: {c['text']}")
    return 0


def _status_word(t: dict) -> str:
    if t["status"] == STATUS_OPEN:
        return "open (nobody has taken it)"
    if t["status"] == STATUS_CLAIMED:
        return "in progress"
    if t["status"] == STATUS_BLOCKED:
        if t["blocked_reason"]:
            return f"blocked ({t['blocked_reason']})"
        return "blocked (stuck on something)"
    return "done"


def _due_word(t: dict) -> str:
    if not t["due"]:
        return "no deadline"
    if _is_overdue(t["due"], t["status"]):
        return f"{t['due']}  (past due!)"
    return t["due"]


def _sorted_for_board(tickets: dict, show_all: bool) -> list[dict]:
    rows = list(tickets.values())
    if not show_all:
        rows = [t for t in rows if t["status"] != STATUS_CLOSED]
    # Deadlines first, soonest at the top; ticket with no deadline after those;
    # ties broken by ticket number so the order never wobbles.
    rows.sort(key=lambda t: (t["due"] == "", t["due"] or "", t["n"], t["nsfx"]))
    return rows


def cmd_board(args) -> int:
    """Print the board as a table in the terminal."""
    root = resolve_root(args)
    cfg = load_config(root)
    tickets = board_state(root)
    rows = _sorted_for_board(tickets, args.all)
    print(cfg["name"])
    if not rows:
        print("(no open tickets)" if not args.all else "(no tickets yet)")
        return 0
    table = []
    for t in rows:
        due = t["due"] or "-"
        if _is_overdue(t["due"], t["status"]):
            due = f"{t['due']} !"
        latest = t["update"]["text"] if t["update"] else "-"
        if len(latest) > 48:
            latest = latest[:47] + "\u2026"
        table.append([
            key_of(cfg["prefix"], t),
            t["title"],
            t["owner"] or "-",
            due,
            _short_status(t),
            latest,
        ])
    headers = ["key", "title", "owner", "due", "status", "latest update"]
    print(_render_table(headers, table))
    overdue = sum(1 for t in rows if _is_overdue(t["due"], t["status"]))
    if overdue:
        print(f"\n! = past its deadline ({overdue} "
              f"{'ticket' if overdue == 1 else 'tickets'})")
    return 0


def _short_status(t: dict) -> str:
    return {
        STATUS_OPEN: "open",
        STATUS_CLAIMED: "in progress",
        STATUS_BLOCKED: "blocked",
        STATUS_CLOSED: "done",
    }[t["status"]]


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


def cmd_view(args) -> int:
    """Show the current board in a browser.

    The page is rebuilt from the latest synced actions every time, so it is
    never stale, and it is written to this machine's temp folder — never into
    the shared folder — so viewing can't create sync conflicts."""
    import tempfile
    import webbrowser
    root = resolve_root(args)
    cfg = load_config(root)
    tickets = board_state(root)
    html = _render_html(cfg, tickets)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in cfg["prefix"])
    path = os.path.join(tempfile.gettempdir(), f"ticket-board-{safe}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Board is current as of the latest synced actions: {path}")
    if getattr(args, "no_open", False):
        return 0
    if hasattr(os, "startfile"):
        os.startfile(path)  # Windows: default browser
    else:
        webbrowser.open("file://" + path)
    return 0


def cmd_page(args) -> int:
    """Rebuild board.md and board.html from scratch."""
    root = resolve_root(args)
    cfg = load_config(root)
    tickets = board_state(root)
    md = _render_markdown(cfg, tickets)
    html = _render_html(cfg, tickets)
    with open(os.path.join(root, "board.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(root, "board.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {os.path.join(root, 'board.md')}")
    print(f"Wrote {os.path.join(root, 'board.html')}")
    return 0


def _render_markdown(cfg: dict, tickets: dict) -> str:
    lines = [f"# {cfg['name']}", ""]
    active = _sorted_for_board(tickets, show_all=False)
    closed = [t for t in tickets.values() if t["status"] == STATUS_CLOSED]
    closed.sort(key=lambda t: (t["n"], t["nsfx"]))
    lines.append("| key | title | owner | due | status | latest update |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    if not active:
        lines.append("| | _no open tickets_ | | | | |")
    for t in active:
        due = t["due"] or ""
        if _is_overdue(t["due"], t["status"]):
            due = f"{t['due']} (past due!)"
        latest = t["update"]["text"] if t["update"] else ""
        lines.append(f"| {key_of(cfg['prefix'], t)} | {_md_cell(t['title'])} | "
                     f"{_md_cell(t['owner'])} | {due} | {_short_status(t)} | "
                     f"{_md_cell(latest)} |")
    if closed:
        lines += ["", "## Done", "",
                  "| key | title | owner | proof |",
                  "| --- | --- | --- | --- |"]
        for t in closed:
            lines.append(f"| {key_of(cfg['prefix'], t)} | {_md_cell(t['title'])} | "
                         f"{_md_cell(t['owner'])} | {_md_cell(t['source'])} |")
    lines.append("")
    return "\n".join(lines)


def _md_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _esc(text: str) -> str:
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# Structure only, no design language: browser-default fonts and colors, a
# readable table, and shrink-to-fit columns so a ticket key or a date never
# wraps mid-value (`fit` = nowrap + width:1%; the title column absorbs the
# rest). Deployments that want a look restyle on top; the tool ships bones.
_STRUCTURAL_CSS = (
    "body{margin:1rem auto;max-width:64rem;padding:0 1rem;"
    "font-family:sans-serif;}"
    "table{border-collapse:collapse;width:100%;margin-top:1rem;}"
    "th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #999;"
    "vertical-align:top;}"
    "th.fit,td.fit{white-space:nowrap;width:1%;}"
    "tr.closed td{opacity:.55;}"
)


def _render_html(cfg: dict, tickets: dict) -> str:
    """A single self-contained HTML file: all styling is inline, nothing is
    loaded from the internet, so it opens the same on any machine offline."""
    active = _sorted_for_board(tickets, show_all=False)
    closed = [t for t in tickets.values() if t["status"] == STATUS_CLOSED]
    closed.sort(key=lambda t: (t["n"], t["nsfx"]))

    def row(t, done=False):
        cls = f" class=\"{t['status']}\""
        due = _esc(t["due"])
        if _is_overdue(t["due"], t["status"]):
            due = f"<strong>{_esc(t['due'])} (past due!)</strong>"
        if done:
            return ("<tr" + cls + ">"
                    f"<td class=\"fit\">{_esc(key_of(cfg['prefix'], t))}</td>"
                    f"<td>{_esc(t['title'])}</td>"
                    f"<td class=\"fit\">{_esc(t['owner'])}</td>"
                    f"<td>{_esc(t['source'])}</td></tr>")
        latest = ""
        if t["update"]:
            latest = (f"{_esc(t['update']['text'])} "
                      f"<small>\u2014 {_esc(t['update']['writer'])}, "
                      f"{_esc(t['update']['ts'][:10])}</small>")
        return ("<tr" + cls + ">"
                f"<td class=\"fit\">{_esc(key_of(cfg['prefix'], t))}</td>"
                f"<td>{_esc(t['title'])}</td>"
                f"<td class=\"fit\">{_esc(t['owner'])}</td>"
                f"<td class=\"fit\">{due}</td>"
                f"<td class=\"fit\">{_esc(_short_status(t))}</td>"
                f"<td>{latest}</td></tr>")

    active_rows = "\n".join(row(t) for t in active) or \
        "<tr><td colspan=\"6\"><em>no open tickets</em></td></tr>"
    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{_esc(cfg['name'])}</title>",
        "<style>",
        # Deliberately generic and structural only — no design language. Each
        # deployment restyles as it likes; the tool ships the working bones.
        _STRUCTURAL_CSS,
        "</style></head><body>",
        f"<h1>{_esc(cfg['name'])}</h1>",
        "<table><thead><tr><th class=\"fit\">Key</th><th>Title</th>"
        "<th class=\"fit\">Owner</th>"
        "<th class=\"fit\">Due</th><th class=\"fit\">Status</th>"
        "<th>Latest update</th></tr></thead><tbody>",
        active_rows,
        "</tbody></table>",
    ]
    if closed:
        parts += [
            "<h2>Done</h2>",
            "<table><thead><tr><th class=\"fit\">Key</th><th>Title</th>"
            "<th class=\"fit\">Owner</th>"
            "<th>Proof</th></tr></thead><tbody>",
            "\n".join(row(t, done=True) for t in closed),
            "</tbody></table>",
        ]
    parts.append("<p><small>Rebuilt from everyone's entries. "
                 "This page is generated — don't edit it by hand.</small></p>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def _excel_safe(value: str) -> str:
    """Excel treats a cell starting with = + - or @ as a formula and runs it.
    A leading apostrophe makes Excel show the text as-is instead."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def cmd_export(args) -> int:
    """Write board.csv, which opens cleanly in Excel."""
    root = resolve_root(args)
    cfg = load_config(root)
    tickets = board_state(root)
    rows = list(tickets.values())
    rows.sort(key=lambda t: (t["n"], t["nsfx"]))
    path = os.path.join(root, "board.csv")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["key", "title", "owner", "due", "status", "latest update",
                "as of", "proof", "id"])
    for t in rows:
        u = t["update"]
        w.writerow([
            key_of(cfg["prefix"], t),
            _excel_safe(t["title"]),
            _excel_safe(t["owner"]),
            t["due"],
            _short_status(t),
            _excel_safe(u["text"]) if u else "",
            u["ts"][:10] if u else "",
            _excel_safe(t["source"]),
            t["id"],
        ])
    # UTF-8 with a leading BOM so Excel shows accented characters correctly.
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
    print(f"Wrote {path}")
    return 0


def cmd_log(args) -> int:
    """Show one ticket's whole history, oldest first, in plain language."""
    root = resolve_root(args)
    cfg = load_config(root)
    events = sorted_events(root)
    tickets = fold(events)
    t = resolve_ticket(tickets, cfg["prefix"], args.ticket)
    print(f"History of {key_of(cfg['prefix'], t)} — {t['title']}")
    for ev in events:
        if ev.get("id") != t["id"]:
            continue
        print("  " + _describe_event(ev))
    return 0


def _describe_event(ev: dict) -> str:
    ts = ev.get("ts", "")
    who = ev.get("writer", "someone")
    verb = ev.get("verb")
    if verb == "open":
        return f"{ts}  {who} opened it: {ev.get('title','')}"
    if verb == "claim":
        return f"{ts}  {who} put it with {ev.get('owner', who)}"
    if verb == "block":
        return f"{ts}  {who} marked it blocked: {ev.get('reason','')}"
    if verb == "reopen":
        return f"{ts}  {who} reopened it"
    if verb == "close":
        note = f" — {ev['note']}" if ev.get("note") else ""
        return f"{ts}  {who} closed it (proof: {ev.get('source','')}){note}"
    if verb == "edit":
        changed = ", ".join(k for k in ("title", "owner", "due", "notes")
                            if k in ev)
        return f"{ts}  {who} changed {changed or 'the ticket'}"
    if verb == "update":
        return f"{ts}  {who} set the status: {ev.get('text','')}"
    if verb == "comment":
        return f"{ts}  {who} noted: {ev.get('text','')}"
    return f"{ts}  {who} did {verb}"


# --------------------------------------------------------------------------- #
# The interactive board (a small local web page)                              #
# --------------------------------------------------------------------------- #
# `tickets serve` runs a tiny web server on this machine only (127.0.0.1) so a
# non-technical teammate can take, edit, block, comment on, and close tickets
# from a browser — no install, no admin rights. Every change the page makes is
# sent back to this program and goes through the exact same code the typed
# commands use (act_open/claim/…): the writer is still derived here, never sent
# from the browser, one line is still appended to that writer's own file, and
# closing still demands a source. The browser never touches a file directly.


def _board_payload(root: str) -> dict:
    """The whole board as plain data the page can draw — active tickets first
    (soonest deadline on top), then the done ones."""
    cfg = load_config(root)
    prefix = cfg["prefix"]
    tickets = board_state(root)
    active = _sorted_for_board(tickets, show_all=False)
    closed = [t for t in tickets.values() if t["status"] == STATUS_CLOSED]
    closed.sort(key=lambda t: (t["n"], t["nsfx"]))

    def item(t: dict) -> dict:
        return {
            "id": t["id"],
            "key": key_of(prefix, t),
            "title": t["title"],
            "owner": t["owner"],
            "due": t["due"],
            "status": t["status"],
            "prefix": t["prefix"],
            "blocked_reason": t["blocked_reason"],
            "update": t["update"],
            "notes": t["notes"],
            "source": t["source"],
            "close_note": t["close_note"],
            "comments": t["comments"],
            "overdue": _is_overdue(t["due"], t["status"]),
        }

    return {
        "name": cfg["name"],
        "prefix": prefix,
        "active": [item(t) for t in active],
        "done": [item(t) for t in closed],
    }


# One writer process, one action at a time: overlapping page requests (a
# double-click, a browser retry) run on parallel server threads, and an
# unguarded read-then-append could mint duplicate ids or interleave a line.
_ACT_LOCK = threading.Lock()


def _dispatch_act(root: str, data: dict) -> dict:
    """Turn one message from the page ({"verb":…, "ticket":…, …}) into a real
    action through the shared core. Raises TicketError (→ a plain 400) on any
    bad request, exactly like the command line does."""
    with _ACT_LOCK:
        return _dispatch_act_locked(root, data)


def _dispatch_act_locked(root: str, data: dict) -> dict:
    verb = (data.get("verb") or "").strip()
    ticket = data.get("ticket")
    if verb in ("claim", "block", "reopen", "close", "edit", "comment",
                "update") \
            and not (isinstance(ticket, str) and ticket.strip()):
        raise TicketError("Which ticket? The request needs its number or key.")
    if verb == "open":
        return act_open(root, title=data.get("title", ""),
                        owner=data.get("owner"), due=data.get("due"),
                        notes=data.get("notes"), prefix=data.get("prefix"))
    if verb == "claim":
        return act_claim(root, ticket, owner=data.get("owner"))
    if verb == "block":
        return act_block(root, ticket, reason=data.get("reason", ""))
    if verb == "reopen":
        return act_reopen(root, ticket)
    if verb == "close":
        return act_close(root, ticket, source=data.get("source", ""),
                         note=data.get("note"))
    if verb == "edit":
        fields = {k: data[k] for k in ("title", "owner", "due", "notes",
                                       "prefix") if k in data}
        return act_edit(root, ticket, fields=fields)
    if verb == "update":
        return act_update(root, ticket, text=data.get("text", ""))
    if verb == "comment":
        return act_comment(root, ticket, text=data.get("text", ""))
    raise TicketError(f"I don't know the action \"{verb}\".")


class _BoardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # set right after construction:
    root: str
    token: str
    writer: str
    allowed_origins: set


def make_server(root: str) -> _BoardServer:
    """Build (but don't start) the local board server on a free port. A fresh
    random token is minted for this run; the page carries it and every action
    request must present it, so a stray web page open in the same browser can't
    quietly drive the board."""
    server = _BoardServer(("127.0.0.1", 0), _BoardHandler)
    server.root = root
    server.token = secrets.token_urlsafe(24)
    server.writer = writer_id()
    # Self-closing: the page heartbeats /api/ping; when the beats stop (every
    # tab closed, laptop asleep) the watchdog shuts the server down. The grace
    # head start covers a slow browser launch; both are overridable so tests
    # can prove the auto-exit with a short window.
    server.idle_limit = float(os.environ.get("TICKETS_SERVE_IDLE", "15"))
    server.last_seen = time.monotonic() + float(
        os.environ.get("TICKETS_SERVE_GRACE", "45"))
    port = server.server_address[1]
    # The only origins the browser could legitimately be on: this exact server.
    server.allowed_origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }
    return server


class _BoardHandler(http.server.BaseHTTPRequestHandler):
    """One request. GET / serves the page; the /api/* calls read and change the
    board. Everything under /api requires this run's token and must come from
    the page's own origin — so no other website can read or write the board."""

    server_version = "tickets-board"

    def log_message(self, *args) -> None:  # keep the console quiet
        pass

    # -- writing responses -------------------------------------------------- #
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

    # -- the two guards on every /api call ---------------------------------- #
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # same-tab navigations don't send Origin; token still gates
        return origin in self.server.allowed_origins

    def _token_ok(self) -> bool:
        tok = self.headers.get("X-Tickets-Token")
        if not tok:
            query = urllib.parse.urlparse(self.path).query
            tok = (urllib.parse.parse_qs(query).get("token") or [None])[0]
        return bool(tok) and secrets.compare_digest(str(tok), self.server.token)

    def _guard(self) -> bool:
        if not self._origin_ok():
            self._json({"error": "This request came from another website, so "
                                 "it was refused."}, 403)
            return False
        if not self._token_ok():
            self._json({"error": "This request was missing the board's access "
                                 "code. Open the board from its own window."},
                       403)
            return False
        # Any authenticated request proves a live tab — feed the watchdog.
        self.server.last_seen = time.monotonic()
        return True

    # -- routes ------------------------------------------------------------- #
    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", ""):
            self._html(_render_app(load_config(self.server.root),
                                   self.server.token, self.server.writer))
            return
        if path == "/api/board":
            if not self._guard():
                return
            self._json(_board_payload(self.server.root))
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
            token = (query.get("ticket") or [""])[0]
            try:
                events = sorted_events(self.server.root)
                folded = fold(events)
                cfg = load_config(self.server.root)
                t = resolve_ticket(folded, cfg["prefix"], token)
            except TicketError as e:
                self._json({"error": str(e)}, 400)
                return
            lines = [_describe_event(ev) for ev in events
                     if ev.get("id") == t["id"]]
            self._json({"key": key_of(cfg["prefix"], t),
                        "title": t["title"], "lines": lines})
            return
        self._json({"error": "There is nothing at that address."}, 404)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/goodbye":
            # The closing tab's parting beacon: put the watchdog on a short
            # fuse instead of exiting outright — if another tab is still open,
            # its next heartbeat (every ~2s) rescues the server.
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
        except TicketError as e:
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:  # never leak a stack trace to the browser
            self._json({"error": f"Something went wrong: {e}"}, 500)
            return
        # Hand back the freshly rebuilt board so the page redraws immediately.
        self._json({"ok": True, "board": _board_payload(self.server.root)})


def start_idle_watchdog(server: _BoardServer) -> threading.Thread:
    """Shut the server down once the page's heartbeats stop — closing the last
    browser tab is the off switch; there is no server for anyone to manage."""
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
    """Run the interactive board in a browser (this machine only)."""
    import webbrowser
    root = resolve_root(args)
    cfg = load_config(root)
    server = make_server(root)
    start_idle_watchdog(server)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"The board \"{cfg['name']}\" is open at:")
    print(f"  {url}")
    print("Anyone at this computer can take, edit, and close tickets there.")
    print("It closes itself a few seconds after its last browser tab closes.")
    if not getattr(args, "no_open", False):
        try:
            if hasattr(os, "startfile"):
                os.startfile(url)  # Windows: default browser
            else:
                webbrowser.open(url)
        except Exception:
            pass  # a headless machine still serves; the URL is printed above
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _render_app(cfg: dict, token: str, writer: str) -> str:
    """The interactive page: one self-contained HTML file, all styling and
    behaviour inline, nothing loaded from the internet. It reads the board from
    /api/board and sends every change to /api/act, redrawing after each one."""
    boot = json.dumps({
        "token": token,
        "writer": writer,
        "name": cfg["name"],
        "prefix": cfg["prefix"],
    })
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(cfg['name'])}</title>\n"
        "<style>\n" + _APP_CSS + "</style></head><body>\n"
        "<div id=\"app\"><p class=\"muted\">Loading the board…</p></div>\n"
        "<script>window.__BOARD__ = " + boot + ";</script>\n"
        "<script>\n" + _APP_JS + "</script>\n"
        "</body></html>\n"
    )


# Same rule as the static page: structural CSS only — layout that makes the
# controls usable (form widths, spacing, the shrink-to-fit columns), zero
# design language. Emphasis is plain <strong>/<em>/opacity, never a palette.
_APP_CSS = (
    _STRUCTURAL_CSS +
    ".muted{opacity:.6;} .err{font-weight:bold;}"
    ".actions button,.newbtn,.panel button{font:inherit;cursor:pointer;"
    "margin:0 .25rem .25rem 0;}"
    ".panel{margin:.4rem 0 .2rem;padding:.5rem .6rem;border:1px solid #999;}"
    ".panel label{display:block;margin:.3rem 0 .15rem;}"
    ".panel input,.panel textarea{width:100%;box-sizing:border-box;font:inherit;}"
    ".panel textarea{min-height:3rem;}"
    ".panel .cancel{background:none;border:none;text-decoration:underline;}"
    ".new{margin:1rem 0;padding:.5rem .6rem;border:1px solid #999;}"
    ".new .row{display:flex;gap:.6rem;flex-wrap:wrap;}"
    ".new .row>div{flex:1;min-width:9rem;}"
    ".new input{width:100%;box-sizing:border-box;font:inherit;margin-top:.15rem;}"
    ".newbtn{margin-top:.7rem;}"
    ".reason,.cmts,.proof{opacity:.75;font-size:.9em;}"
    ".cmts div{margin-top:.15rem;}"
    ".flash{margin:.6rem 0;font-weight:bold;}"
    ".latest{margin-top:.15rem;}"
    ".hist div{margin-top:.15rem;opacity:.8;font-size:.9em;}"
)


# Plain hand-rolled JavaScript (no framework, no external files). Reads the
# board, draws it, and posts every change back with the run's access token.
_APP_JS = r"""
(function(){
  var BOOT = window.__BOARD__;
  var app = document.getElementById('app');
  var board = null;
  var panel = null;   // {id: <ticket id or '_new'>, kind: <action>}
  var flash = null;   // {ok: bool, text: str}

  function esc(s){
    return String(s==null?'':s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }
  function h(strs){ return strs.join(''); }

  function api(path, opts){
    opts = opts || {};
    opts.headers = Object.assign({'X-Tickets-Token': BOOT.token},
                                 opts.headers || {});
    return fetch(path, opts).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(body){
        if(!r.ok){ throw new Error(body.error || ('Error ' + r.status)); }
        return body;
      });
    });
  }

  function load(){
    api('/api/board').then(function(b){ board = b; render(); })
      .catch(function(e){
        app.innerHTML = '<p class="err">' + esc(e.message) + '</p>';
      });
  }

  // Keep the board alive while any tab shows it; say goodbye when this tab
  // goes. When the beats can no longer reach it (board closed itself, laptop
  // slept), tell the reader how to get back in plain words.
  var gone = false;
  function boardGone(){
    if(gone){ return; }
    gone = true;
    app.innerHTML = '<p class="err">The board closed. Double-click ' +
      '"Open board.cmd" in the board folder to open it again.</p>';
  }
  setInterval(function(){
    if(gone){ return; }
    api('/api/ping').catch(boardGone);
  }, 2000);
  window.addEventListener('pagehide', function(){
    if(navigator.sendBeacon){
      navigator.sendBeacon('/api/goodbye?token=' +
                           encodeURIComponent(BOOT.token));
    }
  });

  function act(payload){
    api('/api/act', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    }).then(function(res){
      board = res.board;
      panel = null;
      flash = {ok:true, text: done_text(payload)};
      render();
    }).catch(function(e){
      flash = {ok:false, text: e.message};
      render();
    });
  }

  function done_text(p){
    var m = {open:'New ticket added.', claim:'Ownership updated.',
             block:'Marked blocked.', reopen:'Back to active.',
             close:'Ticket closed.', edit:'Ticket updated.',
             comment:'Note added.', update:'Status posted.'};
    return m[p.verb] || 'Done.';
  }

  function openPanel(id, kind){ panel = {id:id, kind:kind}; flash = null; render(); }
  function closePanel(){ panel = null; render(); }

  // ---- drawing ---------------------------------------------------------- //
  function render(){
    if(!board){ return; }
    var out = [];
    out.push('<h1>' + esc(board.name) + '</h1>');
    out.push('<p class="muted">You are signed in as <b>' + esc(BOOT.writer) +
             '</b>. Every change is saved under your name.</p>');
    if(flash){
      out.push('<div class="flash ' + (flash.ok?'ok':'bad') + '">' +
               esc(flash.text) + '</div>');
    }
    out.push(newTicketBlock());

    out.push('<h2>To do</h2>');
    if(board.active.length === 0){
      out.push('<p class="muted">Nothing open right now.</p>');
    } else {
      out.push('<table><thead><tr><th>Key</th><th>Title</th><th>Owner</th>' +
               '<th>Due</th><th>Status</th></tr></thead><tbody>');
      board.active.forEach(function(t){ out.push(rowFor(t)); });
      out.push('</tbody></table>');
    }

    if(board.done.length){
      out.push('<h2>Done</h2>');
      out.push('<table><thead><tr><th>Key</th><th>Title</th><th>Owner</th>' +
               '<th>Proof it\'s done</th><th class="fit"></th></tr></thead><tbody>');
      board.done.forEach(function(t){ out.push(doneRowFor(t)); });
      out.push('</tbody></table>');
    }
    out.push('<p><small>The board rebuilds from everyone\'s entries each ' +
             'time. It closes itself when the last tab closes.</small></p>');
    app.innerHTML = out.join('');
    wire();
  }

  function statusWord(t){
    if(t.status === 'open') return 'nobody yet';
    if(t.status === 'claimed') return 'in progress';
    if(t.status === 'blocked') return 'blocked';
    return 'done';
  }

  function rowFor(t){
    var due = esc(t.due) || '<span class="muted">no deadline</span>';
    if(t.overdue){ due = '<strong>' + esc(t.due) + ' (past due!)</strong>'; }
    var extra = '';
    if(t.update){
      extra += '<div class="latest"><em>' + esc(t.update.text) + '</em> ' +
               '<span class="muted">\u2014 ' + esc(t.update.writer) + ', ' +
               esc((t.update.ts||'').slice(0,10)) + '</span></div>';
    }
    if(t.status === 'blocked' && t.blocked_reason){
      extra += '<div class="reason">Stuck: ' + esc(t.blocked_reason) + '</div>';
    }
    if(t.notes){ extra += '<div class="cmts">Notes: ' + esc(t.notes) + '</div>'; }
    if(t.comments && t.comments.length){
      extra += '<div class="cmts">';
      t.comments.forEach(function(c){
        extra += '<div>' + esc(c.writer) + ': ' + esc(c.text) + '</div>';
      });
      extra += '</div>';
    }
    var cls = t.status === 'blocked' ? ' class="blocked"' : '';
    var row = h(['<tr' + cls + '>',
      '<td class="fit">' + esc(t.key) + '</td>',
      '<td>' + esc(t.title) + extra + actionBar(t) + panelFor(t) + '</td>',
      '<td class="fit">' + (esc(t.owner) || '<span class="muted">—</span>') + '</td>',
      '<td class="fit">' + due + '</td>',
      '<td class="fit">' + statusWord(t) + '</td>',
      '</tr>']);
    return row;
  }

  function doneRowFor(t){
    var reopenBtn = '<div class="actions"><button data-act="reopen" ' +
                    'data-id="' + esc(t.id) + '">Reopen</button>' +
                    btn(t, 'history', 'History') + '</div>';
    var extra = '';
    if(t.close_note){
      extra = '<div class="cmts">' + esc(t.close_note) + '</div>';
    }
    return h(['<tr class="closed">',
      '<td class="fit">' + esc(t.key) + '</td>',
      '<td>' + esc(t.title) + extra + panelFor(t) + '</td>',
      '<td class="fit">' + (esc(t.owner) || '—') + '</td>',
      '<td class="proof">' + esc(t.source) + '</td>',
      '<td class="fit">' + reopenBtn + '</td>',
      '</tr>']);
  }

  function actionBar(t){
    var b = ['<div class="actions">'];
    b.push(btn(t, 'take', 'Take it'));
    b.push(btn(t, 'hand', 'Hand to…'));
    b.push(btn(t, 'edit', 'Edit'));
    if(t.status === 'blocked'){ b.push(btn(t, 'reopen', 'Unblock')); }
    else { b.push(btn(t, 'block', 'Block')); }
    b.push(btn(t, 'update', 'Post update'));
    b.push(btn(t, 'comment', 'Comment'));
    b.push(btn(t, 'close', 'Close'));
    b.push(btn(t, 'history', 'History'));
    b.push('</div>');
    return b.join('');
  }

  function btn(t, kind, label){
    return '<button data-open="' + kind + '" data-id="' + esc(t.id) + '">' +
           esc(label) + '</button>';
  }

  function panelFor(t){
    if(!panel || panel.id !== t.id){ return ''; }
    var k = panel.kind;
    if(k === 'hand'){
      return formPanel('Give this ticket to someone', [
        field('owner', 'Their name', 'text', '')
      ], 'Hand it over', t.id, 'claim');
    }
    if(k === 'edit'){
      return formPanel('Edit this ticket', [
        field('title', 'Title', 'text', t.title),
        field('owner', 'Owner (blank = nobody yet)', 'text', t.owner),
        field('due', 'Deadline (a date like 2026-09-30, or leave blank)', 'text', t.due),
        field('prefix', 'Code before the number (blank = the board\'s own, ' +
              BOOT.prefix + ')', 'text', t.prefix),
        area('notes', 'Notes', t.notes)
      ], 'Save changes', t.id, 'edit');
    }
    if(k === 'update'){
      return formPanel('Where does this stand right now?', [
        area('text', 'Latest status (one or two lines)', '')
      ], 'Post update', t.id, 'update');
    }
    if(k === 'history'){
      var rows = (panel.lines || []).map(function(l){
        return '<div>' + esc(l) + '</div>';
      }).join('');
      return h(['<div class="panel hist" data-panel="' + esc(t.id) + '">',
        '<div class="muted">Everything that has happened to this ticket:</div>',
        rows || '<div>(nothing yet)</div>',
        '<button class="cancel" data-cancel="1">close</button>',
        '</div>']);
    }
    if(k === 'block'){
      return formPanel('What is in the way?', [
        field('reason', 'Reason (required)', 'text', '')
      ], 'Mark blocked', t.id, 'block');
    }
    if(k === 'comment'){
      return formPanel('Add a note to this ticket', [
        area('text', 'Your note', '')
      ], 'Add note', t.id, 'comment');
    }
    if(k === 'close'){
      return closePanel_(t);
    }
    return '';
  }

  function field(name, label, type, val){
    return '<label>' + esc(label) + '</label>' +
           '<input data-f="' + name + '" type="' + type + '" value="' +
           esc(val) + '">';
  }
  function area(name, label, val){
    return '<label>' + esc(label) + '</label>' +
           '<textarea data-f="' + name + '">' + esc(val) + '</textarea>';
  }

  function formPanel(title, fields, goLabel, id, verb){
    return h(['<div class="panel" data-panel="' + esc(id) + '">',
      '<div class="muted">' + esc(title) + '</div>',
      fields.join(''),
      '<button class="go" data-go="' + verb + '" data-id="' + esc(id) + '">' +
        esc(goLabel) + '</button>',
      '<button class="cancel" data-cancel="1">cancel</button>',
      '</div>']);
  }

  function closePanel_(t){
    return h(['<div class="panel" data-panel="' + esc(t.id) + '">',
      '<div class="muted">Close this ticket. Say where the proof it\'s done ' +
        'can be found — a document name, a link, or what happened.</div>',
      '<label>Where\'s the proof?</label>',
      '<textarea data-f="source"></textarea>',
      '<label>Closing note (optional)</label>',
      '<input data-f="note" type="text" value="">',
      '<button class="go" data-go="close" data-id="' + esc(t.id) + '">' +
        'Close with this evidence</button>',
      '<button class="vouch" data-vouch="1" data-id="' + esc(t.id) + '">' +
        'I\'m vouching for this myself</button>',
      '<button class="cancel" data-cancel="1">cancel</button>',
      '</div>']);
  }

  function newTicketBlock(){
    var open = panel && panel.id === '_new';
    if(!open){
      return '<div class="new"><button class="newbtn" data-open="new" ' +
             'data-id="_new">+ New ticket</button></div>';
    }
    return h(['<div class="new">',
      '<div class="row">',
        '<div><label>Title (required)</label>' +
          '<input data-f="title" type="text"></div>',
        '<div><label>Owner (optional)</label>' +
          '<input data-f="owner" type="text"></div>',
        '<div><label>Deadline (optional)</label>' +
          '<input data-f="due" type="text" placeholder="2026-09-30"></div>',
        '<div><label>Code before the number (optional)</label>' +
          '<input data-f="prefix" type="text" placeholder="' +
          esc(BOOT.prefix) + '"></div>',
      '</div>',
      '<div class="row"><div style="flex:1 1 100%"><label>Notes (optional)' +
        '</label><input data-f="notes" type="text"></div></div>',
      '<button class="newbtn" data-go="open" data-id="_new">Add ticket</button> ',
      '<button class="cancel" data-cancel="1">cancel</button>',
      '</div>']);
  }

  // ---- wiring ----------------------------------------------------------- //
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
        else if(kind === 'take'){ act({verb:'claim', ticket:id}); }
        else if(kind === 'reopen'){ act({verb:'reopen', ticket:id}); }
        else if(kind === 'history'){
          api('/api/log?ticket=' + encodeURIComponent(id))
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
    app.querySelectorAll('[data-act="reopen"]').forEach(function(el){
      el.addEventListener('click', function(){
        act({verb:'reopen', ticket: el.getAttribute('data-id')});
      });
    });
    app.querySelectorAll('[data-go]').forEach(function(el){
      el.addEventListener('click', function(){
        var verb = el.getAttribute('data-go');
        var id = el.getAttribute('data-id');
        var f = readFields(id);
        if(verb === 'open'){
          act({verb:'open', title:f.title, owner:f.owner, due:f.due,
               notes:f.notes, prefix:f.prefix});
        } else if(verb === 'claim'){
          act({verb:'claim', ticket:id, owner:f.owner});
        } else if(verb === 'edit'){
          act({verb:'edit', ticket:id, title:f.title, owner:f.owner,
               due:f.due, notes:f.notes, prefix:f.prefix});
        } else if(verb === 'update'){
          act({verb:'update', ticket:id, text:f.text});
        } else if(verb === 'block'){
          act({verb:'block', ticket:id, reason:f.reason});
        } else if(verb === 'comment'){
          act({verb:'comment', ticket:id, text:f.text});
        } else if(verb === 'close'){
          act({verb:'close', ticket:id, source:f.source, note:f.note});
        }
      });
    });
    // The vouch button: close with source person:<me>.
    app.querySelectorAll('[data-vouch]').forEach(function(el){
      el.addEventListener('click', function(){
        var id = el.getAttribute('data-id');
        var f = readFields(id);
        act({verb:'close', ticket:id, source:'person:' + BOOT.writer, note:f.note});
      });
    });
  }

  load();
})();
"""


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #


class TicketError(SystemExit):
    """A plain, person-facing problem with a request (bad ticket, missing
    source, empty reason). Subclasses SystemExit so the command line still
    exits non-zero exactly as before; the web server catches it and turns it
    into a 400 with the same plain message instead of crashing the request."""


def _die(msg: str) -> None:
    raise TicketError(msg)


# --------------------------------------------------------------------------- #
# Command line                                                                 #
# --------------------------------------------------------------------------- #


def _add_root(p) -> None:
    p.add_argument("--root", help="the board's folder (or set TICKETS_ROOT)")


def _add_ticket(p) -> None:
    p.add_argument("--ticket", "-t", required=True,
                   help="which ticket: a number (3), a key (PREFIX-3), or its id")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tickets", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a new board folder")
    p.add_argument("--root", required=True, help="folder to create the board in")
    p.add_argument("--prefix", required=True,
                   help="short code for this specific work, e.g. ACME-TP "
                        "(shows in every ticket key)")
    p.add_argument("--name", required=True,
                   help="full name shown at the top of the board")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("open", help="add a new ticket")
    _add_root(p)
    p.add_argument("title", help="what the ticket is about")
    p.add_argument("--owner", help="who will do it (defaults to nobody yet)")
    p.add_argument("--due", help="an outside deadline — a client, a filing, "
                                 "someone waiting; not a plan for when to work on it")
    p.add_argument("--notes", help="anything else worth recording")
    p.add_argument("--prefix",
                   help="file this ticket under a different short code than "
                        "the board's — the project or engagement it really "
                        "belongs to (its number stays board-wide)")
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser("claim", help="take a ticket (or assign it with --owner)")
    _add_root(p)
    _add_ticket(p)
    p.add_argument("--owner", help="assign to someone else instead of yourself")
    p.set_defaults(fn=cmd_claim)

    p = sub.add_parser("block", help="mark a ticket stuck, with the reason")
    _add_root(p)
    _add_ticket(p)
    p.add_argument("reason", help="what is in the way")
    p.set_defaults(fn=cmd_block)

    p = sub.add_parser("reopen", help="move a done or blocked ticket back to active")
    _add_root(p)
    _add_ticket(p)
    p.set_defaults(fn=cmd_reopen)

    p = sub.add_parser("close", help="finish a ticket (a source is required)")
    _add_root(p)
    _add_ticket(p)
    p.add_argument("--source",
                   help="where the proof it's done lives: a document name, a "
                        "link, what happened, or person:<you> if you are vouching")
    p.add_argument("--note", help="an optional closing note")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("edit",
                       help="change a ticket's title, owner, due, notes, "
                            "or prefix")
    _add_root(p)
    _add_ticket(p)
    p.add_argument("--title")
    p.add_argument("--owner")
    p.add_argument("--due")
    p.add_argument("--notes")
    p.add_argument("--prefix",
                   help="move the ticket under a different short code; its "
                        "number stays the same. An empty value (--prefix \"\") "
                        "goes back to the board's own code")
    p.set_defaults(fn=cmd_edit)

    p = sub.add_parser("update",
                       help="post the latest one-line status of a ticket")
    _add_root(p)
    _add_ticket(p)
    p.add_argument("text", help="where the ticket stands right now")
    p.set_defaults(fn=cmd_update)

    p = sub.add_parser("comment", help="add a note to a ticket's history")
    _add_root(p)
    _add_ticket(p)
    p.add_argument("text", help="your note")
    p.set_defaults(fn=cmd_comment)

    p = sub.add_parser("show", help="show one ticket in full")
    _add_root(p)
    _add_ticket(p)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("board", help="print the board as a table")
    _add_root(p)
    p.add_argument("--all", action="store_true", help="include done tickets")
    p.set_defaults(fn=cmd_board)

    p = sub.add_parser("view", help="open the up-to-date board in a browser")
    _add_root(p)
    p.add_argument("--no-open", dest="no_open", action="store_true",
                   help="write the page but don't open a browser")
    p.set_defaults(fn=cmd_view)

    p = sub.add_parser("serve", help="open the interactive board in a browser "
                                     "(take, edit, and close tickets)")
    _add_root(p)
    p.add_argument("--no-open", dest="no_open", action="store_true",
                   help="start the board but don't open a browser")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("page", help="rebuild board.md and board.html")
    _add_root(p)
    p.set_defaults(fn=cmd_page)

    p = sub.add_parser("export", help="write board.csv for Excel")
    _add_root(p)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("log", help="show one ticket's whole history")
    _add_root(p)
    _add_ticket(p)
    p.set_defaults(fn=cmd_log)

    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except TicketError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())
