"""Merge-gate suite for brain.py — write gate (§2), papercut type (§3), and the
the fail-loud dispatch stub + wake's ticket-board section (§4).

Every test runs against a throwaway BRAIN_ROOT built by the `root` fixture — the
real data root is never touched. Commands are driven end to end via
subprocess so argparse wiring and exit codes are exactly what the reviewer
re-runs. A handful of pure-function checks import brain directly.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import importlib.util

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)               # package root (brain.py, hooks/, tickets.py)
BRAIN = os.path.join(ROOT_DIR, "brain.py")
HAZ_HOOK = os.path.join(ROOT_DIR, "hooks", "kb_hazard_inject.py")
# The hazard hook resolves a path to a scope as the first component under
# BRAIN_PROJECTS_ROOT; the hook never opens the file, so a path string under
# that root that doesn't exist on disk is fine. Neutral, deployment-agnostic.
PROJ_ROOT = os.path.join(tempfile.gettempdir(), "brain_projects")

# Import the module directly for pure-function unit checks (no KB_ROOT touch).
_spec = importlib.util.spec_from_file_location("brain", BRAIN)
brain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brain)


@pytest.fixture
def root(tmp_path):
    """A fresh, isolated BRAIN_ROOT plus an isolated transcript root for liveness."""
    kb = tmp_path / "kb"
    kb.mkdir()
    tr = tmp_path / "transcripts"
    tr.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    return {"kb": str(kb), "tr": str(tr), "cache": str(cache)}


def run(root, *args, expect=None):
    env = dict(os.environ, BRAIN_ROOT=root["kb"], BRAIN_TRANSCRIPT_ROOT=root["tr"])
    # Never let a stray real session id leak into a claim default.
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    r = subprocess.run([sys.executable, BRAIN, *args], env=env,
                       capture_output=True, text=True)
    if expect is not None:
        assert r.returncode == expect, (
            f"args={args} rc={r.returncode}\nOUT:{r.stdout}\nERR:{r.stderr}")
    return r


def run_hook(root, path, session_id="sess1", cache=None):
    """Drive the file-matched hazard hook end to end: feed it a PostToolUse
    payload for an edit to `path`, return (parsed_json_or_None, raw_stdout).
    Points BRAIN_CLI at this repo's brain.py and isolates the sentinel cache."""
    env = dict(os.environ, BRAIN_ROOT=root["kb"], BRAIN_TRANSCRIPT_ROOT=root["tr"],
               BRAIN_CLI=BRAIN, BRAIN_HAZ_CACHE=cache or root["cache"],
               BRAIN_PROJECTS_ROOT=PROJ_ROOT)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("BRAIN_NO_INJECT", None)
    payload = json.dumps({"session_id": session_id,
                          "tool_input": {"file_path": path}, "cwd": PROJ_ROOT})
    r = subprocess.run([sys.executable, HAZ_HOOK], input=payload, env=env,
                       capture_output=True, text=True)
    out = r.stdout.strip()
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = None
    return parsed, out


