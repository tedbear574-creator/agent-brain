"""Structural guards for the package tree.

Two invariants the package must hold forever:
  1. No INSTANCE DATA inside the repo — a `brain/` data folder, stream logs,
     spool files, or the transcript DB must never be committed. The repo is the
     vehicle; the instance is created per deployment, never shipped.
  2. No personal-infrastructure references — a stranger cloning this must not
     find the origin instance's paths, tool names, or scopes anywhere.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories/paths that would mean instance data leaked into the package tree.
FORBIDDEN_PATHS = [
    "brain",              # the default data root
    "_state",             # runtime spool dir
]
FORBIDDEN_FILE_RX = re.compile(
    r"(^|/)(stream|tree)/.*\.log$|brain-history\.db$|brain-spend\.jsonl$|"
    r"log-audit\.txt$|brain\.config\.json$")

# Tokens that name the origin instance's private infrastructure. `.gitignore`
# legitimately names the data-root basename `brain/`, so it is exempt from the
# path scan; these tokens, though, must appear NOWHERE.
PERSONAL_TOKENS = ["Users\\tod", "Users/tod", "global-kb", "okf-tools", "ccdesk", "cockpit",
                   "family-estate", "wealth-plan", "wholesale-ops", "networth"]

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "brain", "_state"}


def _iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def test_no_instance_data_dir_in_tree():
    for name in FORBIDDEN_PATHS:
        assert not os.path.isdir(os.path.join(ROOT, name)), (
            f"instance data dir '{name}/' must not exist in the package tree")


def test_no_spool_or_stream_files_in_tree():
    offenders = []
    for path in _iter_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if FORBIDDEN_FILE_RX.search(rel):
            offenders.append(rel)
    assert not offenders, f"instance spool/stream files in tree: {offenders}"


def test_no_personal_tokens_in_source():
    offenders = []
    for path in _iter_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        # Exempt: this guard names the tokens on purpose; .gitignore names the
        # data-root basename; SPEC.md is the internal founding design record that
        # deliberately names what STAYS at the origin instance (the publish step
        # drops it — it is not stranger-facing documentation).
        if rel in (".gitignore", "SPEC.md") or rel.startswith("tests/test_structure"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        for tok in PERSONAL_TOKENS:
            if tok in text:
                offenders.append(f"{rel}: {tok}")
    assert not offenders, f"personal-infrastructure tokens found: {offenders}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
