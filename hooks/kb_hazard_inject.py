#!/usr/bin/env python3
"""PostToolUse hook (Write|Edit|NotebookEdit): file-matched, just-in-time hazard
injection.

Read-side counterpart of kb_stop_guard: capture is hook-enforced, so retrieval
must be too. When a session edits a file, surface the LIVE hazards (gotchas /
dead-ends, verbatim) whose text or subsystem label whole-word-matches that file's
path — the ones that actually bite THIS edit — rather than dumping the whole
scope index. The wake injection deliberately never carries hazards, and
"remember to run `brain hazards`" is exactly the discipline gap this closes.

Bounded and non-noisy:
  - Once per HAZARD per session: a per-(session, scope) sentinel records the
    entry numbers already injected; each subsequent edit passes them to
    `brain hazards --exclude` so no hazard is shown twice.
  - Silent when the edited path matches nothing — no blank injection, no noise.
  - First-edit safety net: if the very first edit in a scope matches no hazard
    (a generically-named file), the scope's full live hazard index is injected
    once so a real trap is never missed just because the filename gave no signal.
    Its design invariants inject once on that first edit too.

Sentinels (`haz-inject-<session>-<scope>`) hold the injected entry numbers and
are GC'd after 7 days (same scheme as kb_stop_guard). Hazards are keyed to the
files a session actually touches, which no brief author can predict, so the gate
fires for every session. BRAIN_NO_INJECT=1 suppresses everything.

Always exits 0; on any failure it injects nothing rather than blocking edits.
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_card_inject import (  # noqa: E402
    _slug_for, scope_for, scope_has_stream, BRAIN_CLI, CACHE_DIR as _CACHE_DIR,
)

# BRAIN_HAZ_CACHE lets a test harness isolate the sentinel cache from the
# default (the data root's _state/cache).
CACHE_DIR = os.environ.get("BRAIN_HAZ_CACHE") or _CACHE_DIR
SENTINEL_MAX_AGE_S = 7 * 24 * 3600
MAX_INJECT_CHARS = 6000


def _gc_sentinels() -> None:
    try:
        now = time.time()
        for name in os.listdir(CACHE_DIR):
            if name.startswith("haz-inject-"):
                p = os.path.join(CACHE_DIR, name)
                try:
                    if now - os.path.getmtime(p) > SENTINEL_MAX_AGE_S:
                        os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


def _read_seen(sentinel: str) -> set[int]:
    try:
        with open(sentinel, "r", encoding="utf-8") as f:
            return {int(x) for x in re.findall(r"\d+", f.read())}
    except OSError:
        return set()


def _write_seen(sentinel: str, ids: set[int]) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(",".join(str(i) for i in sorted(ids)))
    except OSError:
        pass


def _brain(*args: str) -> str:
    try:
        r = subprocess.run([sys.executable, BRAIN_CLI, *args],
                           capture_output=True, timeout=15)
        return r.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def main() -> None:
    # The edit gate fires for every session — hazards are keyed to the files a
    # session actually touches, which no brief author can predict, so it is the
    # one gate that cannot be pre-satisfied by a curated context.
    if os.environ.get("BRAIN_NO_INJECT"):
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    inp = data.get("tool_input") or {}
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if not path or "/.claude/" in path.replace("\\", "/"):
        return  # KB/memory/settings edits are not project code
    slug = _slug_for(os.path.dirname(path)) or _slug_for(data.get("cwd") or os.getcwd())
    scope = scope_for(slug)
    if not scope_has_stream(scope):
        return
    session_id = data.get("session_id", "unknown")
    sentinel = os.path.join(CACHE_DIR, f"haz-inject-{session_id}-{scope}")
    first_edit = not os.path.exists(sentinel)
    seen = _read_seen(sentinel)
    _gc_sentinels()

    # File-matched hazards for THIS path, minus anything already injected.
    exclude = ",".join(str(i) for i in sorted(seen))
    matched_out = _brain("hazards", "--scope", scope, "--match-path", path,
                       *(["--exclude", exclude] if exclude else []))
    matched_ids: set[int] = set()
    body = ""
    if matched_out:
        m = re.search(r"^MATCHED-IDS:\s*(.*)$", matched_out, re.MULTILINE)
        if m:
            matched_ids = {int(x) for x in re.findall(r"\d+", m.group(1))}
        # Strip the machine line before it reaches the model.
        body = re.sub(r"^MATCHED-IDS:.*$", "", matched_out, flags=re.MULTILINE).strip()

    parts: list[str] = []
    if body:
        parts.append(body)
    elif first_edit:
        # Weak-signal filename on the first edit — surface the full scope index
        # once so a real trap is never missed. Budget-aware: brain keeps entries
        # whole and degrades overflow groups to a one-line digest.
        full = _brain("hazards", "--scope", scope,
                    "--budget", str(MAX_INJECT_CHARS - 1200), "--prefer", path)
        if full and "(no live hazard" not in full and "(no hazard entries)" not in full:
            parts.append(full)
            matched_ids |= {int(x) for x in re.findall(r"(?m)#(\d+)\b", full)}

    if first_edit:
        design = _brain("design", "--scope", scope, "--budget", "1000")
        if design and "(no invariant" not in design and "(no live invariant" not in design:
            parts.append(design)

    # Record progress even when nothing was injectable this edit, so the
    # first-edit full-index net does not re-fire on every subsequent edit.
    _write_seen(sentinel, seen | matched_ids)

    if not parts:
        return  # silent: nothing new keys to this edit

    ctx = (
        f"[BRAIN HAZARD GATE] Edit in scope '{scope}' touched files whose subsystem has "
        "live standing entries (verbatim, never summarized; absent from the wake state "
        "by design). This gate is two-way: fixed a hazard? retire it "
        f"(`brain resolve --project {scope} N \"how\"`); does your change FALSIFY an "
        "invariant below? supersede it (`brain note --supersedes N`) — a stale "
        "invariant is worse than none.\n\n" + "\n\n".join(parts)
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": ctx,
        }
    }))


if __name__ == "__main__":
    main()
