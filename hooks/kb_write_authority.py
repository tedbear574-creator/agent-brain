#!/usr/bin/env python3
"""PreToolUse hook — only the session that owns a work item writes to the brain.

The Agent Brain stream is single-writer per instance (SPEC § Deployment
profiles). A delegated worker runs with ``BRAIN_DELEGATE=1``: its context is a
brief, not the whole project, so a stream entry written from two layers down is
unattributable and usually wrong — it records what one sub-task did, not what
the change means, and the session that actually owns the work item then can't
tell its own state from a delegate's guess.

This is the write half of the single-writer rule: it denies
Write/Edit/NotebookEdit whose path lands inside the data root when the session
is a delegate. Everything else passes through untouched. Always exits 0 — a
malformed payload must never wedge a session.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_card_inject import KB_ROOT  # noqa: E402

DATA_ROOT_MARKER = (os.path.basename(os.path.normpath(KB_ROOT)) or "brain").lower()
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

REASON = (
    "Blocked: delegated workers don't write to the brain. This session is "
    "BRAIN_DELEGATE=1 — its context is a brief, not the whole project, so a "
    "stream entry from here is unattributable (single-writer rule). Put what you "
    "learned in your final output instead: the session that owns the work item "
    "records it, and that session is the one that can tell whether it's true of "
    "the project as a whole."
)


def _is_delegate() -> bool:
    return bool((os.environ.get("BRAIN_DELEGATE") or "").strip())


def _targets_brain(tool_input: dict) -> bool:
    """True if any path-ish field in *tool_input* points into the data root.

    Checked with normalised separators so a Windows path matches the same
    marker as a POSIX one.
    """
    marker = "/" + DATA_ROOT_MARKER + "/"
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        normalised = "/" + value.replace("\\", "/").strip("/").lower() + "/"
        if marker in normalised:
            return True
    return False


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # malformed input: never block on our own parse failure

    if not _is_delegate():
        return

    if data.get("tool_name") not in WRITE_TOOLS:
        return

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict) or not _targets_brain(tool_input):
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }
    }))


if __name__ == "__main__":
    main()