def hook_ctx(parsed):
    return ((parsed or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")


# --------------------------------------------------------------- §2 write gate

def test_gotcha_requires_subsystem(root):
    r = run(root, "note", "-p", "demo", "-t", "gotcha", "a nuance", expect=1)
    assert "--subsystem" in r.stderr


def test_invariant_requires_subsystem(root):
    r = run(root, "note", "-p", "demo", "-t", "invariant", "must hold", expect=1)
    assert "--subsystem" in r.stderr


def test_gotcha_with_subsystem_ok(root):
    run(root, "note", "-p", "demo", "-t", "gotcha", "--subsystem", "parser",
        "surprising but correct behavior", expect=0)


def test_deadend_type_is_retired(root):
    # dead-end was folded into gotcha (it already indexed beside gotcha in the
    # hazard index). Writing it is now a clear, teaching rejection that names the
    # replacement — the stream still PARSES old dead-end lines, but none are minted.
    # (This replaces the pre-existing test_deadend_needs_no_subsystem, which
    # predated the retirement — OKF-TOOLS-28.)
    r = run(root, "note", "-p", "demo", "-t", "dead-end", "tried X, does not work", expect=1)
    assert "retired" in r.stderr and "gotcha" in r.stderr


@pytest.mark.parametrize("text", [
    "fixed abc1234 the crash",
    "resolved deadbee the leak",
    "resolved same run after a retry",
    "fixed: the off-by-one",
])
def test_gotcha_fix_language_rejected(root, text):
    r = run(root, "note", "-p", "demo", "-t", "gotcha", "--subsystem", "x", text, expect=1)
    assert "Routing test" in r.stderr


def test_force_type_overrides_fix_language(root):
    run(root, "note", "-p", "demo", "-t", "gotcha", "--subsystem", "x",
        "--force-type", "fixed abc1234 but genuinely a standing nuance", expect=0)


def test_plain_gotcha_not_falsely_rejected(root):
    # No sha, no fix-verb: must pass.
    run(root, "note", "-p", "demo", "-t", "gotcha", "--subsystem", "x",
        "the API returns stale reads for 200ms after a write", expect=0)


# --------------------------------------------------------------- §3 papercut

def test_papercut_no_subsystem_required(root):
    run(root, "note", "-p", "demo", "-t", "papercut",
        "build script prints a noisy warning", expect=0)


def test_papercuts_oldest_first(root):
    run(root, "note", "-p", "demo", "-t", "papercut", "first cut", expect=0)
    time.sleep(0.05)
    run(root, "note", "-p", "demo", "-t", "papercut", "second cut", expect=0)
    r = run(root, "papercuts", "--scope", "demo", expect=0)
    assert r.stdout.index("first cut") < r.stdout.index("second cut")


def test_papercut_excluded_from_hazards(root):
    run(root, "note", "-p", "demo", "-t", "papercut", "some annoyance", expect=0)
    run(root, "note", "-p", "demo", "-t", "gotcha", "--subsystem", "x",
        "a real hazard", expect=0)
    r = run(root, "hazards", "--scope", "demo", expect=0)
    assert "some annoyance" not in r.stdout
    assert "a real hazard" in r.stdout


def test_papercut_excluded_from_summary_inputs():
    # Pure-function check: _state_indices must skip papercut entries so they
    # never feed a branch/root summary.
    entries = [
        "2026-08-10T00:00 | decision | keep it",
        "2026-08-10T00:01 | papercut | noisy warning",
        "2026-08-10T00:02 | milestone | shipped",
        "2026-08-10T00:03 | gotcha | subsystem nuance",
    ]
    idx = brain._state_indices(entries, 1, len(entries), set())
    assert idx == [1, 3]  # papercut(2) and gotcha(4) both excluded


def test_resolve_discards_papercut(root):
    run(root, "note", "-p", "demo", "-t", "papercut", "cut to drain", expect=0)
    run(root, "resolve", "-p", "demo", "1", "wontfix", expect=0)
    r = run(root, "papercuts", "--scope", "demo", expect=0)
    assert "no open papercuts" in r.stdout


# --------------------------------------- ticket board (dispatch/agenda retired)
# The dispatch/agenda queue is gone; all tracked work lives on the ticket board
# (tickets.py). `brain dispatch` is now a fail-loud stub, and `brain wake` surfaces
# a compact board section read from the home board (KB_ROOT/tickets). Tests
# build their OWN throwaway board under the temp BRAIN_ROOT via tickets.py — the
# live board is never touched.

TICKETS = os.path.join(ROOT_DIR, "tickets.py")


def tickets_run(root, *args, expect=None):
    """Drive tickets.py against the temp board (KB_ROOT/tickets)."""
    board = os.path.join(root["kb"], "tickets")
    r = subprocess.run([sys.executable, TICKETS, *args, "--root", board],
                       env=dict(os.environ, BRAIN_ROOT=root["kb"]),
                       capture_output=True, text=True)
    if expect is not None:
        assert r.returncode == expect, (
            f"args={args} rc={r.returncode}\nOUT:{r.stdout}\nERR:{r.stderr}")
    return r


def init_board(root):
    """Create an empty HUB board under the temp BRAIN_ROOT; return its path."""
    board = os.path.join(root["kb"], "tickets")
    r = subprocess.run([sys.executable, TICKETS, "init", "--root", board,
                        "--prefix", "HUB", "--name", "Hub work board"],
                       env=dict(os.environ, BRAIN_ROOT=root["kb"]),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return board


# --- the fail-loud dispatch stub -----------------------------------------

def test_dispatch_stub_exits_loud(root):
    r = run(root, "dispatch", "list", expect=1)
    assert "retired" in r.stderr.lower()
    # names the exact replacement: the tickets.py board command on KB_ROOT/tickets
    assert "tickets.py board --root" in r.stderr
    assert os.path.join(root["kb"], "tickets") in r.stderr


def test_dispatch_stub_swallows_any_args(root):
    # Every old subcommand (and its flags) routes to the same fail-loud stub —
    # no silent pass-through, no leftover machinery.
    for args in (("dispatch", "add", "-p", "webapp", "do a thing"),
                 ("dispatch", "board"),
                 ("dispatch", "claim", "D-1", "--sid", "x"),
                 ("dispatch",)):
        r = run(root, *args, expect=1)
        assert "retired" in r.stderr.lower()


# --- wake surfaces the ticket board --------------------------------------

def test_wake_shows_board_section(root):
    run(root, "note", "-p", "global", "-t", "decision", "seed", expect=0)
    init_board(root)
    tickets_run(root, "open", "wire the amount normalizer", "--due", "2020-01-01",
                "--owner", "tod", expect=0)
    r = run(root, "wake", "--scope", "global", expect=0)
    assert "-- board:" in r.stdout
    assert "1 open, 1 due or overdue" in r.stdout
    assert "wire the amount normalizer" in r.stdout
    assert "HUB-1" in r.stdout
    assert "board --root" in r.stdout          # full-board pointer


def test_wake_board_lists_only_due(root):
    run(root, "note", "-p", "global", "-t", "decision", "seed", expect=0)
    init_board(root)
    tickets_run(root, "open", "overdue call", "--due", "2020-01-01", expect=0)
    tickets_run(root, "open", "future filing", "--due", "2999-01-01", expect=0)
    r = run(root, "wake", "--scope", "global", expect=0)
    assert "2 open, 1 due or overdue" in r.stdout
    assert "overdue call" in r.stdout
    # the not-yet-due ticket counts as open but is never listed as a due row
    board = r.stdout[r.stdout.index("-- board:"):]
    assert "future filing" not in board


def test_wake_board_caps_due_rows(root):
    run(root, "note", "-p", "global", "-t", "decision", "seed", expect=0)
    init_board(root)
    for i in range(9):
        tickets_run(root, "open", f"overdue task {i}", "--due", "2020-01-01", expect=0)
    r = run(root, "wake", "--scope", "global", expect=0)
    board = r.stdout[r.stdout.index("-- board:"):]
    rows = [ln for ln in board.splitlines() if ln.strip().startswith("HUB-")]
    assert len(rows) == 8                       # capped at 8
    assert "more due" in board                  # overflow pointer, no full dump


def test_wake_silent_when_no_board_root(root):
    run(root, "note", "-p", "global", "-t", "decision", "seed", expect=0)
    # No board created under KB_ROOT/tickets -> no board section, no error.
    r = run(root, "wake", "--scope", "global", expect=0)
    assert "-- board:" not in r.stdout


# ------------------------------------------------ §5 existing commands unchanged

def test_existing_commands_smoke(root):
    run(root, "note", "-p", "demo", "-t", "decision", "chose approach A", expect=0)
    run(root, "note", "-p", "demo", "-t", "gotcha", "--subsystem", "net",
        "connections leak on retry", expect=0)
    run(root, "note", "-p", "demo", "-t", "invariant", "--subsystem", "store",
        "stream is append-only", expect=0)

    r = run(root, "decisions", "--scope", "demo", expect=0)
    assert "chose approach A" in r.stdout

    r = run(root, "wake", "--scope", "demo", expect=0)
    assert "BRAIN WAKE" in r.stdout

    r = run(root, "hazards", "--scope", "demo", expect=0)
    assert "connections leak on retry" in r.stdout

    r = run(root, "design", "--scope", "demo", expect=0)
    assert "append-only" in r.stdout

    r = run(root, "grep", "approach", "--scope", "demo", expect=0)
    assert "chose approach A" in r.stdout

    r = run(root, "gotchas", "--scope", "demo", expect=0)
    assert "connections leak" in r.stdout


def test_resolve_hazard_still_works(root):
    run(root, "note", "-p", "demo", "-t", "gotcha", "--subsystem", "net",
        "some hazard", expect=0)
    run(root, "resolve", "-p", "demo", "1", "fixed by rewrite", expect=0)
    r = run(root, "hazards", "--scope", "demo", expect=0)
    assert "some hazard" not in r.stdout or "resolved" in r.stdout.lower()


# ------------------------------------------ D-14 hazards --match-path selection

def test_match_path_selects_matching_hazard(root):
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "normalize",
        "bank feed amount sign flips per provider; normalize once at ingest", expect=0)
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "cache",
        "tried caching the balance in memory, it races the writer", expect=0)
    r = run(root, "hazards", "--scope", "ledgerapp",
            "--match-path", "projects/ledgerapp/server/normalize_amount.py",
            expect=0)
    assert "sign flips" in r.stdout                     # #1 matched (normalize/amount)
    assert "races the writer" not in r.stdout           # #2 unrelated to this path
    assert "MATCHED-IDS: 1" in r.stdout


def test_match_path_silent_when_nothing_matches(root):
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "normalize",
        "amount sign flips per provider", expect=0)
    r = run(root, "hazards", "--scope", "ledgerapp",
            "--match-path", "projects/ledgerapp/ui/theme_colors.css", expect=0)
    assert r.stdout.strip() == ""                       # no noise on a non-match


def test_match_path_exclude_dedupes(root):
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "normalize",
        "amount sign flips per provider", expect=0)
    r = run(root, "hazards", "--scope", "ledgerapp",
            "--match-path", "ledgerapp/server/normalize_amount.py",
            "--exclude", "1", expect=0)
    assert r.stdout.strip() == ""                       # already-injected id excluded


def test_match_path_skips_superseded(root):
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "normalize",
        "old amount rule", expect=0)
    run(root, "resolve", "-p", "ledgerapp", "1", "amount rule replaced", expect=0)
    r = run(root, "hazards", "--scope", "ledgerapp",
            "--match-path", "ledgerapp/normalize_amount.py", expect=0)
    assert "old amount rule" not in r.stdout            # resolved hazard not resurfaced


