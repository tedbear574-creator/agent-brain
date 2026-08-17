"""End-to-end suite for brain_mcp.py — the MCP server.

The server is driven exactly as a real MCP client would: a subprocess over
stdio, one compact JSON-RPC object per line. Every check goes through the wire
(handshake, tools/list, tools/call) so the JSON-RPC framing and the engine
wiring are exactly what a client re-runs. No network, no sleeps beyond process
startup — the temp instance is built once per test via the real CLIs.
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
MCP = os.path.join(ROOT_DIR, "brain_mcp.py")
BRAIN = os.path.join(ROOT_DIR, "brain.py")
TICKETS = os.path.join(ROOT_DIR, "tickets.py")
REGISTERS = os.path.join(ROOT_DIR, "registers.py")

PROTOCOL_VERSION = "2025-06-18"


def _clean_env(**extra):
    env = dict(os.environ, **extra)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    return env


def _init_instance(kb):
    """A fresh, empty no-git brain instance at kb."""
    r = subprocess.run([sys.executable, BRAIN, "init", "--no-git"],
                       env=_clean_env(BRAIN_ROOT=kb),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


class Client:
    """A minimal MCP client over the server's stdio."""

    def __init__(self, kb=None, root_flag=None):
        argv = [sys.executable, MCP]
        if root_flag:
            argv += ["--root", root_flag]
        env = _clean_env(**({"BRAIN_ROOT": kb} if kb else {}))
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        self._id = 0

    def request(self, method, params=None):
        self._id += 1
        rid = self._id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, "server closed the stream without replying"
        resp = json.loads(line)
        assert resp.get("id") == rid, f"id mismatch: sent {rid}, got {resp}"
        return resp

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def handshake(self):
        resp = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        })
        self.notify("notifications/initialized")
        return resp

    def call(self, name, arguments):
        resp = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "result" in resp, resp
        return resp["result"]

    def call_text(self, name, arguments):
        result = self.call(name, arguments)
        return result["isError"], result["content"][0]["text"]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


@pytest.fixture
def client(tmp_path):
    kb = str(tmp_path / "kb")
    _init_instance(kb)
    c = Client(kb=kb)
    c.handshake()
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# Handshake / protocol                                                         #
# --------------------------------------------------------------------------- #
def test_handshake_reports_supported_version(tmp_path):
    kb = str(tmp_path / "kb")
    _init_instance(kb)
    c = Client(kb=kb)
    resp = c.handshake()
    result = resp["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"]
    c.close()


def test_unknown_protocol_degrades_to_server_latest(tmp_path):
    kb = str(tmp_path / "kb")
    _init_instance(kb)
    c = Client(kb=kb)
    resp = c.request("initialize", {
        "protocolVersion": "1999-01-01",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    })
    # Never a hard failure on a version string — we reply with our latest.
    assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
    c.close()


def test_ping(client):
    resp = client.request("ping")
    assert resp["result"] == {}


def test_unknown_method_is_method_not_found(client):
    resp = client.request("no/such/method")
    assert resp["error"]["code"] == -32601


def test_parse_error_on_bad_line(client):
    client.proc.stdin.write("this is not json\n")
    client.proc.stdin.flush()
    resp = json.loads(client.proc.stdout.readline())
    assert resp["error"]["code"] == -32700


# --------------------------------------------------------------------------- #
# tools/list                                                                   #
# --------------------------------------------------------------------------- #
def test_tools_list_shape(client):
    resp = client.request("tools/list")
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    expected = {
        "brain_note", "brain_wake", "brain_hazards", "brain_design",
        "brain_recall", "brain_decisions", "brain_grep", "brain_resolve",
        "brain_papercuts", "tickets_board", "tickets_open", "tickets_update",
        "tickets_close", "registers_board", "registers_show", "registers_post",
        "registers_add",
    }
    assert expected <= names
    for t in tools:
        assert t["description"], f"{t['name']} has no description"
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert isinstance(schema.get("required", []), list)


