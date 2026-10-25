#!/usr/bin/env python3
"""SessionStart hook: Tod's profile, the tickets due today or overdue, and the
phone inbox. Nothing else — a project's own CLAUDE.md and docs/ carry the rest.

Delegated workers (env CCDESK_ROLE set) get nothing: their context is the brief.
Prints nothing and never raises when a source is missing."""
import datetime
import os
import sys

KB = os.path.join(os.path.expanduser("~"), ".claude", "global-kb")
USER_MD = os.path.join(KB, "user.md")
INBOX_MD = os.path.join(KB, "inbox.md")
TICKETS_ROOT = os.path.join(KB, "tickets")
TICKETS_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Claude", "agent-brain")
MAX_DUE_ROWS = 8


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _user_part():
    text = _read(USER_MD)
    if not text:
        return None
    if text.startswith("---"):
        end = text.find("\n---", 3)
        text = text[end + 4:] if end != -1 else text
    body = "\n".join(ln for ln in text.strip().splitlines()
                     if not ln.lstrip().startswith("**Budget:")).strip()
    return f"[USER]\n\n{body}" if body else None


def _board_part():
    try:
        sys.path.insert(0, TICKETS_DIR)
        import tickets
        prefix = tickets.load_config(TICKETS_ROOT).get("prefix", "")
        state = tickets.board_state(TICKETS_ROOT)
    except Exception:
        return None
    active = [t for t in state.values() if t["status"] != "closed"]
    if not active:
        return None
    today = datetime.date.today().isoformat()
    due = sorted((t for t in active if t["due"] and t["due"] <= today),
                 key=lambda t: (t["due"], t["n"], t["nsfx"]))
    rows = ["[TICKETS DUE]"]
    for t in due[:MAX_DUE_ROWS]:
        rows.append(f"- {tickets.key_of(prefix, t)} (due {t['due']}, "
                    f"{t['owner'] or 'nobody yet'}) {t['title']}")
    if len(due) > MAX_DUE_ROWS:
        rows.append(f"   (+{len(due) - MAX_DUE_ROWS} more due)")
    unowned = sum(1 for t in active if not t["owner"])
    rows.append(f"{len(active)} open · {unowned} unowned · full board: python "
                f"{os.path.join(TICKETS_DIR, 'tickets.py')} board --root {TICKETS_ROOT}")
    return "\n".join(rows)


def _inbox_part():
    lines = [ln for ln in (_read(INBOX_MD) or "").splitlines() if ln.startswith("- ")]
    if not lines:
        return None
    return ("[INBOX] Phone replies awaiting processing (inbox.md — act on each, "
            "then delete the line):\n" + "\n".join(lines))


def main():
    if os.environ.get("CCDESK_ROLE"):
        return
    parts = [p for p in (_user_part(), _board_part(), _inbox_part()) if p]
    if parts:
        sys.stdout.buffer.write(("\n\n".join(parts) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
