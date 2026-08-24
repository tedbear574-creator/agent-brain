"""Role-gate behaviors merged from the live hooks into the package copies.

A delegated worker is marked by the deployment's role env (default CCDESK_ROLE).
Injection hooks (SessionStart card, UserPromptSubmit topic) must inject NOTHING
into such a session — its context is its brief. The write-authority hook must
DENY a brain write from an executor-role session. Each hook is driven exactly as
Claude Code drives it: a subprocess fed the event JSON on stdin.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
HOOKS = os.path.join(ROOT_DIR, "hooks")


def _clean_env(**extra):
    env = dict(os.environ, **extra)
    env.pop("CCDESK_ROLE", None)
    env.pop("BRAIN_NO_INJECT", None)
    return env


def _run(script, event, env):
    proc = subprocess.run(
        [sys.executable, os.path.join(HOOKS, script)],
        input=json.dumps(event), capture_output=True, text=True, env=env)
    return (proc.stdout or "").strip()


def test_card_inject_suppressed_for_delegated_role(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "user.md").write_text("Alice prefers terse output.\n", encoding="utf-8")
    event = {"cwd": str(tmp_path)}
    base = _clean_env(BRAIN_ROOT=str(kb))
    # Interactive: the user model is injected.
    assert "BRAIN USER" in _run("kb_card_inject.py", event, base)
    # Delegated: nothing ambient.
    role = _clean_env(BRAIN_ROOT=str(kb))
    role["CCDESK_ROLE"] = "executor"
    assert _run("kb_card_inject.py", event, role) == ""


def test_topic_inject_suppressed_for_delegated_role(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    event = {"session_id": "s1", "prompt": "how should I model the debt stack here?"}
    role = _clean_env(BRAIN_ROOT=str(kb), BRAIN_HAZ_CACHE=str(tmp_path / "c"))
    role["CCDESK_ROLE"] = "executor"
    # The role gate returns before any recall runs — always silent.
    assert _run("kb_topic_inject.py", event, role) == ""


def test_write_authority_denies_executor_brain_write(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    event = {"tool_name": "Write",
             "tool_input": {"file_path": str(kb / "stream" / "demo.log")}}
    env = _clean_env(BRAIN_ROOT=str(kb))
    env["CCDESK_ROLE"] = "executor"
    out = _run("kb_write_authority.py", event, env)
    assert out, "an executor brain write must be denied"
    decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    assert decision == "deny"
    # A non-executor role does not trigger the write block.
    env2 = _clean_env(BRAIN_ROOT=str(kb))
    assert _run("kb_write_authority.py", event, env2) == ""


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
