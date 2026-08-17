#!/usr/bin/env python3
"""install.py — stand up an Agent Brain instance and wire Claude Code hooks.

Stdlib only. Two things happen:

  1. A data root is created (via `brain init`) and named in a config file next
     to brain.py (brain.config.json) so every tool resolves the same instance.
  2. The six Claude Code hooks are wired into a settings.json — merged in safely
     with a timestamped backup, or printed for you to paste if you prefer.

Nothing here is destructive: an existing settings.json is backed up before any
change, and existing hook entries pointing at this package are replaced in place
rather than duplicated. Re-running is safe.

Usage:
    python install/install.py [--root DIR] [--name NAME] [--no-git]
                              [--settings PATH] [--print-only]

--root       where the instance data lives (default: ./brain next to brain.py)
--name       a human label for the deployment, stored in the config
--no-git     create the data root without a git repo (folder-sync profile)
--settings   the Claude Code settings.json to wire (default: ~/.claude/settings.json)
--print-only don't touch settings.json — just print the hook snippet to paste
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAIN_PY = os.path.join(PACKAGE_DIR, "brain.py")
HOOKS_DIR = os.path.join(PACKAGE_DIR, "hooks")
CONFIG_PATH = os.path.join(PACKAGE_DIR, "brain.config.json")

# (event, script) — the enforcement layer. Paths are absolute so a hook works
# regardless of the session's cwd.
HOOKS = [
    ("SessionStart", "kb_card_inject.py"),
    ("UserPromptSubmit", "kb_topic_inject.py"),
    ("PostToolUse", "kb_hazard_inject.py"),
    ("Stop", "kb_stop_guard.py"),
    ("PreToolUse", "kb_write_authority.py"),
    ("SessionEnd", "kb_session_log.py"),
]
# PostToolUse / PreToolUse are scoped to the write tools; the others run on every
# firing of their event.
HOOK_MATCHERS = {
    "PostToolUse": "Write|Edit|NotebookEdit",
    "PreToolUse": "Write|Edit|NotebookEdit|MultiEdit",
}


def _hook_command(script: str) -> str:
    py = sys.executable or "python"
    return f'"{py}" "{os.path.join(HOOKS_DIR, script)}"'


def _hook_entry(event: str, script: str) -> dict:
    entry = {"hooks": [{"type": "command", "command": _hook_command(script)}]}
    if event in HOOK_MATCHERS:
        entry["matcher"] = HOOK_MATCHERS[event]
    return entry


def _is_ours(hook_block: dict) -> bool:
    for h in hook_block.get("hooks", []):
        cmd = h.get("command", "")
        if HOOKS_DIR in cmd or "brain" in os.path.basename(cmd):
            return True
    return False


def _merge_hooks(settings: dict) -> dict:
    hooks = settings.setdefault("hooks", {})
    for event, script in HOOKS:
        bucket = hooks.setdefault(event, [])
        # Drop any prior entry we own (idempotent re-install), keep the rest.
        bucket[:] = [b for b in bucket if not _is_ours(b)]
        bucket.append(_hook_entry(event, script))
    return settings


def write_config(root: str, name: str) -> None:
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except ValueError:
            cfg = {}
    cfg["root"] = root
    if name:
        cfg["name"] = name
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"wrote config: {CONFIG_PATH} (root={root}"
          + (f", name={name}" if name else "") + ")")


def run_init(root: str, no_git: bool) -> None:
    env = dict(os.environ, BRAIN_ROOT=os.path.abspath(os.path.expanduser(root)))
    cmd = [sys.executable, BRAIN_PY, "init"] + (["--no-git"] if no_git else [])
    subprocess.run(cmd, env=env, check=True)


def wire_settings(settings_path: str, print_only: bool) -> None:
    snippet = {"hooks": {}}
    for event, script in HOOKS:
        snippet["hooks"].setdefault(event, []).append(_hook_entry(event, script))
    if print_only:
        print("\n--- paste into your Claude Code settings.json ---")
        print(json.dumps(snippet, indent=2))
        print("--- end ---")
        return
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except ValueError:
            print(f"! {settings_path} is not valid JSON — printing the snippet instead.",
                  file=sys.stderr)
            print(json.dumps(snippet, indent=2))
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{settings_path}.bak-{stamp}"
        shutil.copy(settings_path, backup)
        print(f"backed up settings: {backup}")
    else:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    _merge_hooks(settings)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"wired {len(HOOKS)} hooks into {settings_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Install an Agent Brain instance.")
    ap.add_argument("--root", default=os.path.join(PACKAGE_DIR, "brain"),
                    help="data root (default: ./brain next to brain.py)")
    ap.add_argument("--name", default="", help="human label for this deployment")
    ap.add_argument("--no-git", dest="no_git", action="store_true",
                    help="folder-sync profile: create the data root without git")
    ap.add_argument("--settings",
                    default=os.path.expanduser(os.path.join("~", ".claude", "settings.json")),
                    help="Claude Code settings.json to wire the hooks into")
    ap.add_argument("--print-only", dest="print_only", action="store_true",
                    help="print the hook snippet instead of editing settings.json")
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.root))
    run_init(root, args.no_git)
    write_config(root, args.name)
    wire_settings(args.settings, args.print_only)
    print("\ndone. Verify with:  python brain.py doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
