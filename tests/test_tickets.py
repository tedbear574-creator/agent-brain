"""Tests for tickets.py — the multi-writer ticket board.

Roots live under tmp_path; the writer id is controlled with the TICKETS_WRITER
environment variable (monkeypatched) so tests never depend on the machine's
real user account.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tickets  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def run(*argv):
    """Run the CLI, return the exit code (raises SystemExit on error paths)."""
    return tickets.main(list(argv))


def as_writer(monkeypatch, name):
    monkeypatch.setenv("TICKETS_WRITER", name)


def make_board(tmp_path, monkeypatch, writer="alice", prefix="ACME-DOC",
               name="Acme documentation 2026"):
    root = str(tmp_path / "board")
    as_writer(monkeypatch, writer)
    run("init", "--root", root, "--prefix", prefix, "--name", name)
    return root


def write_raw(root, writer, events):
    """Write events straight to a writer's file (bypassing the CLI) so tests
    can stage exact timestamps and orderings."""
    os.makedirs(os.path.join(root, "events"), exist_ok=True)
    path = os.path.join(root, "events", f"{writer}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


# --------------------------------------------------------------------------- #
# init + root resolution                                                       #
# --------------------------------------------------------------------------- #


def test_init_creates_config_and_events_dir(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    cfg = json.load(open(os.path.join(root, "config.json"), encoding="utf-8"))
    assert cfg == {"prefix": "ACME-DOC", "name": "Acme documentation 2026"}
    assert os.path.isdir(os.path.join(root, "events"))


def test_missing_root_is_a_plain_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TICKETS_ROOT", raising=False)
    as_writer(monkeypatch, "alice")
    with pytest.raises(SystemExit):
        run("board")


def test_root_without_config_is_a_plain_error(tmp_path, monkeypatch, capsys):
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    as_writer(monkeypatch, "alice")
    with pytest.raises(SystemExit):
        run("board", "--root", empty)
    err = capsys.readouterr().err
    assert "not a board yet" in err


def test_root_from_env(tmp_path, monkeypatch, capsys):
    root = make_board(tmp_path, monkeypatch)
    monkeypatch.setenv("TICKETS_ROOT", root)
    run("open", "from env ticket")
    out = capsys.readouterr().out
    assert "ACME-DOC-1" in out


# --------------------------------------------------------------------------- #
# writer derivation                                                            #
# --------------------------------------------------------------------------- #


def test_writer_from_home_basename(monkeypatch):
    monkeypatch.delenv("TICKETS_WRITER", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: r"C:\Users\jsmith")
    assert tickets.writer_id() == "jsmith"


def test_writer_env_override(monkeypatch):
    monkeypatch.setenv("TICKETS_WRITER", "agent-7")
    assert tickets.writer_id() == "agent-7"


def test_writer_name_is_filename_safe(monkeypatch):
    monkeypatch.setenv("TICKETS_WRITER", "j smith/2")
    assert "/" not in tickets.writer_id()
    assert " " not in tickets.writer_id()


def test_each_writer_gets_own_file(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    as_writer(monkeypatch, "alice")
    run("open", "alice ticket", "--root", root)
    as_writer(monkeypatch, "bob")
    run("open", "bob ticket", "--root", root)
    files = sorted(os.listdir(os.path.join(root, "events")))
    assert files == ["alice.jsonl", "bob.jsonl"]


# --------------------------------------------------------------------------- #
# fold determinism                                                             #
# --------------------------------------------------------------------------- #


def test_fold_deterministic_under_shuffled_read_order(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaa", "title": "A", "propose_n": 1},
        {"ts": "2026-08-16T09:05:00", "writer": "alice", "verb": "open",
         "id": "cccccc", "title": "C", "propose_n": 3},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:02:00", "writer": "bob", "verb": "open",
         "id": "bbbbbb", "title": "B", "propose_n": 2},
    ])

    real_listdir = os.listdir

    def forward(path):
        return sorted(real_listdir(path))

    def reverse(path):
        return sorted(real_listdir(path), reverse=True)

    monkeypatch.setattr(tickets.os, "listdir", forward)
    b1 = {t["id"]: t["n"] for t in tickets.board_state(root).values()}
    monkeypatch.setattr(tickets.os, "listdir", reverse)
    b2 = {t["id"]: t["n"] for t in tickets.board_state(root).values()}
    assert b1 == b2 == {"aaaaaa": 1, "bbbbbb": 2, "cccccc": 3}


# --------------------------------------------------------------------------- #
# propose_n collision                                                          #
# --------------------------------------------------------------------------- #


def _keys(root, prefix="ACME-DOC"):
    b = tickets.board_state(root)
    return {t["id"]: tickets.key_of(prefix, t) for t in b.values()}


def test_propose_collision_gets_letter_suffix_no_renumber(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    # Ticket #1 exists; two writers both, unaware of each other, propose #2.
    write_raw(root, "seed", [
        {"ts": "2026-08-16T08:00:00", "writer": "seed", "verb": "open",
         "id": "111111", "title": "seed", "propose_n": 1},
    ])
    # Same timestamp -> tie broken by writer name: alice < bob.
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaa", "title": "alice #2", "propose_n": 2},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:00:00", "writer": "bob", "verb": "open",
         "id": "bbbbbb", "title": "bob #2", "propose_n": 2},
    ])
    keys = _keys(root)
    assert keys["111111"] == "ACME-DOC-1"
    assert keys["aaaaaa"] == "ACME-DOC-2"   # winner keeps the plain number
    assert keys["bbbbbb"] == "ACME-DOC-2b"  # loser keeps the number + letter


def test_late_sync_never_cascades(tmp_path, monkeypatch):
    """The reviewer's scenario: alice opens A (#1), bob opens B (#2); then
    carol's ticket from yesterday — also proposing #1 — syncs in late. Carol
    wins the plain #1 (earlier timestamp), A becomes 1b, and B is untouched:
    a clash between two tickets can never move a third."""
    root = make_board(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaa", "title": "A", "propose_n": 1},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:05:00", "writer": "bob", "verb": "open",
         "id": "bbbbbb", "title": "B", "propose_n": 2},
    ])
    before = _keys(root)
    assert before["aaaaaa"] == "ACME-DOC-1"
    assert before["bbbbbb"] == "ACME-DOC-2"
    write_raw(root, "carol", [
        {"ts": "2026-08-15T17:00:00", "writer": "carol", "verb": "open",
         "id": "cccccc", "title": "C from yesterday", "propose_n": 1},
    ])
    after = _keys(root)
    assert after["cccccc"] == "ACME-DOC-1"
    assert after["aaaaaa"] == "ACME-DOC-1b"  # only the collider is touched
    assert after["bbbbbb"] == "ACME-DOC-2"   # bystander never moves


def test_duplicate_id_across_writers_warns_not_silent(tmp_path, monkeypatch,
                                                      capsys):
    root = make_board(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaaaa", "title": "alice's", "propose_n": 1},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:01:00", "writer": "bob", "verb": "open",
         "id": "aaaaaaaa", "title": "bob's", "propose_n": 2},
    ])
    board = tickets.board_state(root)
    err = capsys.readouterr().err
    assert board["aaaaaaaa"]["title"] == "alice's"   # first one kept
    assert "same id" in err and "bob" in err          # loss is loud, not silent


# --------------------------------------------------------------------------- #
# close requires a source                                                      #
# --------------------------------------------------------------------------- #


def test_close_without_source_rejected_with_attestation_hint(
        tmp_path, monkeypatch, capsys):
    root = make_board(tmp_path, monkeypatch)
    run("open", "needs proof", "--root", root)
    with pytest.raises(SystemExit):
        run("close", "--root", root, "--ticket", "1")
    err = capsys.readouterr().err
    assert "source" in err.lower()
    assert "person:" in err          # the explicit attestation fallback


def test_close_with_source_succeeds(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "will close", "--root", root)
    run("close", "--root", root, "--ticket", "1",
        "--source", "Acme master file v3")
    t = _ticket_by_n(root, 1)
    assert t["status"] == tickets.STATUS_CLOSED
    assert t["source"] == "Acme master file v3"


def test_close_source_may_contain_spaces(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "spaces ok", "--root", root)
    run("close", "--root", root, "--ticket", "1",
        "--source", "doc name with spaces")
    assert _ticket_by_n(root, 1)["source"] == "doc name with spaces"


# --------------------------------------------------------------------------- #
# id resolution                                                                #
# --------------------------------------------------------------------------- #


def _ticket_by_n(root, n):
    for t in tickets.board_state(root).values():
        if t["n"] == n:
            return t
    raise AssertionError(f"no ticket {n}")


def test_resolve_by_number_key_and_hash(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "resolve me", "--root", root)
    t = _ticket_by_n(root, 1)
    tid = t["id"]
    tk = tickets.board_state(root)
    prefix = "ACME-DOC"
    assert tickets.resolve_ticket(tk, prefix, "1")["id"] == tid
    assert tickets.resolve_ticket(tk, prefix, "ACME-DOC-1")["id"] == tid
    assert tickets.resolve_ticket(tk, prefix, tid)["id"] == tid
    assert tickets.resolve_ticket(tk, prefix, "acme-doc-1")["id"] == tid


def test_resolve_unknown_number_errors(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    tk = tickets.board_state(root)
    with pytest.raises(SystemExit):
        tickets.resolve_ticket(tk, "ACME-DOC", "99")


# --------------------------------------------------------------------------- #
# status: claim / block / reopen / edit                                        #
# --------------------------------------------------------------------------- #


def test_claim_sets_owner_to_writer(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    as_writer(monkeypatch, "alice")
    run("open", "claim test", "--root", root)
    as_writer(monkeypatch, "bob")
    run("claim", "--root", root, "--ticket", "1")
    t = _ticket_by_n(root, 1)
    assert t["owner"] == "bob"
    assert t["status"] == tickets.STATUS_CLAIMED


def test_claim_with_explicit_owner(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "assign test", "--root", root)
    run("claim", "--root", root, "--ticket", "1", "--owner", "carol")
    assert _ticket_by_n(root, 1)["owner"] == "carol"


def test_block_then_reopen(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "block test", "--root", root, "--owner", "alice")
    run("block", "--root", root, "--ticket", "1", "waiting on client")
    assert _ticket_by_n(root, 1)["status"] == tickets.STATUS_BLOCKED
    run("reopen", "--root", root, "--ticket", "1")
    # It had an owner, so reopening returns it to in-progress (claimed).
    assert _ticket_by_n(root, 1)["status"] == tickets.STATUS_CLAIMED


def test_block_keeps_notes_and_reason_separate(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "note keeper", "--root", root, "--notes", "context from open")
    run("block", "--root", root, "--ticket", "1", "waiting on client")
    t = _ticket_by_n(root, 1)
    assert t["notes"] == "context from open"        # never clobbered
    assert t["blocked_reason"] == "waiting on client"
    run("reopen", "--root", root, "--ticket", "1")
    assert _ticket_by_n(root, 1)["blocked_reason"] == ""


def test_suffixed_key_resolves(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaaaa", "title": "plain five", "propose_n": 5},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:01:00", "writer": "bob", "verb": "open",
         "id": "bbbbbbbb", "title": "suffixed five", "propose_n": 5},
    ])
    board = tickets.board_state(root)
    assert tickets.resolve_ticket(board, "ACME-DOC", "5")["id"] == "aaaaaaaa"
    assert tickets.resolve_ticket(board, "ACME-DOC", "5b")["id"] == "bbbbbbbb"
    assert tickets.resolve_ticket(
        board, "ACME-DOC", "ACME-DOC-5b")["id"] == "bbbbbbbb"


def test_reopen_closed_ticket(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "reopen closed", "--root", root)
    run("close", "--root", root, "--ticket", "1", "--source", "done doc")
    assert _ticket_by_n(root, 1)["status"] == tickets.STATUS_CLOSED
    run("reopen", "--root", root, "--ticket", "1")
    assert _ticket_by_n(root, 1)["status"] == tickets.STATUS_OPEN


def test_edit_later_wins_per_field(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "first title", "--root", root, "--due", "2026-09-01")
    run("edit", "--root", root, "--ticket", "1", "--title", "second title")
    run("edit", "--root", root, "--ticket", "1", "--title", "third title")
    t = _ticket_by_n(root, 1)
    assert t["title"] == "third title"
    assert t["due"] == "2026-09-01"   # untouched field survives


def test_open_with_owner_is_claimed(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "owned at open", "--root", root, "--owner", "dana")
    assert _ticket_by_n(root, 1)["status"] == tickets.STATUS_CLAIMED


# --------------------------------------------------------------------------- #
# board / page / export / log                                                  #
# --------------------------------------------------------------------------- #


def test_board_hides_closed_unless_all(tmp_path, monkeypatch, capsys):
    root = make_board(tmp_path, monkeypatch)
    run("open", "visible", "--root", root)
    run("open", "hidden when closed", "--root", root)
    run("close", "--root", root, "--ticket", "2", "--source", "proof")
    capsys.readouterr()
    run("board", "--root", root)
    out = capsys.readouterr().out
    assert "visible" in out
    assert "hidden when closed" not in out
    run("board", "--root", root, "--all")
    out = capsys.readouterr().out
    assert "hidden when closed" in out


def test_view_writes_current_page_to_temp_never_the_root(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "fresh ticket", "--root", root)
    tmp = str(tmp_path / "faketemp")
    os.makedirs(tmp)
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: tmp)
    run("view", "--root", root, "--no-open")
    page = os.path.join(tmp, "ticket-board-ACME-DOC.html")
    assert os.path.exists(page)
    text = open(page, encoding="utf-8").read()
    assert "fresh ticket" in text
    # nothing generated inside the shared folder
    assert not os.path.exists(os.path.join(root, "board.html"))


def test_init_drops_launcher(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    launcher = os.path.join(root, "Open board.cmd")
    assert os.path.exists(launcher)
    text = open(launcher, encoding="utf-8").read()
    assert "serve" in text and "%~dp0" in text


def test_export_guards_against_excel_formulas(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "=2+5|cmd", "--root", root, "--owner", "@dana")
    run("export", "--root", root)
    text = open(os.path.join(root, "board.csv"),
                encoding="utf-8-sig").read()
    assert "'=2+5|cmd" in text     # leading apostrophe defuses the formula
    assert "'@dana" in text
    assert ",=2+5" not in text


def test_board_overdue_flagged(tmp_path, monkeypatch, capsys):
    root = make_board(tmp_path, monkeypatch)
    run("open", "late one", "--root", root, "--due", "2000-01-01")
    capsys.readouterr()
    run("board", "--root", root)
    out = capsys.readouterr().out
    assert "!" in out
    assert "past its deadline" in out


def test_page_generates_self_contained_html(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "on the page", "--root", root, "--due", "2026-12-01")
    run("page", "--root", root)
    html = open(os.path.join(root, "board.html"), encoding="utf-8").read()
    assert os.path.exists(os.path.join(root, "board.md"))
    assert "on the page" in html
    assert "<style>" in html
    # Self-contained: nothing loaded from the network.
    for bad in ("http://", "https://", "src=", "<link"):
        assert bad not in html


def test_page_html_escapes_content(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "danger <script> & co", "--root", root)
    run("page", "--root", root)
    html = open(os.path.join(root, "board.html"), encoding="utf-8").read()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_export_csv_has_bom_and_rows(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "exported", "--root", root)
    run("export", "--root", root)
    path = os.path.join(root, "board.csv")
    raw = open(path, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")   # UTF-8 BOM for Excel
    text = raw.decode("utf-8-sig")
    assert "key,title,owner,due,status,latest update,as of,proof,id" in text
    assert "exported" in text


def test_log_shows_history(tmp_path, monkeypatch, capsys):
    root = make_board(tmp_path, monkeypatch)
    run("open", "trace me", "--root", root)
    run("claim", "--root", root, "--ticket", "1", "--owner", "alice")
    run("comment", "--root", root, "--ticket", "1", "made progress")
    run("close", "--root", root, "--ticket", "1", "--source", "final doc")
    capsys.readouterr()
    run("log", "--root", root, "--ticket", "1")
    out = capsys.readouterr().out
    assert "opened it" in out
    assert "made progress" in out
    assert "closed it" in out
    assert "final doc" in out


# --------------------------------------------------------------------------- #
# the latest-status field (an append-only log; the board shows the newest)     #
# --------------------------------------------------------------------------- #


def test_update_latest_wins_history_kept(tmp_path, monkeypatch, capsys):
    root = make_board(tmp_path, monkeypatch)
    run("open", "track me", "--root", root)
    run("update", "--root", root, "--ticket", "1", "drafting section 2")
    run("update", "--root", root, "--ticket", "1", "sent for review")
    t = _ticket_by_n(root, 1)
    assert t["update"]["text"] == "sent for review"
    assert t["update"]["writer"] == "alice"
    # Both updates are appended events — the older one is still in the log.
    verbs = [e["verb"] for e in _events(root, "alice")]
    assert verbs.count("update") == 2
    capsys.readouterr()
    run("log", "--root", root, "--ticket", "1")
    out = capsys.readouterr().out
    assert "drafting section 2" in out
    assert "sent for review" in out


def test_update_requires_text(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "no empty status", "--root", root)
    with pytest.raises(SystemExit):
        run("update", "--root", root, "--ticket", "1", "   ")
    assert _ticket_by_n(root, 1)["update"] is None


def test_latest_update_shows_on_board_page_and_export(tmp_path, monkeypatch,
                                                      capsys):
    root = make_board(tmp_path, monkeypatch)
    run("open", "visible status", "--root", root)
    run("update", "--root", root, "--ticket", "1", "waiting on the client")
    capsys.readouterr()
    run("board", "--root", root)
    assert "waiting on the client" in capsys.readouterr().out
    run("page", "--root", root)
    html = open(os.path.join(root, "board.html"), encoding="utf-8").read()
    assert "waiting on the client" in html
    run("export", "--root", root)
    csv_text = open(os.path.join(root, "board.csv"),
                    encoding="utf-8-sig").read()
    assert "waiting on the client" in csv_text


# --------------------------------------------------------------------------- #
# full life-cycle smoke                                                        #
# --------------------------------------------------------------------------- #


def test_full_lifecycle_smoke(tmp_path, monkeypatch, capsys):
    root = str(tmp_path / "smoke")
    as_writer(monkeypatch, "alice")
    run("init", "--root", root, "--prefix", "ACME-TP",
        "--name", "Acme transfer pricing 2026")
    run("open", "gather source data", "--root", root)
    run("open", "draft the report", "--root", root)
    run("open", "review with partner", "--root", root)
    run("claim", "--root", root, "--ticket", "1")
    run("block", "--root", root, "--ticket", "2", "waiting on client numbers")
    run("close", "--root", root, "--ticket", "3",
        "--source", "doc name with spaces")
    run("board", "--root", root)
    run("page", "--root", root)
    run("export", "--root", root)
    run("log", "--root", root, "--ticket", "1")

    for fname in ("board.md", "board.html", "board.csv"):
        assert os.path.exists(os.path.join(root, fname))
    html = open(os.path.join(root, "board.html"), encoding="utf-8").read()
    for bad in ("http://", "https://", "<link", "src="):
        assert bad not in html
    board = tickets.board_state(root)
    by_n = {t["n"]: t for t in board.values()}
    assert by_n[1]["status"] == tickets.STATUS_CLAIMED
    assert by_n[2]["status"] == tickets.STATUS_BLOCKED
    assert by_n[3]["status"] == tickets.STATUS_CLOSED
    assert by_n[3]["source"] == "doc name with spaces"


# --------------------------------------------------------------------------- #
# per-ticket prefixes (multi-project boards)                                   #
# --------------------------------------------------------------------------- #
# On a board that tracks several projects, a ticket can carry its own short
# code. Each code numbers from 1 in its own space, so the first ticket under a
# new code is CODE-1 even on a board already holding tickets under other codes.


def test_open_with_prefix_override(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "board-default ticket", "--root", root)
    run("open", "web-side ticket", "--root", root, "--prefix", "web")
    keys = set(_keys(root).values())
    # WEB numbers from 1 in its own space — not board-wide 2.
    assert keys == {"ACME-DOC-1", "WEB-1"}


def test_edit_prefix_relabels_without_renumbering(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "first", "--root", root)
    run("open", "second", "--root", root)
    run("edit", "--root", root, "--ticket", "1", "--prefix", "zeta")
    keys = set(_keys(root).values())
    assert keys == {"ZETA-1", "ACME-DOC-2"}
    # The number is the identity: the old key's number and the new key both
    # still find the same ticket.
    tk = tickets.board_state(root)
    t1 = _ticket_by_n(root, 1)
    assert tickets.resolve_ticket(tk, "ACME-DOC", "ZETA-1")["id"] == t1["id"]
    assert tickets.resolve_ticket(tk, "ACME-DOC", "1")["id"] == t1["id"]
    # A blank prefix goes back to the board's own code.
    run("edit", "--root", root, "--ticket", "1", "--prefix", "")
    assert set(_keys(root).values()) == {"ACME-DOC-1", "ACME-DOC-2"}


def test_prefix_is_tidied_for_display(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    run("open", "messy code", "--root", root, "--prefix", "  gadget ")
    assert set(_keys(root).values()) == {"GADGET-1"}


# --------------------------------------------------------------------------- #
# per-prefix numbering                                                         #
# --------------------------------------------------------------------------- #
# Each effective prefix counts from 1 in its own space. The board's own prefix
# and any per-ticket prefix are independent number lines; the same number under
# two different prefixes is not a clash and takes no letter suffix.


def test_each_prefix_counts_independently(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)   # board prefix ACME-DOC
    # Interleave the two spaces to prove the count is per-prefix, not order.
    run("open", "acme one", "--root", root)                       # ACME-DOC-1
    run("open", "web one", "--root", root, "--prefix", "web")     # WEB-1
    run("open", "acme two", "--root", root)                       # ACME-DOC-2
    run("open", "web two", "--root", root, "--prefix", "web")     # WEB-2
    run("open", "web three", "--root", root, "--prefix", "web")   # WEB-3
    run("open", "acme three", "--root", root)                     # ACME-DOC-3
    keys = set(_keys(root).values())
    assert keys == {"ACME-DOC-1", "ACME-DOC-2", "ACME-DOC-3",
                    "WEB-1", "WEB-2", "WEB-3"}
    # No letter suffix anywhere — the same number in two spaces is no clash.
    assert not any(any(c.isalpha() for c in k.rsplit("-", 1)[1])
                   for k in keys)


def test_same_prefix_sync_clash_still_suffixes(tmp_path, monkeypatch):
    """Two writers open into the SAME prefix offline, both proposing 5 — the
    later one (by replay order) keeps the number with a letter: 5 and 5b."""
    root = make_board(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaaaa", "title": "alice five", "propose_n": 5,
         "prefix": "WEB"},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:01:00", "writer": "bob", "verb": "open",
         "id": "bbbbbbbb", "title": "bob five", "propose_n": 5,
         "prefix": "WEB"},
    ])
    keys = _keys(root)
    assert keys["aaaaaaaa"] == "WEB-5"
    assert keys["bbbbbbbb"] == "WEB-5b"


def test_same_number_two_prefixes_no_suffix(tmp_path, monkeypatch):
    """The same proposed number under two DIFFERENT prefixes is not a clash."""
    root = make_board(tmp_path, monkeypatch)
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaaaa", "title": "web two", "propose_n": 2, "prefix": "WEB"},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:01:00", "writer": "bob", "verb": "open",
         "id": "bbbbbbbb", "title": "side two", "propose_n": 2,
         "prefix": "SIDE"},
    ])
    keys = _keys(root)
    assert keys["aaaaaaaa"] == "WEB-2"    # both keep the plain number
    assert keys["bbbbbbbb"] == "SIDE-2"


def test_old_board_wide_log_folds_to_unchanged_keys(tmp_path, monkeypatch):
    """Regression: a log written under the OLD board-wide rule (propose_n =
    max-known+1 across every prefix) must fold to exactly the same keys under
    the per-prefix rule. Events are never rewritten, and board-wide allocation
    guarantees no (prefix, n) pair repeats, so the numbers are identical."""
    root = make_board(tmp_path, monkeypatch)   # board prefix ACME-DOC
    # A synthetic old-style board: numbers run 1..5 across all prefixes.
    write_raw(root, "legacy", [
        {"ts": "2026-08-16T09:00:00", "writer": "legacy", "verb": "open",
         "id": "d0000001", "title": "board one", "propose_n": 1},
        {"ts": "2026-08-16T09:01:00", "writer": "legacy", "verb": "open",
         "id": "d0000002", "title": "web two", "propose_n": 2, "prefix": "WEB"},
        {"ts": "2026-08-16T09:02:00", "writer": "legacy", "verb": "open",
         "id": "d0000003", "title": "board three", "propose_n": 3},
        {"ts": "2026-08-16T09:03:00", "writer": "legacy", "verb": "open",
         "id": "d0000004", "title": "web four", "propose_n": 4,
         "prefix": "WEB"},
        {"ts": "2026-08-16T09:04:00", "writer": "legacy", "verb": "open",
         "id": "d0000005", "title": "side five", "propose_n": 5,
         "prefix": "SIDE"},
    ])
    keys = _keys(root)
    # These are the exact keys the old board-wide code would have shown.
    assert keys == {
        "d0000001": "ACME-DOC-1",
        "d0000002": "WEB-2",
        "d0000003": "ACME-DOC-3",
        "d0000004": "WEB-4",
        "d0000005": "SIDE-5",
    }


def test_edit_prefix_keeps_number_when_free(tmp_path, monkeypatch, capsys):
    root = make_board(tmp_path, monkeypatch)
    run("open", "will move", "--root", root)       # ACME-DOC-1
    run("open", "stays put", "--root", root)        # ACME-DOC-2
    capsys.readouterr()
    # WEB has no ticket 1, so the moved ticket keeps its number as WEB-1.
    run("edit", "--root", root, "--ticket", "1", "--prefix", "web")
    out = capsys.readouterr().out
    assert "WEB-1" in out                           # the new key is printed
    keys = set(_keys(root).values())
    assert keys == {"WEB-1", "ACME-DOC-2"}


def test_edit_prefix_renumbers_on_collision(tmp_path, monkeypatch, capsys):
    root = make_board(tmp_path, monkeypatch)
    run("open", "acme one", "--root", root)                     # ACME-DOC-1
    run("open", "web one", "--root", root, "--prefix", "web")   # WEB-1
    capsys.readouterr()
    # Move ACME-DOC-1 into WEB, where 1 is taken — it takes the next free (2).
    run("edit", "--root", root, "--ticket", "ACME-DOC-1", "--prefix", "web")
    out = capsys.readouterr().out
    assert "WEB-2" in out                           # the renumbered key printed
    keys = set(_keys(root).values())
    assert keys == {"WEB-1", "WEB-2"}
    # The permanent id still resolves, and the number underneath moved cleanly.
    tk = tickets.board_state(root)
    moved = tickets.resolve_ticket(tk, "ACME-DOC", "WEB-2")
    assert moved["title"] == "acme one"
    assert moved["n"] == 2 and moved["nsfx"] == ""


# --------------------------------------------------------------------------- #
# edit-move clash accounting (the fold resolves collisions, not just opens)    #
# --------------------------------------------------------------------------- #
# The open path registers every proposed number in the fold's per-slot
# accounting and suffixes same-slot clashes. Edits that move a ticket into
# another prefix must run through that same accounting, or two offline writers
# can each drop a ticket into the same (prefix, number) slot and the board
# silently shows two tickets with the identical key.


def _read_forward_reverse(root, monkeypatch):
    """Fold the board with the events read in both directions; assert the
    keys are identical (determinism) and return them once."""
    real_listdir = os.listdir
    monkeypatch.setattr(tickets.os, "listdir",
                        lambda p: sorted(real_listdir(p)))
    forward = _keys(root)
    monkeypatch.setattr(tickets.os, "listdir",
                        lambda p: sorted(real_listdir(p), reverse=True))
    reverse = _keys(root)
    assert forward == reverse
    return forward


def test_two_offline_moves_into_same_slot_suffix(tmp_path, monkeypatch):
    """Two writers, each offline, move a ticket into the same free WEB-5 slot.
    Each saw the slot free, so neither edit carries a propose_n. The fold must
    hand the later mover (by replay order) the letter suffix; the earlier keeps
    the plain number, and no third ticket moves."""
    root = make_board(tmp_path, monkeypatch)
    # A bystander that must never be touched by the clash resolution.
    write_raw(root, "seed", [
        {"ts": "2026-08-16T08:00:00", "writer": "seed", "verb": "open",
         "id": "cccccccc", "title": "bystander", "propose_n": 7,
         "prefix": "WEB"},
    ])
    # Two tickets numbered 5, each under its own prefix (no clash there).
    write_raw(root, "alice", [
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaaaa", "title": "foo five", "propose_n": 5, "prefix": "FOO"},
        # ...then alice moves it into WEB, where (from her view) 5 is free.
        {"ts": "2026-08-16T10:00:00", "writer": "alice", "verb": "edit",
         "id": "aaaaaaaa", "prefix": "WEB"},
    ])
    write_raw(root, "bob", [
        {"ts": "2026-08-16T09:00:00", "writer": "bob", "verb": "open",
         "id": "bbbbbbbb", "title": "bar five", "propose_n": 5, "prefix": "BAR"},
        # ...bob does the same, later in replay order (10:01 > 10:00).
        {"ts": "2026-08-16T10:01:00", "writer": "bob", "verb": "edit",
         "id": "bbbbbbbb", "prefix": "WEB"},
    ])
    keys = _read_forward_reverse(root, monkeypatch)
    assert keys["aaaaaaaa"] == "WEB-5"    # earlier mover keeps the plain number
    assert keys["bbbbbbbb"] == "WEB-5b"   # later mover folds with the suffix
    assert keys["cccccccc"] == "WEB-7"    # the bystander never moves


def test_open_and_move_into_same_slot_suffix(tmp_path, monkeypatch):
    """One writer opens WEB-5 while another moves a ticket into WEB-5. The
    fold resolves them exactly like two opens: whoever lands later in replay
    order takes the suffix. Deterministic regardless of file read order."""
    root = make_board(tmp_path, monkeypatch)
    # carol opens WEB-5 directly, earlier in replay order.
    write_raw(root, "carol", [
        {"ts": "2026-08-16T09:00:00", "writer": "carol", "verb": "open",
         "id": "cccccccc", "title": "opened five", "propose_n": 5,
         "prefix": "WEB"},
    ])
    # alice opens FOO-5 then moves it into WEB, later in replay order.
    write_raw(root, "alice", [
        {"ts": "2026-08-16T08:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaaaa", "title": "moved five", "propose_n": 5,
         "prefix": "FOO"},
        {"ts": "2026-08-16T10:00:00", "writer": "alice", "verb": "edit",
         "id": "aaaaaaaa", "prefix": "WEB"},
    ])
    keys = _read_forward_reverse(root, monkeypatch)
    assert keys["cccccccc"] == "WEB-5"    # the open landed first
    assert keys["aaaaaaaa"] == "WEB-5b"   # the move landed later -> suffix


def test_open_and_move_into_same_slot_suffix_reversed_order(tmp_path,
                                                            monkeypatch):
    """The mirror of the above: when the move lands *earlier* than the open,
    the move keeps the plain number and the open takes the suffix. Same rule,
    opposite winner — the replay order decides, nothing else."""
    root = make_board(tmp_path, monkeypatch)
    # alice's move now lands at 09:00; carol's open at 10:00.
    write_raw(root, "alice", [
        {"ts": "2026-08-16T08:00:00", "writer": "alice", "verb": "open",
         "id": "aaaaaaaa", "title": "moved five", "propose_n": 5,
         "prefix": "FOO"},
        {"ts": "2026-08-16T09:00:00", "writer": "alice", "verb": "edit",
         "id": "aaaaaaaa", "prefix": "WEB"},
    ])
    write_raw(root, "carol", [
        {"ts": "2026-08-16T10:00:00", "writer": "carol", "verb": "open",
         "id": "cccccccc", "title": "opened five", "propose_n": 5,
         "prefix": "WEB"},
    ])
    keys = _read_forward_reverse(root, monkeypatch)
    assert keys["aaaaaaaa"] == "WEB-5"    # the move landed first this time
    assert keys["cccccccc"] == "WEB-5b"   # the later open takes the suffix


def test_edit_into_free_slot_still_folds_plain(tmp_path, monkeypatch):
    """Regression: an ordinary move into a free slot is unchanged — plain
    number, empty suffix, no letter anywhere. Solo boards see zero difference."""
    root = make_board(tmp_path, monkeypatch)
    run("open", "will move", "--root", root)                    # ACME-DOC-1
    run("open", "stays put", "--root", root)                    # ACME-DOC-2
    run("edit", "--root", root, "--ticket", "1", "--prefix", "web")
    tk = tickets.board_state(root)
    moved = _ticket_by_n(root, 1)
    assert tickets.key_of("ACME-DOC", moved) == "WEB-1"
    assert moved["nsfx"] == ""
    assert set(_keys(root).values()) == {"WEB-1", "ACME-DOC-2"}


# --------------------------------------------------------------------------- #
# bare-number resolution across prefixes                                       #
# --------------------------------------------------------------------------- #


def test_bare_number_prefers_board_prefix(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)   # board prefix ACME-DOC
    run("open", "board one", "--root", root)                     # ACME-DOC-1
    run("open", "web one", "--root", root, "--prefix", "web")    # WEB-1
    tk = tickets.board_state(root)
    # Both ACME-DOC-1 and WEB-1 exist; a bare "1" resolves to the board's own.
    t = tickets.resolve_ticket(tk, "ACME-DOC", "1")
    assert t["title"] == "board one"
    assert tickets.key_of("ACME-DOC", t) == "ACME-DOC-1"


def test_bare_number_unique_elsewhere_accepted(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)   # board prefix ACME-DOC
    # Only a WEB ticket carries number 2; the board's own space has none.
    run("open", "board one", "--root", root)                     # ACME-DOC-1
    run("open", "web one", "--root", root, "--prefix", "web")    # WEB-1
    run("open", "web two", "--root", root, "--prefix", "web")    # WEB-2
    tk = tickets.board_state(root)
    t = tickets.resolve_ticket(tk, "ACME-DOC", "2")
    assert t["title"] == "web two"


def test_bare_number_ambiguous_dies_listing_candidates(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)   # board prefix ACME-DOC
    # Two different prefixes both carry number 1, and the board's own does not.
    run("open", "web one", "--root", root, "--prefix", "web")    # WEB-1
    run("open", "side one", "--root", root, "--prefix", "side")  # SIDE-1
    tk = tickets.board_state(root)
    # resolve_ticket raises TicketError (a SystemExit) carrying the message;
    # both candidate keys are named so the caller can pick one.
    with pytest.raises(SystemExit) as excinfo:
        tickets.resolve_ticket(tk, "ACME-DOC", "1")
    text = str(excinfo.value)
    assert "WEB-1" in text and "SIDE-1" in text


# --------------------------------------------------------------------------- #
# serve — the interactive local board                                          #
# --------------------------------------------------------------------------- #
# All writes still go through the same python append path the CLI uses; the
# server only ever binds 127.0.0.1 on a free port and never writes a file from
# the browser side. Tests run it in a background thread against a temp root.

import contextlib          # noqa: E402
import http.client         # noqa: E402
import threading           # noqa: E402


@contextlib.contextmanager
def running_board(root):
    server = tickets.make_server(root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def api(port, method, path, token="__use__", origin=None, body=None,
        server=None):
    """One HTTP request to the board. Returns (status, parsed-json-or-bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {}
    if token == "__use__":
        token = server.token if server is not None else None
    if token is not None:
        headers["X-Tickets-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return resp.status, raw


def _events(root, writer):
    path = os.path.join(root, "events", f"{writer}.jsonl")
    return [json.loads(line) for line in open(path, encoding="utf-8")]


def test_serve_board_json_roundtrip(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    run("open", "gather data", "--root", root, "--owner", "bob",
        "--due", "2026-09-30")
    with running_board(root) as (server, port):
        status, body = api(port, "GET", "/api/board", server=server)
    assert status == 200
    assert body["name"] == "Acme documentation 2026"
    t = body["active"][0]
    assert t["title"] == "gather data"
    assert t["owner"] == "bob"
    assert t["due"] == "2026-09-30"
    assert t["key"] == "ACME-DOC-1"
    # the full per-ticket shape the page needs
    for field in ("id", "key", "title", "owner", "due", "status",
                  "blocked_reason", "notes", "source", "comments"):
        assert field in t


def test_serve_full_lifecycle_lands_in_acting_writers_file(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="carol")
    with running_board(root) as (server, port):
        # A browser-side "writer" is ignored — the server derives it.
        assert api(port, "POST", "/api/act", server=server,
                   body={"verb": "open", "title": "do the thing",
                         "writer": "mallory"})[0] == 200
        for payload in (
            {"verb": "claim", "ticket": "1"},
            {"verb": "edit", "ticket": "1", "due": "2026-10-01"},
            {"verb": "block", "ticket": "1", "reason": "waiting on numbers"},
            {"verb": "reopen", "ticket": "1"},
            {"verb": "comment", "ticket": "1", "text": "made a start"},
            {"verb": "close", "ticket": "1", "source": "master file v3"},
        ):
            assert api(port, "POST", "/api/act", server=server,
                       body=payload)[0] == 200
    # Every action is one line in carol's file, in order — and no other
    # writer file was created (the forged "mallory" never got one).
    assert os.listdir(os.path.join(root, "events")) == ["carol.jsonl"]
    verbs = [e["verb"] for e in _events(root, "carol")]
    assert verbs == ["open", "claim", "edit", "block", "reopen",
                     "comment", "close"]
    t = _ticket_by_n(root, 1)
    assert t["status"] == tickets.STATUS_CLOSED
    assert t["source"] == "master file v3"


def test_serve_vouch_close_records_person_source(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="dave")
    with running_board(root) as (server, port):
        api(port, "POST", "/api/act", server=server,
            body={"verb": "open", "title": "vouch me"})
        status, _ = api(port, "POST", "/api/act", server=server,
                        body={"verb": "close", "ticket": "1",
                              "source": "person:dave"})
    assert status == 200
    assert _ticket_by_n(root, 1)["source"] == "person:dave"


def test_serve_close_without_source_is_400_with_hint(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    with running_board(root) as (server, port):
        api(port, "POST", "/api/act", server=server,
            body={"verb": "open", "title": "needs proof"})
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "close", "ticket": "1"})
    assert status == 400
    assert "source" in body["error"].lower()
    assert "person:" in body["error"]


def test_serve_missing_ticket_field_is_400_not_500(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    with running_board(root) as (server, port):
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "claim"})   # no "ticket" at all
    assert status == 400
    assert "ticket" in body["error"].lower()


def test_serve_edit_cannot_blank_a_title(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    with running_board(root) as (server, port):
        api(port, "POST", "/api/act", server=server,
            body={"verb": "open", "title": "keep me"})
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "edit", "ticket": "1", "title": "  "})
    assert status == 400
    assert _ticket_by_n(root, 1)["title"] == "keep me"


