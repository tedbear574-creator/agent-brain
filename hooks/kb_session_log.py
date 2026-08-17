#!/usr/bin/env python3
"""
SessionEnd hook: append one audit line per session to <data root>/_state/log-audit.txt.

Records: timestamp, session_id, end reason, and whether any file inside the data
root was written this session. Purely observational — SessionEnd hooks cannot
block or inject context into the model.
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_card_inject import KB_ROOT  # noqa: E402

AUDIT_LOG = os.path.join(KB_ROOT, "_state", "log-audit.txt")
DATA_ROOT_MARKER = "/" + (os.path.basename(os.path.normpath(KB_ROOT)) or "brain") + "/"


def _load_transcript(transcript_path: str) -> list:
    """Load transcript as list of message dicts. Handles both JSON and JSONL."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            return [data]
        except json.JSONDecodeError:
            pass
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


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "unknown")
    reason = data.get("reason", "unknown")
    transcript_path = data.get("transcript_path", "")

    # Scan writes once: did the session touch the data root, and did it edit
    # project code (any write outside the Claude Code config dir)? Without
    # code_changed, kb_updated=no is ambiguous (no obligation vs. skipped).
    kb_touched = False
    code_changed = False
    write_tools = {"Write", "Edit", "NotebookEdit", "write", "edit"}
    transcript = _load_transcript(transcript_path)
    for obj in transcript:
        if not isinstance(obj, dict):
            continue
        # Claude Code JSONL: outer wrapper with type=="assistant" + message.content
        if obj.get("type") == "assistant":
            content = obj.get("message", {}).get("content", [])
        elif obj.get("role") == "assistant":
            content = obj.get("content", [])
        else:
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in write_tools:
                inp = block.get("input", {})
                path = (inp.get("file_path", "") or inp.get("notebook_path", "")).replace("\\", "/")
                if not path:
                    continue
                if DATA_ROOT_MARKER in ("/" + path.strip("/") + "/"):
                    kb_touched = True
                elif "/.claude/" not in path:
                    code_changed = True
        if kb_touched and code_changed:
            break

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (
        f"{ts} | session={session_id} | end_reason={reason} "
        f"| code_changed={'yes' if code_changed else 'no'} "
        f"| kb_updated={'yes' if kb_touched else 'no'}\n"
    )

    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # Never fail — this is observability only

    sys.exit(0)


if __name__ == "__main__":
    main()
