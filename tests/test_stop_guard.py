"""Stop-guard suite for hooks/kb_stop_guard.py — the capture-duty gate.

The gate is keyed to session SUBSTANCE: it fires when the session wrote a
project file (code, docx, xlsx, md — anything outside the instance data root)
OR ran a mutating tickets/registers command, and it is satisfied by a
`brain note`/`resolve`/`attest` (shell or MCP) or a write inside the data root.

Each check drives the real hook as Claude Code does: a subprocess fed the Stop
event JSON on stdin, with a JSONL transcript fixture on disk. A blocked stop
prints `{"decision": "block", ...}`; an allowed stop prints nothing.
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
GUARD = os.path.join(ROOT_DIR, "hooks", "kb_stop_guard.py")


def _clean_env(**extra):
    env = dict(os.environ, **extra)
    env.pop("BRAIN_NO_STOP_GUARD", None)
    return env


def _write_transcript(path, blocks):
    """Write a JSONL transcript: one assistant message per tool_use block."""
    with open(path, "w", encoding="utf-8") as f:
        for b in blocks:
            f.write(json.dumps(
                {"type": "assistant", "message": {"content": [b]}}) + "\n")


def _tool_use(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def _run_guard(tmp_path, blocks, session_id="s1"):
    """Drive the hook once. Returns the blocked reason string, or None if allowed."""
    kb = str(tmp_path / "kb")
    os.makedirs(kb, exist_ok=True)
    transcript = str(tmp_path / "transcript.jsonl")
    _write_transcript(transcript, blocks)
    event = {"session_id": session_id, "transcript_path": transcript,
             "stop_hook_active": False}
    proc = subprocess.run(
        [sys.executable, GUARD], input=json.dumps(event),
        capture_output=True, text=True, env=_clean_env(BRAIN_ROOT=kb))
    out = (proc.stdout or "").strip()
    if not out:
        return None
    return json.loads(out).get("reason")


# --------------------------------------------------------------------------- #
# Fires                                                                        #
# --------------------------------------------------------------------------- #
def test_docx_only_edit_session_fires(tmp_path):
    """A document-only engagement (no code) still carries the capture duty."""
    reason = _run_guard(tmp_path, [
        _tool_use("Write", file_path="/home/u/engagement/report.docx"),
    ])
    assert reason is not None and "capture" in reason.lower()


def test_board_mutation_only_session_fires(tmp_path):
    """No file write at all — only a ticket close — still fires."""
    reason = _run_guard(tmp_path, [
        _tool_use("Bash", command='python tickets.py close --root /b --ticket 3 '
                  '--source person:me'),
    ])
    assert reason is not None and "capture" in reason.lower()


def test_xlsx_edit_fires(tmp_path):
    reason = _run_guard(tmp_path, [
        _tool_use("Edit", file_path="/home/u/work/model.xlsx"),
    ])
    assert reason is not None


# --------------------------------------------------------------------------- #
# Does not fire                                                                #
# --------------------------------------------------------------------------- #
def test_read_only_session_does_not_fire(tmp_path):
    reason = _run_guard(tmp_path, [
        _tool_use("Bash", command="python tickets.py board --root /b"),
        _tool_use("Read", file_path="/home/u/work/report.docx"),
    ])
    assert reason is None


def test_data_root_only_writes_do_not_fire(tmp_path):
    """Writes confined to the instance data root are persistence, not substance."""
    reason = _run_guard(tmp_path, [
        _tool_use("Write", file_path="/home/u/kb/tree/demo/root.md"),
    ])
    assert reason is None


def test_dotclaude_only_writes_do_not_fire(tmp_path):
    reason = _run_guard(tmp_path, [
        _tool_use("Edit", file_path="/home/u/.claude/settings.json"),
    ])
    assert reason is None


# --------------------------------------------------------------------------- #
# Satisfaction                                                                 #
# --------------------------------------------------------------------------- #
def test_brain_note_satisfies(tmp_path):
    reason = _run_guard(tmp_path, [
        _tool_use("Write", file_path="/home/u/engagement/report.docx"),
        _tool_use("Bash", command='python brain.py note --project demo '
                  '--type milestone "delivered the report"'),
    ])
    assert reason is None


def test_board_mutation_satisfied_by_note(tmp_path):
    reason = _run_guard(tmp_path, [
        _tool_use("Bash", command="python tickets.py close --root /b --ticket 3 "
                  "--source person:me"),
        _tool_use("Bash", command='python brain.py note --project demo '
                  '--type decision "closed the ticket, here is why"'),
    ])
    assert reason is None


def test_mcp_brain_note_satisfies(tmp_path):
    """The capture verb reached through the MCP server also satisfies."""
    reason = _run_guard(tmp_path, [
        _tool_use("Write", file_path="/home/u/engagement/report.docx"),
        _tool_use("mcp__agent-brain__brain_note", project="demo", type="milestone",
                  text="delivered"),
    ])
    assert reason is None


def test_mcp_brain_attest_satisfies(tmp_path):
    reason = _run_guard(tmp_path, [
        _tool_use("Bash", command="python tickets.py update --root /b --ticket 3 "
                  '-- "moved to review"'),
        _tool_use("mcp__agent-brain__brain_attest", reason="pure housekeeping"),
    ])
    assert reason is None


def test_data_root_write_satisfies(tmp_path):
    reason = _run_guard(tmp_path, [
        _tool_use("Write", file_path="/home/u/engagement/report.docx"),
        _tool_use("Write", file_path="/home/u/kb/stream/demo.log"),
    ])
    assert reason is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
