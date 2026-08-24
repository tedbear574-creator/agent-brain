#!/usr/bin/env python3
"""SessionStart hook: inject the current scope's Agent Brain wake state.

When a session starts, this injects, for INTERACTIVE sessions (a delegated
worker whose role env is set gets nothing ambient — its context is its brief):

  1. user.md — the user model, always, regardless of cwd (if the instance has
     one). Trimmed round-robin across its ## sections so none vanishes whole.
  2. For scopes with a stream: `brain wake` — root state + newest raw tail + a
     pull map. For scopes with only a card (a pointer stub for a not-yet-migrated
     scope): the card's ## Current State / ## Active Blockers.
  3. The home ticket board (tickets.py): tickets whose due date is today or past.
  4. inbox.md — phone/desk responses awaiting processing.

This module also holds the SHARED instance-resolution helpers the other hooks
import (root/cli/cache/scope/role). Everything is instance-relative: the data
root comes from BRAIN_ROOT, then a `root` key in the config, then a `brain/`
folder next to the package — never a hardcoded home path. Machine-specific
scope mapping (a projects root, directory aliases, extra-root slugs, the
delegated-role env name) comes from the config / environment too, so this file
installed verbatim into any deployment's hooks dir carries no origin paths.

Scope derivation:
  - If a projects root is configured and the path lives under it, the scope is
    the first path component under that root, after applying any directory
    aliases (multi-project deployments: one folder per project).
  - Extra-root slugs map specific absolute directories (outside the projects
    root) to a scope.
  - Otherwise the scope is the basename of the directory (single-project
    deployments: the checkout IS the project).

Nothing is injected for a scope with no stream and no card, so a fresh instance
is silent until it has memory. Always exits 0.
"""
import datetime
import json
import os
import subprocess
import sys

# Windows console default is cp1252 — injected text may contain Unicode (Δ, →, …).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# --- shared instance resolution (imported by the other hooks) --------------

