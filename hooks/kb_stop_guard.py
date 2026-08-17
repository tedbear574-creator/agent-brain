#!/usr/bin/env python3
"""
Stop hook: enforce the Agent Brain capture duty.

Enforcement is deterministic and keyed to session SUBSTANCE, not to code alone —
a document-only engagement (docx/xlsx/markdown) and a board-mutation-only
session both carry the discipline, because the rulebook applies to every surface,
not just the ones that touch code:

  - The gate FIRES when the session either (a) wrote or edited ANY project file —
    code, docx, xlsx, md, anything OUTSIDE the instance's own data root and the
    Claude Code config dir — OR (b) ran a mutating tickets/registers command (a
    Bash/PowerShell tool call whose verb changes the board or a register).

  - It does NOT fire on: read-only / Q&A sessions, sessions whose only writes
    landed inside the data root or its generated artifacts, or sessions that
    already captured (a `brain note`/`brain resolve`) or attested.

  - When it fires, the first stop is blocked until the session either (a) ran a
    `brain note` (or `brain resolve`) — inline capture, the normal path, or (b)
    wrote a file inside the data root (fixing a pin, a reference doc — also real
    persistence). A trivial change is still one line.

This is the hook half of a two-layer model: the engine accrues capture debt on
every MCP surface (see brain_mcp.py) and the hooks gate Claude Code — the SAME
capture-or-attest contract on both. The hook is an accelerator, not the
mechanism.

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
SHELL_TOOLS = {"Bash", "PowerShell", "bash", "powershell"}

# Inline capture is the protocol. `brain resolve` also counts — retiring a
# hazard is capture too — and `brain attest` is the sanctioned "nothing to
# capture" exit. Matches `brain note`, `brain.py note`, or a full path to
# brain.py followed by note/resolve/attest.
BRAIN_NOTE_RE = re.compile(r"brain(?:\.py)?[\"']?\s+(note|resolve|attest)\b",
                           re.IGNORECASE)
# The same capture verbs reached through the MCP server show up as tool_use
# blocks whose name ends in brain_note / brain_resolve / brain_attest.
CAPTURE_TOOL_SUFFIXES = ("brain_note", "brain_resolve", "brain_attest")
# A session has substance if it ran a MUTATING board/register command. Verbs
# that change stored data (not the read-only board/show/view/log/export/init).
BOARD_MUTATION_RE = re.compile(
    r"\b(?:tickets|registers)(?:\.py)?[\"']?\s+"
    r"(?:open|claim|block|reopen|close|edit|update|comment|add|set|post|"
    r"correct|retire|restore)\b",
    re.IGNORECASE)
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


def _is_data_root_path(path: str) -> bool:
    """True if a write path lands inside the instance data root."""
    marker = "/" + DATA_ROOT_MARKER + "/"
    return marker in ("/" + path.strip("/") + "/")


def _wrote_project_file(transcript: list) -> bool:
    """True if any Write/Edit touched a project file — anything OUTSIDE both the
    Claude Code config dir and the instance data root. Code, docx, xlsx, md,
    anything: substance is not code-only."""
    for path in _iter_write_paths(transcript):
        if DOTCLAUDE_MARKER not in path and not _is_data_root_path(path):
            return True
    return False


def _check_kb_written(transcript: list) -> bool:
    """True if any Write/Edit touched a path inside the data root this session."""
    for path in _iter_write_paths(transcript):
        if _is_data_root_path(path):
            return True   # data-root name appears as a path component
    return False


def _iter_shell_commands(transcript: list):
    """Yield the command string of every Bash/PowerShell tool_use."""
    for obj in transcript:
        if not isinstance(obj, dict):
            continue
        for block in _get_assistant_content(obj):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in SHELL_TOOLS:
                yield block.get("input", {}).get("command", "") or ""


def _ran_board_mutation(transcript: list) -> bool:
    """True if any shell command ran a MUTATING tickets/registers verb."""
    for cmd in _iter_shell_commands(transcript):
        if BOARD_MUTATION_RE.search(cmd):
            return True
    return False


def _check_brain_note(transcript: list) -> bool:
    """True if the session captured or attested — a `brain note`/`resolve`/`attest`
    run as a shell command, or the same verbs reached through the MCP server."""
    for cmd in _iter_shell_commands(transcript):
        if BRAIN_NOTE_RE.search(cmd):
            return True
    for obj in transcript:
        if not isinstance(obj, dict):
            continue
        for block in _get_assistant_content(obj):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name") or ""
                if name.endswith(CAPTURE_TOOL_SUFFIXES):
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

    # No substance this session -> nothing to enforce. Substance is either a
    # project-file write (code, docx, xlsx, md — anything outside the data root
    # and the config dir) OR a mutating tickets/registers command.
    if not (_wrote_project_file(transcript) or _ran_board_mutation(transcript)):
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

    # Substance this session but nothing was captured -> block and prompt.
    reason = (
        "This session did substantive work (edited a project file or changed the "
        "ticket/register board). Capture what the diff can't tell: run "
        f"`python \"{BRAIN_CLI}\" note --project <scope> --type "
        "<decision|milestone|gotcha|invariant|question|papercut> \"one line\"` "
        "for each memorable thing (scope = project slug; gotcha/invariant also "
        "require --subsystem <label>; `brain resolve` for a fixed hazard also counts). "
        "If there is genuinely nothing to capture, attest it instead. "
        "A defect you would fix belongs on the ticket board (tickets.py), not in a note. "
        "Even a trivial change is one line."
    )
    result = {"decision": "block", "reason": reason}
    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