# --------------------------------------------------------------------------- #
# Memory round-trips                                                           #
# --------------------------------------------------------------------------- #
def test_note_round_trip(client):
    is_error, text = client.call_text(
        "brain_note",
        {"project": "demo", "type": "decision", "text": "use the deterministic tree"})
    assert not is_error, text
    assert "demo #1" in text and "decision" in text

    is_error, text = client.call_text("brain_wake", {"scope": "demo"})
    assert not is_error, text
    assert "deterministic tree" in text


def test_over_cap_note_returns_teaching_error(client):
    is_error, text = client.call_text(
        "brain_note",
        {"project": "demo", "type": "gotcha", "subsystem": "parsing",
         "text": "x" * 400})
    assert is_error
    assert "cap for gotcha" in text and "compress" in text


def test_missing_required_argument_is_error(client):
    is_error, text = client.call_text("brain_note", {"type": "decision"})
    assert is_error
    assert "text" in text


def test_unknown_tool_is_error(client):
    is_error, text = client.call_text("nope_tool", {})
    assert is_error
    assert "unknown tool" in text


# --------------------------------------------------------------------------- #
# Board round-trips                                                            #
# --------------------------------------------------------------------------- #
def test_tickets_round_trip(client, tmp_path):
    board = str(tmp_path / "work")
    r = subprocess.run([sys.executable, TICKETS, "init", "--root", board,
                        "--prefix", "WORK", "--name", "Work board"],
                       env=_clean_env(), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    is_error, text = client.call_text(
        "tickets_open", {"root": board, "title": "fix the flaky test"})
    assert not is_error, text

    is_error, text = client.call_text("tickets_board", {"root": board})
    assert not is_error, text
    assert "flaky" in text


def test_registers_round_trip(client, tmp_path):
    reg = str(tmp_path / "ent")
    schema = [
        {"name": "code", "label": "Code", "kind": "fixed"},
        {"name": "name", "label": "Name", "kind": "fixed", "required": True},
        {"name": "revenue", "label": "Revenue", "kind": "tracked", "type": "number"},
    ]
    sfile = str(tmp_path / "schema.json")
    with open(sfile, "w", encoding="utf-8") as f:
        json.dump(schema, f)
    r = subprocess.run([sys.executable, REGISTERS, "init", "--root", reg,
                        "--prefix", "ENT", "--name", "Entities",
                        "--schema-file", sfile, "--key", "code"],
                       env=_clean_env(), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    is_error, _ = client.call_text(
        "registers_add", {"root": reg, "fields": {"code": "ACME", "name": "Acme Ltd"}})
    assert not is_error

    is_error, _ = client.call_text(
        "registers_post",
        {"root": reg, "key": "ACME", "field": "revenue", "value": "100", "asof": "2025"})
    assert not is_error

    is_error, text = client.call_text("registers_board", {"root": reg})
    assert not is_error, text
    assert "ACME" in text

    is_error, text = client.call_text("registers_show", {"root": reg, "key": "ACME"})
    assert not is_error, text
    assert "100" in text


# --------------------------------------------------------------------------- #
# Capture debt — engine-side enforcement                                      #
# --------------------------------------------------------------------------- #
def _make_board(tmp_path):
    board = str(tmp_path / "work")
    r = subprocess.run([sys.executable, TICKETS, "init", "--root", board,
                        "--prefix", "WORK", "--name", "Work board"],
                       env=_clean_env(), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return board


NOTICE = "capture debt"


def test_debt_notice_after_two_mutations(client, tmp_path):
    board = _make_board(tmp_path)
    # First mutation: debt = 1, no notice yet.
    is_error, text = client.call_text("tickets_open", {"root": board, "title": "one"})
    assert not is_error, text
    assert NOTICE not in text
    # Second mutation: debt = 2, the notice appears — appended, not replacing.
    is_error, text = client.call_text("tickets_open", {"root": board, "title": "two"})
    assert not is_error, text
    assert NOTICE in text
    assert "two" in text  # underlying result is intact — never blocked
    assert "2 mutations" in text


def test_brain_note_clears_debt(client, tmp_path):
    board = _make_board(tmp_path)
    client.call_text("tickets_open", {"root": board, "title": "a"})
    is_error, text = client.call_text("tickets_open", {"root": board, "title": "b"})
    assert NOTICE in text  # debt >= 2 now
    # A brain_note captures and clears the debt — its own result carries no notice.
    is_error, text = client.call_text(
        "brain_note", {"project": "demo", "type": "milestone", "text": "captured it"})
    assert not is_error, text
    assert NOTICE not in text
    # And a following mutation starts the count over: debt = 1, no notice.
    is_error, text = client.call_text("tickets_open", {"root": board, "title": "c"})
    assert not is_error, text
    assert NOTICE not in text


def test_brain_attest_clears_debt_and_writes_log(client, tmp_path):
    kb = str(tmp_path / "kb")  # the client fixture builds its instance here
    board = _make_board(tmp_path)
    client.call_text("tickets_open", {"root": board, "title": "a"})
    is_error, text = client.call_text("tickets_open", {"root": board, "title": "b"})
    assert NOTICE in text  # debt >= 2
    # brain_attest is the sanctioned "nothing to capture" exit.
    is_error, text = client.call_text(
        "brain_attest", {"reason": "pure board housekeeping, nothing to capture"})
    assert not is_error, text
    assert NOTICE not in text  # its own result is clear
    # The attestation landed in the append-only log under the instance root.
    log = os.path.join(kb, "_state", "attest.log")
    assert os.path.exists(log), "attest.log was not written"
    with open(log, encoding="utf-8") as f:
        body = f.read()
    assert "pure board housekeeping" in body
    # Debt is cleared: the next mutation is debt = 1, no notice.
    is_error, text = client.call_text("tickets_open", {"root": board, "title": "c"})
    assert not is_error, text
    assert NOTICE not in text


def test_brain_attest_requires_reason(client):
    is_error, text = client.call_text("brain_attest", {})
    assert is_error
    assert "reason" in text


def test_notice_never_blocks_the_result(client, tmp_path):
    board = _make_board(tmp_path)
    client.call_text("tickets_open", {"root": board, "title": "a"})
    client.call_text("tickets_open", {"root": board, "title": "b"})
    # A read-only call while in debt still returns its real content, plus notice.
    is_error, text = client.call_text("tickets_board", {"root": board})
    assert not is_error, text
    assert NOTICE in text
    assert "WORK-1" in text  # the board itself is present, unblocked


def test_brain_attest_listed_with_contract(client):
    resp = client.request("tools/list")
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert "brain_attest" in tools
    # Its description states what it is for.
    assert "nothing to capture" in tools["brain_attest"]["description"].lower()
    # brain_note's description tells the model mutations create debt.
    assert "capture debt" in tools["brain_note"]["description"].lower()


# --------------------------------------------------------------------------- #
# Root resolution                                                             #
# --------------------------------------------------------------------------- #
def test_root_flag_selects_the_instance(tmp_path):
    kb = str(tmp_path / "flagkb")
    _init_instance(kb)
    # No BRAIN_ROOT in the env — the --root flag alone must point the brain
    # tools at this instance.
    c = Client(kb=None, root_flag=kb)
    c.handshake()
    is_error, text = c.call_text(
        "brain_note", {"project": "viaflag", "type": "milestone", "text": "flag works"})
    assert not is_error, text
    assert "viaflag #1" in text
    c.close()
    # And the write really landed under kb, not anywhere else.
    assert os.path.exists(os.path.join(kb, "stream", "viaflag.log"))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_debt_notice_on_error_paths(client, tmp_path):
    """A malformed call while in debt still carries the notice (review note)."""
    board = _make_board(tmp_path)
    client.call_text("tickets_open", {"root": board, "title": "one"})
    client.call_text("tickets_open", {"root": board, "title": "two"})
    # builder-validation error (missing required args) while in debt
    is_error, text = client.call_text("brain_note", {})
    assert is_error
    assert NOTICE in text
    # unknown tool while in debt
    is_error, text = client.call_text("no_such_tool", {})
    assert is_error
    assert NOTICE in text
