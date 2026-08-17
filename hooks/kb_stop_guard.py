#!/usr/bin/env python3
"""
Stop hook: enforce the Agent Brain capture duty.

Enforcement is narrow and deterministic — it fires only when the session
actually changed project code, and on those sessions it requires a real brain
write (not a throwaway token):

  - If the session made NO project-code edit (read-only, Q&A, or edits confined
    to the data root / the Claude Code config dir), the stop is allowed with no
    obligation. Trivial sessions are not taxed.

  - If the session DID edit project code, the first stop is blocked until either:
      (a) a `brain note` (or `brain resolve`) was run this session — inline
          capture, the normal path, OR
      (b) a file inside the data root was written/edited this session (fixing a
          pin, a reference doc — also real persistence).
    A trivial change is still one line.

After the first satisfied stop, a per-session sentinel
(brain-ack-<session_id> in the cache dir) allows subsequent stops without
re-checking.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_card_inject import KB_ROOT, CACHE_DIR, BRAIN_CLI  # noqa: E402

# Writes inside the data root count as persistence, not project code. Match on
# the resolved root's basename so a synced/relocated instance still matches.
DATA_ROOT_MARKER = os.path.basename(os.path.normpath(KB_ROOT)) or "brain"
DOTCLAUDE_MARKER = "/.claude/"
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "write", "edit"}

# Inline capture is the protocol. `brain resolve` also counts — retiring a
# hazard is capture too. Matches `brain note`, `brain.py note`, or a full path
# to brain.py followed by note/resolve.
BRAIN_NOTE_RE = re.compile(r"brain(?:\.py)?[\"']?\s+(note|resolve)\b", re.IGNORECASE)
# Session sentinels (brain-ack-*) are one-per-session and were never cleaned;
# GC anything older than 7 days on each run.
SENTINEL_MAX_AGE_S = 7 * 24 * 3600


def _gc_sentinels() -> None:
    try:
        now = time.time()
        for name in os.listdir(CACHE_DIR):
            if name.startswith("brain-ack-"):
                p = os.path.join(CACHE_DIR, name)
                try:
                    if now - os.path.getmtime(p) > SENTINEL_MAX_AGE_S:
                        os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


def _load_transcript(transcript_path: str) -> list:
    """Load transcript as list of message dicts. Handles both JSON and JSONL."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return []
        # Try JSON array first
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            return [data]
        except json.JSONDecodeError:
            pass
        # Fall back to JSONL
        messages = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return messages
    except Exception:
        return []


def _get_assistant_content(obj: dict) -> list:
    """Extract content list from a Claude Code transcript entry.

    Transcript JSONL format: each line is a wrapper object with
    obj["type"] == "assistant" and obj["message"]["content"] == [...].
    Falls back to treating obj itself as a message for bare API format.
    """
    if obj.get("type") == "assistant":
        content = obj.get("message", {}).get("content", [])
    elif obj.get("role") == "assistant":
        content = obj.get("content", [])
    else:
        return []
    return content if isinstance(content, list) else []


def _iter_write_paths(transcript: list):
    """Yield the normalized file_path of every Write/Edit tool_use."""
    for obj in transcript:
        if not isinstance(obj, dict):
            continue
        for block in _get_assistant_content(obj):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in WRITE_TOOLS:
                inp = block.get("input", {})
                path = inp.get("file_path", "") or inp.get("notebook_path", "")
                yield path.replace("\\", "/")


def _check_project_code_changed(transcript: list) -> bool:
    """True if any Write/Edit touched a path OUTSIDE ~/.claude/ (real project work)."""
    for path in _iter_write_paths(transcript):
        if DOTCLAUDE_MARKER not in path:
            return True
    return False


def _check_kb_written(transcript: list) -> bool:
    """True if any Write/Edit touched a path inside the data root this session."""
    marker = "/" + DATA_ROOT_MARKER + "/"
    for path in _iter_write_paths(transcript):
        if marker in ("/" + path.strip("/") + "/"):
            return True   # data-root name appears as a path component
    return False


def _check_brain_note(transcript: list) -> bool:
    """True if any Bash tool_use this session ran a `brain note` (inline capture)."""
    for obj in transcript:
        if not isinstance(obj, dict):
            continue
        for block in _get_assistant_content(obj):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in ("Bash", "PowerShell"):
                if BRAIN_NOTE_RE.search(block.get("input", {}).get("command", "") or ""):
                    return True
    return False


def main():
    # A session may opt a delegated worker out of the capture duty by setting
    # BRAIN_NO_STOP_GUARD (the reviewing/dispatching session carries it instead).
    if os.environ.get("BRAIN_NO_STOP_GUARD"):
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    # Safety: if a previous block is already looping, don't re-block
    if data.get("stop_hook_active"):
        sys.exit(0)

    _gc_sentinels()

    session_id = data.get("session_id", "unknown")
    sentinel = os.path.join(CACHE_DIR, f"brain-ack-{session_id}")

    # Already satisfied for this session
    if os.path.exists(sentinel):
        sys.exit(0)

    transcript_path = data.get("transcript_path", "")
    transcript = _load_transcript(transcript_path)

    # No project-code change this session -> nothing to enforce.
    if not _check_project_code_changed(transcript):
        sys.exit(0)

    # Inline capture (or a direct write inside the data root, e.g. fixing a
    # stale claim). "bumped dep version" is a valid entry.
    satisfied = _check_brain_note(transcript) or _check_kb_written(transcript)

    if satisfied:
        # Write sentinel to allow all future stops this session
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(session_id)
        sys.exit(0)

    # Project code changed but nothing was captured -> block and prompt.
    reason = (
        "This session changed project code. Capture what the diff can't tell: run "
        f"`python \"{BRAIN_CLI}\" note --project <scope> --type "
        "<decision|milestone|gotcha|invariant|question|papercut> \"one line\"` "
        "for each memorable thing (scope = project slug; gotcha/invariant also "
        "require --subsystem <label>; `brain resolve` for a fixed hazard also counts). "
        "A defect you would fix belongs on the ticket board (tickets.py), not in a note. "
        "Even a trivial change is one line."
    )
    result = {"decision": "block", "reason": reason}
    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