def test_match_path_ignores_scope_name_token(root):
    # The scope name appears as a parent dir on almost every path and is named
    # in most of its hazards — it must NOT act as a match token, or precision
    # collapses. Only the subsystem-specific word should select.
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "feeds",
        "the ledgerapp importer double-counts pending rows", expect=0)
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "charts",
        "ledgerapp chart axis clips negative values", expect=0)
    # A path naming only the scope (no subsystem word) matches nothing.
    r = run(root, "hazards", "--scope", "ledgerapp",
            "--match-path", "projects/ledgerapp/run.py", expect=0)
    assert r.stdout.strip() == ""
    # A path naming a subsystem word matches just that one.
    r = run(root, "hazards", "--scope", "ledgerapp",
            "--match-path", "projects/ledgerapp/charts_view.py", expect=0)
    assert "chart axis clips" in r.stdout
    assert "double-counts" not in r.stdout


def test_path_tokens_drops_scaffolding_and_short_words():
    # Pure-function check on the matching axis.
    toks = brain._path_tokens("a/b/c/src/normalize_amount.py")
    assert "normalize" in toks and "amount" in toks
    assert "src" not in toks and "py" not in toks       # stoplist + <4-char dropped


def test_hazard_path_matches_whole_word_only():
    entries = [
        "2026-08-10T00:00 | gotcha | the amount field flips sign",
        "2026-08-10T00:01 | gotcha | amounts of memory leak on retry",
    ]
    # 'amount' (path token) matches entry 1's whole word, not entry 2's 'amounts'.
    # Exercise the matcher directly against a temp scope.
    import tempfile
    d = tempfile.mkdtemp()
    try:
        os.environ_backup = None
        sd = os.path.join(d, "stream")
        os.makedirs(sd)
        with open(os.path.join(sd, "s.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(entries) + "\n")
        old = brain.KB_ROOT, brain.STREAM_DIR, brain.TREE_DIR
        brain.KB_ROOT, brain.STREAM_DIR, brain.TREE_DIR = d, sd, os.path.join(d, "tree")
        try:
            hits = brain._hazard_path_matches("s", "x/normalize_amount.py")
            idxs = [i for i, _ in hits]
        finally:
            brain.KB_ROOT, brain.STREAM_DIR, brain.TREE_DIR = old
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    assert idxs == [1]


# ------------------------- D-14 (cont.) generic-noun flood + relevance ordering

def test_path_tokens_drops_generic_engineering_nouns():
    # config / server / worker / session name a LAYER, not a subsystem — they
    # flood the match keyed alone, so they must not survive tokenisation.
    assert brain._path_tokens("lib/config.py") == set()
    assert brain._path_tokens("server/worker.py") == set()
    assert brain._path_tokens("web/hooks/useSession.ts") == set()
    # A domain word riding alongside a generic one still survives.
    assert "normalize" in brain._path_tokens("server/normalize_worker.py")
    assert "worker" not in brain._path_tokens("server/normalize_worker.py")


def test_generic_noun_matcher_does_not_flood(root):
    # Seed many hazards that all merely mention the generic word "config" but are
    # about unrelated subsystems, plus one genuinely config-shaped one keyed on a
    # distinctive token. Editing config.py must not surface the flood.
    for i in range(11):
        run(root, "note", "-p", "webapp", "-t", "gotcha", "--subsystem", f"sub{i}",
            f"subsystem {i} reads the config at startup and caches it", expect=0)
    r = run(root, "hazards", "--scope", "webapp",
            "--match-path", "lib/config.py", expect=0)
    assert r.stdout.strip() == ""            # generic 'config' floods nothing


def test_flood_toggle_restores_pre_fix_behaviour(root):
    # The eval's regression arm needs a way to prove it has teeth. With the
    # stoplist disabled (pre-fix), the same generic token floods again.
    for i in range(11):
        run(root, "note", "-p", "webapp", "-t", "gotcha", "--subsystem", f"sub{i}",
            f"subsystem {i} reads the config at startup and caches it", expect=0)
    env = dict(os.environ, BRAIN_ROOT=root["kb"], BRAIN_TRANSCRIPT_ROOT=root["tr"],
               BRAIN_DISABLE_PATH_STOP="1")
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    r = subprocess.run([sys.executable, BRAIN, "hazards", "--scope", "webapp",
                        "--match-path", "lib/config.py"], env=env,
                       capture_output=True, text=True)
    assert "MATCHED-IDS:" in r.stdout        # flooding restored when stop disabled


def test_match_relevance_ordered_truncation(root):
    # One hazard names BOTH subsystems the edited path names; many name only one.
    # Relevance ordering must float the two-token hit to the top of the cap even
    # though it was appended late (higher id = later in the stream).
    for i in range(10):
        run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "ledger",
            f"ledger rule {i} for postings", expect=0)
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "recon",
        "the ledger and recon totals disagree when a posting is voided", expect=0)
    hits = brain_hits(root, "ledgerapp", "server/ledger_recon.py")
    # The last-seeded two-token hit (id 11) must rank first despite its recency.
    assert hits[0] == 11


def brain_hits(root, scope, path):
    """Return matched ids in ranked order via the CLI MATCHED-IDS line."""
    r = run(root, "hazards", "--scope", scope, "--match-path", path, expect=0)
    import re as _re
    m = _re.search(r"^MATCHED-IDS:\s*(.*)$", r.stdout, _re.MULTILINE)
    return [int(x) for x in _re.findall(r"\d+", m.group(1))] if m else []


def test_match_cap_bounds_output(root):
    # A hot but genuine subsystem token can match more than MATCH_CAP hazards;
    # the printed rows must never exceed the cap regardless.
    for i in range(20):
        run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "ledger",
            f"ledger posting rule {i}", expect=0)
    ids = brain_hits(root, "ledgerapp", "server/ledger_view.py")
    assert len(ids) == brain.MATCH_CAP


def test_scope_alias_dir_stripped(root):
    # A deployment can declare that a folder name resolves to a different scope
    # (dir != scope). That alias dir sits on every path in the scope and must be
    # stripped from the match tokens like the scope name itself.
    alias_env = {"webapp": ["srv"]}
    env = dict(os.environ, BRAIN_ROOT=root["kb"], BRAIN_TRANSCRIPT_ROOT=root["tr"],
               BRAIN_SCOPE_ALIAS_DIRS=json.dumps(alias_env))
    env.pop("CLAUDE_CODE_SESSION_ID", None)

    def run_env(*args):
        r = subprocess.run([sys.executable, BRAIN, *args], env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r

    run_env("note", "-p", "webapp", "-t", "gotcha", "--subsystem", "auth",
            "the auth layer gates requests by role")
    # A path naming only the alias dir (no subsystem word) matches nothing.
    r = run_env("hazards", "--scope", "webapp", "--match-path", "srv/run.py")
    assert r.stdout.strip() == ""


# ---------------------------------- D-14 kb_hazard_inject.py (file-matched gate)

NW_AMOUNT = os.path.join(PROJ_ROOT, "ledgerapp", "server", "normalize_amount.py")
NW_THEME = os.path.join(PROJ_ROOT, "ledgerapp", "ui", "theme_colors.css")


def _seed_ledgerapp_hazards(root):
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "normalize",
        "bank feed amount sign flips per provider; normalize once at ingest", expect=0)
    run(root, "note", "-p", "ledgerapp", "-t", "gotcha", "--subsystem", "theme",
        "theme color tokens must stay in the css layer, not inline", expect=0)


def test_hook_injects_file_matched_hazard(root):
    _seed_ledgerapp_hazards(root)
    parsed, _ = run_hook(root, NW_AMOUNT)
    ctx = hook_ctx(parsed)
    assert "sign flips" in ctx                       # amount hazard surfaced
    assert "color tokens" not in ctx                 # theme hazard not for this file
    assert "MATCHED-IDS" not in ctx                   # machine line stripped


