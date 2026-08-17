#!/usr/bin/env python3
"""SessionStart hook: inject the current scope's Agent Brain wake state.

When a session starts, this resolves the scope for the working directory and,
if that scope has a stream, injects `brain wake` (root state + newest raw tail
+ pull map). This is the read-side counterpart of the stop-gate capture duty:
capture is enforced, so orientation is provided.

This module also holds the SHARED instance-resolution helpers the other hooks
import (root/cli/cache/scope). Everything is instance-relative: the data root
comes from BRAIN_ROOT, then a `root` key in <package>/brain.config.json, then a
`brain/` folder next to the package — never a hardcoded home path.

Scope derivation:
  - If BRAIN_PROJECTS_ROOT is set and the path lives under it, the scope is the
    first path component under that root (multi-project deployments: one folder
    per project, folder name == scope).
  - Otherwise the scope is the basename of the directory (single-project
    deployments: the checkout IS the project).

Nothing is injected for a scope with no stream, so a fresh instance is silent
until it has memory. Always exits 0.
"""
import json
import os
import subprocess
import sys

# Windows console default is cp1252 — injected text may contain Unicode.
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


def resolve_root() -> str:
    env = os.environ.get("BRAIN_ROOT")
    if env and env.strip():
        return os.path.abspath(os.path.expanduser(env.strip()))
    cfg_root = _load_config().get("root")
    if cfg_root and str(cfg_root).strip():
        root = os.path.expanduser(str(cfg_root).strip())
        if not os.path.isabs(root):
            root = os.path.join(_package_dir(), root)
        return os.path.abspath(root)
    return os.path.join(_package_dir(), "brain")


KB_ROOT = resolve_root()
STREAM_DIR = os.path.join(KB_ROOT, "stream")
# BRAIN_CLI is env-overridable so a test harness can point at a workspace copy
# of brain.py without editing this file.
BRAIN_CLI = os.environ.get("BRAIN_CLI") or os.path.join(_package_dir(), "brain.py")
# Runtime sentinel cache (session-scoped, not instance data). Lives under the
# data root's _state/ dir unless BRAIN_CACHE overrides it.
CACHE_DIR = os.environ.get("BRAIN_CACHE") or os.path.join(KB_ROOT, "_state", "cache")
# Optional: parent of per-project folders. When set, a path under it resolves
# to <first-component> as the scope; when unset, scope == basename(dir).
PROJECTS_ROOT = os.environ.get("BRAIN_PROJECTS_ROOT")
WAKE_MAX_CHARS = 5000


def scope_for(slug):
    """Map a directory slug to a stream scope. Identity by default; a
    deployment can special-case names via BRAIN_SCOPE_ALIASES (name=scope,...)."""
    if not slug:
        return None
    aliases = os.environ.get("BRAIN_SCOPE_ALIASES", "")
    for pair in aliases.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() == slug:
                return v.strip()
    return slug


def scope_has_stream(scope) -> bool:
    return bool(scope) and os.path.isfile(os.path.join(STREAM_DIR, f"{scope}.log"))


def _slug_for(cwd):
    if not cwd:
        return None
    path = os.path.normpath(os.path.abspath(cwd))
    if PROJECTS_ROOT:
        root = os.path.normpath(os.path.abspath(os.path.expanduser(PROJECTS_ROOT)))
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            return None  # different drive
        rel = rel.replace("\\", "/")
        if rel.startswith("..") or rel == ".":
            return None
        return rel.split("/", 1)[0]
    base = os.path.basename(path)
    return base or None


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


def _wake_part(slug) -> str:
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
        "[BRAIN WAKE] This scope runs on the Agent Brain stream store. Capture inline the "
        "moment a decision, abandoned approach or gotcha lands:\n"
        f"  python \"{BRAIN_CLI}\" note --project {scope} --type "
        "<decision|milestone|gotcha|invariant|question|papercut> \"one line\"\n"
        "  Caps: 800 chars for decision/question (they carry the reasoning), 300 for the "
        "rest. gotcha and invariant ALSO require --subsystem <label>.\n"
        "Route before you write: a defect you would fix -> the ticket board (tickets.py), "
        "NOT a note; something the next session must know -> gotcha; neither, but it cost "
        "you time -> papercut.\n"
        "The state below is orientation only — it is deliberately lossy. Hazards are never "
        "summarized into it; pull them before touching a subsystem.\n\n"
        + _trim_wake(out, WAKE_MAX_CHARS)
    )


def main() -> None:
    if os.environ.get("BRAIN_NO_INJECT"):
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    cwd = data.get("cwd") or os.getcwd()
    slug = _slug_for(cwd)
    part = _wake_part(slug)
    if part:
        print(part)


if __name__ == "__main__":
    main()