def test_serve_concurrent_opens_mint_distinct_tickets(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    results = []
    with running_board(root) as (server, port):
        def hit(i):
            results.append(api(port, "POST", "/api/act", server=server,
                               body={"verb": "open", "title": f"racer {i}"}))
        threads = [threading.Thread(target=hit, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    assert all(status == 200 for status, _ in results)
    board = tickets.board_state(root)
    assert len(board) == 8                       # nothing silently dropped
    assert len({t["id"] for t in board.values()}) == 8   # all ids distinct
    events = _events(root, "alice")
    assert len(events) == 8                      # no interleaved/corrupt lines


def _serving_refused(port):
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        conn.request("GET", "/")
        conn.getresponse().read()
        conn.close()
        return False
    except (ConnectionRefusedError, OSError):
        return True


def test_serve_exits_when_heartbeats_stop(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    monkeypatch.setenv("TICKETS_SERVE_IDLE", "1")
    monkeypatch.setenv("TICKETS_SERVE_GRACE", "0")
    server = tickets.make_server(root)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    watchdog = tickets.start_idle_watchdog(server)
    try:
        watchdog.join(timeout=10)          # no pings -> watchdog fires
        assert not watchdog.is_alive()
        thread.join(timeout=5)
        assert not thread.is_alive()       # serve_forever returned
        assert _serving_refused(port)
    finally:
        server.server_close()


def test_serve_heartbeats_keep_it_alive_and_goodbye_ends_it(tmp_path,
                                                            monkeypatch):
    import time as _time
    root = make_board(tmp_path, monkeypatch, writer="alice")
    monkeypatch.setenv("TICKETS_SERVE_IDLE", "1")
    monkeypatch.setenv("TICKETS_SERVE_GRACE", "0")
    server = tickets.make_server(root)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    watchdog = tickets.start_idle_watchdog(server)
    try:
        # Ping past several idle windows: the watchdog must not fire.
        for _ in range(6):
            status, _b = api(port, "GET", "/api/ping", server=server)
            assert status == 200
            _time.sleep(0.4)
        assert watchdog.is_alive()
        # The closing tab's beacon (token in the query, like sendBeacon sends
        # it) puts the watchdog on a short fuse; with no other tab it exits.
        api(port, "POST", f"/api/goodbye?token={server.token}", token=None)
        watchdog.join(timeout=10)
        assert not watchdog.is_alive()
    finally:
        server.server_close()


def test_launcher_uses_serve_without_a_console(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch)
    text = open(os.path.join(root, "Open board.cmd"), encoding="utf-8").read()
    assert "serve" in text
    assert "pythonw" in text


def test_serve_missing_token_is_403(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    with running_board(root) as (server, port):
        board_status, _ = api(port, "GET", "/api/board", token=None)
        act_status, _ = api(port, "POST", "/api/act", token=None,
                            body={"verb": "comment", "ticket": "1",
                                  "text": "x"})
    assert board_status == 403
    assert act_status == 403


def test_serve_foreign_origin_is_403(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    run("open", "existing", "--root", root)
    with running_board(root) as (server, port):
        status, _ = api(port, "POST", "/api/act", server=server,
                        origin="http://evil.example",
                        body={"verb": "comment", "ticket": "1", "text": "x"})
    assert status == 403
    # nothing was written by the refused request
    assert not os.path.exists(
        os.path.join(root, "events", "evil.jsonl"))


def test_serve_page_is_self_contained(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    with running_board(root) as (server, port):
        status, raw = api(port, "GET", "/", token=None)
        token = server.token
    assert status == 200
    html = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    # the run's token is embedded so the page's own JS can authenticate
    assert token in html
    # nothing is fetched from the internet
    for bad in ("http://", "https://", "src=\"", "<link"):
        assert bad not in html


def test_serve_same_origin_is_allowed(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="alice")
    with running_board(root) as (server, port):
        origin = f"http://127.0.0.1:{port}"
        status, body = api(port, "GET", "/api/board", origin=origin,
                           server=server)
    assert status == 200
    assert "active" in body


def test_serve_update_roundtrip(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="erin")
    with running_board(root) as (server, port):
        api(port, "POST", "/api/act", server=server,
            body={"verb": "open", "title": "status me"})
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "update", "ticket": "1",
                                 "text": "halfway through the draft"})
    assert status == 200
    t = body["board"]["active"][0]
    assert t["update"]["text"] == "halfway through the draft"
    assert t["update"]["writer"] == "erin"   # derived server-side
    # An empty update is refused with a plain message.
    with running_board(root) as (server, port):
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "update", "ticket": "1", "text": ""})
    assert status == 400
    assert "empty" in body["error"]


def test_serve_log_endpoint(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="fred")
    run("open", "history me", "--root", root)
    run("update", "--root", root, "--ticket", "1", "first pass done")
    run("comment", "--root", root, "--ticket", "1", "see the shared doc")
    with running_board(root) as (server, port):
        status, body = api(port, "GET", "/api/log?ticket=1", server=server)
        bad, err = api(port, "GET", "/api/log?ticket=99", server=server)
        noauth, _ = api(port, "GET", "/api/log?ticket=1", token=None)
    assert status == 200
    assert body["key"] == "ACME-DOC-1"
    joined = "\n".join(body["lines"])
    assert "opened it" in joined
    assert "first pass done" in joined
    assert "see the shared doc" in joined
    assert bad == 400 and "error" in err
    assert noauth == 403


def test_serve_edit_can_set_owner_and_prefix(tmp_path, monkeypatch):
    root = make_board(tmp_path, monkeypatch, writer="gina")
    with running_board(root) as (server, port):
        api(port, "POST", "/api/act", server=server,
            body={"verb": "open", "title": "full edit"})
        status, body = api(port, "POST", "/api/act", server=server,
                           body={"verb": "edit", "ticket": "1",
                                 "owner": "harry", "prefix": "side"})
        status2, body2 = api(port, "POST", "/api/act", server=server,
                             body={"verb": "open", "title": "prefixed at birth",
                                   "prefix": "other"})
    assert status == 200
    t = body["board"]["active"][0]
    assert t["owner"] == "harry"
    assert t["key"] == "SIDE-1"
    assert status2 == 200
    keys = {x["key"] for x in body2["board"]["active"]}
    # OTHER numbers from 1 in its own space, independent of SIDE.
    assert keys == {"SIDE-1", "OTHER-1"}