def test_hook_silent_when_no_match(root):
    # A scope with hazards, but a first edit to a file that matches none AND has
    # its own live hazards -> the first-edit net still surfaces the index once.
    # Here we verify the *pure* no-match case: a scope whose only hazard does not
    # match, on a NON-first edit, stays silent.
    _seed_ledgerapp_hazards(root)
    run_hook(root, NW_AMOUNT)                          # first edit consumes the net
    parsed, out = run_hook(root, NW_THEME)            # matches the theme hazard #2
    # theme file matches hazard #2 -> injected once
    assert "color tokens" in hook_ctx(parsed)
    # editing a truly unrelated file now stays silent (nothing new matches)
    parsed2, out2 = run_hook(root, os.path.join(PROJ_ROOT, "ledgerapp", "misc", "readme_notes.md"))
    assert out2 == ""


def test_hook_once_per_hazard(root):
    _seed_ledgerapp_hazards(root)
    p1, _ = run_hook(root, NW_AMOUNT)
    assert "sign flips" in hook_ctx(p1)
    # Same file, same session -> hazard already injected -> silent.
    p2, out2 = run_hook(root, NW_AMOUNT)
    assert out2 == ""


def test_hook_new_session_reinjects(root):
    _seed_ledgerapp_hazards(root)
    p1, _ = run_hook(root, NW_AMOUNT, session_id="sessA")
    assert "sign flips" in hook_ctx(p1)
    # Different session id -> fresh sentinel -> injects again.
    p2, _ = run_hook(root, NW_AMOUNT, session_id="sessB")
    assert "sign flips" in hook_ctx(p2)


def test_hook_first_edit_net_when_generic_filename(root):
    # First edit hits a generically-named file that matches no hazard; the
    # net surfaces the full scope index once so a real trap is not missed.
    _seed_ledgerapp_hazards(root)
    generic = os.path.join(PROJ_ROOT, "ledgerapp", "run.py")
    parsed, _ = run_hook(root, generic)
    ctx = hook_ctx(parsed)
    assert "sign flips" in ctx and "color tokens" in ctx   # full index once
    # Subsequent generic edit does not re-dump the index.
    _, out2 = run_hook(root, os.path.join(PROJ_ROOT, "ledgerapp", "go.py"))
    assert out2 == ""


def test_hook_ignores_dotclaude_paths(root):
    _seed_ledgerapp_hazards(root)
    _, out = run_hook(root, os.path.expanduser(os.path.join("~", ".claude", "x.py")))
    assert out == ""                                   # KB/settings edits skipped


def test_hook_silent_for_scope_without_stream(root):
    # No stream for this scope -> gate never fires.
    _, out = run_hook(root, os.path.join(PROJ_ROOT, "webapp", "server", "app.py"))
    assert out == ""


def test_hook_respects_no_inject(root):
    _seed_ledgerapp_hazards(root)
    env = dict(os.environ, BRAIN_ROOT=root["kb"], BRAIN_TRANSCRIPT_ROOT=root["tr"],
               BRAIN_CLI=BRAIN, BRAIN_HAZ_CACHE=root["cache"], BRAIN_NO_INJECT="1")
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    payload = json.dumps({"session_id": "s", "tool_input": {"file_path": NW_AMOUNT}})
    r = subprocess.run([sys.executable, HAZ_HOOK], input=payload, env=env,
                       capture_output=True, text=True)
    assert r.stdout.strip() == ""


# ---------------------------------------------------------------- D-37: durable
# commit-on-write must self-heal an orphaned .git/index.lock rather than skip
# every later commit silently. These drive brain._git_commit against a real,
# throwaway git repo with brain.KB_ROOT monkeypatched onto it.

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git not on PATH")


