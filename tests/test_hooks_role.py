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
BRAIN = os.path.join(ROOT_DIR, "brain.py")
TICKETS = os.path.join(ROOT_DIR, "tickets.py")

INJECT_MAX_CHARS = 7000  # kept in lockstep with hooks/kb_card_inject.py


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


def _brain(kb, *args):
    subprocess.run([sys.executable, BRAIN, *args],
                   env=dict(os.environ, BRAIN_ROOT=str(kb)),
                   capture_output=True, text=True, check=True)


def _tickets(kb, *args):
    board = os.path.join(str(kb), "tickets")
    subprocess.run([sys.executable, TICKETS, *args, "--root", board],
                   env=dict(os.environ, BRAIN_ROOT=str(kb)),
                   capture_output=True, text=True, check=True)


def _worst_case_kb(kb, scope):
    """Build the injection's worst case: 1200-char pins, a 3000-char user.md,
    8 due tickets, and 40 long rulings — so the joined hook output is maximal."""
    # ~1200 chars of pinned invariants (PIN_BUDGET), each under the 300-char cap.
    for i in range(5):
        _brain(kb, "note", "-p", scope, "-t", "invariant", "--pin",
               "--subsystem", f"id{i}", f"pinned identity fact {i} " + "P" * 210)
    # 40 long rulings; each clips to 200 in --inject, filling the 1500 state budget.
    for i in range(40):
        _brain(kb, "note", "-p", scope, "-t", "decision",
               f"ruling {i}: " + "R" * 400)
    # A 3000-char user.md across several ## sections.
    body = "\n".join(f"## Section {s}\n" + "\n".join(
        f"- fact {s}.{n} " + "U" * 60 for n in range(8)) for s in range(6))
    (kb / "user.md").write_text(body[:3000], encoding="utf-8")
    # 8 due tickets on the home board.
    _tickets(kb, "init", "--prefix", "HUB", "--name", "Hub work board")
    for i in range(8):
        _tickets(kb, "open", f"overdue task {i} " + "T" * 40,
                 "--due", "2020-01-01", "--owner", "tod")


def _hook_env(kb):
    env = _clean_env(BRAIN_ROOT=str(kb), BRAIN_CLI=BRAIN)
    env.pop("BRAIN_CONFIG", None)  # isolate from the machine's real config
    return env


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


def _load_hook_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "kb_card_inject", os.path.join(HOOKS, "kb_card_inject.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_assemble_trim_order_state_then_user():
    mod = _load_hook_module()
    header = "[BRAIN scope=x] header"
    board = "[BRAIN BOARD]\n- row\nsummary"
    inbox = "[BRAIN INBOX]\n- item"
    user = "## U\n" + "\n".join(f"- user fact {i} " + "u" * 60 for i in range(40))
    state = ("[BRAIN STATE] rulings:\n"
             + "\n".join(f"#{i} " + "s" * 180 for i in range(40))
             + "\n   other scopes with unsummarized entries:\n     y: 2 entries, 2 not yet summarized")
    out = mod._assemble(header, user, board, inbox, state)
    assert len(out) <= mod.INJECT_MAX_CHARS
    # Never-trimmed sections survive whole and the header stays first.
    assert out.startswith(header)
    assert board in out and inbox in out
    # STATE is sacrificed before USER: rulings are dropped from the state block
    # while the user model is still largely intact.
    kept_rulings = sum(1 for ln in out.splitlines() if ln.startswith("#"))
    assert kept_rulings < 40                      # oldest rulings trimmed
    assert "user fact 0" in out                   # user protected until state is spent


def test_inject_worst_case_under_total_budget(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    proj = tmp_path / "demo"                 # basename 'demo' -> scope 'demo'
    _worst_case_kb(kb, "demo")
    out = _run("kb_card_inject.py", {"cwd": str(proj)}, _hook_env(kb))
    assert out, "the worst case must still inject something"
    assert len(out) <= INJECT_MAX_CHARS, f"joined output {len(out)} > {INJECT_MAX_CHARS}"


def test_inject_board_appears_exactly_once(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    proj = tmp_path / "demo"
    _worst_case_kb(kb, "demo")
    out = _run("kb_card_inject.py", {"cwd": str(proj)}, _hook_env(kb))
    # The board is printed ONCE (the old double-print — wake footer AND hook — is gone).
    assert out.count("[BRAIN BOARD]") == 1
    assert out.count("-- board") == 0        # the human-wake footer never leaks in


def test_inject_header_present_and_first_under_worst_case(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    proj = tmp_path / "demo"
    _worst_case_kb(kb, "demo")
    out = _run("kb_card_inject.py", {"cwd": str(proj)}, _hook_env(kb))
    assert out.startswith("[BRAIN scope=demo]"), out[:80]
    # The header line carries all four computed counts and is never trimmed away.
    first = out.splitlines()[0]
    for token in ("live rulings", "live hazards", "live invariants", "entries total"):
        assert token in first


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