def _package_dir() -> str:
    """The package root — hooks live in <package>/hooks/, so go up one."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config_path() -> str:
    return os.environ.get("BRAIN_CONFIG") or os.path.join(_package_dir(), "brain.config.json")


def _load_config() -> dict:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_CONFIG = _load_config()


def _abs_under_package(value: str) -> str:
    p = os.path.expanduser(str(value).strip())
    if not os.path.isabs(p):
        p = os.path.join(_package_dir(), p)
    return os.path.abspath(p)


def resolve_root() -> str:
    env = os.environ.get("BRAIN_ROOT")
    if env and env.strip():
        return os.path.abspath(os.path.expanduser(env.strip()))
    cfg_root = _CONFIG.get("root")
    if cfg_root and str(cfg_root).strip():
        return _abs_under_package(cfg_root)
    return os.path.join(_package_dir(), "brain")


def resolve_cli() -> str:
    """Path to brain.py. Env overrides config `cli` overrides brain.py next to
    the package. A deployment that COPIES these hooks into its own hooks dir
    (so <package> is no longer the engine) points `cli` at the real brain.py in
    its config — the hooks never assume the engine sits one dir up from them."""
    env = os.environ.get("BRAIN_CLI")
    if env and env.strip():
        return env.strip()
    cfg_cli = _CONFIG.get("cli")
    if cfg_cli and str(cfg_cli).strip():
        return _abs_under_package(cfg_cli)
    return os.path.join(_package_dir(), "brain.py")


KB_ROOT = resolve_root()
STREAM_DIR = os.path.join(KB_ROOT, "stream")
KB_PROJECTS = os.path.join(KB_ROOT, "projects")
USER_MD = os.path.join(KB_ROOT, "user.md")
INBOX_MD = os.path.join(KB_ROOT, "inbox.md")
# The home ticket board (tickets.py) — all tracked work lives here. Derived from
# the data root so an eval copy (BRAIN_ROOT) gets its own board.
TICKETS_ROOT = os.path.join(KB_ROOT, "tickets")
# BRAIN_CLI is env-overridable so a test harness can point at a workspace copy
# of brain.py without editing this file.
BRAIN_CLI = resolve_cli()
# Runtime sentinel cache (session-scoped, not instance data). Lives under the
# data root's _state/ dir unless BRAIN_CACHE overrides it.
CACHE_DIR = os.environ.get("BRAIN_CACHE") or os.path.join(KB_ROOT, "_state", "cache")

USER_MAX_CHARS = 3000       # user.md's budget — chars, not lines (lines were gameable)
MAX_INJECT_CHARS = 3000     # card body cap
WAKE_MAX_CHARS = 5000       # safety net; `brain wake` is already self-limiting


def _parse_map(raw) -> dict:
    """Accept a dict, or an env-style "k=v,k=v" string, and return a str->str map."""
    out: dict = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k is not None and v is not None:
                out[str(k)] = str(v)
    elif isinstance(raw, str):
        for pair in raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
    return out


# Optional: parent of per-project folders. A path under it resolves to
# <first-component> (after aliasing) as the scope; unset -> scope == basename.
_pr = os.environ.get("BRAIN_PROJECTS_ROOT") or _CONFIG.get("projects_root")
PROJECTS_ROOT = os.path.abspath(os.path.expanduser(str(_pr).strip())) if _pr else None

# rel-path-under-root (posix, lowercase) -> scope. Longest key wins. Legacy
# directory renames and nested checkouts live here, in the (gitignored,
# machine-specific) config — never hardcoded in this shipped file.
DIR_ALIASES = {k.lower(): v for k, v in
               _parse_map(_CONFIG.get("dir_aliases")).items()}
DIR_ALIASES.update({k.lower(): v for k, v in
                    _parse_map(os.environ.get("BRAIN_DIR_ALIASES")).items()})

# Absolute dirs OUTSIDE the projects root that still carry a scope. Checked
# before the projects-root walk.
EXTRA_ROOT_SLUGS = {os.path.normcase(os.path.normpath(os.path.expanduser(k))): v
                    for k, v in _parse_map(_CONFIG.get("extra_root_slugs")).items()}
EXTRA_ROOT_SLUGS.update({os.path.normcase(os.path.normpath(os.path.expanduser(k))): v
                         for k, v in _parse_map(os.environ.get("BRAIN_EXTRA_ROOT_SLUGS")).items()})

# name -> scope aliases (e.g. the assistant-home dir -> "global").
SCOPE_ALIASES = _parse_map(_CONFIG.get("scope_aliases"))
SCOPE_ALIASES.update(_parse_map(os.environ.get("BRAIN_SCOPE_ALIASES")))

# The env var whose presence marks a delegated worker (issue owner, loop lane,
# fusion, verify). Injection is suppressed for it and the stop guard exempts it.
ROLE_ENV = (os.environ.get("BRAIN_ROLE_ENV") or _CONFIG.get("role_env")
            or "CCDESK_ROLE")


def role_value() -> str:
    return (os.environ.get(ROLE_ENV) or "").strip()


def role_is_delegated() -> bool:
    return bool(role_value())


def scope_for(slug):
    """Map a directory slug to a stream scope. Identity unless a scope alias
    maps it (e.g. the assistant-home folder -> the global scope)."""
    if not slug:
        return None
    return SCOPE_ALIASES.get(slug, slug)


def scope_has_stream(scope) -> bool:
    return bool(scope) and os.path.isfile(os.path.join(STREAM_DIR, f"{scope}.log"))


def _slug_for(cwd):
    if not cwd:
        return None
    path = os.path.normpath(os.path.abspath(cwd))
    ncpath = os.path.normcase(path)
    # Extra roots first (dirs that live outside the projects root).
    for root, slug in EXTRA_ROOT_SLUGS.items():
        if ncpath == root or ncpath.startswith(root + os.sep):
            return slug
    if PROJECTS_ROOT:
        try:
            rel = os.path.relpath(path, PROJECTS_ROOT)
        except ValueError:
            return None  # different drive
        rel = rel.replace("\\", "/")
        if rel.startswith("..") or rel == ".":
            return None
        rel_l = rel.lower()
        # longest-prefix match against the alias map
        best = None
        for key, slug in DIR_ALIASES.items():
            if rel_l == key or rel_l.startswith(key + "/"):
                if best is None or len(key) > len(best[0]):
                    best = (key, slug)
        if best:
            return best[1]
        return rel.split("/", 1)[0]
    base = os.path.basename(path)
    return base or None


# --- trimming helpers -------------------------------------------------------

def _trim_wake(out: str, budget: int) -> str:
    """Trim the ROOT, never the tail. `brain wake` emits root, then `-- ...--`
    blocks holding the newest raw entries and the pull map — the freshest and
    most actionable material sits at the END, so a head-first cut discards
    exactly what should survive."""
    if len(out) <= budget:
        return out
    lines = out.splitlines()
    marks = [i for i, ln in enumerate(lines) if ln.startswith("-- ")]
    first = marks[0] if marks else len(lines)
    head, tail = lines[:first], lines[first:]
    tail_chars = sum(len(ln) + 1 for ln in tail)
    if tail_chars >= budget - 120:
        kept = []
        for ln in reversed(tail):
            if sum(len(k) + 1 for k in kept) + len(ln) > budget - 120:
                break
            kept.insert(0, ln)
        return "…(root omitted to fit — `brain wake` for the full state)\n" + "\n".join(kept)
    room = budget - tail_chars - 160
    kept_head = []
    for ln in head:
        if sum(len(k) + 1 for k in kept_head) + len(ln) > room:
            break
        kept_head.append(ln)
    dropped = len(head) - len(kept_head)
    return (
        "\n".join(kept_head)
        + f"\n…({dropped} root line(s) trimmed to fit — `brain wake` for all; the newest "
          "entries and the pull map below are never trimmed)\n"
        + "\n".join(tail)
    )


def _fair_trim(body: str, budget: int):
    """Trim to budget by round-robin across ## sections, dropping each section's
    LAST lines. A straight head-cut silently deletes whole trailing sections —
    which is how a routing section kept disappearing while the disposition
    bullets above it survived intact. Returns (trimmed, dropped_line_count)."""
    sections = []
    cur = {"head": None, "lines": []}
    for ln in body.split("\n"):
        if ln.startswith("## "):
            sections.append(cur)
            cur = {"head": ln, "lines": []}
        elif ln.strip():
            cur["lines"].append(ln)
    sections.append(cur)
    for s in sections:
        s["keep"] = []
    used = sum(len(s["head"]) + 2 for s in sections if s["head"])
    preamble = sections[0]
    queues = []
    if len(sections) > 1:
        for ln in preamble["lines"]:  # the budget rule itself is mandatory
            preamble["keep"].append(ln)
            used += len(ln) + 1
    else:
        queues.append((preamble, list(preamble["lines"])))  # no sections: plain trim
    for s in sections[1:]:
        queues.append((s, list(s["lines"])))
    progress = True
    while progress:
        progress = False
        for s, q in queues:
            if not q:
                continue
            if used + len(q[0]) + 1 > budget:
                continue
            s["keep"].append(q.pop(0))
            used += len(s["keep"][-1]) + 1
            progress = True
    dropped = sum(len(q) for _, q in queues)
    out = []
    for s in sections:
        if s["head"]:
            out.append("")
            out.append(s["head"])
        out.extend(s["keep"])
    return "\n".join(out).strip("\n"), dropped


# --- file / section helpers -------------------------------------------------

def _read(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _extract_sections(text: str, wanted) -> str:
    """Return the bodies of the wanted H2 sections (## Name ... next ##)."""
    out = []
    keep = False
    for ln in text.splitlines():
        if ln.startswith("## "):
            keep = ln[3:].strip() in wanted
            if keep:
                out.append(ln)
            continue
        if keep:
            out.append(ln)
    return "\n".join(out).strip()


# --- the four injected parts ------------------------------------------------

def _user_part():
    text = _read(USER_MD)
    if not text:
        return None
    body = _strip_frontmatter(text).strip()
    if not body:
        return None
    if len(body) > USER_MAX_CHARS:
        over = len(body) - USER_MAX_CHARS
        body, dropped = _fair_trim(body, USER_MAX_CHARS)
        if dropped:
            body += (
                f"\n\n…({dropped} line(s) withheld — user.md is {over} chars over its "
                f"{USER_MAX_CHARS}-char budget; the tail of EACH section was trimmed, so "
                f"every section is represented but none is complete. Hygiene it: evict weak "
                f"facts, do NOT merge facts onto one line.)"
            )
    return (f"[BRAIN USER] User model (user.md — agent-curated, {USER_MAX_CHARS}-char "
            f"budget):\n\n{body}")


def _ticket_part():
    """Tickets on the home board (tickets.py) whose due date is today or past.
    Due-ness is the only filter — no project scoping. Injects nothing if the
    board root is missing or unreadable (a machine without a board sees silence,
    never a traceback)."""
    if not os.path.isdir(TICKETS_ROOT):
        return None
    try:
        engine_dir = os.path.dirname(os.path.abspath(BRAIN_CLI))  # tickets.py sits beside brain.py
        if engine_dir not in sys.path:
            sys.path.insert(0, engine_dir)
        import tickets
        cfg = tickets.load_config(TICKETS_ROOT)
        state = tickets.board_state(TICKETS_ROOT)
    except Exception:
        return None
    prefix = cfg.get("prefix", "")
    today = datetime.date.today().isoformat()
    due = [t for t in state.values()
           if t["status"] != "closed" and t["due"] and t["due"] <= today]
    due.sort(key=lambda t: (t["due"], t["n"], t["nsfx"]))
    if not due:
        return None
    rows = []
    for t in due:
        owner = t["owner"] or "nobody yet"
        rows.append(f"- {tickets.key_of(prefix, t)} (due {t['due']}, {owner}) {t['title']}")
    return (
        "[BRAIN BOARD] Tickets due or overdue on the work board (tickets.py) — close "
        "each when done (a close needs a source), or push its due date if it is "
        "still waiting:\n" + "\n".join(rows)
    )


def _inbox_part():
    """Pending phone→desk responses (an inbox writer appends `- ` lines)."""
    text = _read(INBOX_MD)
    if not text:
        return None
    lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    if not lines:
        return None
    return (
        "[BRAIN INBOX] Responses awaiting processing (inbox.md — act on each, then "
        "delete the line):\n" + "\n".join(lines)
    )


def _wake_part(slug):
    scope = scope_for(slug)
    if not scope_has_stream(scope):
        return None
    try:
        r = subprocess.run(
            [sys.executable, BRAIN_CLI, "wake", "--scope", scope],
            capture_output=True, timeout=15,
        )
        out = r.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        out = ""
    if not out:
        return None
    return (
        "[BRAIN WAKE] This scope runs on the Agent Brain stream store (stream + tree), "
        "not a card. Capture inline the moment a decision, abandoned approach or gotcha "
        "lands:\n"
        f"  python \"{BRAIN_CLI}\" note --project {scope} --type "
        "<decision|milestone|gotcha|invariant|question|papercut> \"one line\"\n"
        "  Caps: 800 chars for decision/question (they carry the reasoning), 300 for the "
        "rest. Reasoning that will not fit goes in projects/<scope>/<topic>.md, with the "
        "ruling noted here pointing at it.\n"
        "  gotcha and invariant ALSO require --subsystem <label> — it is the hazard "
        "index's only retrieval axis, so the command fails without it.\n"
        "Route before you write: a defect you would fix -> a TICKET on the work board "
        "(tickets.py open, NOT a note); something the next session must know to work "
        "correctly -> gotcha; neither, but it cost you time -> papercut (duplicates count "
        "instead of blocking; recurrence is what promotes it to the board).\n"
        "Starting on a new subject? Pin its identity facts so they load every wake:\n"
        f"  python \"{BRAIN_CLI}\" note --project {scope} --type invariant --pin "
        "--subsystem identity \"...\"\n"
        f"Fixed a known gotcha? Retire it: brain resolve --project {scope} N \"how\".\n"
        "A near-duplicate of a live entry is rejected: reconcile it with --supersedes N, "
        "or pass --distinct if it truly is a separate fact.\n"
        "The state below is orientation only — it is deliberately lossy. Hazards are never "
        "summarized into it; pull them before touching a subsystem.\n\n"
        + _trim_wake(out, WAKE_MAX_CHARS)
    )


def _card_part(slug):
    if not slug:
        return None
    card = os.path.join(KB_PROJECTS, f"{slug}.md")
    text = _read(card) if os.path.isfile(card) else None
    if not text:
        return None
    body = _extract_sections(text, ("Current State", "Active Blockers", "Superseded By"))
    if not body:
        return None
    if len(body) > MAX_INJECT_CHARS:
        body = body[:MAX_INJECT_CHARS] + "\n…(truncated — read the card)"
    return (
        f"[BRAIN CARD] Canonical card for this project: projects/{slug}.md — trust it, "
        f"fix stale claims on sight, update it before ending a code-changing session.\n\n{body}"
    )


def main() -> None:
    # Role gate: a delegated worker gets NOTHING ambient — its context is the
    # brief, curated by the dispatching parent.
    if role_is_delegated():
        return
    # Eval control arm: a control session must provably receive NO brain context,
    # or the measured effect is confounded.
    if os.environ.get("BRAIN_NO_INJECT"):
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    cwd = data.get("cwd") or os.getcwd()
    slug = _slug_for(cwd)

    context_part = _wake_part(slug) or _card_part(slug)
    parts = [p for p in (_user_part(), context_part, _ticket_part(), _inbox_part()) if p]
    if parts:
        print("\n\n".join(parts))


if __name__ == "__main__":
    main()
