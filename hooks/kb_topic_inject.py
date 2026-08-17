#!/usr/bin/env python3
"""UserPromptSubmit hook: topic-matched recall of live rulings.

The conversational counterpart of kb_hazard_inject. Code memory works because
retrieval is PUSHED at the moment of risk — the edit gate fires on a Write and
puts the subsystem's hazards in front of the session whether or not it thought
to ask. Conversation had no such moment: an advisory scope (career,
family-estate, wealth-plan) received the wake at minute zero and nothing for
the next two hours, so every later retrieval depended on the agent remembering
to run `gkb decisions` — the same discipline gap the hazard gate closes on the
code side.

When a prompt raises a topic that live decisions/questions/invariants already
speak to, those rulings are injected verbatim. Rulings, never summaries: the
whole point is that the session sees what was actually decided rather than a
lossy root's paraphrase of it.

Bounded and non-noisy, the same way the hazard gate is:
  - Once per ENTRY per session. A sentinel records the scope#id values already
    surfaced and passes them to `gkb recall --exclude`, so a topic raised five
    times in one conversation injects once.
  - Silent unless the match is strong. Matching is lexical and curated (never
    frequency-weighted — okf-tools #1), and deliberately biased toward
    precision: this fires on EVERY prompt, so a false positive costs context
    on every turn while a false negative merely returns to the silence that
    already existed.
  - Hard char budget with an explicit overflow pointer (invariant #31).

Sentinels (`topic-inject-<session>`) are GC'd after 7 days, same scheme as
kb_hazard_inject and kb_stop_guard. Delegated workers (CCDESK_ROLE) are skipped
— a delegate's context is its brief, and unlike file-keyed hazards, a topic
match on the dispatcher's prose is exactly the ambient identity that role gate
exists to keep out. GKB_NO_INJECT=1 (eval control arm) suppresses everything.

Always exits 0; on any failure it injects nothing rather than blocking the turn.
"""
import json
import os
import re
import subprocess
import sys
import time

GKB_CLI = os.environ.get("GKB_CLI") or os.path.expanduser(r"~/.claude/okf-tools/gkb.py")
CACHE_DIR = os.environ.get("GKB_HAZ_CACHE") or os.path.expanduser(r"~/.claude/.cache")
SENTINEL_MAX_AGE_S = 7 * 24 * 3600
MAX_INJECT_CHARS = 2200
MIN_PROMPT_CHARS = 25   # "yes", "go on", "fix it" carry no topic to match on


def _gc_sentinels() -> None:
    try:
        now = time.time()
        for name in os.listdir(CACHE_DIR):
            if name.startswith("topic-inject-"):
                p = os.path.join(CACHE_DIR, name)
                try:
                    if now - os.path.getmtime(p) > SENTINEL_MAX_AGE_S:
                        os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


def _read_seen(sentinel: str) -> set[str]:
    try:
        with open(sentinel, "r", encoding="utf-8") as f:
            return {t for t in re.findall(r"[a-z0-9_-]+#\d+", f.read().lower())}
    except OSError:
        return set()


def _write_seen(sentinel: str, ids: set[str]) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(",".join(sorted(ids)))
    except OSError:
        pass


def main() -> None:
    if os.environ.get("GKB_NO_INJECT") or os.environ.get("CCDESK_ROLE"):
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    prompt = (data.get("prompt") or "").strip()
    if len(prompt) < MIN_PROMPT_CHARS:
        return
    # A pasted stack trace or file dump is not a topic — matching over it
    # produces scattershot hits on whatever words happen to appear.
    if prompt.count("\n") > 25:
        return

    session_id = data.get("session_id", "unknown")
    sentinel = os.path.join(CACHE_DIR, f"topic-inject-{session_id}")
    seen = _read_seen(sentinel)
    _gc_sentinels()

    argv = [sys.executable, GKB_CLI, "recall", "--budget", str(MAX_INJECT_CHARS)]
    if seen:
        argv += ["--exclude", ",".join(sorted(seen))]
    argv += ["--", prompt]  # a prompt starting with '-' must not parse as a flag
    try:
        r = subprocess.run(argv, capture_output=True, timeout=15)
        out = r.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return
    if not out:
        return

    m = re.search(r"^MATCHED-IDS:\s*(.*)$", out, re.MULTILINE)
    ids = {t for t in re.findall(r"[a-z0-9_-]+#\d+", (m.group(1) if m else "").lower())}
    body = re.sub(r"^MATCHED-IDS:.*$", "", out, flags=re.MULTILINE).strip()
    if not body or not ids:
        return
    _write_seen(sentinel, seen | ids)

    ctx = (
        body + "\n\nThese are live rulings from the knowledge bank, surfaced because "
        "the topic came up — not instructions for this turn. If one is now wrong, "
        "supersede it (`gkb note --supersedes N`); a stale ruling served verbatim is "
        "worse than none."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }
    }))


if __name__ == "__main__":
    main()
