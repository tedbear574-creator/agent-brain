#!/usr/bin/env python3
"""install.py — stand up an Agent Brain instance and wire Claude Code hooks.

Stdlib only. Two things happen:

  1. A data root is created (via `brain init`) and named in a config file next
     to brain.py (brain.config.json) so every tool resolves the same instance.
  2. The six Claude Code hooks are COPIED into a hooks dir (default
     ~/.claude/hooks) — over an existing install, idempotently, reporting which
     changed — and a config is written beside them so the copied hooks resolve
     the data root and brain.py from config rather than from their own location.
  3. The hooks are wired into a settings.json — merged in safely with a
     timestamped backup, or printed for you to paste if you prefer.

Nothing here is destructive: an existing settings.json is backed up before any
change, existing hook entries we own are replaced in place rather than
duplicated, and a hook file is rewritten only when its bytes actually differ.
Re-running is safe.

Usage:
    python install/install.py [--root DIR] [--name NAME] [--no-git]
                              [--hooks-dest DIR] [--settings PATH]
                              [--print-only]

--root       where the instance data lives (default: ./brain next to brain.py)
--name       a human label for the deployment, stored in the config
--no-git     create the data root without a git repo (folder-sync profile)
--hooks-dest where the six hooks are installed (default: ~/.claude/hooks)
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
DEFAULT_HOOKS_DEST = os.path.expanduser(os.path.join("~", ".claude", "hooks"))
# The six hook files that make up the enforcement layer (copied verbatim).
HOOK_FILES = [script for _, script in [
    ("SessionStart", "kb_card_inject.py"),
    ("UserPromptSubmit", "kb_topic_inject.py"),
    ("PostToolUse", "kb_hazard_inject.py"),
    ("Stop", "kb_stop_guard.py"),
    ("PreToolUse", "kb_write_authority.py"),
    ("SessionEnd", "kb_session_log.py"),
]]

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


def _hook_command(script: str, hooks_dir: str = HOOKS_DIR) -> str:
    # Forward slashes on every OS: the Claude Code hook runner treats backslashes
    # in a settings.json command as escape sequences, so a Windows path with `\U`
    # or `\k` would break. Forward slashes work on Windows too.
    py = (sys.executable or "python").replace("\\", "/")
    script_path = os.path.join(hooks_dir, script).replace("\\", "/")
    return f'"{py}" "{script_path}"'


def _hook_entry(event: str, script: str, hooks_dir: str = HOOKS_DIR) -> dict:
    entry = {"hooks": [{"type": "command", "command": _hook_command(script, hooks_dir)}]}
    if event in HOOK_MATCHERS:
        entry["matcher"] = HOOK_MATCHERS[event]
    return entry


def _is_ours(hook_block: dict, hooks_dir: str = HOOKS_DIR) -> bool:
    hooks_fs = hooks_dir.replace("\\", "/")
    for h in hook_block.get("hooks", []):
        cmd = h.get("command", "").replace("\\", "/")
        if hooks_fs in cmd:
            return True
    return False


def _merge_hooks(settings: dict, hooks_dir: str = HOOKS_DIR) -> dict:
    hooks = settings.setdefault("hooks", {})
    for event, script in HOOKS:
        bucket = hooks.setdefault(event, [])
        # Drop any prior entry we own (idempotent re-install), keep the rest.
        bucket[:] = [b for b in bucket if not _is_ours(b, hooks_dir)]
        bucket.append(_hook_entry(event, script, hooks_dir))
    return settings


def copy_hooks(dest_dir: str) -> list:
    """Copy the six hooks into *dest_dir*, overwriting an existing install.
    Idempotent: a file is rewritten only when its bytes differ. Returns the
    list of hook filenames that were created or changed (empty on a no-op
    re-run). No symlinks — a copy is portable across Windows and syncs."""
    src_dir = os.path.abspath(HOOKS_DIR)
    dest_dir = os.path.abspath(dest_dir)
    if dest_dir == src_dir:
        return []  # the package hooks are already canonical in place
    os.makedirs(dest_dir, exist_ok=True)
    changed = []
    for script in HOOK_FILES:
        src = os.path.join(src_dir, script)
        dst = os.path.join(dest_dir, script)
        try:
            with open(src, "rb") as f:
                src_bytes = f.read()
            with open(dst, "rb") as f:
                dst_bytes = f.read()
        except OSError:
            dst_bytes = None
        if dst_bytes != src_bytes:
            shutil.copyfile(src, dst)
            changed.append(script)
    return changed


def write_hooks_config(dest_dir: str, root: str, name: str) -> None:
    """Write the config the COPIED hooks resolve from. They read it from
    <parent-of-hooks-dir>/brain.config.json (their _package_dir), so a copy
    installed away from the engine still finds the data root and brain.py via
    config — never by assuming the engine sits one dir up from them."""
    dest_dir = os.path.abspath(dest_dir)
    if dest_dir == os.path.abspath(HOOKS_DIR):
        return  # in place: the package config already covers it
    cfg_path = os.path.join(os.path.dirname(dest_dir), "brain.config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except ValueError:
            cfg = {}
    cfg["root"] = os.path.abspath(os.path.expanduser(root))
    cfg["cli"] = BRAIN_PY
    if name:
        cfg["name"] = name
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"wrote hooks config: {cfg_path} (root + cli for the copied hooks)")


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


def wire_settings(settings_path: str, print_only: bool, hooks_dir: str = HOOKS_DIR) -> None:
    snippet = {"hooks": {}}
    for event, script in HOOKS:
        snippet["hooks"].setdefault(event, []).append(_hook_entry(event, script, hooks_dir))
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
    _merge_hooks(settings, hooks_dir)
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
    ap.add_argument("--hooks-dest", dest="hooks_dest", default=DEFAULT_HOOKS_DEST,
                    help="where the six hooks are installed (default: ~/.claude/hooks)")
    ap.add_argument("--settings",
                    default=os.path.expanduser(os.path.join("~", ".claude", "settings.json")),
                    help="Claude Code settings.json to wire the hooks into")
    ap.add_argument("--print-only", dest="print_only", action="store_true",
                    help="print the hook snippet instead of editing settings.json")
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.root))
    run_init(root, args.no_git)
    write_config(root, args.name)

    hooks_dest = os.path.abspath(os.path.expanduser(args.hooks_dest))
    if not args.print_only:
        changed = copy_hooks(hooks_dest)
        write_hooks_config(hooks_dest, root, args.name)
        if os.path.abspath(hooks_dest) == os.path.abspath(HOOKS_DIR):
            print(f"hooks: package copies are canonical in place ({HOOKS_DIR})")
        elif changed:
            print(f"hooks: copied {len(changed)} to {hooks_dest} "
                  f"(changed: {', '.join(changed)})")
        else:
            print(f"hooks: {hooks_dest} already up to date (no changes)")
        wire_settings(args.settings, args.print_only, hooks_dest)
    else:
        wire_settings(args.settings, args.print_only, hooks_dest)
    print("\ndone. Verify with:  python brain.py doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
