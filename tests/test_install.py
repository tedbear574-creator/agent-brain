"""Installer guards: settings.json wiring is safe, idempotent, forward-slashed."""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
INSTALL_PY = os.path.join(ROOT_DIR, "install", "install.py")

_spec = importlib.util.spec_from_file_location("install", INSTALL_PY)
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


def test_hook_commands_use_forward_slashes():
    # The Claude Code hook runner treats backslashes as escapes — every command
    # path must be forward-slashed, even on Windows.
    for event, script in install.HOOKS:
        cmd = install._hook_command(script)
        assert "\\" not in cmd, f"{script}: {cmd}"
        assert script in cmd


def test_all_six_hooks_are_wired():
    events = {e for e, _ in install.HOOKS}
    assert events == {"SessionStart", "UserPromptSubmit", "PostToolUse",
                      "Stop", "PreToolUse", "SessionEnd"}


def test_merge_is_idempotent_and_preserves_foreign_hooks(tmp_path):
    settings = {"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "echo keep-me"}]},
    ]}}
    install._merge_hooks(settings)
    install._merge_hooks(settings)          # second pass must not duplicate
    stop = settings["hooks"]["Stop"]
    ours = [b for b in stop if install._is_ours(b)]
    foreign = [b for b in stop if not install._is_ours(b)]
    assert len(ours) == 1                    # exactly one of ours, not two
    assert len(foreign) == 1                 # the pre-existing hook survived
    assert foreign[0]["hooks"][0]["command"] == "echo keep-me"


def test_wire_settings_backs_up_and_writes(tmp_path):
    sp = tmp_path / "settings.json"
    sp.write_text(json.dumps({"hooks": {}, "other": 1}), encoding="utf-8")
    install.wire_settings(str(sp), print_only=False)
    written = json.loads(sp.read_text(encoding="utf-8"))
    assert "SessionStart" in written["hooks"]
    assert written["other"] == 1             # untouched keys preserved
    backups = [p for p in os.listdir(tmp_path) if p.startswith("settings.json.bak-")]
    assert backups, "a backup must be written before mutating settings.json"


def test_copy_hooks_is_idempotent_and_reports_changes(tmp_path):
    dest = tmp_path / "hooks"
    # First run: all six land and are reported as changed.
    first = install.copy_hooks(str(dest))
    assert set(first) == set(install.HOOK_FILES)
    for script in install.HOOK_FILES:
        assert (dest / script).exists()
        assert (dest / script).read_bytes() == \
            (open(os.path.join(install.HOOKS_DIR, script), "rb").read())
    # Second run over the same install: nothing differs, nothing reported.
    assert install.copy_hooks(str(dest)) == []
    # A drifted copy is re-synced and reported; the others stay quiet.
    (dest / "kb_stop_guard.py").write_text("drifted\n", encoding="utf-8")
    assert install.copy_hooks(str(dest)) == ["kb_stop_guard.py"]


def test_write_hooks_config_points_copied_hooks_at_root_and_cli(tmp_path):
    dest = tmp_path / "sub" / "hooks"
    dest.mkdir(parents=True)
    install.write_hooks_config(str(dest), root=str(tmp_path / "data"), name="demo")
    # The copied hooks resolve config from <parent-of-hooks-dir>/brain.config.json.
    cfg_path = tmp_path / "sub" / "brain.config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["cli"] == install.BRAIN_PY
    assert os.path.abspath(cfg["root"]) == os.path.abspath(str(tmp_path / "data"))


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
