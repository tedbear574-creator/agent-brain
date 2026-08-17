#!/usr/bin/env python3
"""brain_mcp.py — the Agent Brain MCP server (stdlib only).

Speaks the Model Context Protocol over stdio as line-delimited JSON-RPC 2.0.
It exposes the three Agent Brain command-line tools — the memory engine
(brain.py), the ticket board (tickets.py), and the registers (registers.py) —
to any MCP client (a desktop chat app, an editor, another agent).

Why it exists: the Claude Code hooks give hook-driven sessions the engine's
discipline for free. Desktop apps and other non-hook surfaces have no hook
layer. This server closes that gap by putting the SAME engine behind every
tool call — every caps check, duplicate rejection, routing test, and
subsystem requirement is enforced by the real engine, and its teaching error
comes straight back in the tool result so the model reads it and corrects.

Implementation notes:
  * No third-party packages. MCP is just JSON-RPC 2.0 over stdio, one compact
    JSON object per line. We implement `initialize`, `notifications/initialized`,
    `tools/list`, `tools/call`, and `ping` directly.
  * The tools do not re-implement any engine logic — they shell out to the
    three CLIs and hand back exactly what the engine prints (stdout for a
    result, stderr for a teaching error), with `isError` set from the exit code.
  * Windows: stdio is forced to unbuffered UTF-8 so a stray code page never
    corrupts a message.

Root resolution mirrors brain.py: a `--root` flag wins, else BRAIN_ROOT, else
the config file / default `brain/` next to brain.py. That root is the single
memory instance for the `brain_*` tools. The board tools (`tickets_*`,
`registers_*`) take a `root` argument per call — the folder IS the deployment.
"""
import io
import json
import os
import subprocess
import sys

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BRAIN_PY = os.path.join(PACKAGE_DIR, "brain.py")
TICKETS_PY = os.path.join(PACKAGE_DIR, "tickets.py")
REGISTERS_PY = os.path.join(PACKAGE_DIR, "registers.py")

# The protocol version we implement, plus older ones we still understand. On an
# `initialize` we echo the client's version if we support it, otherwise we reply
# with our latest and let the client decide — graceful degrade, never a hard
# failure on a version string.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "agent-brain", "version": "1.0.0"}


# --------------------------------------------------------------------------- #
# Root resolution (same precedence as brain.py)                               #
# --------------------------------------------------------------------------- #
def _resolve_root(cli_root=None) -> str:
    """--root flag > BRAIN_ROOT env > brain.py's config/default resolution."""
    if cli_root and str(cli_root).strip():
        return os.path.abspath(os.path.expanduser(str(cli_root).strip()))
    # Defer to brain.py so the config-file / default path stays in exactly one
    # place. Importing it is side-effect-free (module-level code only defines
    # constants and resolves paths — it never creates or writes anything).
    sys.path.insert(0, PACKAGE_DIR)
    import brain  # noqa: E402
    return brain._resolve_root()


# --------------------------------------------------------------------------- #
# Running the engine CLIs                                                      #
# --------------------------------------------------------------------------- #
def _run_cli(script: str, argv: list) -> tuple:
    """Run one CLI and return (isError, text).

    The engine prints a result to stdout and a teaching error to stderr, so we
    combine both (a rejected write has its correction on stderr) and read the
    exit code for isError. This is the whole point of the server: the model
    sees the engine's real validation verbatim.
    """
    cmd = [sys.executable, script] + [str(a) for a in argv]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env=os.environ.copy())
    except OSError as e:  # the interpreter or script path is wrong — a real fault
        return True, f"failed to run {os.path.basename(script)}: {e}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out and err:
        text = out + "\n" + err
    else:
        text = out or err
    if not text:
        text = "(no output)" if proc.returncode == 0 else "(command failed, no output)"
    return proc.returncode != 0, text


class ToolError(Exception):
    """A required argument is missing — reported as an isError tool result."""


def _need(args: dict, key: str):
    v = args.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ToolError(f"missing required argument: {key}")
    return v


# --------------------------------------------------------------------------- #
# Tool builders — each returns (script, argv). A leading "--" before free text  #
# keeps a value that starts with "-" from being read as a flag.                 #
# --------------------------------------------------------------------------- #
def _t_brain_note(a):
    argv = ["note", "--type", _need(a, "type")]
    if a.get("project"):
        argv += ["--project", a["project"]]
    if a.get("supersedes"):
        argv += ["--supersedes", str(a["supersedes"])]
    if a.get("subsystem"):
        argv += ["--subsystem", a["subsystem"]]
    if a.get("source"):
        argv += ["--source", a["source"]]
    if a.get("pin"):
        argv += ["--pin"]
    if a.get("distinct"):
        argv += ["--distinct"]
    if a.get("force_type"):
        argv += ["--force-type"]
    argv += ["--", _need(a, "text")]
    return BRAIN_PY, argv