def _init_repo(tmp_path):
    """A real, isolated git repo with one commit, usable as brain.KB_ROOT."""
    repo = tmp_path / "kbrepo"
    repo.mkdir()
    env = dict(os.environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    for cmd in (["init", "-q"],
                ["config", "user.email", "t@x"],
                ["config", "user.name", "t"]):
        subprocess.run([GIT, "-C", str(repo), *cmd], check=True,
                       capture_output=True, env=env)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run([GIT, "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True, env=env)
    subprocess.run([GIT, "-C", str(repo), "commit", "-q", "-m", "seed"],
                   check=True, capture_output=True, env=env)
    return repo


def _log(repo):
    return subprocess.run([GIT, "-C", str(repo), "log", "--oneline"],
                          capture_output=True, text=True).stdout


def _porcelain(repo):
    return subprocess.run([GIT, "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True).stdout.strip()


def _slow_git_ok(monkeypatch):
    """Decouple happy-path git-lands assertions from machine load: give real git
    a generous window so a slow-but-progressing commit is never killed. (The
    fast-skip-under-load behavior is exercised by the fault-injection tests.)"""
    monkeypatch.setattr(brain, "COMMIT_TIMEOUT", 120)
    monkeypatch.setattr(brain, "COMMIT_DEADLINE", 120.0)


@requires_git
def test_git_commit_basic(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(brain, "KB_ROOT", str(repo))
    _slow_git_ok(monkeypatch)
    (repo / "note.txt").write_text("hello\n")
    brain._git_commit(["note.txt"], "add note")
    assert "add note" in _log(repo)
    assert _porcelain(repo) == ""                       # clean tree


@requires_git
def test_stale_prelocked_repo_self_heals(tmp_path, monkeypatch):
    """A pre-planted stale index.lock (no live git) is cleared on the next write
    and the commit lands — no hand intervention."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(brain, "KB_ROOT", str(repo))
    _slow_git_ok(monkeypatch)
    lock = repo / ".git" / "index.lock"
    lock.write_text("stale")
    old = time.time() - 3600                            # unmistakably old
    os.utime(lock, (old, old))
    (repo / "note.txt").write_text("data\n")
    brain._git_commit(["note.txt"], "add note after stale lock")
    assert not lock.exists()                            # lock cleared
    assert "add note after stale lock" in _log(repo)    # commit landed
    assert _porcelain(repo) == ""


@requires_git
def test_timeout_clears_our_own_orphaned_lock(tmp_path, monkeypatch):
    """When our own git is killed at the timeout and orphans the lock, the retry
    clears it and commits."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(brain, "KB_ROOT", str(repo))
    _slow_git_ok(monkeypatch)
    lock = repo / ".git" / "index.lock"
    real_run = subprocess.run
    state = {"first_add": True}

    def fake_run(cmd, **kw):
        if len(cmd) > 3 and cmd[3] == "add" and state["first_add"]:
            state["first_add"] = False
            lock.write_text("ours")                     # our git created it...
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))  # ...then died
        return real_run(cmd, **kw)

    monkeypatch.setattr(brain.subprocess, "run", fake_run)
    (repo / "note.txt").write_text("data\n")
    brain._git_commit(["note.txt"], "commit after killed git")
    assert not lock.exists()                            # our orphan cleared
    assert "commit after killed git" in _log(repo)


@requires_git
def test_live_lock_is_never_deleted(tmp_path, monkeypatch):
    """A lock whose mtime advances during our window (a live holder) is preserved
    by _remove_stale_lock; an unchanged one is treated as stale and removed."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(brain, "KB_ROOT", str(repo))
    lock = repo / ".git" / "index.lock"
    lock.write_text("held")
    seen = lock.stat().st_mtime
    # Live holder refreshed the lock since we first saw it -> must be preserved.
    os.utime(lock, (seen + 30, seen + 30))
    assert brain._remove_stale_lock(seen) is False
    assert lock.exists()
    # Unchanged since first sighting -> stale -> removed.
    now = lock.stat().st_mtime
    assert brain._remove_stale_lock(now) is True
    assert not lock.exists()


@requires_git
def test_live_lock_survives_full_commit_effort(tmp_path, monkeypatch):
    """End to end: a lock a live git keeps touching is never deleted by
    _git_commit; the write is skipped and a pending marker is left."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(brain, "KB_ROOT", str(repo))
    monkeypatch.setattr(brain, "COMMIT_ATTEMPTS", 2)
    monkeypatch.setattr(brain, "COMMIT_DEADLINE", 2.0)
    lock = repo / ".git" / "index.lock"
    lock.write_text("held")
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        # Simulate git refusing (lock present) AND a live holder that keeps the
        # lock's mtime advancing between our attempts.
        if len(cmd) > 3 and cmd[3] in ("add", "commit"):
            os.utime(lock, None)                        # touch -> mtime advances
            r = subprocess.CompletedProcess(cmd, 128, b"",
                b"fatal: Unable to create '.git/index.lock': File exists.")
            return r
        return real_run(cmd, **kw)

    monkeypatch.setattr(brain.subprocess, "run", fake_run)
    (repo / "note.txt").write_text("data\n")
    brain._git_commit(["note.txt"], "should not steal a live lock")
    assert lock.exists()                                # live lock untouched
    assert os.path.exists(brain._pending_path())          # marker left for sweep


@requires_git
def test_pending_backlog_swept_on_next_write(tmp_path, monkeypatch):
    """An earlier skipped commit (marker + uncommitted backlog) is swept into the
    next successful write."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(brain, "KB_ROOT", str(repo))
    _slow_git_ok(monkeypatch)
    (repo / "stream").mkdir()
    (repo / "stream" / "a.log").write_text("earlier entry\n")   # skipped backlog
    brain._mark_pending()
    assert os.path.exists(brain._pending_path())
    (repo / "stream" / "b.log").write_text("new entry\n")
    brain._git_commit(["stream/b.log"], "add b")
    assert not os.path.exists(brain._pending_path())      # marker cleared
    assert _porcelain(repo) == ""                       # a.log AND b.log committed
    log = _log(repo)
    assert "add b" in log and "sweep pending commit backlog" in log


# ---------------------------------------------- history: durable transcript archive

def _write_transcript(root, munged, sid, records):
    """Write a synthetic Claude Code transcript (JSONL) under an isolated
    projects root: <tr>/<munged>/<sid>.jsonl. Returns the file path."""
    d = os.path.join(root["tr"], munged)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sid}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def _hist_db(root):
    return os.path.join(root["kb"], "..", "hist.db")


def _hrun(root, *args, expect=None):
    """Drive `brain history …` with an isolated archive DB + transcript root."""
    env = dict(os.environ, BRAIN_ROOT=root["kb"], BRAIN_TRANSCRIPT_ROOT=root["tr"],
               BRAIN_HISTORY_DB=_hist_db(root))
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    r = subprocess.run([sys.executable, BRAIN, "history", *args],
                       env=env, capture_output=True, text=True)
    if expect is not None:
        assert r.returncode == expect, (
            f"args={args} rc={r.returncode}\nOUT:{r.stdout}\nERR:{r.stderr}")
    return r


def _open_hist(root):
    import sqlite3
    c = sqlite3.connect(_hist_db(root))
    c.row_factory = sqlite3.Row
    return c


_CWD = os.path.join("C:", os.sep, "proj", "demo")

_SPINE = [
    {"type": "user", "isSidechain": False, "timestamp": "2026-08-01T10:00:00.000Z",
     "cwd": _CWD, "gitBranch": "main",
     "message": {"role": "user", "content": "please find the bugs in the parser"}},
    {"type": "assistant", "timestamp": "2026-08-01T10:00:05.000Z",
     "message": {"model": "claude-test", "content": [
         {"type": "thinking", "thinking": "SECRETTHOUGHT must never be stored"},
         {"type": "text", "text": "Running the tests now."},
         {"type": "tool_use", "id": "tu1", "name": "Bash",
          "input": {"command": "npm test"}}]}},
    {"type": "user", "timestamp": "2026-08-01T10:00:09.000Z",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "tu1",
          "content": "BIGTOOLBODY line one\nline two\nline three",
          "is_error": False}]}},
    {"type": "assistant", "timestamp": "2026-08-01T10:00:12.000Z",
     "message": {"model": "claude-test", "content": [
         {"type": "text", "text": "The parser dropped a token; patching brain.py"},
         {"type": "tool_use", "id": "tu2", "name": "Edit",
          "input": {"file_path": os.path.join(_CWD, "brain.py")}}]}},
]


def test_history_ingest_stub_no_bodies(root):
    _write_transcript(root, "C--proj-demo", "sess-aaaa", _SPINE)
    _hrun(root, "ingest", expect=0)
    c = _open_hist(root)
    # schema is versioned (persisted-data rule)
    assert c.execute("PRAGMA user_version").fetchone()[0] == brain.HISTORY_SCHEMA_VERSION
    turns = "\n".join(r["text"] for r in c.execute("SELECT text FROM turns"))
    assert "Running the tests now." in turns
    assert "The parser dropped a token" in turns
    # thinking text and tool-result bodies are NEVER stored (spine only)
    assert "SECRETTHOUGHT" not in turns
    assert "BIGTOOLBODY" not in turns
    stubs = [r["stub"] for r in c.execute("SELECT stub FROM tool_events ORDER BY rowid")]
    assert "Bash: npm test -> 3 lines" in stubs      # stub enriched by the result
    assert any(s.startswith("Edit:") and s.endswith("brain.py") for s in stubs)
    # deterministic metadata captured for free
    s = c.execute("SELECT * FROM sessions WHERE id='sess-aaaa'").fetchone()
    assert s["git_branch"] == "main" and s["model"] == "claude-test"
    assert s["scope"] == "demo"                        # from cwd basename


def test_history_ingest_incremental_offset_resume(root):
    path = _write_transcript(root, "C--proj-demo", "sess-bbbb", _SPINE[:2])
    _hrun(root, "ingest", expect=0)
    c = _open_hist(root)
    n1 = c.execute("SELECT COUNT(*) FROM turns WHERE session_id='sess-bbbb'").fetchone()[0]
    off1 = c.execute("SELECT src_offset FROM sessions WHERE id='sess-bbbb'").fetchone()[0]
    c.close()
    # Second ingest of the UNCHANGED file must add nothing (idempotent).
    r = _hrun(root, "ingest", expect=0)
    assert "0 new sessions, +0 turns" in r.stdout
    c = _open_hist(root)
    assert c.execute("SELECT COUNT(*) FROM turns WHERE session_id='sess-bbbb'").fetchone()[0] == n1
    c.close()
    # Append more of the session; ingest resumes from the stored offset.
    with open(path, "a", encoding="utf-8") as f:
        for rec in _SPINE[2:]:
            f.write(json.dumps(rec) + "\n")
    _hrun(root, "ingest", expect=0)
    c = _open_hist(root)
    n2 = c.execute("SELECT COUNT(*) FROM turns WHERE session_id='sess-bbbb'").fetchone()[0]
    off2 = c.execute("SELECT src_offset FROM sessions WHERE id='sess-bbbb'").fetchone()[0]
    assert n2 > n1 and off2 > off1                     # resumed, didn't re-scan from 0


def test_history_defensive_unknown_record(root):
    # An unknown record type and a malformed line must be skipped, never fatal.
    recs = [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-08-01T09:00:00Z"},
        {"type": "some-future-record", "payload": {"whatever": 1}},
        {"type": "user", "isSidechain": False, "timestamp": "2026-08-01T09:00:01Z",
         "cwd": _CWD,
         "message": {"role": "user", "content": "the survivor turn text"}},
        {"type": "assistant", "isSidechain": True, "timestamp": "2026-08-01T09:00:02Z",
         "message": {"content": [{"type": "text", "text": "SIDECHAINTEXT skip me"}]}},
    ]
    path = _write_transcript(root, "C--proj-demo", "sess-cccc", recs)
    with open(path, "a", encoding="utf-8") as f:
        f.write("{ this is not valid json at all\n")   # malformed trailing line
    _hrun(root, "ingest", expect=0)                    # exits 0 despite the junk
    c = _open_hist(root)
    turns = "\n".join(x["text"] for x in c.execute(
        "SELECT text FROM turns WHERE session_id='sess-cccc'"))
    assert "the survivor turn text" in turns
    assert "SIDECHAINTEXT" not in turns                 # sidechain skipped (spine only)


def test_history_search_output_capped(root):
    # 8 sessions all matching the same needle; search caps hits at 5.
    for i in range(8):
        recs = [{"type": "user", "isSidechain": False,
                 "timestamp": f"2026-08-0{i+1}T10:00:00Z", "cwd": _CWD,
                 "message": {"role": "user",
                             "content": f"uniqueneedle discussion number {i}"}}]
        _write_transcript(root, "C--proj-demo", f"sess-s{i}", recs)
    _hrun(root, "ingest", expect=0)
    r = _hrun(root, "uniqueneedle", expect=0)
    hit_lines = [ln for ln in r.stdout.splitlines() if "sess-s" in ln]
    assert len(hit_lines) <= brain.HISTORY_SEARCH_HITS   # capped at 5, not all 8
    assert len(r.stdout.splitlines()) <= brain.HISTORY_SEARCH_MAXLINES + 2


def test_history_search_dedups_sessions(root):
    # One session with MANY matching turns must not crowd a distinct session
    # out of the hit list: hits are per-session, not per-turn.
    noisy = [{"type": "user", "isSidechain": False,
              "timestamp": f"2026-08-01T10:0{i}:00Z", "cwd": _CWD,
              "message": {"role": "user",
                          "content": f"dedupneedle repeat {i}"}}
             for i in range(6)]
    _write_transcript(root, "C--proj-demo", "sess-noisy", noisy)
    quiet = [{"type": "user", "isSidechain": False,
              "timestamp": "2026-08-02T10:00:00Z", "cwd": _CWD,
              "message": {"role": "user",
                          "content": "dedupneedle mentioned once here"}}]
    _write_transcript(root, "C--proj-demo", "sess-quiet", quiet)
    _hrun(root, "ingest", expect=0)
    r = _hrun(root, "dedupneedle", expect=0)
    assert r.stdout.count("sess-noisy") == 1
    assert "sess-quiet" in r.stdout


def test_history_search_special_chars_no_crash(root):
    _write_transcript(root, "C--proj-demo", "sess-dddd", _SPINE)
    _hrun(root, "ingest", expect=0)
    # FTS operators in raw user text must not raise a syntax error.
    r = _hrun(root, 'AND OR "(parser', expect=0)
    assert r.returncode == 0


def test_history_show_expands_session(root):
    _write_transcript(root, "C--proj-demo", "sess-eeee", _SPINE)
    _hrun(root, "ingest", expect=0)
    r = _hrun(root, "show", "sess-eeee", "-n", "2", expect=0)
    assert "sess-eeee" in r.stdout
    assert "find the bugs in the parser" in r.stdout


def test_history_summarize_dry_run_no_write(root):
    _write_transcript(root, "C--proj-demo", "sess-ffff", _SPINE)
    _hrun(root, "ingest", expect=0)
    # --dry-run previews the deterministic summary (first user turn) and tags,
    # and never writes to the DB. Summaries are mechanical — no model call ever.
    r = _hrun(root, "summarize", "--dry-run", expect=0)
    assert "would summarize" in r.stdout
    assert "find the bugs in the parser" in r.stdout    # first user turn IS the summary
    c = _open_hist(root)
    assert c.execute("SELECT summary FROM sessions WHERE id='sess-ffff'").fetchone()[0] is None


def test_history_summarize_skips_hot_sessions(root):
    # A session ended just now (idle < 24h) is not summarized.
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    recs = [{"type": "user", "isSidechain": False, "timestamp": recent,
             "cwd": _CWD,
             "message": {"role": "user", "content": "a very recent session turn"}}]
    _write_transcript(root, "C--proj-demo", "sess-hot", recs)
    _hrun(root, "ingest", expect=0)
    r = _hrun(root, "summarize", "--dry-run", expect=0)
    assert "sess-hot" not in r.stdout                   # too fresh to summarize
    assert "skipped (idle < 24h)" in r.stdout


# --------------------------------------------------------------- asof (two time axes)

def _stream(root, scope):
    """Raw stream lines for a scope, exactly as persisted (write order)."""
    p = os.path.join(root["kb"], "stream", f"{scope}.log")
    return open(p, encoding="utf-8").read().splitlines() if os.path.isfile(p) else []


def test_asof_roundtrip_with_and_without(root):
    # With --asof the date lands in the meta segment; without it the line is
    # byte-for-byte the pre-feature shape (no asof=).
    run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "2026-06-30",
        "backfilled fact", expect=0)
    run(root, "note", "-p", "demo", "-t", "milestone", "recorded now", expect=0)
    lines = _stream(root, "demo")
    assert "asof=2026-06-30" in lines[0]
    assert "asof=" not in lines[1]
    # Both parse, and the parser recovers the asof through the standard readers.
    p0 = brain._parse(lines[0])
    p1 = brain._parse(lines[1])
    assert brain._asof(p0[2]) == "2026-06-30"
    assert brain._asof(p1[2]) == ""
    # Effective date: dated entry uses asof; undated falls back to its ts (never empty).
    assert brain._effective_date(lines[0]) == "2026-06-30"
    assert brain._effective_date(lines[1]) == p1[0]


def test_asof_malformed_rejected_with_teaching_error(root):
    r = run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "2026-13-40",
            "bad date", expect=1)
    assert "not a valid YYYY-MM-DD" in r.stderr
    assert "--asof 2026-06-30" in r.stderr          # shows the corrected invocation
    # A wholly malformed string is rejected too, and nothing is written.
    run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "june", "x", expect=1)
    # Non-zero-padded dates pass strptime but ASOF_RX could never read them
    # back — they must be rejected at write, not silently written unreadable.
    r = run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "2026-6-3",
            "unpadded", expect=1)
    assert "zero-padded" in r.stderr
    assert _stream(root, "demo") == []


def test_legacy_lines_still_parse_unchanged():
    # Persisted-data rule: 3-field legacy and 4-field sup/sid lines parse exactly
    # as before; the widened META_RX must not reclassify entry text as meta.
    legacy3 = "2026-01-01T00:00 | milestone | shipped the thing"
    assert brain._parse(legacy3) == ("2026-01-01T00:00", "milestone", "", "shipped the thing")
    withsup = "2026-01-01T00:00 | milestone | sup=7 sid=abc123 | replaced it"
    p = brain._parse(withsup)
    assert p[2] == "sup=7 sid=abc123" and p[3] == "replaced it"
    # Entry text containing " | " and an equals sign is NOT mistaken for meta.
    tricky = "2026-01-01T00:00 | milestone | see a=b | rest of the note"
    tp = brain._parse(tricky)
    assert tp[1] == "milestone" and tp[3] == "see a=b | rest of the note"
    # Hyphens are admitted ONLY for asof=: a legacy line whose text starts with
    # a lowercase key=value fragment containing a date must stay entry text,
    # never be reclassified as meta (silent text loss on old persisted lines).
    hyphen = "2026-01-01T00:00 | milestone | re=2026-06-30 | rest of the sentence"
    hp = brain._parse(hyphen)
    assert hp[2] == "" and hp[3] == "re=2026-06-30 | rest of the sentence"
    # And canonical asof= meta still parses as meta.
    withasof = "2026-01-01T00:00 | milestone | asof=2026-06-30 sid=abc123 | text"
    ap = brain._parse(withasof)
    assert ap[2] == "asof=2026-06-30 sid=abc123" and ap[3] == "text"


def test_asof_ordering_mixed_dated_and_undated():
    # Write order (identity) deliberately differs from date order: late, undated,
    # early. Effective-date ordering must yield early < undated(today) < late,
    # and the undated entry must NOT lose to the older dated one.
    entries = [
        "2026-08-15T10:00 | milestone | asof=2030-01-01 | far future fact",   # #1
        "2026-08-15T10:01 | milestone | recorded today, no asof",             # #2
        "2026-08-15T10:02 | milestone | asof=2026-01-01 | old backfilled fact",  # #3
    ]
    order = brain._order_by_date(range(1, 4), entries)
    assert order == [3, 2, 1]         # early(#3), undated(#2), late(#1)
    # Ties fall back to write order (stable): two same-date entries keep #order.
    same = [
        "2026-05-05T00:00 | milestone | asof=2026-06-30 | a",
        "2026-05-05T00:00 | milestone | asof=2026-06-30 | b",
    ]
    assert brain._order_by_date(range(1, 3), same) == [1, 2]


def test_zoom_orders_by_asof_keeps_numbers_and_shows_date(root):
    # Acceptance gate: three entries (undated, asof-early, asof-late) written in a
    # scrambled order; zoom lists them by effective date with write-order #numbers.
    run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "2030-01-01",
        "late fact", expect=0)                                    # #1
    run(root, "note", "-p", "demo", "-t", "milestone", "undated fact", expect=0)  # #2
    run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "2026-01-01",
        "early fact", expect=0)                                   # #3
    r = run(root, "zoom", "demo", "1-3", expect=0)
    out = r.stdout
    # Display order: early(#3) < undated(#2) < late(#1)
    assert out.index("early fact") < out.index("undated fact") < out.index("late fact")
    # Numbers stay write-order identity, not renumbered by date.
    assert "#1" in out and "#2" in out and "#3" in out
    assert out.index("#1") > out.index("#3")     # #1 (late) printed after #3 (early)
    # asof shown compactly; undated entry shows no asof tag.
    assert "[asof 2026-01-01]" in out and "[asof 2030-01-01]" in out
    assert out.count("[asof") == 2


def test_typed_listing_orders_by_asof(root):
    run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "2030-01-01",
        "listing late", expect=0)
    run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "2026-01-01",
        "listing early", expect=0)
    r = run(root, "milestones", "--scope", "demo", expect=0)
    assert r.stdout.index("listing early") < r.stdout.index("listing late")


def test_lint_warns_supersede_of_later_asof(root):
    # A backfill that corrects FORWARD in time (new fact-date earlier than the
    # entry it replaces) is suspicious — lint warns, non-fatal.
    run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "2026-08-01",
        "the newer, later fact", expect=0)                       # #1
    run(root, "note", "-p", "demo", "-t", "milestone", "--asof", "2026-01-01",
        "--supersedes", "1", "backfill dated earlier", expect=0)  # #2 supersedes #1
    r = run(root, "lint", "--scope", "demo")
    assert "LATER" in r.stdout and "#2" in r.stdout


# ------------------------------------------------ identity pins (OKF-TOOLS-37)

def _pintext(i, n):
    """A length-`n` pin text with a unique-per-i marker so a batch of them are
    distinct enough to reason about (dedup is bypassed with --distinct anyway)."""
    return (f"pin{i}-" + "abcdefghijklmnopqrstuvwxyz" * (n // 26 + 1))[:n]


def test_pin_capture_roundtrip(root):
    # A pinned invariant lands with a pin=1 meta token, parses back through the
    # standard readers, and _is_pinned recognizes it.
    run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--subsystem",
        "identity", "ledgerapp tracks checking, savings, brokerage, 403b", expect=0)
    line = _stream(root, "demo")[0]
    assert "pin=1" in line
    p = brain._parse(line)
    assert p[1] == "invariant"
    assert brain._pinned(p[2]) is True
    assert brain._is_pinned(line) is True
    # A non-pinned invariant is NOT pinned.
    run(root, "note", "-p", "demo", "-t", "invariant", "--subsystem", "parser",
        "the tokenizer is whitespace-only", expect=0)
    assert brain._is_pinned(_stream(root, "demo")[1]) is False


def test_pin_rejected_on_wrong_type(root):
    # --pin is a property of invariant only; any other type is a plain error,
    # and nothing is written.
    for typ in ("decision", "milestone", "gotcha", "question"):
        r = run(root, "note", "-p", "demo", "-t", typ, "--pin", "--subsystem",
                "identity", "should not pin", expect=1)
        assert "only valid with --type invariant" in r.stderr
    assert _stream(root, "demo") == []


def test_pin_requires_subsystem(root):
    r = run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "must hold",
            expect=1)
    assert "--subsystem" in r.stderr and "identity" in r.stderr
    assert _stream(root, "demo") == []


def test_pin_budget_enforced_at_boundary(root):
    # Four 300-char pins sum to exactly 1200 (the cap) and all land; one more
    # char over the boundary is rejected with a plain consolidate/supersede error.
    for i in range(4):
        run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--distinct",
            "--subsystem", "identity", _pintext(i, 300), expect=0)
    r = run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--distinct",
            "--subsystem", "identity", "x", expect=1)
    assert "1200" in r.stderr
    assert "consolidate" in r.stderr or "supersede" in r.stderr
    # The rejected pin was not written.
    assert len(_stream(root, "demo")) == 4
    # A non-pinned invariant is unaffected by the pin budget.
    run(root, "note", "-p", "demo", "-t", "invariant", "--subsystem", "parser",
        "an ordinary invariant, not pinned", expect=0)


def test_pin_budget_frees_superseded_chars(root):
    # A pin this write supersedes drops from the live sum first, so replacing a
    # near-cap pin never trips the budget.
    run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--distinct",
        "--subsystem", "identity", _pintext(1, 300), expect=0)
    run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--distinct",
        "--subsystem", "identity", _pintext(2, 300), expect=0)
    run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--distinct",
        "--subsystem", "identity", _pintext(3, 300), expect=0)
    # Live pins total 900. Superseding #1 with a fresh 300-char pin: 600 live +
    # 300 new = 900 <= 1200, allowed (the freed 300 is not double-counted).
    run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--distinct",
        "--supersedes", "1", "--subsystem", "identity", _pintext(9, 300), expect=0)
    assert len(brain._live_pins(_stream(root, "demo"),
                              brain._superseded(_stream(root, "demo")))) == 3


def test_wake_shows_pin_block_verbatim_at_top(root):
    fact = "ledgerapp tracks checking, savings, brokerage, 403b"
    run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--subsystem",
        "identity", fact, expect=0)
    run(root, "note", "-p", "demo", "-t", "decision", "use the passbook look", expect=0)
    out = run(root, "wake", "--scope", "demo", expect=0).stdout
    assert out.lstrip().startswith("Pinned facts (always loaded):")
    assert fact in out
    # The pin block sits ABOVE the computed-state header.
    assert out.index("Pinned facts") < out.index("computed from the stream")


def test_pin_still_appears_in_design(root):
    # Pins are invariants — they must also surface in the design view.
    fact = "ledgerapp tracks checking, savings, brokerage, 403b"
    run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--subsystem",
        "identity", fact, expect=0)
    out = run(root, "design", "--scope", "demo", expect=0).stdout
    assert fact in out


def test_supersede_drops_pin_unless_repassed(root):
    run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--subsystem",
        "identity", "old identity fact about the accounts", expect=0)
    # Supersede WITHOUT --pin: the replacement is not pinned, pin block empties.
    run(root, "note", "-p", "demo", "-t", "invariant", "--supersedes", "1",
        "--subsystem", "identity", "revised fact, deliberately unpinned now", expect=0)
    out = run(root, "wake", "--scope", "demo", expect=0).stdout
    assert "Pinned facts (always loaded):" not in out
    # Supersede WITH --pin re-pins the replacement.
    run(root, "note", "-p", "demo", "-t", "invariant", "--supersedes", "2", "--pin",
        "--subsystem", "identity", "re-pinned identity fact for the accounts", expect=0)
    out = run(root, "wake", "--scope", "demo", expect=0).stdout
    assert "Pinned facts (always loaded):" in out
    assert "re-pinned identity fact" in out


def test_rebuild_includes_pins(root):
    # A pins-only scope has no state to summarize, so rebuild writes root.md with
    # the pin block verbatim and never calls the model.
    fact = "ledgerapp tracks checking, savings, brokerage, 403b"
    run(root, "note", "-p", "demo", "-t", "invariant", "--pin", "--subsystem",
        "identity", fact, expect=0)
    run(root, "rebuild", "--scope", "demo", expect=0)
    rootmd = os.path.join(root["kb"], "tree", "demo", "root.md")
    assert os.path.isfile(rootmd)
    body = open(rootmd, encoding="utf-8").read()
    assert "Pinned facts (always loaded):" in body
    assert fact in body


# ------------------------------------------ AGENT-BRAIN-41 anti-hits
# When a query's strongest textual match is a DEAD (superseded) entry, retrieval
# must not go silent: recall and grep surface ONE forward pointer to the live
# head of the supersede chain, showing the head's TEXT (never the stale text).


def _antihit_lines(out):
    """Anti-hit pointer lines only — the marker is a leading '~' (recall prefixes
    a scope in brackets, grep prefixes the scope bare, so match either)."""
    return [ln for ln in out.splitlines()
            if ln.lstrip().startswith("~") or " ~ #" in ln]


def test_recall_antihit_points_to_live_head(root):
    # Dead #1 matches the query; the live head #2 uses different words, so it is
    # NOT a normal hit — the pointer is the only way the reader learns of it.
    run(root, "note", "-p", "demo", "-t", "decision",
        "billing retries use exponential backoff scheduling", expect=0)
    run(root, "note", "-p", "demo", "-t", "decision", "--supersedes", "1",
        "billing now delegates to Stripe dunning entirely", expect=0)
    out = run(root, "recall", "exponential backoff scheduling policy", expect=0).stdout
    anti = _antihit_lines(out)
    assert len(anti) == 1, out
    assert "superseded by #2" in anti[0]
    assert "#1" in anti[0]
    # The live head's text is surfaced…
    assert "Stripe dunning" in anti[0]
    # …and the stale text is NEVER re-printed.
    assert "exponential backoff" not in out


def test_recall_antihit_dedupes_when_head_is_a_hit(root):
    # Query matches BOTH dead #1 and live head #2; #2 is already a normal hit, so
    # no extra pointer line is emitted.
    run(root, "note", "-p", "demo", "-t", "decision",
        "cache invalidation uses timestamp comparison for freshness", expect=0)
    run(root, "note", "-p", "demo", "-t", "decision", "--supersedes", "1",
        "cache invalidation uses content-hash comparison for freshness", expect=0)
    out = run(root, "recall", "cache invalidation comparison freshness", expect=0).stdout
    assert "content-hash comparison" in out          # live head shown as a real hit
    assert _antihit_lines(out) == [], out            # no duplicate pointer


def test_recall_antihit_follows_chain_to_final_head(root):
    # Two supersessions: #1 -> #2 -> #3. A match on #1 must point at the FINAL
    # live head #3, never the intermediate (itself-superseded) #2.
    run(root, "note", "-p", "demo", "-t", "decision",
        "kubernetes deployment rollout uses blue-green strategy", expect=0)
    run(root, "note", "-p", "demo", "-t", "decision", "--supersedes", "1",
        "kubernetes deployment rollout uses canary strategy instead", expect=0)
    run(root, "note", "-p", "demo", "-t", "decision", "--supersedes", "2",
        "orchestration handed to a managed platform entirely nowadays", expect=0)
    out = run(root, "recall", "blue-green rollout strategy variant", expect=0).stdout
    anti = _antihit_lines(out)
    assert len(anti) == 1, out
    assert "superseded by #3" in anti[0]
    assert "superseded by #2" not in anti[0]
    assert "managed platform" in anti[0]


def test_recall_antihit_capped_at_three(root):
    # Four independent dead entries all match; only three pointer lines survive.
    for n in range(4):
        run(root, "note", "-p", "demo", "-t", "decision", "--distinct",
            f"biscuit gremlin protocol variant {n}", expect=0)
        # supersede the entry just written (indices: 1,3,5,7 are the dead ones)
        run(root, "note", "-p", "demo", "-t", "decision", "--supersedes",
            str(2 * n + 1), f"topic {n} resolved via unrelated foobar widget", expect=0)
    out = run(root, "recall", "biscuit gremlin", expect=0).stdout
    anti = _antihit_lines(out)
    assert len(anti) == 3, out
    for ln in anti:
        assert "biscuit gremlin" not in ln           # stale text never shown


def test_grep_antihit_points_to_live_head(root):
    # grep already prints the dead line (flagged [SUPERSEDED by #2]); the anti-hit
    # adds the live head's TEXT so the correction is legible in place.
    run(root, "note", "-p", "demo", "-t", "decision",
        "session token stored in localStorage", expect=0)
    run(root, "note", "-p", "demo", "-t", "decision", "--supersedes", "1",
        "session token stored in an httpOnly cookie now", expect=0)
    out = run(root, "grep", "localStorage", "--scope", "demo", expect=0).stdout
    anti = _antihit_lines(out)
    assert len(anti) == 1, out
    assert "superseded by #2" in anti[0]
    assert "httpOnly cookie" in anti[0]


def test_grep_antihit_dedupes_when_head_matches(root):
    # When the live head ALSO matches the regex it is already printed verbatim, so
    # no pointer line is added.
    run(root, "note", "-p", "demo", "-t", "decision",
        "retry budget capped at five attempts per window", expect=0)
    run(root, "note", "-p", "demo", "-t", "decision", "--supersedes", "1",
        "retry budget capped at ten attempts per window", expect=0)
    out = run(root, "grep", "retry budget capped", "--scope", "demo", expect=0).stdout
    assert _antihit_lines(out) == [], out