def _t_brain_wake(a):
    return BRAIN_PY, ["wake", "--scope", _need(a, "scope")]


def _t_brain_hazards(a):
    argv = ["hazards", "--scope", _need(a, "scope")]
    if a.get("grep"):
        argv += ["--grep", a["grep"]]
    return BRAIN_PY, argv


def _t_brain_design(a):
    argv = ["design", "--scope", _need(a, "scope")]
    if a.get("grep"):
        argv += ["--grep", a["grep"]]
    return BRAIN_PY, argv


def _t_brain_recall(a):
    return BRAIN_PY, ["recall", "--", _need(a, "text")]


def _t_brain_decisions(a):
    argv = ["decisions", "--scope", _need(a, "scope")]
    if a.get("n"):
        argv += ["-n", str(a["n"])]
    return BRAIN_PY, argv


def _t_brain_grep(a):
    argv = ["grep"]
    if a.get("scope"):
        argv += ["--scope", a["scope"]]
    argv += ["--", _need(a, "regex")]
    return BRAIN_PY, argv


def _t_brain_resolve(a):
    return BRAIN_PY, ["resolve", "--project", _need(a, "project"),
                      "--", str(_need(a, "n")), _need(a, "text")]


def _t_brain_papercuts(a):
    argv = ["papercuts"]
    if a.get("scope"):
        argv += ["--scope", a["scope"]]
    return BRAIN_PY, argv


def _t_tickets_board(a):
    return TICKETS_PY, ["board", "--root", _need(a, "root")]


def _t_tickets_open(a):
    argv = ["open", "--root", _need(a, "root")]
    if a.get("prefix"):
        argv += ["--prefix", a["prefix"]]
    if a.get("owner"):
        argv += ["--owner", a["owner"]]
    if a.get("due"):
        argv += ["--due", a["due"]]
    argv += ["--", _need(a, "title")]
    return TICKETS_PY, argv


def _t_tickets_update(a):
    return TICKETS_PY, ["update", "--root", _need(a, "root"),
                        "--ticket", str(_need(a, "ticket")), "--", _need(a, "text")]


def _t_tickets_close(a):
    return TICKETS_PY, ["close", "--root", _need(a, "root"),
                        "--ticket", str(_need(a, "ticket")),
                        "--source", _need(a, "source")]


def _t_registers_board(a):
    return REGISTERS_PY, ["board", "--root", _need(a, "root")]


def _t_registers_show(a):
    return REGISTERS_PY, ["show", "--root", _need(a, "root"),
                          "--record", str(_need(a, "key"))]


def _t_registers_post(a):
    argv = ["post", "--root", _need(a, "root"), "--record", str(_need(a, "key")),
            "--field", _need(a, "field"), "--value", str(_need(a, "value"))]
    if a.get("asof"):
        argv += ["--asof", str(a["asof"])]
    return REGISTERS_PY, argv


def _t_registers_add(a):
    fields = _need(a, "fields")
    if not isinstance(fields, dict):
        raise ToolError("`fields` must be an object of field=value pairs")
    argv = ["add", "--root", _need(a, "root")]
    for k, v in fields.items():
        argv.append(f"{k}={v}")
    return REGISTERS_PY, argv


# --------------------------------------------------------------------------- #
# Tool catalog — description + JSON schema surface in the client UI.           #
# --------------------------------------------------------------------------- #
def _schema(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

TOOLS = [
    {
        "name": "brain_note",
        "description": "Append one typed memory entry to a scope's stream. Types: "
                       "decision, milestone, gotcha, dead-end, invariant, question, papercut. "
                       "The engine enforces caps, duplicate rejection, the routing "
                       "test, and the subsystem requirement; a rejected write returns "
                       "the engine's teaching error so you can correct and retry.",
        "builder": _t_brain_note,
        "inputSchema": _schema({
            "project": {**_STR, "description": "scope slug (default: global)"},
            "type": {**_STR, "description": "decision | milestone | gotcha | "
                     "invariant | question | papercut"},
            "text": {**_STR, "description": "the entry, one line, within its cap"},
            "supersedes": {**_STR, "description": "entry number(s) this replaces, e.g. 7,9"},
            "subsystem": {**_STR, "description": "hazard/invariant label "
                          "(required for gotcha and invariant)"},
            "pin": {**_BOOL, "description": "mark an always-loaded identity pin "
                    "(invariant only, needs subsystem)"},
            "source": {**_STR, "description": "citation token: a commit sha, path, or url"},
            "distinct": {**_BOOL, "description": "override the near-duplicate gate: "
                         "this is a separate fact"},
            "force_type": {**_BOOL, "description": "override the fix-language gate "
                           "for a genuine gotcha"},
        }, ["type", "text"]),
    },
    {
        "name": "brain_wake",
        "description": "Orient on a scope: its root state summary, the newest raw "
                       "entries, and the pull map of what else to retrieve.",
        "builder": _t_brain_wake,
        "inputSchema": _schema({"scope": {**_STR, "description": "scope slug"}}, ["scope"]),
    },
    {
        "name": "brain_hazards",
        "description": "The gotchas and dead-ends for a scope, verbatim and grouped "
                       "by subsystem. Read this before touching a subsystem.",
        "builder": _t_brain_hazards,
        "inputSchema": _schema({
            "scope": {**_STR, "description": "scope slug"},
            "grep": {**_STR, "description": "only entries matching this regex"},
        }, ["scope"]),
    },
    {
        "name": "brain_design",
        "description": "The live invariants for a scope, verbatim and grouped by "
                       "subsystem. Read this before redesigning a subsystem.",
        "builder": _t_brain_design,
        "inputSchema": _schema({
            "scope": {**_STR, "description": "scope slug"},
            "grep": {**_STR, "description": "only entries matching this regex"},
        }, ["scope"]),
    },
    {
        "name": "brain_recall",
        "description": "Live rulings that bear on a topic, across all scopes — the "
                       "conversational gate before you act on a stated preference.",
        "builder": _t_brain_recall,
        "inputSchema": _schema({"text": {**_STR, "description": "the topic to recall on"}},
                               ["text"]),
    },
    {
        "name": "brain_decisions",
        "description": "The decision entries for a scope, verbatim, newest last.",
        "builder": _t_brain_decisions,
        "inputSchema": _schema({
            "scope": {**_STR, "description": "scope slug"},
            "n": {**_INT, "description": "keep only the most recent N"},
        }, ["scope"]),
    },
    {
        "name": "brain_grep",
        "description": "Regex search over the raw streams, optionally limited to one scope.",
        "builder": _t_brain_grep,
        "inputSchema": _schema({
            "regex": {**_STR, "description": "the pattern to search for"},
            "scope": {**_STR, "description": "limit to one scope (default: all)"},
        }, ["regex"]),
    },
    {
        "name": "brain_resolve",
        "description": "Retire hazard #N in a scope: appends a milestone that "
                       "supersedes it, dropping it from the live index while the "
                       "stream keeps the original.",
        "builder": _t_brain_resolve,
        "inputSchema": _schema({
            "project": {**_STR, "description": "scope slug"},
            "n": {**_INT, "description": "the hazard entry number to resolve"},
            "text": {**_STR, "description": "how it was fixed"},
        }, ["project", "n", "text"]),
    },
    {
        "name": "brain_papercuts",
        "description": "Open papercuts clustered by recurrence — the small annoyances "
                       "a repeat of which becomes a ticket.",
        "builder": _t_brain_papercuts,
        "inputSchema": _schema({"scope": {**_STR, "description": "one scope (default: all)"}},
                               []),
    },
    {
        "name": "tickets_board",
        "description": "Print a ticket board as a table. The root is the board's "
                       "folder — the folder is the deployment.",
        "builder": _t_tickets_board,
        "inputSchema": _schema({"root": {**_STR, "description": "the board's folder"}},
                               ["root"]),
    },
    {
        "name": "tickets_open",
        "description": "Open a new ticket on a board.",
        "builder": _t_tickets_open,
        "inputSchema": _schema({
            "root": {**_STR, "description": "the board's folder"},
            "title": {**_STR, "description": "what the ticket is about"},
            "prefix": {**_STR, "description": "file it under a different short code "
                       "than the board's own"},
            "owner": {**_STR, "description": "who will do it (default: nobody yet)"},
            "due": {**_STR, "description": "an outside deadline, not a work schedule"},
        }, ["root", "title"]),
    },
    {
        "name": "tickets_update",
        "description": "Post the latest one-line status of a ticket.",
        "builder": _t_tickets_update,
        "inputSchema": _schema({
            "root": {**_STR, "description": "the board's folder"},
            "ticket": {**_STR, "description": "a number, a PREFIX-3 key, or its id"},
            "text": {**_STR, "description": "where the ticket stands right now"},
        }, ["root", "ticket", "text"]),
    },
    {
        "name": "tickets_close",
        "description": "Close a ticket. A source is required — evidence it is done, "
                       "or person:<you> if you are vouching.",
        "builder": _t_tickets_close,
        "inputSchema": _schema({
            "root": {**_STR, "description": "the board's folder"},
            "ticket": {**_STR, "description": "a number, a PREFIX-3 key, or its id"},
            "source": {**_STR, "description": "the proof it is done"},
        }, ["root", "ticket", "source"]),
    },
    {
        "name": "registers_board",
        "description": "Print a register as a table. The root is the register's "
                       "folder — the folder is the deployment.",
        "builder": _t_registers_board,
        "inputSchema": _schema({"root": {**_STR, "description": "the register's folder"}},
                               ["root"]),
    },
    {
        "name": "registers_show",
        "description": "Show one record in a register in full.",
        "builder": _t_registers_show,
        "inputSchema": _schema({
            "root": {**_STR, "description": "the register's folder"},
            "key": {**_STR, "description": "the record: its key, a PREFIX-3 key, or its id"},
        }, ["root", "key"]),
    },
    {
        "name": "registers_post",
        "description": "Post a tracked value for a record for one period (a year or "
                       "date). The register's config decides which fields are trackable.",
        "builder": _t_registers_post,
        "inputSchema": _schema({
            "root": {**_STR, "description": "the register's folder"},
            "key": {**_STR, "description": "the record: its key, a PREFIX-3 key, or its id"},
            "field": {**_STR, "description": "which tracked field"},
            "value": {**_STR, "description": "the value for this period"},
            "asof": {**_STR, "description": "the period it is for, e.g. 2025 or FY25"},
        }, ["root", "key", "field", "value"]),
    },
    {
        "name": "registers_add",
        "description": "Add a new record to a register from a set of field=value pairs.",
        "builder": _t_registers_add,
        "inputSchema": _schema({
            "root": {**_STR, "description": "the register's folder"},
            "fields": {"type": "object", "description": "field=value pairs, "
                       "e.g. {\"code\": \"ACME\", \"name\": \"Acme Ltd\"}"},
        }, ["root", "fields"]),
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def _public_tool(t: dict) -> dict:
    return {"name": t["name"], "description": t["description"],
            "inputSchema": t["inputSchema"]}


# --------------------------------------------------------------------------- #
# JSON-RPC plumbing                                                            #
# --------------------------------------------------------------------------- #
def _result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(rpc_id, code, message):
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _call_tool(name: str, arguments: dict) -> dict:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True}
    try:
        script, argv = tool["builder"](arguments or {})
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    is_error, text = _run_cli(script, argv)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _handle(msg: dict):
    """Return a response dict, or None for a notification (no reply)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None,
                      -32600, "invalid request")
    method = msg.get("method")
    rpc_id = msg.get("id")
    is_notification = "id" not in msg
    params = msg.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        return _result(rpc_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _result(rpc_id, {})
    if method == "tools/list":
        return _result(rpc_id, {"tools": [_public_tool(t) for t in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        return _result(rpc_id, _call_tool(name, arguments))

    # Any other notification: silently ignore. Any other request: method-not-found.
    if is_notification:
        return None
    return _error(rpc_id, -32601, f"method not found: {method}")


def _setup_stdio():
    """Force unbuffered UTF-8 line-delimited stdio (Windows code pages otherwise
    corrupt non-ASCII bytes in a message)."""
    try:
        sys.stdin.reconfigure(encoding="utf-8", newline="\n")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n", write_through=True)
    except AttributeError:  # very old Python without reconfigure — rewrap
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="\n")
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      newline="\n", write_through=True)


def _send(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve():
    _setup_stdio()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _send(_error(None, -32700, "parse error"))
            continue
        try:
            response = _handle(msg)
        except Exception as e:  # never let one bad message kill the server
            response = _error(msg.get("id") if isinstance(msg, dict) else None,
                              -32603, f"internal error: {e}")
        if response is not None:
            _send(response)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cli_root = None
    rest = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            cli_root = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if arg.startswith("--root="):
            cli_root = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    if rest:
        print(f"error: unrecognized arguments: {' '.join(rest)} "
              "(the server takes only --root DIR)", file=sys.stderr)
        return 2
    # Pin the resolved root into the environment so every brain.py subprocess
    # resolves the same instance (the board tools carry their own --root).
    os.environ["BRAIN_ROOT"] = _resolve_root(cli_root)
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
