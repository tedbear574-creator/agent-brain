#!/usr/bin/env python3
"""brain — Agent Brain CLI (append-only typed stream + generated summary tree).

Store lives under the instance data root (BRAIN_ROOT / config / default ./brain).
All paths below are relative to that root (plain text, git-backed, greppable):
  stream/<scope>.log         one entry per line:
                                         "ISO_TS | type | text"          or
                                         "ISO_TS | type | meta | text"   (meta: sup=7,9)
  stream/<scope>/<writer>.log  a SHARED scope's per-writer spool (opt-in via
                             `brain share`): the sibling <scope>.log is frozen
                             legacy history, each writer appends only its own
                             spool, and reads fold all of them deterministically
                             by (ts, writer, seq). Spool ids are <writer>:<seq>;
                             legacy ids stay numeric. Sync-folder safe. See
                             docs/PROTOCOL.md "Shared scopes".
  tree/<scope>/root.md       topical "what is true now" state (generated)
  tree/<scope>/b<lo>-<hi>.md chrono branch summaries (generated)
  tree/<scope>/hazards.md    gotchas + dead ends, VERBATIM, grouped (generated)
  tree/<scope>/hazards.tsv   entry -> subsystem label cache (append-only)

Two paths, on purpose:
  STATE    (decision/milestone/question) is compressed: branches, then root.
  STANDING (gotcha/dead-end/invariant) is NEVER compressed. It is the content
           that cannot be re-derived from code or git, so it is only ever
           indexed — never rewritten, never summarized, never decayed.
           Hazards (gotcha/dead-end) leave the index only via `brain resolve`;
           invariants (what must stay TRUE about a subsystem's design — the
           positive counterpart of a hazard) leave only via `--supersedes`.
           Fixed is not the same as forgotten; the stream keeps every entry.

root is rebuilt FROM the branch summaries, not from itself: it is reproducible
from the stream at any time and cannot drift through successive rewrites.

Scopes are project slugs; "global" is the assistant-home / cross-project scope.
Trees are caches — always rebuildable from the stream. Never hand-edit tree/.

Commands:
  note --project X --type T [--supersedes N,M] [--subsystem L] [--force-type] "text"
                                     append one entry. Types: decision, milestone,
                                     gotcha, invariant, question, papercut. Caps: 800
                                     chars for decision/question (they carry the
                                     reasoning), 300 for the rest. gotcha/invariant
                                     require --subsystem; a gotcha reading as a fixed
                                     bug is rejected (--force-type overrides); a
                                     near-duplicate of a live entry is rejected ->
                                     --supersedes N, or --distinct if it really is a
                                     separate fact. papercut inverts that: duplicates
                                     never block, they COUNT, and recurrence prompts
                                     you to promote it to the board
  resolve --project X N "text"       retire hazard/papercut #N (milestone + sup=N)
  share [--scope X]                  convert a scope to the multi-writer
                                     (sync-folder) layout: freeze its log, route
                                     new notes to a per-writer spool. One-way.
  papercuts [--scope X]              open papercuts clustered by recurrence
  dispatch ...                       RETIRED -> tracked work lives on the ticket
                                     board (tickets.py); the stub names the
                                     replacement command
  wake [--scope X]                   root + newest raw tail + pull map + board
  hazards [--scope X] [--grep RX] [--budget N]   LIVE gotchas/dead-ends verbatim
  design  [--scope X] [--grep RX] [--budget N]   LIVE invariants verbatim
  zoom <scope> <lo>-<hi>             raw entries lo..hi (1-based)
  branches <scope>                   list branch summaries
  decisions|deadends|gotchas|milestones|questions|invariants [--scope X] [-n N]
  grep <regex> [--scope X]           regex over raw streams
  rebuild [--scope X | --all] [--force] [--alert]   regenerate branches + indexes + root
  status                             per-scope counts, watermarks, injected footprint
  lint [--scope X]                   deterministic integrity audit (no LLM)
  sessions [--scope X]               entries grouped by capture session (sid)
  history ingest|summarize|show <id>|"query"
                                     durable transcript archive (SQLite FTS5,
                                     outlives the ~30-day reaper); pull-only search

Durability: every note/resolve auto-commits its stream file to the KB git repo
(the stream is the only non-regenerable artifact); rebuild commits stream+tree.
Entries are stamped with sid=<8 hex> from CLAUDE_CODE_SESSION_ID when present,
giving per-session capture counts (`brain sessions`) a real denominator.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Instance root resolution (SPEC § Instance model). One machine has one brain
# but many boards/registers, so the engine has a single data root, resolved in
# priority order:
#   1. BRAIN_ROOT env var — wins always (tests and evals isolate the store here).
#   2. `root` key in a config file beside this script (brain.config.json), which
#      names the deployment's data folder.
#   3. Default: a `brain/` folder next to this script (folder-copy profile —
#      unzip the package anywhere and it has a working data root, no install).
def _package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _config_path() -> str:
    return os.environ.get("BRAIN_CONFIG") or os.path.join(_package_dir(), "brain.config.json")


def _load_config() -> dict:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolve_root() -> str:
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


KB_ROOT = _resolve_root()
STREAM_DIR = os.path.join(KB_ROOT, "stream")
TREE_DIR = os.path.join(KB_ROOT, "tree")
# The instance ticket board (tickets.py, a sibling of this file). All tracked
# work lives here — the retired dispatch/agenda pair folded into it. Derived from
# the data root so an isolated copy (BRAIN_ROOT) gets its own board; tests
# monkeypatch this to point at a throwaway tickets root built via tickets.py.
TICKETS_ROOT = os.path.join(KB_ROOT, "tickets")

# TYPES is the PARSEABLE set — the stream is append-only, so every type ever
# written must keep reading correctly forever (invariant #32).
TYPES = ("decision", "dead-end", "gotcha", "milestone", "question", "invariant", "papercut")
# WRITABLE_TYPES is what `brain note` still accepts. dead-end (7 entries ever)
# already indexed as a hazard beside gotcha, so the separate type bought nothing.
#
# papercut STAYS. It looked unused at 4 entries, but all four proved actionable
# (three became tickets, one was already fixed) — the type worked and the
# weekly human drain attached to it never happened. It is the accumulator the
# ticket board cannot be: one annoyance is not worth a ticket, the same
# annoyance three times is, and with nowhere to put the first two you never find
# out it recurs. Recurrence is now detected by the CLI (see PAPERCUT_PROMOTE)
# instead of by a standing human duty.
RETIRED_TYPES = {
    "dead-end": "`--type gotcha` — it already indexes beside gotcha in the hazard "
                "index, so the separate type bought nothing",
}
WRITABLE_TYPES = tuple(t for t in TYPES if t not in RETIRED_TYPES)
TYPE_ALIASES = {"deadend": "dead-end", "deadends": "dead-end", "dead-ends": "dead-end",
                "invariants": "invariant", "papercuts": "papercut"}
HAZARD_TYPES = ("gotcha", "dead-end")          # never summarized — indexed verbatim
STANDING_TYPES = ("gotcha", "dead-end", "invariant")  # excluded from compression entirely
# papercut is neither state nor standing: a drained work-queue of small
# annoyances — kept out of the compression path AND the verbatim hazard index.
NOSTATE_TYPES = STANDING_TYPES + ("papercut",)  # never feed the summary/root path
SUBSYSTEM_REQUIRED = ("gotcha", "invariant")   # verbatim-index types need a retrieval axis
# A code fact is atomic ("hook paths need forward slashes") and 300 chars is a
# feature. An advisory fact is conditional — the ruling, its why, and what would
# reverse it — and the reasoning IS the content. In the origin deployment,
# advisory-heavy scopes averaged 94% of a flat 300 cap and their entries were
# being chopped mid-clause by the author to get under the gate. Caps are per
# type; every derived push budget bounds on MAX_TEXT_ANY, never on the default.
MAX_TEXT = 300
MAX_TEXT_BY_TYPE = {"decision": 800, "question": 800}
MAX_TEXT_ANY = max([MAX_TEXT, *MAX_TEXT_BY_TYPE.values()])

# The routing test ships verbatim in CLI help and in write-gate errors (§1/§2):
# the point is to make the operator sort the entry, not merely to block a write.
ROUTING_TEST = (
    "Routing test — is it work that needs doing? -> open a ticket on the board "
    "(tickets.py). Must the next session know this to work correctly? -> "
    "`gotcha`. Neither, but it cost you time? -> `papercut` (it counts; "
    "recurrence is what turns it into a ticket)."
)
# Fix-language: a gotcha that records its own fix is a war story, not a standing
# nuance — reject it (override with --force-type for the rare genuine case).
FIX_LANGUAGE_RX = re.compile(
    r"\b(fixed|resolved)\b.*\b[0-9a-f]{7}\b|resolved same (run|session)|fixed:",
    re.IGNORECASE)

# The tree is built MECHANICALLY — no model calls, no scheduler. Branch gists
# and root state are deterministic functions of the stream, rebuilt inline on
# every write, so nothing is ever stale and the engine has zero LLM dependency
# and zero OS-scheduler dependency. (Wake state is likewise computed live.)
BRANCH_SIZE = 32          # entries per chrono branch gist
BRANCH_VERSION = 3        # bump to invalidate every cached branch gist
ROOT_VERSION = 3          # bump to force a root regeneration
ROOT_MAX_LINES = 28       # root is orientation, not the memory — keep it small
WAKE_TAIL_CAP = 12        # newest raw entries wake prints (retention is tail-first)
WAKE_TAIL_CHARS = 4000    # …and a hard char budget on top, since caps now vary by type
WAKE_STATE_CHARS = 3500   # char budget for wake's computed state view (live rulings)
VIEW_DEFAULT_RECENT = 20  # un-narrowed type views cap out; say what was withheld

# The `-` in the value class carries the asof date (YYYY-MM-DD); every other
# meta value (sup=, sid=) stays within [0-9a-f,#], so old lines are unaffected.
# src= is the citation field (sha / path / doc / url): any run of non-space,
# non-pipe chars — space is the meta separator, " | " the field separator.
# The dedicated `sup=` alternative accepts BOTH id forms: legacy/solo numeric
# refs (sup=7,9) and shared cross-writer refs (sup=alice:3,7) whose ids carry a
# writer prefix and a colon. The generic [a-z]{2,8}=[0-9a-f,#]+ class could never
# hold a writer:seq ref, so sup gets its own token that permits [A-Za-z._:-].
_SUP_TOKEN = r"sup=[A-Za-z0-9._:,#-]+"
_META_TOKEN = (
    rf"(?:{_SUP_TOKEN}|asof=\d{{4}}-\d{{2}}-\d{{2}}|src=[^\s|]+|[a-z]{{2,8}}=[0-9a-f,#]+)"
)
META_RX = re.compile(rf"^{_META_TOKEN}( {_META_TOKEN})*$")


def _sup_refs(meta: str) -> list[str]:
    """Supersede reference ids stored in `meta`, as strings — each is either a
    numeric legacy/solo id ('7') or a cross-writer id ('alice:3'). '#' prefixes
    (if any) are stripped so a ref reads the same as the id it points at."""
    m = re.search(r"\bsup=([A-Za-z0-9._:,#-]+)", meta or "")
    if not m:
        return []
    return [r.lstrip("#") for r in m.group(1).split(",") if r.strip()]


def _cap(typ: str) -> int:
    # BRAIN_NO_CAPS lifts the per-type char caps (eval ablation arms only —
    # production writes stay capped; see brain eval/live caps A/B).
    if os.environ.get("BRAIN_NO_CAPS"):
        return 100_000
    return MAX_TEXT_BY_TYPE.get(typ, MAX_TEXT)


# ---------------------------------------------------------------- write-time dedup
# The dominant failure of an append-only memory is not "could not find it", it is
# two live entries that disagree: the old preference and the new one both index,
# retrieval serves both, behaviour goes inconsistent. The store had 3 supersedes
# across 1072 entries — the reconciliation verb existed and nothing invoked it.
# So reconciliation moves to the write path, where the author still has the
# context to judge. Lexical and deterministic on purpose: no embeddings anywhere
# in this system (assistant-layer invariant).

DUP_STOP = {
    "the", "and", "for", "that", "this", "with", "from", "not", "but", "was", "are",
    "its", "it's", "into", "onto", "than", "then", "when", "what", "which", "who",
    "why", "how", "has", "had", "have", "been", "being", "does", "did", "doing",
    "you", "your", "our", "their", "they", "them", "his", "her", "hers", "one",
    "two", "any", "all", "each", "per", "via", "off", "out", "over", "under",
    "only", "also", "just", "now", "new", "old", "use", "used", "using", "get",
    "gets", "got", "can", "will", "would", "should", "could", "must", "may",
    "brain", "kb", "note", "entry", "session", "sessions",
}
DUP_MIN_TOKENS = 6     # below this, overlap is noise
# 0.50, not 0.55: a genuine restatement of an existing ruling scored 0.53 and
# slipped through as a warning. A backtest over 943 entries — 0.42% would
# block at 0.55, 0.95% at 0.50, 1.06% at 0.45. The step from 0.50 to 0.45 buys
# almost nothing and starts catching entries that merely share a subject.
DUP_BLOCK = 0.50       # reject: reconcile explicitly
# …except for papercut, where a duplicate is the POINT: repetition is the only
# evidence that a small annoyance is worth fixing. So papercuts never block —
# they count, and at this many the CLI says to put it on the board.
PAPERCUT_PROMOTE = 3
DUP_WARN = 0.35        # surface, allow through
DUP_SHOW = 3           # matches printed


def _dup_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", text.lower())
            if w not in DUP_STOP}


def _dup_score(a: set[str], b: set[str]) -> float:
    """Jaccard, floored by scaled containment so a tightened restatement of an
    existing entry (a strict superset or subset) still scores as a duplicate."""
    if len(a) < DUP_MIN_TOKENS or len(b) < DUP_MIN_TOKENS:
        return 0.0
    overlap = len(a & b)
    if not overlap:
        return 0.0
    jaccard = overlap / len(a | b)
    containment = overlap / min(len(a), len(b))
    return max(jaccard, containment * 0.8)


def _dup_family(typ: str) -> tuple[str, ...]:
    """Contradictions live within a type: a ruling disagrees with a ruling, a
    hazard restates a hazard. gotcha/dead-end are one family (retired type)."""
    return HAZARD_TYPES if typ in HAZARD_TYPES else (typ,)


def _near_duplicates(entries: list[str], dead: dict[int, int], typ: str, text: str):
    """-> [(score, idx, line)] over LIVE same-family entries, best first."""
    family = _dup_family(typ)
    cand = _dup_tokens(text)
    hits = []
    for i, ln in enumerate(entries, start=1):
        if i in dead:
            continue
        p = _parse(ln)
        if not p or p[1] not in family:
            continue
        score = _dup_score(cand, _dup_tokens(p[3]))
        if score >= DUP_WARN:
            hits.append((score, i, ln))
    hits.sort(key=lambda h: (-h[0], -h[1]))
    return hits


def _scope_path(scope: str) -> str:
    return os.path.join(STREAM_DIR, f"{scope}.log")


# --------------------------------------------------------------------------- #
# Multi-writer (shared) scopes — SPEC "Shared scopes"                          #
# --------------------------------------------------------------------------- #
# A sync folder (OneDrive, Dropbox) corrupts a single file two machines append
# to at once. The proven answer, copied from tickets.py/registers.py: one spool
# file PER WRITER (stream/<scope>/<writer>.log) plus a deterministic read-side
# fold. No two machines ever write the same file, so nothing conflicts.
#
# A scope is SHARED iff stream/<scope>/ exists (a directory). Sharing is opt-in
# (`brain share`): it freezes the existing stream/<scope>.log as read-only
# legacy history and routes every later note to the writing machine's own spool.
# Solo scopes (a lone stream/<scope>.log, no dir) are untouched by any of this.


def _spool_dir(scope: str) -> str:
    return os.path.join(STREAM_DIR, scope)


def _spool_path(scope: str, writer: str) -> str:
    return os.path.join(_spool_dir(scope), f"{writer}.log")


def _is_shared(scope: str) -> bool:
    return os.path.isdir(_spool_dir(scope))


def _safe_writer(name: str) -> str:
    """Keep a writer id usable as a filename (mirrors tickets.py _safe_name)."""
    keep = [c if (c.isalnum() or c in "-_.") else "-" for c in name]
    return "".join(keep).strip("-") or "unknown"


def _writer_id() -> str:
    """The id of the machine/person appending to a shared scope. One spool file
    per writer, forever. Resolution order: BRAIN_WRITER env (test/CI isolation),
    then the `writer` key in brain.config.json, then the same derivation
    tickets.py uses (the last segment of the home path)."""
    override = os.environ.get("BRAIN_WRITER")
    if override and override.strip():
        return _safe_writer(override.strip())
    cfg = _load_config().get("writer")
    if cfg and str(cfg).strip():
        return _safe_writer(str(cfg).strip())
    home = os.path.expanduser("~")
    parts = [p for p in re.split(r"[\\/]+", home) if p]
    return _safe_writer(parts[-1] if parts else "") or "unknown"


def _read_file_lines(path: str) -> list[str]:
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


def _spool_writers(scope: str) -> list[str]:
    """Writer ids with a spool in this shared scope, deterministically ordered."""
    sdir = _spool_dir(scope)
    if not os.path.isdir(sdir):
        return []
    return sorted(fn[:-4] for fn in os.listdir(sdir) if fn.endswith(".log"))


def _fold(scope: str) -> tuple[list[str], list[str]]:
    """Deterministic read-side merge of a shared scope -> (lines, ids), parallel.

    Display order sorts by (write-ts, writer, seq); legacy frozen entries carry
    writer='' so they sort first on ties, preserving their original file order.
    A late-arriving synced spool may interleave differently, but every entry's
    STABLE id never moves: legacy line N -> "N", spool line S of writer W ->
    "W:S". Same set of files -> identical fold on every machine, no coordination.
    """
    rows: list[tuple[str, str, int, str, str]] = []
    for seq, ln in enumerate(_read_file_lines(_scope_path(scope)), start=1):
        p = _parse(ln)
        rows.append((p[0] if p else "", "", seq, ln, str(seq)))
    for w in _spool_writers(scope):
        for seq, ln in enumerate(_read_file_lines(_spool_path(scope, w)), start=1):
            p = _parse(ln)
            rows.append((p[0] if p else "", w, seq, ln, f"{w}:{seq}"))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in rows], [r[4] for r in rows]


def _ids_for(scope: str) -> list[str] | None:
    """Stable-id list parallel to _read_entries, or None for a solo scope (where
    the 1-based position IS the stable id and no translation is needed)."""
    return _fold(scope)[1] if _is_shared(scope) else None


def _did(ids: list[str] | None, pos: int) -> str:
    """Display/reference id for a 1-based folded position."""
    return ids[pos - 1] if ids else str(pos)


def _read_entries(scope: str) -> list[str]:
    if _is_shared(scope):
        return _fold(scope)[0]
    path = _scope_path(scope)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


def _scopes() -> list[str]:
    if not os.path.isdir(STREAM_DIR):
        return []
    names: set[str] = set()
    for fn in os.listdir(STREAM_DIR):
        path = os.path.join(STREAM_DIR, fn)
        if fn.endswith(".log") and os.path.isfile(path):
            names.add(fn[:-4])           # solo scope, or a shared scope's frozen log
        elif os.path.isdir(path):
            names.add(fn)                # a shared scope's spool directory
    return sorted(names)


def _parse(line: str):
    """-> (ts, type, meta, text). Accepts 3-field (legacy) and 4-field lines.

    A 4th field only counts as meta if it looks like one (`sup=7`) — entry text
    that happens to contain " | " must not be mistaken for a meta field.
    """
    p4 = line.split(" | ", 3)
    if len(p4) == 4 and META_RX.match(p4[2]):
        return p4[0], p4[1], p4[2], p4[3]
    p3 = line.split(" | ", 2)
    if len(p3) != 3:
        return None
    return p3[0], p3[1], "", p3[2]


def _meta_get(meta: str, key: str) -> list[int]:
    m = re.search(rf"\b{key}=([\d,#]+)", meta)
    return [int(x) for x in re.findall(r"\d+", m.group(1))] if m else []


def _meta_str(meta: str, key: str) -> str:
    m = re.search(rf"\b{key}=([0-9a-f,#]+)", meta)
    return m.group(1) if m else ""


SRC_RX = re.compile(r"\bsrc=([^\s|]+)")


def _src(meta: str) -> str:
    """The `src=` citation (commit sha, file path, doc, url), or ''."""
    m = SRC_RX.search(meta or "")
    return m.group(1) if m else ""


# pin=1 marks an identity PIN: a small invariant loaded VERBATIM at the top of
# every wake, never trimmed or summarized. Stored as a meta token exactly like
# src= — append-only, parsed by regex, extends the format without changing it
# (`pin` is 3 chars and `1` is [0-9a-f], so META_RX already accepts it).
PIN_RX = re.compile(r"\bpin=1\b")
# Hard cap on the SUM of a scope's live pinned texts. Pins are always loaded, so
# their budget is a real context cost — mirrors the per-entry char caps.
PIN_BUDGET = 1200


def _pinned(meta: str) -> bool:
    """True if this entry carries the identity-pin marker."""
    return bool(PIN_RX.search(meta or ""))


def _is_pinned(line: str) -> bool:
    """A pin is an invariant carrying pin=1 — the type is enforced at write."""
    p = _parse(line)
    return bool(p) and p[1] == "invariant" and _pinned(p[2])


ASOF_RX = re.compile(r"\basof=(\d{4}-\d{2}-\d{2})")


def _asof(meta: str) -> str:
    """The `asof=` date (when the fact was TRUE), or '' if the entry only
    carries a write timestamp. Distinct from the write ts on purpose:
    backfilled imports must not read as newest just because they were recorded
    last."""
    m = ASOF_RX.search(meta or "")
    return m.group(1) if m else ""


def _effective_date(line: str) -> str:
    """Sort key for display/rebuild ordering: asof when present, else the write
    ts. NEVER empty — an undated entry falls back to its ts so it can't lose to
    an older dated one. ISO strings sort lexically, and a date-only `asof`
    (2026-06-30) orders correctly against a `T`-stamped ts (2026-08-15T19:15)."""
    p = _parse(line)
    if not p:
        return ""
    return _asof(p[2]) or p[0]


def _order_by_date(indices, entries):
    """1-based `indices` reordered by effective date, stable on ties (write
    order, which is entry identity, breaks ties). Numbers are NOT changed."""
    return sorted(indices, key=lambda i: _effective_date(entries[i - 1]))


def _superseded(entries: list[str], ids: list[str] | None = None,
                dangling: list[tuple[int, str]] | None = None) -> dict[int, int]:
    """superseded position -> position of the LIVE head of its supersession chain.

    Keyed by 1-based POSITION in the folded list (so `idx in dead` works for all
    the positional read code), even though supersede refs are STORED as stable
    ids. For a solo scope `ids is None` and refs are numeric == position — the
    original behaviour, unchanged. For a shared scope `ids` maps stable id ->
    position; a ref whose target isn't present yet (a spool not synced) resolves
    to nothing and is appended to `dangling` (a normal sync-lag state, warned by
    lint, never an error).

    A dict, not a set: a verbatim read that hits a stale ruling must point
    FORWARD to its replacement, or the "never paraphrased" path serves the
    old ruling with more confidence than the lossy root does. The pointer
    walks the chain to its live head — a one-hop pointer would serve an
    intermediate (itself-superseded) entry as if current.
    """
    idpos = {sid: i for i, sid in enumerate(ids, start=1)} if ids is not None else None
    direct: dict[int, int] = {}
    for j, ln in enumerate(entries, start=1):
        p = _parse(ln)
        if p and "sup=" in p[2]:
            for ref in _sup_refs(p[2]):
                if idpos is None:
                    pos = int(ref) if ref.isdigit() else None
                else:
                    pos = idpos.get(ref)
                if pos is None:
                    if dangling is not None:
                        dangling.append((j, ref))
                    continue
                direct[pos] = j
    out: dict[int, int] = {}
    for n in direct:
        j, seen = direct[n], {n}
        while j in direct and j not in seen:
            seen.add(j)
            j = direct[j]
        out[n] = j
    return out


def _fmt(idx: int, line: str, superseded: dict[int, int] | None = None,
        ids: list[str] | None = None) -> str:
    disp = _did(ids, idx)
    p = _parse(line)
    if not p:
        return f"#{disp} {line}"
    ts, typ, meta, text = p
    tail = ""
    asof = _asof(meta)
    if asof:
        tail += f" [asof {asof}]"
    src = _src(meta)
    if src:
        tail += f" [src {src}]"
    if "sup=" in meta:
        tail += " (supersedes " + ", ".join(f"#{r}" for r in _sup_refs(meta)) + ")"
    if superseded and idx in superseded:
        tail += f" [SUPERSEDED by #{_did(ids, superseded[idx])}]"
    return f"#{disp} {ts} [{typ}] {text}{tail}"


def _is_hazard(line: str) -> bool:
    p = _parse(line)
    return bool(p) and p[1] in HAZARD_TYPES


def _is_standing(line: str) -> bool:
    """Standing entries (hazards + invariants) never enter the compression
    path — they are indexed verbatim and leave only by resolve/supersede."""
    p = _parse(line)
    return bool(p) and p[1] in STANDING_TYPES


def _is_papercut(line: str) -> bool:
    p = _parse(line)
    return bool(p) and p[1] == "papercut"


def _state_indices(entries: list[str], lo: int, hi: int, dead: set[int]) -> list[int]:
    """1-based indices in [lo,hi] that feed the summary path.

    Papercuts are excluded alongside standing entries: they are a drained queue,
    not durable state, and must never leak into a root/branch summary."""
    return [
        i for i in range(lo, min(hi, len(entries)) + 1)
        if entries[i - 1] and not _is_standing(entries[i - 1])
        and not _is_papercut(entries[i - 1]) and i not in dead
    ]


# ---------------------------------------------------------------- durability

# Commit-on-write durability knobs. A single fixed 15s window used to kill git
# on a loaded machine and orphan .git/index.lock, after which every later write
# silently skipped its commit (D-37, observed live under 7 parallel owner runs).
# Now: shorter per-op timeout + bounded backoff (total wall <= COMMIT_DEADLINE),
# self-healing of the lock we orphan, and a pending-commit marker so a skipped
# commit is recoverable — the next successful write sweeps the backlog.
COMMIT_TIMEOUT = 8            # per-op (add / commit) kill window, seconds
COMMIT_DEADLINE = 10.0        # total wall budget for one _git_commit effort
COMMIT_ATTEMPTS = 4           # max add+commit tries before giving up to the marker


def _lock_path() -> str:
    return os.path.join(KB_ROOT, ".git", "index.lock")


def _pending_path() -> str:
    # Under .git/ so it is never itself tracked or swept into a commit.
    return os.path.join(KB_ROOT, ".git", "brain-pending-commit")


def _mark_pending() -> None:
    """Flag that a commit was skipped so the NEXT successful write sweeps the
    backlog. Best-effort: a missing marker only costs a delayed sweep."""
    try:
        if os.path.isdir(os.path.dirname(_pending_path())):
            with open(_pending_path(), "w", encoding="utf-8") as f:
                f.write("brain: a commit was skipped; next write sweeps the backlog\n")
    except OSError:
        pass


def _sweep_pending(git: str) -> None:
    """On a successful commit, if an earlier write left the pending marker, sweep
    every uncommitted KB artifact into one commit (git add -A) and drop the
    marker. Path-scoped commits alone can't recover a *different* scope's skipped
    write, so the sweep is deliberately repo-wide — the KB repo holds only KB
    artifacts."""
    p = _pending_path()
    if not os.path.exists(p):
        return
    try:
        subprocess.run([git, "-C", KB_ROOT, "add", "-A"],
                       capture_output=True, timeout=COMMIT_TIMEOUT)
        c = subprocess.run([git, "-C", KB_ROOT, "commit", "-q", "-m",
                            "brain: sweep pending commit backlog"],
                           capture_output=True, timeout=COMMIT_TIMEOUT)
    except Exception:
        return  # leave the marker for the next writer to retry
    if b"index.lock" in c.stderr:
        return  # still contended — keep the marker
    try:
        os.remove(p)
    except OSError:
        pass


def _clear_orphaned_lock() -> bool:
    """Remove .git/index.lock unconditionally. Only called after OUR OWN git was
    killed at the timeout: a lock held by a live *other* git makes git fail fast
    ('File exists'), it does not hang — so a hang means the leftover lock is the
    one our killed git created. Returns True if a lock was removed."""
    try:
        os.remove(_lock_path())
        return True
    except OSError:
        return False


def _remove_stale_lock(seen_mtime: float) -> bool:
    """Remove .git/index.lock only if it is the SAME lock we saw at the start of
    the commit effort, untouched throughout (mtime unchanged). A genuinely live
    git holding/replacing the lock advances its mtime, so an unchanged mtime
    across our whole backoff window means the holder is gone (crashed/killed) and
    the lock is stale. Returns True if removed."""
    try:
        if abs(os.path.getmtime(_lock_path()) - seen_mtime) < 1e-3:
            os.remove(_lock_path())
            return True
    except OSError:
        pass
    return False


def _backoff(attempt: int) -> float:
    return min(0.25 * (2 ** attempt), 1.0)


def _git_commit(paths: list[str], msg: str) -> None:
    """Commit-on-write. The stream is the only non-regenerable artifact in the
    KB; 'git-backed' is only a durability invariant if commits actually happen
    at write time (4 days of uncommitted stream logs observed 2026-07-31).
    Best-effort and path-scoped: never blocks the note, never sweeps unrelated
    dirty files into a *normal* commit. Self-heals an orphaned index.lock and,
    on a genuine skip, leaves a marker so the next write sweeps the backlog."""
    git = shutil.which("git")
    if not git:
        return
    paths = [p for p in paths if os.path.exists(os.path.join(KB_ROOT, p))]
    if not paths:
        return
    # Remember the lock we start with (if any): only a lock still bit-identical
    # to this one after our whole backoff window is safe to treat as stale.
    lock = _lock_path()
    lock_seen = os.path.getmtime(lock) if os.path.exists(lock) else None
    deadline = time.time() + COMMIT_DEADLINE
    committed = False
    for attempt in range(COMMIT_ATTEMPTS):
        try:
            a = subprocess.run([git, "-C", KB_ROOT, "add", "--"] + paths,
                               capture_output=True, timeout=COMMIT_TIMEOUT)
            c = subprocess.run([git, "-C", KB_ROOT, "commit", "-q", "-m", msg, "--"] + paths,
                               capture_output=True, timeout=COMMIT_TIMEOUT)  # no-op exit 1 when clean
        except subprocess.TimeoutExpired:
            # Our git was killed mid-op; clear the lock it orphaned so the retry
            # (and any later write) can proceed.
            if _clear_orphaned_lock():
                print("(git commit: cleared our own index.lock after timeout)",
                      file=sys.stderr)
            if attempt < COMMIT_ATTEMPTS - 1 and time.time() < deadline:
                time.sleep(_backoff(attempt))
                continue
            break
        except Exception as e:
            print(f"(git commit skipped: {e})", file=sys.stderr)
            break
        if b"index.lock" not in a.stderr + c.stderr:
            committed = True
            break
        # Lock contention. Give a real concurrent git the backoff window to
        # release; only if the lock is still there AND unchanged from the start
        # (no live holder) do we treat it as stale and clear it for one last try.
        if attempt < COMMIT_ATTEMPTS - 1 and time.time() < deadline:
            time.sleep(_backoff(attempt))
            continue
        if lock_seen is not None and _remove_stale_lock(lock_seen):
            print("(git commit: cleared stale index.lock)", file=sys.stderr)
            try:
                subprocess.run([git, "-C", KB_ROOT, "add", "--"] + paths,
                               capture_output=True, timeout=COMMIT_TIMEOUT)
                c = subprocess.run([git, "-C", KB_ROOT, "commit", "-q", "-m", msg, "--"] + paths,
                                   capture_output=True, timeout=COMMIT_TIMEOUT)
                committed = b"index.lock" not in c.stderr
            except Exception:
                committed = False
        break
    if not committed:
        _mark_pending()
        print("(git commit skipped; left pending-commit marker — next brain write "
              "sweeps the backlog)", file=sys.stderr)
        return
    _sweep_pending(git)


def _alert(body: str) -> None:
    """Failure doorbell. Default surface is stderr; set BRAIN_ALERT_CMD to a
    shell command to route rebuild failures somewhere durable (the failure body
    is appended as the final argument)."""
    cmd = os.environ.get("BRAIN_ALERT_CMD")
    if not cmd:
        print(f"[brain rebuild FAILED] {body}", file=sys.stderr)
        return
    try:
        subprocess.run(cmd + " " + body, shell=True,
                       capture_output=True, timeout=30, check=True)
    except Exception as e:
        print(f"[brain rebuild FAILED] {body} (alert hook failed: {e})", file=sys.stderr)


# ---------------------------------------------------------------- note

def cmd_note(args) -> int:
    typ = TYPE_ALIASES.get(args.type, args.type)
    if typ in RETIRED_TYPES:
        print(f"error: `{typ}` is retired — use {RETIRED_TYPES[typ]}.\n"
              f"{ROUTING_TEST}", file=sys.stderr)
        return 1
    if typ not in WRITABLE_TYPES:
        print(f"error: type must be one of {', '.join(WRITABLE_TYPES)}", file=sys.stderr)
        return 1
    text = " ".join(args.text).strip()
    if "\n" in text or "\r" in text:
        print("error: entry must be a single line", file=sys.stderr)
        return 1
    if not text:
        print("error: empty entry", file=sys.stderr)
        return 1
    cap = _cap(typ)
    if len(text) > cap:
        hint = ("" if cap > MAX_TEXT else
                "  (decision/question carry the reasoning and cap at "
                f"{MAX_TEXT_ANY}; reasoning too long for either belongs in "
                "projects/<scope>/<topic>.md, with the ruling noted here)")
        print(f"error: {len(text)} chars > {cap} cap for {typ} — compress it{hint}",
              file=sys.stderr)
        return 1
    # ---- asof: the second time axis (§1) ---------------------------------
    # When the fact was TRUE, recorded separately from when it was written.
    # Rejected at write if malformed so a bad date never reaches the stream.
    asof = (getattr(args, "asof", None) or "").strip()
    if asof:
        # Canonical zero-padded form required: strptime alone accepts 2026-6-3,
        # which ASOF_RX could never extract back out of the stream.
        ok = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", asof))
        if ok:
            try:
                datetime.strptime(asof, "%Y-%m-%d")
            except ValueError:
                ok = False
        if not ok:
            print(f"error: --asof {asof!r} is not a valid YYYY-MM-DD date (zero-padded).\n"
                  f"  fix: brain note -t {args.type} --asof 2026-06-30 "
                  f"{'-p ' + args.project + ' ' if args.project else ''}\"...\"",
                  file=sys.stderr)
            return 1
    # ---- write gate (§2) -------------------------------------------------
    subsystem = getattr(args, "subsystem", None)
    force_type = getattr(args, "force_type", False)
    pin = getattr(args, "pin", False)
    # ---- pin gate: identity pins are invariants only ---------------------
    # --pin is a property of the invariant type (always-loaded identity facts).
    # Any other type is a plain error — not a silent no-op.
    if pin and typ != "invariant":
        print("error: --pin is only valid with --type invariant — a pin is an "
              "always-loaded identity fact, which is an invariant.", file=sys.stderr)
        return 1
    if pin and not (subsystem and subsystem.strip()):
        print("error: --pin requires --subsystem — pins are indexed by subsystem "
              "like any invariant; use --subsystem identity for identity facts.",
              file=sys.stderr)
        return 1
    if typ in SUBSYSTEM_REQUIRED and not (subsystem and subsystem.strip()):
        print(f"error: --type {typ} requires --subsystem — the verbatim index has "
              f"exactly one retrieval axis (its label), so an unlabelled {typ} is "
              f"unfindable.\n{ROUTING_TEST}", file=sys.stderr)
        return 1
    if typ == "gotcha" and not force_type and FIX_LANGUAGE_RX.search(text):
        print("error: this reads like a fixed bug (fix-language: a sha, 'resolved "
              "same run', 'fixed:'), not a standing nuance the next session must "
              "know.\n" + ROUTING_TEST + "\nAlready fixed -> `--type milestone`, or "
              "note the nuance without the war story. Genuine nuance that happens to "
              "match? `--force-type`.", file=sys.stderr)
        return 1
    scope = args.project or "global"
    shared = _is_shared(scope)
    writer = _writer_id() if shared else None
    entries = _read_entries(scope)
    ids = _ids_for(scope)
    meta = ""
    # `refs` holds the supersede reference ids exactly as they are stored (numeric
    # strings for a solo scope, "<writer>:<seq>" for a shared one). `ref_positions`
    # is their 1-based folded positions, for the position-keyed pin-budget math.
    refs: list[str] = []
    ref_positions: set[int] = set()
    if args.supersedes:
        if shared:
            idset = set(ids or [])
            refs = [r.lstrip("#") for r in re.split(r"[,\s]+", args.supersedes.strip()) if r]
            bad = [r for r in refs if r not in idset]
            if bad:
                print(f"error: --supersedes id(s) not found in {scope}: {', '.join(bad)}\n"
                      "  a shared scope references entries by their stable id "
                      "(<writer>:<seq>, or a legacy number) — a target you cannot see "
                      "yet (an unsynced spool) cannot be superseded from here.",
                      file=sys.stderr)
                return 1
            ref_positions = {ids.index(r) + 1 for r in refs}
        else:
            num = [int(x) for x in re.findall(r"\d+", args.supersedes)]
            bad = [r for r in num if r < 1 or r > len(entries)]
            if bad:
                print(f"error: --supersedes out of range for {scope} (1-{len(entries)}): {bad}",
                      file=sys.stderr)
                return 1
            refs = [str(r) for r in num]
            ref_positions = set(num)
        if not refs:
            print("error: --supersedes needs entry numbers", file=sys.stderr)
            return 1
        meta = "sup=" + ",".join(refs)
    # ---- pin budget (§4) -------------------------------------------------
    # The sum of a scope's LIVE pinned texts is hard-capped: pins load on every
    # wake, so their budget is real context, enforced like the per-type caps.
    # A pin this write supersedes frees its chars first (refs drop from live).
    if pin:
        dead_now = _superseded(entries, ids)
        used = 0
        for i, ln in enumerate(entries, start=1):
            if i in dead_now or i in ref_positions:
                continue
            if _is_pinned(ln):
                pp = _parse(ln)
                used += len(pp[3]) if pp else 0
        total = used + len(text)
        if total > PIN_BUDGET:
            print(f"error: pinned facts for {scope} would total {total} chars > "
                  f"{PIN_BUDGET} cap. Pins are always loaded, so the budget is hard — "
                  "consolidate an existing pin or supersede a stale one first "
                  f"(brain design --scope {scope} lists the live pins).", file=sys.stderr)
            return 1
    # ---- write-time reconciliation (§0) ----------------------------------
    # Skipped when the author already reconciled (--supersedes) or ruled the
    # entry genuinely distinct (--distinct). `resolve` routes through here with
    # its own supersedes, so retiring a hazard is never blocked.
    recurrence: list[tuple[float, int, str]] = []
    if not refs and not getattr(args, "distinct", False):
        dead_now = _superseded(entries, ids)
        hits = _near_duplicates(entries, dead_now, typ, text)
        if typ == "papercut":
            # Inverted on purpose: for every other type a duplicate is pollution,
            # for a papercut it is the evidence. Count, never block.
            recurrence = [h for h in hits if h[0] >= DUP_BLOCK]
            hits = []
        blocking = [h for h in hits if h[0] >= DUP_BLOCK]
        if blocking:
            print(f"error: near-duplicate of {len(blocking)} live {typ} "
                  f"entr{'y' if len(blocking) == 1 else 'ies'} in {scope} — two live "
                  "entries that disagree is the failure this gate exists to stop.",
                  file=sys.stderr)
            for score, i, ln in blocking[:DUP_SHOW]:
                print(f"  {score:.2f}  {_fmt(i, ln, ids=ids)}", file=sys.stderr)
            print("Corrects one of them? `--supersedes N` (the old entry drops out of "
                  "the live index, the stream keeps it).\nGenuinely a separate fact? "
                  "`--distinct`.", file=sys.stderr)
            return 1
        for score, i, ln in hits[:DUP_SHOW]:
            print(f"(related, {score:.2f}: {_fmt(i, ln, ids=ids)[:160]})", file=sys.stderr)
    # ---- src: the citation axis ------------------------------------------
    # Points at the evidence OUTSIDE the stream (commit sha, file path, deep
    # doc, url). Convention, not a gate: entries without one are fine.
    src = (getattr(args, "source", None) or "").strip()
    if src:
        if re.search(r"[\s|]", src):
            print("error: --source must be a single token (sha / path / doc / "
                  "url) — no spaces or pipes; separate multiple sources with "
                  "commas", file=sys.stderr)
            return 1
        meta = f"{meta} src={src}".strip()
    # pin marker: stored exactly like src=, never inherited across a supersede
    # (a superseding entry is pinned only if IT passes --pin).
    if pin:
        meta = f"{meta} pin=1".strip()
    # Session stamp: gives per-session capture counts a denominator (pilot
    # criterion 1). Absent outside Claude Code sessions — that's fine.
    if asof:
        meta = f"{meta} asof={asof}".strip()
    sid = re.sub(r"[^0-9a-f]", "",
                 os.environ.get("CLAUDE_CODE_SESSION_ID", "").lower())[:8]
    if sid:
        meta = f"{meta} sid={sid}".strip()
    os.makedirs(STREAM_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M")
    line = f"{ts} | {typ} | {meta} | {text}\n" if meta else f"{ts} | {typ} | {text}\n"
    # Solo -> the one <scope>.log. Shared -> this writer's own spool ONLY, so no
    # two machines ever touch the same file on the sync folder.
    if shared:
        os.makedirs(_spool_dir(scope), exist_ok=True)
        path = _spool_path(scope, writer)
        new_seq = len(_read_file_lines(path)) + 1
        new_id = f"{writer}:{new_seq}"
        commit_paths = [f"stream/{scope}/{writer}.log", f"tree/{scope}"]
    else:
        path = _scope_path(scope)
        commit_paths = [f"stream/{scope}.log", f"tree/{scope}"]
    for attempt in range(5):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            break
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))
    # Re-fold after the write: the new entry's display position (and, for a shared
    # scope, its stable id) is read back from the fold, never assumed.
    new_ids = _ids_for(scope)
    if shared:
        entry_id = new_id                       # stable "<writer>:<seq>"
        n = (new_ids.index(new_id) + 1) if new_ids and new_id in new_ids \
            else len(_read_entries(scope))
    else:
        n = len(_read_entries(scope))
        entry_id = n
    disp = _did(new_ids, n)
    if getattr(args, "subsystem", None):
        if typ in STANDING_TYPES:
            label = _snap_label(set(_load_labels(scope, new_ids).values()),
                                args.subsystem.strip().lower()[:40])
            _append_labels(scope, [(entry_id, label)])
        else:
            print("(--subsystem ignored: only standing entries — gotcha/dead-end/"
                  "invariant — carry labels)", file=sys.stderr)
    extra = " (supersedes " + ",".join(refs) + ")" if refs else ""
    print(f"noted: {scope} #{disp} [{typ}]{extra}")
    if recurrence:
        times = len(recurrence) + 1
        suffix = "th" if 10 <= times % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(times % 10, "th")
        print(f"\nThis is the {times}{suffix} time this "
              f"papercut has been logged in {scope}:", file=sys.stderr)
        for _score, i, ln in recurrence[:DUP_SHOW]:
            print(f"  {_fmt(i, ln, ids=new_ids)[:150]}", file=sys.stderr)
        if times >= PAPERCUT_PROMOTE:
            print(f"Recurring {times}x is the evidence that it is worth fixing — put "
                  f"it on the ticket board now:\n  python {_tickets_cli()} open "
                  f"\"...\" --root {TICKETS_ROOT}\n"
                  f"then retire the papercuts (`brain resolve --project {scope} N "
                  "\"promoted to ticket PROJ-NN\"`).", file=sys.stderr)
    # Rebuild the tree inline on write — it is a deterministic, cheap function
    # of the stream now, so the branch/root/index layer is never stale and no
    # scheduled rebuild task exists.
    _rebuild_scope(scope, force=False)
    _git_commit(commit_paths, f"brain: {scope} #{disp} {typ}")
    return 0


def cmd_resolve(args) -> int:
    """Retire a hazard: append a milestone that supersedes it, dropping it from
    the live index while the stream keeps the original verbatim."""
    scope = args.project or "global"
    entries = _read_entries(scope)
    ids = _ids_for(scope)
    token = str(args.entry).lstrip("#")
    if ids is not None:
        # Shared scope: the argument is a stable id ("<writer>:<seq>" or a legacy
        # number). Resolve it to a folded position.
        if token not in ids:
            print(f"error: no entry {token} in {scope}", file=sys.stderr)
            return 1
        pos = ids.index(token) + 1
    else:
        if not token.isdigit():
            print(f"error: entry must be a number for {scope}", file=sys.stderr)
            return 1
        pos = int(token)
        if pos < 1 or pos > len(entries):
            print(f"error: entry out of range for {scope} (1-{len(entries)})", file=sys.stderr)
            return 1
    if not (_is_hazard(entries[pos - 1]) or _is_papercut(entries[pos - 1])):
        print(f"error: #{token} is not a hazard or papercut — retire an invariant or "
              "stale state entry with `brain note --supersedes N`", file=sys.stderr)
        return 1
    if pos in _superseded(entries, ids):
        print(f"error: #{token} is already resolved", file=sys.stderr)
        return 1
    args.type = "milestone"
    args.supersedes = token
    args.text = ["resolved", f"#{token}:"] + args.text
    return cmd_note(args)


# ---------------------------------------------------------------- share

def cmd_share(args) -> int:
    """Convert a solo scope to the multi-writer (sync-folder) layout.

    Opt-in and one-way: the existing stream/<scope>.log is FROZEN as read-only
    legacy history (its entries keep their numeric ids forever, guaranteed stable
    by the freeze) and a stream/<scope>/ directory is created. From now on every
    machine appends only to its own spool, stream/<scope>/<writer>.log, so no two
    machines ever write the same file — structurally conflict-free on OneDrive /
    Dropbox / any sync folder. Read paths fold the frozen log and all spools
    deterministically; nothing about a solo scope changes."""
    scope = getattr(args, "scope", None) or getattr(args, "project", None) or "global"
    if _is_shared(scope):
        print(f"{scope} is already a shared scope "
              f"(writers: {', '.join(_spool_writers(scope)) or 'none yet'}).")
        return 0
    frozen = len(_read_file_lines(_scope_path(scope)))
    os.makedirs(_spool_dir(scope), exist_ok=True)
    # A marker keeps the spool dir non-empty (so it survives git and a folder
    # copy even before the first note) and records what the conversion froze.
    with open(os.path.join(_spool_dir(scope), ".shared"), "w", encoding="utf-8") as f:
        f.write(f"frozen-legacy-entries: {frozen}\n")
    writer = _writer_id()
    print(f"shared: {scope} — froze {frozen} legacy entr"
          f"{'y' if frozen == 1 else 'ies'} in stream/{scope}.log (read-only history). "
          f"New notes append to stream/{scope}/<writer>.log; this machine writes as "
          f"'{writer}'.")
    _git_commit([f"stream/{scope}", f"stream/{scope}.log"], f"brain: share {scope}")
    return 0


# ---------------------------------------------------------------- tree readers

def _frontmatter(path: str):
    """-> (dict, body) for a tree file, or (None, None) if absent."""
    if not os.path.isfile(path):
        return None, None
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    head: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for ln in text[3:end].splitlines():
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    head[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return head, body.strip()


def _root_meta(scope: str):
    """-> (watermark, generated, version, body, head); (0, None, 0, None, {}) if no root."""
    head, body = _frontmatter(os.path.join(TREE_DIR, scope, "root.md"))
    if head is None:
        return 0, None, 0, None, {}
    try:
        wm = int(head.get("watermark", "0"))
    except ValueError:
        wm = 0
    try:
        ver = int(head.get("v", "0"))
    except ValueError:
        ver = 0
    return wm, head.get("generated"), ver, body, head


def _branch_files(scope: str) -> list[tuple[int, int, str]]:
    d = os.path.join(TREE_DIR, scope)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        m = re.fullmatch(r"b(\d+)-(\d+)\.md", fn)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), os.path.join(d, fn)))
    return sorted(out)


# ---------------------------------------------------------------- hazards

def _hazard_tsv(scope: str) -> str:
    return os.path.join(TREE_DIR, scope, "hazards.tsv")


def _load_labels(scope: str, ids: list[str] | None = None) -> dict[int, str]:
    """Subsystem labels keyed by 1-based folded POSITION (so the positional render
    code consumes them directly). The hazards.tsv stores them by STABLE id: a
    numeric id for a solo/legacy entry, "<writer>:<seq>" for a spool entry. For a
    shared scope the stored id is translated to its current position through the
    fold; an entry whose spool hasn't synced yet simply drops out until it does."""
    if ids is None and _is_shared(scope):
        ids = _fold(scope)[1]
    out: dict[int, str] = {}
    path = _hazard_tsv(scope)
    if not os.path.isfile(path):
        return out
    idpos = {sid: i for i, sid in enumerate(ids, start=1)} if ids is not None else None
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            parts = ln.rstrip("\n").split("\t")
            if len(parts) != 2:
                continue
            key, label = parts[0], parts[1].strip()
            if idpos is None:
                if key.isdigit():
                    out[int(key)] = label
            else:
                pos = idpos.get(key)
                if pos is not None:
                    out[pos] = label
    return out


def _append_labels(scope: str, rows: list[tuple[object, str]]) -> None:
    """Append (id, label) pairs to hazards.tsv. `id` is a numeric position for a
    solo scope, a stable id string ("writer:seq") for a shared one."""
    os.makedirs(os.path.join(TREE_DIR, scope), exist_ok=True)
    with open(_hazard_tsv(scope), "a", encoding="utf-8") as f:
        for idx, label in rows:
            f.write(f"{idx}\t{label}\n")


def _norm_label(label: str) -> str:
    """Canonical form for near-duplicate detection: lowercase, alnum words,
    naive singulars, word-order-insensitive ('stop hooks' == 'hook, stop')."""
    words = re.findall(r"[a-z0-9]+", label.lower())
    words = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]
    return " ".join(sorted(words))


def _snap_label(known: set[str], label: str) -> str:
    """Snap a new label onto an existing one when they differ only by case,
    plural, punctuation, or word order — label drift is a silent recall
    failure of the index's one retrieval axis. A leading '*' (cross-cutting:
    the entry injects into EVERY scope's gate) is owner-set, never minted by
    the labeler, and snaps only within the starred namespace."""
    star = label.startswith("*")
    if star:
        label = label[1:].strip()
        known = {k[1:].strip() for k in known if k.startswith("*")}
    elif any(k.startswith("*") for k in known):
        known = {k for k in known if not k.startswith("*")}
    if label not in known:
        by_norm = {_norm_label(k): k for k in sorted(known)}
        label = by_norm.get(_norm_label(label), label)
    return ("*" + label) if star else label


def _label_standing(scope: str, entries: list[str]) -> dict[int, str]:
    """Ensure every standing entry (hazards + invariants) carries a subsystem
    label. Labels are assigned ONCE and never revised — the index may grow, it
    never rewrites. gotcha/invariant labels are set explicitly at write time
    (--subsystem is required), so this only backfills the rare standing entry
    without one (a dead-end, which does not require a subsystem). Deterministic:
    no model call — an unlabelled standing entry defaults to 'unsorted'."""
    ids = _ids_for(scope)
    labels = _load_labels(scope, ids)
    todo = [i for i, ln in enumerate(entries, start=1)
            if _is_standing(ln) and i not in labels]
    if not todo:
        return labels
    _append_labels(scope, [(_did(ids, i), "unsorted") for i in todo])
    labels.update({i: "unsorted" for i in todo})
    return labels


def _render_index(scope: str, entries: list[str], labels: dict[int, str],
                  types: tuple, noun: str, pull_cmd: str,
                  pattern: str | None = None, dead: set[int] | None = None,
                  budget: int = 0, prefer: str = "",
                  ids: list[str] | None = None) -> str:
    """Verbatim subsystem-grouped index over the given standing types.

    With a budget: groups stay whole (an entry is never cut mid-line); when the
    full index exceeds the budget, whole groups degrade to a one-line digest
    naming the label and entry numbers, with the pull command for the rest.
    Kept-first order: groups whose label shares a token with `prefer` (the
    edited path, at the gate), then newest activity — recency alone is a weak
    proxy for what bites THIS edit. Bounded with an overflow signal, never
    silently."""
    idxs = []
    for i, ln in enumerate(entries, start=1):
        p = _parse(ln)
        if p and p[1] in types:
            idxs.append(i)
    resolved = len([i for i in idxs if dead and i in dead])
    if dead:
        idxs = [i for i in idxs if i not in dead]
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        idxs = [i for i in idxs if rx.search(entries[i - 1])]
    if not idxs:
        return (f"(no live {noun} entries in {scope}"
                + (" matching that pattern" if pattern else "")
                + (f"; {resolved} resolved in the stream)" if resolved else ")"))
    groups: dict[str, list[int]] = {}
    for i in idxs:
        groups.setdefault(labels.get(i, "(unlabelled)"), []).append(i)
    header = (f"{len(idxs)} live {noun} entries — verbatim, never summarized."
              + (f" ({resolved} resolved, omitted — they stay in the stream)"
                 if resolved else ""))

    def block(label: str) -> str:
        return "\n".join([f"\n**{label}** ({len(groups[label])})"]
                         + [f"- {_fmt(i, entries[i - 1], ids=ids)}" for i in groups[label]])

    order = sorted(groups, key=lambda k: (-len(groups[k]), k))
    full = "\n".join([header] + [block(lb) for lb in order])
    if not budget or len(full) <= budget:
        return full
    # Over budget: keep whole groups — relevance-boosted first, then recency.
    # Tokens come from _path_tokens, the same curated matcher --match-path uses:
    # a raw split over the whole path drags in ancestors and generic nouns
    # ("users", "documents", "claude", "session"), which then boost any label
    # containing them on EVERY edit — noise dressed as relevance.
    hot = _path_tokens(prefer)
    hot -= set(re.findall(r"[a-z0-9]{4,}", scope.lower()))
    for _alias in _SCOPE_ALIAS_DIRS.get(scope, ()):
        hot -= set(re.findall(r"[a-z0-9]{4,}", _alias.lower()))

    def _relevance(label: str) -> int:
        """Graded, not boolean: a label hit is the strong signal, and an entry
        whose TEXT names the path counts too — --match-path scores text as well
        as label, and a group should not sink just because its label is vague."""
        if not hot:
            return 0
        score = 2 * len(hot & set(re.findall(r"[a-z0-9]+", label.lower())))
        for i in groups[label]:
            if hot & set(re.findall(r"[a-z0-9]+", entries[i - 1].lower())):
                score += 1
        return score

    by_recency = sorted(groups, key=lambda k: (-_relevance(k), -max(groups[k])))
    kept: list[str] = []
    used = len(header) + 250  # reserve for the digest footer
    for label in by_recency:
        b = block(label)
        if used + len(b) <= budget:
            kept.append(label)
            used += len(b)
    digested = [lb for lb in order if lb not in kept]
    n_digested = sum(len(groups[lb]) for lb in digested)
    # Render in the order they were KEPT (relevance, then recency), not by group
    # size — the model reads top-down, so the group that bites this edit goes
    # first. `kept` is already in that order.
    lines = [header] + [block(lb) for lb in kept]
    digest = "; ".join(
        f"{lb} ({', '.join(f'#{_did(ids, i)}' for i in groups[lb][:6])}"
        + (", …" if len(groups[lb]) > 6 else "") + ")"
        for lb in digested)
    lines.append(f"\n[over budget — {n_digested} entries in digest only: {digest}. "
                 f"Full index: `{pull_cmd}`]")
    return "\n".join(lines)


def _render_hazards(scope: str, entries: list[str], labels: dict[int, str],
                    pattern: str | None = None, dead: set[int] | None = None,
                    budget: int = 0, prefer: str = "",
                    ids: list[str] | None = None) -> str:
    return _render_index(scope, entries, labels, HAZARD_TYPES,
                         "hazard (gotcha / dead-end)",
                         f"brain hazards --scope {scope}", pattern, dead, budget, prefer,
                         ids)


def _render_xcut(exclude_scope: str, cap: int = 1200) -> str:
    """Cross-cutting hazards: entries in OTHER scopes whose subsystem label is
    owner-starred (*windows, *shopify, ...) inject into every scope's gate —
    a hazard that rhymes across projects shouldn't wait to be re-hit locally."""
    rows: list[tuple[str, int, str]] = []
    for s in _scopes():
        if s == exclude_scope:
            continue
        ids = _ids_for(s)
        labels = _load_labels(s, ids)
        starred = {i for i, lb in labels.items() if lb.startswith("*")}
        if not starred:
            continue
        entries = _read_entries(s)
        dead = _superseded(entries, ids)
        for i in sorted(starred):
            if i <= len(entries) and i not in dead and _is_standing(entries[i - 1]):
                rows.append((s, i, f"- {s} [{labels[i]}] {_fmt(i, entries[i - 1], ids=ids)}"))
    if not rows:
        return ""
    rows.sort(key=lambda r: -r[1])  # newest first within the cap
    lines, used, skipped = [], 0, 0
    for _s, _i, ln in rows:
        if used + len(ln) > cap:
            skipped += 1
            continue
        lines.append(ln)
        used += len(ln) + 1
    out = "\n**cross-project (starred labels, all scopes)**\n" + "\n".join(lines)
    if skipped:
        out += f"\n  ({skipped} more — `brain hazards --grep '\\*'`)"
    return out


def _render_invariants(scope: str, entries: list[str], labels: dict[int, str],
                       pattern: str | None = None, dead: set[int] | None = None,
                       budget: int = 0, ids: list[str] | None = None) -> str:
    return _render_index(scope, entries, labels, ("invariant",),
                         "invariant (must stay true)",
                         f"brain design --scope {scope}", pattern, dead, budget, "", ids)


def _live_pins(entries: list[str], dead) -> list[int]:
    """1-based indices of live (non-superseded) pinned invariants, write-order."""
    return [i for i, ln in enumerate(entries, start=1)
            if _is_pinned(ln) and (not dead or i not in dead)]


def _render_pins(entries: list[str], dead, ids: list[str] | None = None) -> str:
    """The identity-pin block: live pins VERBATIM, labelled plainly. Empty string
    when the scope has no live pins. Rendered at the very top of wake and root —
    never trimmed, never summarized."""
    idxs = _live_pins(entries, dead)
    if not idxs:
        return ""
    lines = ["Pinned facts (always loaded):"]
    lines += [f"- {_fmt(i, entries[i - 1], ids=ids)}" for i in idxs]
    return "\n".join(lines)


# --------------------------------------------------------- file-matched hazards

# Generic scaffolding dir/file names carry no subsystem signal — keying a hazard
# on them would manufacture false matches on almost every edit.
_PATH_STOP = {"src", "lib", "dist", "build", "index", "test", "tests",
              "spec", "specs", "node_modules", "pycache", "__pycache__",
              "main", "app",
              # ambient ancestors of every project path — no subsystem signal
              "users", "home", "documents", "claude", "code", "repo", "repos",
              "workspace", "project", "projects"}

# Generic engineering nouns: real words, but they name a LAYER, not a subsystem,
# so they show up in a large fraction of any codebase's hazards and, keyed alone,
# flood the match (config.py -> 11 unrelated CLAUDE_CONFIG_DIR rows, worker.py ->
# launcher noise, useSession.ts -> 19 rows on the bare word "session"). Frequency
# alone can't separate these from a genuine hot subsystem — a domain word like
# `queue` or `transcript` can be just as frequent yet ARE the subsystem — so the
# split is semantic, curated here, not statistical. Domain
# tokens (normalize, amount, theme, charts, feeds, queue, worker, …) are
# deliberately absent so precision is kept, not traded away.
_GENERIC_NOUNS = {
    "config", "configs", "settings", "setting", "server", "servers", "worker",
    "workers", "session", "sessions", "data", "client", "clients", "service",
    "services", "handler", "handlers", "manager", "managers", "util", "utils",
    "utility", "utilities", "helper", "helpers", "common", "core", "model",
    "models", "view", "views", "controller", "controllers", "route", "router",
    "routes", "routing", "request", "requests", "response", "responses", "error",
    "errors", "state", "store", "stores", "module", "modules", "component",
    "components", "hook", "hooks", "page", "pages", "style", "styles", "wrapper",
    "wrappers", "provider", "providers", "context", "value", "values", "item",
    "items", "object", "objects", "entry", "field", "fields", "param", "params",
    "option", "options", "argument", "arguments", "type", "types", "constant",
    "constants", "global", "globals", "shared", "misc", "temp", "default",
    "defaults", "setup", "base", "middleware", "schema", "schemas",
}

# Directory names that resolve to a DIFFERENT scope than themselves (dir != slug).
# Such a dir name is the scope's own alias — it sits on almost every path in the
# scope and carries no discriminating signal, exactly like the scope name itself,
# so it is stripped from the match tokens the same way. Empty by default (a
# deployment where the folder name IS the scope needs none); a deployment with
# renamed dirs sets BRAIN_SCOPE_ALIAS_DIRS to a JSON object {scope: [dir, ...]}.
def _load_scope_alias_dirs() -> dict:
    raw = os.environ.get("BRAIN_SCOPE_ALIAS_DIRS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    out = {}
    if isinstance(data, dict):
        for scope, dirs in data.items():
            if isinstance(dirs, str):
                dirs = [dirs]
            if isinstance(dirs, list):
                out[scope] = {str(d) for d in dirs}
    return out


_SCOPE_ALIAS_DIRS = _load_scope_alias_dirs()
MATCH_CAP = 8  # file-matched hazards a single edit surfaces before pointing to the full index


def _path_tokens(path: str) -> set[str]:
    """Subsystem-signal tokens from a file path: the filename stem plus its two
    nearest parent dirs, split on separators / snake_case / camelCase, lowercased,
    keeping words >= 4 chars that aren't generic scaffolding (src, lib, tests…)
    nor generic engineering nouns (config, server, session, …).

    Deep ancestors (home, Documents, the repo root) carry no subsystem signal and
    would only manufacture false matches, so only the tail of the path is used.

    BRAIN_DISABLE_PATH_STOP=1 keeps the generic-noun words as match tokens — the
    pre-fix, flooding behaviour — so the eval can demonstrate its regression arm
    has teeth without maintaining a second matcher."""
    parts = [p for p in re.split(r"[\\/]+", (path or "").strip()) if p]
    if not parts:
        return set()
    drop_generic = not os.environ.get("BRAIN_DISABLE_PATH_STOP")
    base = parts[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    chunks = parts[-3:-1] + [stem]  # up to two parent dirs + the filename stem
    toks: set[str] = set()
    for chunk in chunks:
        for w in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", chunk):
            w = w.lower()
            if len(w) < 4 or w.isdigit() or w in _PATH_STOP:
                continue
            if drop_generic and w in _GENERIC_NOUNS:
                continue
            toks.add(w)
    return toks


def _hazard_path_matches(scope: str, path: str,
                         exclude: set[int] | None = None) -> list[tuple[int, str]]:
    """Live hazards in `scope` whose text OR subsystem label shares a whole word
    with the edited path's subsystem tokens. Whole-word (set intersection), so a
    path token 'amount' matches the word 'amount' but not the substring in
    'amounts'. Superseded and excluded (already-injected) entries drop out.

    Cheap: one linear pass over the stream, no LLM. This is the retrieval axis the
    just-in-time hazard gate keys on — a session editing a subsystem is shown the
    gotchas/dead-ends that name that subsystem, and nothing else."""
    toks = _path_tokens(path)
    # The whole index is already scoped, so the scope's own name (which shows up
    # as a parent dir on almost every path and is named in most of its hazards)
    # carries no discriminating signal — drop it, or it floods the match. Scope
    # ALIAS dir names (assistant/ under the global scope, aero-pnl/ under
    # aerodrome-lp-tracker, …) sit on the path the same way and are dropped too.
    scope_words = set(re.findall(r"[a-z0-9]{4,}", scope.lower()))
    for alias in _SCOPE_ALIAS_DIRS.get(scope, ()):  # type: ignore[union-attr]
        scope_words |= set(re.findall(r"[a-z0-9]{4,}", alias.lower()))
    toks -= scope_words
    if not toks:
        return []
    exclude = exclude or set()
    entries = _read_entries(scope)
    ids = _ids_for(scope)
    dead = _superseded(entries, ids)
    labels = _load_labels(scope, ids)
    # (matched-token count, id, line): rank by how many DISTINCT path tokens the
    # entry hits, so relevance — not stream position — decides what survives the
    # MATCH_CAP truncation. A file naming two subsystems surfaces the hazard that
    # names both ahead of one that shares only a single word; ties break on
    # recency (higher id first). Insertion order used to truncate blindly, so a
    # flooding common token could push a genuinely relevant hazard past the cap.
    scored: list[tuple[int, int, str]] = []
    for i, ln in enumerate(entries, start=1):
        if i in dead or i in exclude or not _is_hazard(ln):
            continue
        p = _parse(ln)
        if not p:
            continue
        hay = f"{p[3]} {labels.get(i, '')}".lower()
        words = set(re.findall(r"[a-z0-9]+", hay))
        hits = toks & words
        if hits:
            scored.append((len(hits), i, ln))
    scored.sort(key=lambda r: (-r[0], -r[1]))  # most tokens matched, then newest
    return [(i, ln) for _hits, i, ln in scored]


def cmd_hazards(args) -> int:
    match_path = getattr(args, "match_path", None)
    if match_path:
        scope = args.scope or "global"
        exclude = {int(x) for x in re.findall(r"\d+", getattr(args, "exclude", "") or "")}
        matched = _hazard_path_matches(scope, match_path, exclude)
        if not matched:
            return 0  # silent: nothing in this scope keys to the edited path
        entries = _read_entries(scope)
        ids = _ids_for(scope)
        labels = _load_labels(scope, ids)
        capped = matched[:MATCH_CAP]
        print(f"[BRAIN HAZARDS scope={scope} match-path]")
        print(f"{len(matched)} live hazard(s) key to the file you just edited "
              f"(verbatim; whole-word path match — never summarized):")
        for i, ln in capped:
            lb = labels.get(i, "")
            print(f"- {('[' + lb + '] ') if lb else ''}{_fmt(i, entries[i - 1], ids=ids)}")
        if len(matched) > len(capped):
            print(f"  (+{len(matched) - len(capped)} more match — "
                  f"`brain hazards --scope {scope}`)")
        # Machine line the just-in-time gate reads to dedupe once-per-hazard.
        print("MATCHED-IDS: " + ",".join(_did(ids, i) for i, _ in capped))
        return 0
    scopes = [args.scope] if args.scope else _scopes()
    shown = 0
    for s in scopes:
        entries = _read_entries(s)
        if not any(_is_hazard(ln) for ln in entries):
            continue
        ids = _ids_for(s)
        print(f"[BRAIN HAZARDS scope={s}]")
        print(_render_hazards(s, entries, _load_labels(s, ids), args.grep,
                              _superseded(entries, ids), args.budget or 0,
                              getattr(args, "prefer", "") or "", ids))
        if args.scope and not args.grep:
            xcut = _render_xcut(s)
            if xcut:
                print(xcut)
        print()
        shown += 1
    if not shown:
        print("(no hazard entries)")
    return 0


def cmd_design(args) -> int:
    """The design view: live invariants (what must stay true and why),
    grouped by subsystem — the standing counterpart of the hazard index.
    Deterministic; the stream is the source, supersession is the eraser."""
    scopes = [args.scope] if args.scope else _scopes()
    shown = 0
    for s in scopes:
        entries = _read_entries(s)
        if not any(_parse(ln) and _parse(ln)[1] == "invariant" for ln in entries):
            continue
        ids = _ids_for(s)
        print(f"[BRAIN DESIGN scope={s}]")
        print(_render_invariants(s, entries, _load_labels(s, ids), args.grep,
                                 _superseded(entries, ids), args.budget or 0, ids))
        print()
        shown += 1
    if not shown:
        print("(no invariant entries — capture one: "
              "`brain note --type invariant --subsystem LABEL \"what must stay true\"`)")
    return 0


# ---------------------------------------------------------------- wake

def cmd_wake(args) -> int:
    scope = args.scope or "global"
    entries = _read_entries(scope)
    n = len(entries)
    if not n:
        print(f"[BRAIN WAKE scope={scope}] empty stream.")
        return 0
    ids = _ids_for(scope)
    dead = _superseded(entries, ids)
    # Pinned identity facts render VERBATIM at the very top, above the computed
    # state — always loaded, never trimmed, never summarized (§3).
    pins = _render_pins(entries, dead, ids)
    if pins:
        print(pins)
        print()
    # State is COMPUTED at wake time, not model-summarized (design ruling
    # 2026-08-16): live rulings straight from the stream, supersede-resolved,
    # verbatim, newest first. It can never be stale and costs no model call;
    # thematic compression is traded away for traceable entry numbers.
    parsed = [(i, _parse(ln)) for i, ln in enumerate(entries, start=1)]
    live_state = [(i, p) for i, p in parsed
                  if p and p[1] in ("decision", "question") and i not in dead]
    mile = [(i, p) for i, p in parsed
            if p and p[1] == "milestone" and i not in dead]
    print(f"[BRAIN WAKE scope={scope}] state — computed from the stream: "
          f"{len(live_state)} live rulings (of {n} entries), newest first "
          f"(long rulings clipped — `brain decisions` for full text):")
    shown_state, used = [], 0
    for i, p in reversed(live_state):
        line = _fmt(i, entries[i - 1], dead, ids)
        if len(line) > 300:
            line = line[:297] + "..."
        if used + len(line) > WAKE_STATE_CHARS:
            break
        shown_state.append(line)
        used += len(line)
    for line in shown_state:
        print(line)
    older = len(live_state) - len(shown_state)
    if older:
        print(f"   (+{older} older live rulings: brain decisions --scope {scope})")
    if mile:
        recent = "; ".join(f"#{_did(ids, i)} {p[3][:70]}" for i, p in mile[-3:])
        print(f"   recent milestones: {recent}")
    counts: dict[str, int] = {}
    for ln in entries:
        p = _parse(ln)
        if p:
            counts[p[1]] = counts.get(p[1], 0) + 1
    haz = len([i for i, ln in enumerate(entries, start=1)
               if _is_hazard(ln) and i not in dead])
    inv = len([i for i, ln in enumerate(entries, start=1)
               if (p := _parse(ln)) and p[1] == "invariant" and i not in dead])
    print("\n-- pull map (nothing below is pushed; retrieve on demand) --")
    if haz:
        print(f"   {haz} live hazards (gotcha/dead-end), indexed by subsystem, never summarized:")
        print(f"     brain hazards --scope {scope} [--grep RX]   <- read BEFORE touching a subsystem")
        print(f"     brain resolve --project {scope} N \"how\"     <- fixed a known gotcha? retire it")
    if inv:
        print(f"   {inv} live invariants (what must stay true), never summarized:")
        print(f"     brain design --scope {scope} [--grep RX]    <- read BEFORE redesigning a subsystem")
    print(f"   {n} entries total ({', '.join(f'{v} {k}' for k, v in sorted(counts.items()))})")
    print(f"     brain grep RX [--scope {scope}] | brain zoom {scope} <lo>-<hi> | brain branches {scope}")
    print(f"     brain decisions|milestones|questions --scope {scope} [-n N]")
    print("   transcript archive (un-curated safety net under the stream; pull-only):")
    print("     brain history \"query\"   <- did we ever discuss X? | brain history show <id>")
    # The ticket board owns all tracked work now (the dispatch queue folded into
    # it). Name it here, always — a session that never sees the board cannot
    # infer it exists, and files a fixable defect as a gotcha instead.
    print("   ticket board — a DEFECT YOU WOULD FIX belongs on the board, not in a note:")
    print(f"     python {_tickets_cli()} open \"...\" --root {TICKETS_ROOT}  | "
          f"tickets board --root {TICKETS_ROOT}")
    # Deep docs: reasoning that does not survive a one-line cap lives in
    # projects/<scope>/<topic>.md. They were reachable only through a routing
    # table, so sessions wrote around them; naming them here makes the second
    # tier visible at the same moment the first tier is.
    deep_dir = os.path.join(KB_ROOT, "projects", scope)
    if os.path.isdir(deep_dir):
        docs = sorted(fn for fn in os.listdir(deep_dir) if fn.endswith(".md"))
        if docs:
            print(f"   deep docs (reasoning too long for an entry — read before "
                  f"re-deriving, update when a ruling changes):")
            for fn in docs:
                print(f"     projects/{scope}/{fn}")
    if scope == "global":
        others = [s for s in _scopes() if s != "global"]
        if others:
            # Only scopes with entries their summary hasn't absorbed yet are
            # worth a line: a scope whose count equals its watermark has
            # nothing a session could learn from being told about it, and 20
            # such lines are the bulk of this block.
            fresh, current = [], []
            for s in others:
                sn = len(_read_entries(s))
                swm, _, _, _, _ = _root_meta(s)
                (fresh if sn - swm > 0 else current).append((s, sn, sn - swm))
            print("\n-- other scopes --")
            for s, sn, unsum in sorted(fresh, key=lambda r: -r[2]):
                print(f"   {s}: {sn} entries, {unsum} not yet summarized")
            if current:
                print(f"   up to date, nothing new since their last summary: "
                      f"{', '.join(s for s, _, _ in current)}")
            print("   any of them: brain wake --scope <slug>")
    _print_ticket_board(TICKETS_ROOT)
    return 0


# ---------------------------------------------------------------- zoom / branches / views / grep

def cmd_zoom(args) -> int:
    m = re.fullmatch(r"(\d+)-(\d+)", args.range)
    if not m:
        print("error: range must be <lo>-<hi> (1-based)", file=sys.stderr)
        return 1
    lo, hi = int(m.group(1)), int(m.group(2))
    entries = _read_entries(args.scope)
    if not entries:
        print(f"(scope {args.scope} is empty)")
        return 0
    ids = _ids_for(args.scope)
    dead = _superseded(entries, ids)
    lo = max(1, lo)
    hi = min(len(entries), hi)
    # Display in effective-date order (asof if present, else ts); #numbers are
    # identity and stay write-order.
    for i in _order_by_date(range(lo, hi + 1), entries):
        print(_fmt(i, entries[i - 1], dead, ids))
    return 0


def cmd_branches(args) -> int:
    files = _branch_files(args.scope)
    if not files:
        print("(no branches yet)")
        return 0
    for lo, hi, path in files:
        _head, body = _frontmatter(path)
        print(f"[b{lo}-{hi}]")
        print(body)
        print()
    return 0


def cmd_view(args, typ: str) -> int:
    scopes = [args.scope] if args.scope else _scopes()
    rows = []
    for s in scopes:
        entries = _read_entries(s)
        ids = _ids_for(s)
        dead = _superseded(entries, ids)
        for i, ln in enumerate(entries, start=1):
            p = _parse(ln)
            if p and p[1] == typ:
                rows.append((_effective_date(ln), s, i, ln, dead, ids))
    rows.sort(key=lambda r: r[0])  # by effective date (asof if present, else ts)
    if not rows:
        print(f"(no {typ} entries)")
        return 0
    cap = args.recent or VIEW_DEFAULT_RECENT
    withheld = max(0, len(rows) - cap)
    rows = rows[-cap:]
    for _ts, s, i, ln, dead, ids in rows:
        print(f"{s} {_fmt(i, ln, dead, ids)}")
    if withheld:
        print(f"\n({withheld} older {typ} entries withheld — `-n {len(rows) + withheld}` "
              f"for all, or `brain grep RX`)")
    return 0


def cmd_grep(args) -> int:
    try:
        rx = re.compile(args.pattern, re.IGNORECASE)
    except re.error as e:
        print(f"error: bad regex: {e}", file=sys.stderr)
        return 1
    scopes = [args.scope] if args.scope else _scopes()
    hits = 0
    printed: set[tuple[str, int]] = set()
    # (scope, head) -> (dead_idx, head_line) for dead matches whose live head is
    # not itself a printed hit. grep already shows the dead line (flagged
    # [SUPERSEDED by #N]); the anti-hit adds the head's TEXT so the correction is
    # legible without a second lookup.
    anti: dict[tuple[str, int], tuple[int, str, list[str] | None]] = {}
    for s in scopes:
        entries = _read_entries(s)
        sids = _ids_for(s)
        dead = _superseded(entries, sids)
        for i, ln in enumerate(entries, start=1):
            if rx.search(ln):
                print(f"{s} {_fmt(i, ln, dead, sids)}")
                hits += 1
                printed.add((s, i))
                if i in dead:
                    head = dead[i]
                    key = (s, head)
                    if key not in anti:
                        anti[key] = (i, entries[head - 1], sids)
    anti_rows = []
    for (s, head), (dead_i, head_ln, sids) in sorted(anti.items(),
                                                     key=lambda kv: (kv[0][0], kv[1][0])):
        if len(anti_rows) >= ANTIHIT_MAX:
            break
        if (s, head) in printed:                # live head already shown verbatim
            continue
        anti_rows.append(f"{s} {_antihit_line(_did(sids, dead_i), _did(sids, head), head_ln)}")
    if not hits:
        print("(no matches)")
    for row in anti_rows:
        print(row)
    return 0


# ---------------------------------------------------------------- papercuts

def cmd_papercuts(args) -> int:
    """Open papercuts, grouped by recurrence — the promotion queue.

    A papercut is friction you would NOT open a board row for today. Its value
    is cumulative: `brain note` counts near-duplicates on write and says when one
    has recurred enough to promote. This view is the same signal pulled on
    demand, clustered so a repeat is visible at a glance. No standing duty
    attaches to it — the CLI raises recurrence when it happens."""
    scopes = [args.scope] if args.scope else _scopes()
    rows = []
    for s in scopes:
        entries = _read_entries(s)
        sids = _ids_for(s)
        dead = _superseded(entries, sids)
        for i, ln in enumerate(entries, start=1):
            if _is_papercut(ln) and i not in dead:
                rows.append((s, i, ln, sids))
    if not rows:
        print("(no open papercuts)")
        return 0
    # Cluster near-duplicates so recurrence reads at a glance.
    clusters: list[list[tuple[str, int, str, list[str] | None]]] = []
    for s, i, ln, sids in rows:
        toks = _dup_tokens(_parse(ln)[3])
        for cl in clusters:
            if cl[0][0] == s and _dup_score(toks, _dup_tokens(_parse(cl[0][2])[3])) >= DUP_BLOCK:
                cl.append((s, i, ln, sids))
                break
        else:
            clusters.append([(s, i, ln, sids)])
    clusters.sort(key=lambda c: (-len(c), c[0][0], c[0][1]))
    hot = [c for c in clusters if len(c) >= PAPERCUT_PROMOTE]
    print(f"{len(rows)} open papercut(s) in {len({r[0] for r in rows})} scope(s)"
          + (f" — {len(hot)} recurring {PAPERCUT_PROMOTE}x or more, promote those first."
             if hot else ". None recurring yet; leave them to accumulate."))
    for cl in clusters:
        if len(cl) > 1:
            print(f"\n  [{cl[0][0]}] recurred {len(cl)}x:")
        for s, i, ln, sids in cl:
            print(f"    {s} {_fmt(i, ln, ids=sids)}" if len(cl) > 1
                  else f"  {s} {_fmt(i, ln, ids=sids)}")
    print(f"\nPromote: open a ticket (`python {_tickets_cli()} open \"...\" "
          f"--root {TICKETS_ROOT}`), then "
          "`brain resolve --project <s> N \"promoted to ticket PROJ-NN\"`. "
          "Not worth fixing: `brain resolve --project <s> N \"wontfix\"`.")
    return 0


# ---------------------------------------------------------------- recall
# The conversational counterpart of the file-matched hazard gate.
#
# Code memory works because it is PUSHED at the moment of risk: the edit gate
# fires on a Write and puts the subsystem's hazards in front of the session
# whether or not it thought to ask. Conversation had no such moment — an
# advisory scope got the wake at minute
# zero and nothing for the next two hours, so every later retrieval depended on
# the agent remembering to run `brain decisions`, which is exactly the discipline
# gap the hazard gate was built to close on the code side.
#
# Matching is lexical and curated, never frequency-weighted: a design ruling set
# that IDF cannot separate a hot subsystem from a generic flood, and the same
# holds for topic words. Precision comes from the stoplist plus a two-token
# floor, not from statistics.

RECALL_TYPES = ("decision", "question", "invariant")
RECALL_MIN_HITS = 2      # one shared word is a coincidence
RECALL_MAX = 6           # entries surfaced at once
RECALL_MIN_TOKEN = 4
# …and at least this many of the matched words must be LONG. Short words carry
# little topic signal, and the 5-char stem makes them collide outright
# (converts/convergence, position/positive), so "cash" + "keep" was scoring the
# same as "Roth" + "bracket". This gate pushes into every prompt: a false
# positive costs context on every turn, a false negative only returns to the
# silence that already exists. Precision wins the trade.
RECALL_STRONG_LEN = 6
RECALL_MIN_STRONG = 2
# A ticker or acronym naming the exact subject ("ASTS", "TIAA") identifies a
# topic on its own, so it clears the two-token floor by itself. Nothing else does.
RECALL_ACRONYM = 99

# Conversational filler that carries no topic signal. Distinct from DUP_STOP
# (which tunes duplicate detection) because the jobs differ: here a word is
# dropped when it fails to NARROW the store, not when it is merely common.
_RECALL_STOP = {
    "about", "above", "after", "again", "against", "already", "also", "always",
    "another", "around", "because", "before", "being", "below", "between",
    "both", "could", "does", "doing", "done", "down", "during", "each", "even",
    "every", "first", "from", "further", "give", "going", "have", "here",
    "into", "just", "know", "last", "less", "like", "long", "look", "made",
    "make", "many", "more", "most", "much", "need", "next", "once", "only",
    "other", "over", "part", "same", "should", "since", "some", "still",
    "such", "sure", "take", "than", "that", "their", "them", "then", "there",
    "these", "they", "thing", "things", "think", "this", "those", "through",
    "time", "under", "until", "very", "want", "well", "were", "what", "when",
    "where", "which", "while", "will", "with", "would", "your", "yours",
    "actually", "maybe", "really", "right", "thanks", "please", "okay",
    "let's", "lets", "we're", "were", "don't", "dont", "can't", "cant",
    "good", "best", "better", "thoughts", "question", "questions", "answer",
}


def _recall_stem(word: str) -> str:
    """Crude prefix stem so 'conversion' reaches 'Roth-convert' and 'brackets'
    reaches 'bracket'. Five chars is short enough to survive normal inflection
    and long enough that unrelated words rarely collide; the two-token floor
    absorbs the collisions that remain. No stemmer library — this system stays
    plain text with no new dependencies."""
    return word[:5] if len(word) >= 6 else word


def _recall_words(text: str) -> list[str]:
    # Split ON hyphens rather than keeping them inside a token: 'roth-convert'
    # must yield 'roth' and 'convert', or the compound never matches either.
    return [w for w in re.split(r"[^a-z0-9']+", (text or "").lower()) if w]


def _recall_tokens(text: str) -> dict[str, int]:
    """-> {stem: strength}, where strength is the longest raw word that produced
    the stem, so the strong-token floor can tell 'conversion' from 'cash'.

    An ALL-CAPS word in prose is a ticker or an acronym (ASTS, TIAA, RMD, SBA)
    and discriminates regardless of length, so it counts as strong outright —
    the length heuristic alone would drop exactly the words that identify a
    topic most precisely."""
    # 4+ chars: ASTS, TIAA, LOCOMO qualify; API, CLI, TUI, KB do not — those are
    # ambient in this store and would match half of it.
    shouty = {w.lower() for w in re.findall(r"\b[A-Z][A-Z0-9]{3,}\b", text or "")}
    out: dict[str, int] = {}
    for w in _recall_words(text):
        if (len(w) < RECALL_MIN_TOKEN or not w[0].isalpha()
                or w in _RECALL_STOP or w in _GENERIC_NOUNS or w in _PATH_STOP):
            continue
        s = _recall_stem(w)
        strength = RECALL_ACRONYM if w in shouty else len(w)
        out[s] = max(out.get(s, 0), strength)
    return out


def _recall_entry_tokens(text: str) -> set[str]:
    return {_recall_stem(w) for w in _recall_words(text) if len(w) >= RECALL_MIN_TOKEN}


def _recall_hit(toks: dict[str, int], text: str) -> int:
    """Recall match score for `text` against query tokens, or 0 for no match.
    Same lexical gate cmd_recall applies to live entries, factored out so the
    dead-entry (anti-hit) path scores by exactly the same rule."""
    hits = set(toks) & _recall_entry_tokens(text)
    if not hits:
        return 0
    strong = [h for h in hits if toks[h] >= RECALL_STRONG_LEN]
    acronym = [h for h in hits if toks[h] >= RECALL_ACRONYM]
    if acronym or (len(hits) >= RECALL_MIN_HITS and len(strong) >= RECALL_MIN_STRONG):
        return len(hits) + len(strong) + 2 * len(acronym)
    return 0


# --- anti-hits --------------------------------------------------------------
# When a query's strongest textual match is a DEAD (superseded) entry, silence
# is the worst answer: the reader either sees nothing or digs the stale text out
# of the raw stream and acts on it. Instead surface ONE forward pointer to the
# live head of the supersede chain — the dead text itself is never re-printed
# (a stale ruling served verbatim is worse than none; the correction pointer is
# the whole product).
ANTIHIT_MAX = 3          # at most this many anti-hit lines per query
ANTIHIT_CLIP = 140       # live-head text clip length


def _antihit_line(dead_id: object, head_id: object, head_line: str) -> str:
    """One forward-pointer line: `~ #<dead> superseded by #<head> [type]: <head text>`.
    Only the LIVE head's text is shown, clipped; the dead text never appears.
    `dead_id`/`head_id` are display ids (a position for a solo scope, a stable
    "<writer>:<seq>" for a shared one)."""
    p = _parse(head_line)
    typ = p[1] if p else "?"
    text = " ".join((p[3] if p else head_line).split())
    if len(text) > ANTIHIT_CLIP:
        text = text[:ANTIHIT_CLIP - 1].rstrip() + "…"
    return f"~ #{dead_id} superseded by #{head_id} [{typ}]: {text}"


def cmd_recall(args) -> int:
    """Live advisory entries across scopes whose text shares topic words with
    the given text. Bounded with an explicit overflow pointer (invariant #31)."""
    toks = _recall_tokens(" ".join(args.text))
    if not toks:
        return 0
    exclude = set()
    for part in re.findall(r"[a-z0-9_-]+#[a-z0-9._:-]+", (getattr(args, "exclude", "") or "").lower()):
        exclude.add(part)
    scopes = [args.scope] if args.scope else _scopes()
    budget = getattr(args, "budget", 0) or 0
    scored: list[tuple[int, str, int, str]] = []
    # Anti-hit candidates: a DEAD entry matched, so its live head carries the
    # correction. Keyed forward to (scope, head) — deduped there against real
    # hits and against sibling dead entries sharing the same head.
    anti: dict[tuple[str, int], tuple[int, int, str, list[str] | None]] = {}
    for s in scopes:
        entries = _read_entries(s)
        sids = _ids_for(s)
        dead = _superseded(entries, sids)
        for i, ln in enumerate(entries, start=1):
            if f"{s}#{_did(sids, i)}" in exclude:
                continue
            p = _parse(ln)
            if not p or p[1] not in RECALL_TYPES:
                continue
            score = _recall_hit(toks, p[3])
            if not score:
                continue
            if i in dead:
                head = dead[i]
                key = (s, head)
                # Keep the strongest-scoring dead entry per live head; a shorter
                # #dead loses to a stronger match on the same chain.
                if key not in anti or score > anti[key][0]:
                    anti[key] = (score, i, entries[head - 1], sids)
                continue
            scored.append((score, s, i, ln, sids))
    scored.sort(key=lambda r: (-r[0], -r[2]))
    kept, used, kept_ids_list = [], 0, []
    for hits, s, i, ln, sids in scored[:RECALL_MAX]:
        row = f"- [{s}] {_fmt(i, ln, ids=sids)}"
        if budget and used + len(row) > budget:
            break
        kept.append(row)
        kept_ids_list.append(f"{s}#{_did(sids, i)}")
        used += len(row) + 1
    # Anti-hits come AFTER real hits, share the same char budget, and cap at
    # ANTIHIT_MAX. Drop any whose live head is already a printed hit (no
    # duplicate) or was excluded as already-injected.
    kept_ids = set(kept_ids_list)
    anti_rows = []
    for (s, head), (score, dead_i, head_ln, sids) in sorted(
            anti.items(), key=lambda kv: (-kv[1][0], kv[0][0], kv[1][1])):
        if len(anti_rows) >= ANTIHIT_MAX:
            break
        if f"{s}#{_did(sids, head)}" in kept_ids or f"{s}#{_did(sids, head)}" in exclude:
            continue
        row = _antihit_line(_did(sids, dead_i), _did(sids, head), head_ln).replace("~ ", f"~ [{s}] ", 1)
        if budget and used + len(row) > budget:
            break
        anti_rows.append(row)
        used += len(row) + 1
    if not kept and not anti_rows:
        return 0
    if kept:
        withheld = len(scored) - len(kept)
        print(f"[BRAIN RECALL] {len(kept)} live entr{'y' if len(kept) == 1 else 'ies'} "
              "bearing on what you just raised — verbatim rulings, not a summary:")
        print("\n".join(kept))
        if withheld:
            by_scope = sorted({s for _h, s, _i, _l, _sids in scored[len(kept):]})
            print(f"({withheld} more in {', '.join(by_scope)} — "
                  f"`brain decisions --scope <s>` or `brain grep RX`)")
    if anti_rows:
        print("[BRAIN RECALL] superseded match(es) — pointer to the live head, "
              "not the stale text:")
        print("\n".join(anti_rows))
    print("MATCHED-IDS: " + ",".join(kept_ids_list))
    return 0


# ---------------------------------------------------------------- ticket board
# The dispatch/agenda pair is retired: all tracked work now lives on the ticket
# board (tickets.py, a sibling of this file; the home board is TICKETS_ROOT).
# What remains here is (a) the compact board view `wake` injects and (b) a
# fail-loud stub, so `brain dispatch ...` names its replacement instead of
# silently doing nothing on a command that used to change state.


def _tickets_cli() -> str:
    """Path to the sibling tickets.py — the board's command-line tool."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickets.py")


def _ticket_board_command(root: str) -> str:
    return f"python {_tickets_cli()} board --root {root}"


def _print_ticket_board(root: str) -> None:
    """Wake's compact board section: one summary line, the tickets that are due
    or overdue (capped at 8), and a pointer to the full board. Prints nothing if
    the board root does not exist yet (and never errors) — a session on a
    machine without a board should see silence, not a traceback."""
    if not os.path.isdir(root):
        return
    try:
        import tickets
        cfg = tickets.load_config(root)
        state = tickets.board_state(root)
    except Exception:
        return
    prefix = cfg.get("prefix", "")
    today = datetime.now().strftime("%Y-%m-%d")
    active = [t for t in state.values() if t["status"] != "closed"]
    due = [t for t in active if t["due"] and t["due"] <= today]
    due.sort(key=lambda t: (t["due"], t["n"], t["nsfx"]))
    print(f"\n-- board: {len(active)} open, {len(due)} due or overdue --")
    for t in due[:8]:
        title = t["title"] if len(t["title"]) <= 90 else t["title"][:89] + "…"
        owner = t["owner"] or "(nobody yet)"
        print(f"   {tickets.key_of(prefix, t)}  {title}  — {owner}, due {t['due']}")
    if len(due) > 8:
        print(f"   (+{len(due) - 8} more due — see the full board)")
    print(f"   full board: {_ticket_board_command(root)}")


def cmd_dispatch(args) -> int:
    """Retired. The dispatch board folded into the ticket board (tickets.py);
    this stub fails loudly and names the replacement rather than silently doing
    nothing on a command that used to change tracked work."""
    print(
        "brain dispatch is retired — the dispatch board no longer exists. All "
        "tracked work now lives on the ticket board (tickets.py). See the "
        "board with:\n"
        f"  {_ticket_board_command(TICKETS_ROOT)}\n"
        "and open / claim / close a ticket with the matching tickets.py "
        "commands.",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------- rebuild
# The tree is regenerated MECHANICALLY from the stream — no model calls.
# Branch gists and root state are deterministic; wake state is computed live.


def _branch_gist(idx: int, line: str, ids: list[str] | None = None) -> str:
    """One deterministic bullet for a state entry: type + the first clause of
    its text, clipped, ending with its citation [#id]. No model, no judgment —
    just a stable, greppable pointer back into the stream."""
    p = _parse(line)
    typ = p[1] if p else "?"
    text = (p[3] if p else line).strip()
    # First sentence/clause keeps the gist scannable; the stream holds the full text.
    clip = re.split(r"(?<=[.;])\s", text, maxsplit=1)[0]
    if len(clip) > 140:
        clip = clip[:137].rstrip() + "…"
    return f"- [{typ}] {clip} [#{_did(ids, idx)}]"


def _build_branches(scope: str, entries: list[str], dead: set[int], force: bool,
                    ids: list[str] | None = None) -> bool:
    """Deterministic chrono gists over complete 32-entry chunks of the STATE
    path. A gist is one bullet per live state entry (type + first clause +
    citation) plus a header naming the type counts and date range. Regenerated
    only when the gist version changed or supersession removed a source entry;
    otherwise a stable cache. No model call."""
    n = len(entries)
    tdir = os.path.join(TREE_DIR, scope)
    os.makedirs(tdir, exist_ok=True)
    for lo in range(1, n + 1 - BRANCH_SIZE + 1, BRANCH_SIZE):
        hi = lo + BRANCH_SIZE - 1
        bpath = os.path.join(tdir, f"b{lo}-{hi}.md")
        srcs = _state_indices(entries, lo, hi, dead)
        want = ",".join(str(i) for i in srcs)
        srcs = _order_by_date(srcs, entries)
        head, _body = _frontmatter(bpath)
        if head is not None and not force:
            if head.get("v") == str(BRANCH_VERSION) and head.get("srcs", "") == want:
                continue
        if not srcs:
            with open(bpath, "w", encoding="utf-8") as f:
                f.write(f"---\nv: {BRANCH_VERSION}\ncovers: {lo}-{hi}\nsrcs: \n---\n\n"
                        f"(no state entries in #{lo}-#{hi} — hazards only)\n")
            continue
        counts: dict[str, int] = {}
        dates: list[str] = []
        for i in srcs:
            p = _parse(entries[i - 1])
            if p:
                counts[p[1]] = counts.get(p[1], 0) + 1
            d = _effective_date(entries[i - 1])
            if d:
                dates.append(d)
        span = f"{min(dates)}..{max(dates)}" if dates else "—"
        tallies = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        bullets = "\n".join(_branch_gist(i, entries[i - 1], ids) for i in srcs)
        with open(bpath, "w", encoding="utf-8") as f:
            f.write(f"---\nv: {BRANCH_VERSION}\ncovers: {lo}-{hi}\nsrcs: {want}\n---\n\n"
                    f"_{len(srcs)} state entries ({tallies}); {span}_\n\n{bullets}\n")
    return True


def _build_root(scope: str, entries: list[str], dead: set[int],
                ids: list[str] | None = None) -> bool:
    """Root state = what is TRUE NOW, composed MECHANICALLY: pins verbatim on
    top, then the live state entries (decision/milestone/question, minus
    superseded) newest first as citation-bearing bullets, capped at
    ROOT_MAX_LINES. Deterministic function of the stream — no model, no drift,
    never composed from a previous root."""
    n = len(entries)
    tdir = os.path.join(TREE_DIR, scope)
    os.makedirs(tdir, exist_ok=True)
    branches = _branch_files(scope)
    live = _state_indices(entries, 1, n, dead)
    ordered = list(reversed(_order_by_date(live, entries)))  # newest first
    pins = _render_pins(entries, dead, ids)
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M")
    if not ordered:
        body = (pins + "\n") if pins else "(no state)\n"
        with open(os.path.join(tdir, "root.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nwatermark: {n}\ngenerated: {ts}\ngenerator: deterministic\n"
                    f"v: {ROOT_VERSION}\nbranches: {len(branches)}\ncompresses: 0\n---\n\n"
                    + body)
        return True
    shown = ordered[:ROOT_MAX_LINES]
    bullets = "\n".join(_branch_gist(i, entries[i - 1], ids) for i in shown)
    if len(ordered) > len(shown):
        bullets += (f"\n- …(+{len(ordered) - len(shown)} older live rulings — "
                    f"`brain decisions --scope {scope}`)")
    body = (pins + "\n\n" + bullets) if pins else bullets
    with open(os.path.join(tdir, "root.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nwatermark: {n}\ngenerated: {ts}\ngenerator: deterministic\n"
                f"v: {ROOT_VERSION}\nbranches: {len(branches)}\ncompresses: {len(live)}\n---\n\n"
                + body + "\n")
    return True


def _rebuild_scope(scope: str, force: bool) -> bool:
    entries = _read_entries(scope)
    if not entries:
        return True
    ids = _ids_for(scope)
    dead = _superseded(entries, ids)
    if not _build_branches(scope, entries, dead, force, ids):
        return False
    labels = _label_standing(scope, entries)
    haz = _render_hazards(scope, entries, labels, dead=dead, ids=ids)
    if not haz.startswith("(no live hazard"):
        os.makedirs(os.path.join(TREE_DIR, scope), exist_ok=True)
        with open(os.path.join(TREE_DIR, scope, "hazards.md"), "w", encoding="utf-8") as f:
            f.write(f"<!-- generated by brain rebuild; verbatim index, never hand-edit -->\n\n{haz}\n")
    inv = _render_invariants(scope, entries, labels, dead=dead, ids=ids)
    if not inv.startswith("(no live invariant"):
        os.makedirs(os.path.join(TREE_DIR, scope), exist_ok=True)
        with open(os.path.join(TREE_DIR, scope, "design.md"), "w", encoding="utf-8") as f:
            f.write(f"<!-- generated by brain rebuild; verbatim index, never hand-edit -->\n\n{inv}\n")
    wm, _gen, ver, _body, _head = _root_meta(scope)
    if not force and ver == ROOT_VERSION and wm >= len(entries):
        return True
    return _build_root(scope, entries, dead, ids)


def cmd_rebuild(args) -> int:
    scopes = _scopes() if args.all else [args.scope or "global"]
    failed: list[str] = []
    for s in scopes:
        print(f"rebuild {s}:")
        wm, _, ver, _, _ = _root_meta(s)
        entries = _read_entries(s)
        n = len(entries)
        if wm >= n and ver == ROOT_VERSION and not args.force:
            labels = _load_labels(s)
            unlabelled = [i for i, ln in enumerate(entries, start=1)
                          if _is_standing(ln) and i not in labels]
            if not unlabelled:
                print("  up to date")
                continue
        if not _rebuild_scope(s, args.force):
            failed.append(s)
        for warn in _lint_scope(s):
            print(f"  LINT: {warn}")
    _git_commit(["stream", "tree"], "brain: rebuild"
                + (f" (FAILED: {', '.join(failed)})" if failed else ""))
    # Transcript archive: fold ingest+summarize into the daily rebuild so the
    # index always beats Claude Code's ~30-day JSONL reaper. Best-effort — a
    # history failure never fails the KB rebuild. BRAIN_SKIP_HISTORY opts out.
    if not os.environ.get("BRAIN_SKIP_HISTORY"):
        try:
            cmd_history_ingest(argparse.Namespace())
            cmd_history_summarize(argparse.Namespace(dry_run=False, limit=0))
        except Exception as e:
            print(f"(history hook skipped: {e})", file=sys.stderr)
    if failed and args.alert:
        # The rebuild fails slow-toward-bloat: a stalled summarizer silently
        # grows every wake's raw tail. Ring the doorbell instead.
        _alert(f"scopes: {', '.join(failed)} — see the rebuild output above")
    return 1 if failed else 0


def _lint_scope(scope: str) -> list[str]:
    """Deterministic, LLM-free integrity audit. The generated views make
    claims (provenance, supersession, labels); lint is what makes those
    claims checkable instead of trusted."""
    warns: list[str] = []
    entries = _read_entries(scope)
    n = len(entries)
    if not n:
        return warns
    ids = _ids_for(scope)
    def did(k: int) -> str:
        return _did(ids, k)
    idpos = {sid: i for i, sid in enumerate(ids, start=1)} if ids is not None else None
    direct: dict[int, int] = {}
    for j, ln in enumerate(entries, start=1):
        p = _parse(ln)
        if not p:
            warns.append(f"{scope} #{did(j)}: unparseable line")
            continue
        if p[1] not in TYPES:
            warns.append(f"{scope} #{did(j)}: unknown type '{p[1]}'")
        for ref in _sup_refs(p[2]):
            if idpos is None:
                r = int(ref) if ref.isdigit() else None
            else:
                r = idpos.get(ref)
            if r is None:
                # A ref to an id not (yet) present. On a shared scope this is a
                # normal sync-lag state — a spool that hasn't arrived — so it is a
                # WARNING, never an error. On a solo scope it is genuinely dangling.
                if idpos is None:
                    warns.append(f"{scope} #{did(j)}: supersedes #{ref} — out of range")
                else:
                    warns.append(f"{scope} #{did(j)}: supersedes #{ref} — no such id "
                                 "yet (a spool not synced? sync lag is normal)")
                continue
            direct[r] = j
            # Backfill correcting FORWARD in time is suspicious (not fatal):
            # a replacement whose fact-date is earlier than the entry it
            # replaces usually means the asof dates are crossed.
            new_d, old_d = _effective_date(ln), _effective_date(entries[r - 1])
            if new_d and old_d and new_d < old_d:
                warns.append(f"{scope} #{did(j)}: supersedes #{ref} which has a LATER "
                             f"date ({old_d} > {new_d}) — check the asof dates")
    for start in direct:
        j, seen = direct[start], {start}
        while j in direct:
            if j in seen:
                warns.append(f"{scope}: supersession cycle through #{did(j)}")
                break
            seen.add(j)
            j = direct[j]
    dead = set(_superseded(entries, ids))
    # Every [#N] a generated view serves must resolve to a real, live entry.
    tdir = os.path.join(TREE_DIR, scope)
    files = [os.path.join(tdir, "root.md")] + [p for _, _, p in _branch_files(scope)]
    for path in files:
        _head, body = _frontmatter(path)
        if not body:
            continue
        name = os.path.basename(path)
        # Only bracketed citations ([#12,#15]) are provenance claims — a bare
        # #N in prose may be a literal (an EIN, an issue number), not a ref.
        refs: set[int] = set()
        for grp in re.findall(r"\[((?:#\d+[,\s]*)+)\]", body):
            refs.update(int(x) for x in re.findall(r"\d+", grp))
        for ref in refs:
            if ref < 1 or ref > n:
                warns.append(f"{scope}/{name}: cites #{ref} — no such entry")
            elif ref in dead and name == "root.md":
                warns.append(f"{scope}/root.md: cites superseded #{ref} — stale; rebuild")
    labels = _load_labels(scope, ids)
    for i in labels:
        if i < 1 or i > n:
            warns.append(f"{scope}/hazards.tsv: label for nonexistent #{did(i)}")
        elif not _is_standing(entries[i - 1]):
            warns.append(f"{scope}/hazards.tsv: label on non-standing entry #{did(i)}")
    live = sorted({lb for i, lb in labels.items()
                   if 1 <= i <= n and i not in dead and _is_standing(entries[i - 1])})
    for a in range(len(live)):
        for b in range(a + 1, len(live)):
            wa, wb = set(_norm_label(live[a]).split()), set(_norm_label(live[b]).split())
            if wa and wb and (wa == wb or wa < wb or wb < wa):
                warns.append(f"{scope}: near-duplicate labels '{live[a]}' / '{live[b]}'"
                             " — recall splits across them; unify at next capture")
    # Invariant staleness + sprawl: the type's failure mode is going silently
    # false — nothing else forces re-validation, so lint is the clock.
    live_inv = []
    for i, ln in enumerate(entries, start=1):
        p = _parse(ln)
        if p and p[1] == "invariant" and i not in dead:
            live_inv.append((i, p[0]))
    for i, ts in live_inv:
        try:
            age = (datetime.now() - datetime.strptime(ts[:10], "%Y-%m-%d")).days
        except ValueError:
            continue
        if age > 120:
            warns.append(f"{scope} #{did(i)}: invariant unconfirmed for {age}d — still true? "
                         "Supersede it (even with identical text) to re-date it")
    if len(live_inv) > 12:
        warns.append(f"{scope}: {len(live_inv)} live invariants — sprawl; 'invariant' "
                     "is a load-bearing constraint, not a no-compress retention hack")
    _wm, _gen, _ver, body, _head = _root_meta(scope)
    if body and len(body.splitlines()) > ROOT_MAX_LINES + 6:
        warns.append(f"{scope}/root.md: {len(body.splitlines())} lines > "
                     f"{ROOT_MAX_LINES} budget")
    return warns


def cmd_lint(args) -> int:
    scopes = [args.scope] if args.scope else _scopes()
    total = 0
    for s in scopes:
        for w in _lint_scope(s):
            print(w)
            total += 1
    user_md = os.path.join(KB_ROOT, "user.md")
    if os.path.isfile(user_md) and (not args.scope or args.scope == "global"):
        sz = len(open(user_md, "r", encoding="utf-8").read())
        if sz > 3000:
            print(f"user.md: {sz} chars > 3000 budget — evict a weak line")
            total += 1
    print(f"({total} finding{'s' if total != 1 else ''})" if total else "clean")
    return 1 if total else 0


def cmd_status(_args) -> int:
    scopes = _scopes()
    if not scopes:
        print("(no streams yet)")
        return 0
    for s in scopes:
        entries = _read_entries(s)
        ids = _ids_for(s)
        n = len(entries)
        wm, gen, ver, body, _head = _root_meta(s)
        dead = _superseded(entries, ids)
        haz = [i for i, ln in enumerate(entries, start=1) if _is_hazard(ln)]
        labels = _load_labels(s, ids)
        unl = len([i for i, ln in enumerate(entries, start=1)
                   if _is_standing(ln) and i not in labels])
        # Injected-footprint meter: context frugality is the system's reason
        # to exist — what each surface would push, in chars, so growth is
        # visible before it becomes bloat.
        root_c = len(body or "")
        tail_c = sum(len(entries[i - 1]) for i in range(wm + 1, n + 1)) if n > wm else 0
        haz_view = _render_hazards(s, entries, labels, dead=dead, ids=ids)
        haz_c = 0 if haz_view.startswith("(no live") else len(haz_view)
        foot = f", push root {root_c}c + tail {min(tail_c, WAKE_TAIL_CHARS)}c + hazards {haz_c}c"
        if haz_c > 6000:
            foot += " (gate digests: over 6000c)"
        shared_tag = f", shared: {len(_spool_writers(s))} writer(s)" if _is_shared(s) else ""
        print(f"{s}: {n} entries, watermark {wm} (root v{ver}, generated {gen or 'never'}), "
              f"{len(_branch_files(s))} branches, {len(haz)} hazards"
              + (f" ({unl} unlabelled)" if unl else "") + foot + shared_tag)
    return 0


def cmd_sessions(args) -> int:
    """Entries grouped by capture session — the denominator pilot criterion 1
    never had. The Stop hook floor is 1 entry/code-changing session; anything
    beyond that is voluntary capture, which is the number that matters."""
    scopes = [args.scope] if args.scope else _scopes()
    sess: dict[str, dict] = {}
    unstamped = 0
    for s in scopes:
        for i, ln in enumerate(_read_entries(s), start=1):
            p = _parse(ln)
            if not p:
                continue
            sid = _meta_str(p[2], "sid")
            if not sid:
                unstamped += 1
                continue
            d = sess.setdefault(sid, {"first": p[0], "n": 0,
                                      "scopes": set(), "types": {}})
            d["n"] += 1
            d["scopes"].add(s)
            d["types"][p[1]] = d["types"].get(p[1], 0) + 1
    if not sess:
        print(f"(no session-stamped entries yet; {unstamped} pre-instrumentation)")
        return 0
    tax_only = 0
    for sid, d in sorted(sess.items(), key=lambda kv: kv[1]["first"]):
        types = ", ".join(f"{v} {k}" for k, v in sorted(d["types"].items()))
        # Milestone-only capture is the Stop-hook-tax signature, not memory.
        flag = ""
        if set(d["types"]) == {"milestone"}:
            flag = "  <- milestone-only (tax signature)"
            tax_only += 1
        print(f"{d['first']}  sid={sid}  {d['n']} entries ({types})  "
              f"[{', '.join(sorted(d['scopes']))}]{flag}")
    total = sum(d["n"] for d in sess.values())
    voluntary = sum(d["n"] - 1 for d in sess.values())
    print(f"\n{len(sess)} sessions, {total} entries "
          f"({total / len(sess):.1f}/session; {voluntary} beyond the 1/session "
          f"hook floor; {tax_only} milestone-only"
          f"{f'; {unstamped} pre-instrumentation unstamped' if unstamped else ''})")
    print("(capture-rate is a denominator, not a quality signal — the seeded-trap "
          "replay is the quality proxy)")
    return 0


# ---------------------------------------------------------------- history
# A durable transcript archive that outlives Claude Code's ~30-day JSONL reaper.
# The stream above is CURATED memory; this is the un-curated safety net under
# it — it answers "did we discuss X?" even when no session captured an entry.
# Storage is a single SQLite file kept OUT of the stream's git (a growing binary
# would bloat it). Only the conversational SPINE is stored (user + assistant
# text); thinking blocks are dropped and tool calls/results collapse to one-line
# stubs, so the index stays a fraction of the raw transcript bytes. Pull-only:
# NOTHING from this DB is ever auto-injected into a session.

HISTORY_SCHEMA_VERSION = 1              # PRAGMA user_version — bump on a migration
HISTORY_IDLE_SECS = 24 * 3600          # a session must be idle this long before summarizing
HISTORY_SEARCH_HITS = 5                # FTS hits shown by `brain history "query"`
HISTORY_SEARCH_MAXLINES = 25          # hard cap on total search output lines
HISTORY_SHOW_TURNS = 5                 # default turns `brain history show` expands
HISTORY_TURN_CLIP = 500               # per-turn char clip in `show` output
HISTORY_STUB_TARGET_CLIP = 200        # per tool-event target/stub clip


def _projects_root() -> str:
    """Root of Claude Code's transcript tree. BRAIN_TRANSCRIPT_ROOT overrides it
    (tests point it at a temp dir)."""
    return os.environ.get("BRAIN_TRANSCRIPT_ROOT") or os.path.expanduser(
        os.path.join("~", ".claude", "projects"))


def _history_db_path() -> str:
    """The archive DB — kept out of the stream's git. Default lives under the
    data root's _state/ dir (git-excluded). BRAIN_HISTORY_DB wins."""
    env = os.environ.get("BRAIN_HISTORY_DB")
    if env:
        return os.path.expanduser(env)
    return os.path.join(KB_ROOT, "_state", "brain-history.db")


def _history_connect():
    """Open (creating if needed) the archive DB with the schema applied. WAL
    mode, single writer (the indexer). Raises RuntimeError if this interpreter's
    sqlite3 lacks FTS5 — the archive is not hand-rollable, so we stop loudly."""
    db = _history_db_path()
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
    except sqlite3.OperationalError as e:
        conn.close()
        raise RuntimeError(
            f"sqlite3 in this interpreter lacks FTS5 ({e}); the transcript "
            "archive requires it. Aborting rather than hand-rolling an index.")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS sessions(
            id           TEXT PRIMARY KEY,
            scope        TEXT,
            cwd          TEXT,
            git_branch   TEXT,
            model        TEXT,
            started_at   TEXT,
            ended_at     TEXT,
            summary      TEXT,
            tags         TEXT,
            src_path     TEXT,
            src_offset   INTEGER DEFAULT 0,
            summarized_at TEXT
        );
        CREATE TABLE IF NOT EXISTS turns(
            session_id TEXT,
            turn_no    INTEGER,
            role       TEXT,
            ts         TEXT,
            text       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, turn_no);
        CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
            text, content='turns', content_rowid='rowid'
        );
        CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
            INSERT INTO turns_fts(rowid, text) VALUES (new.rowid, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
            INSERT INTO turns_fts(turns_fts, rowid, text) VALUES('delete', old.rowid, old.text);
        END;
        CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
            INSERT INTO turns_fts(turns_fts, rowid, text) VALUES('delete', old.rowid, old.text);
            INSERT INTO turns_fts(rowid, text) VALUES (new.rowid, new.text);
        END;
        CREATE TABLE IF NOT EXISTS tool_events(
            session_id TEXT,
            turn_no    INTEGER,
            tool       TEXT,
            target     TEXT,
            stub       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tool_session ON tool_events(session_id);
    """)
    conn.execute(f"PRAGMA user_version={HISTORY_SCHEMA_VERSION}")
    conn.commit()
    return conn


def _history_scope(munged_dir: str, cwd: str) -> str:
    """A best-effort scope tag for a session. The real cwd is authoritative when
    present; the munged project-dir name is the fallback. This is a display/
    filter axis, not a KB scope key — it need not match a stream slug exactly."""
    path = cwd or ""
    if path:
        seg = re.split(r"[\\/]+", path.rstrip("\\/"))
        seg = [s for s in seg if s]
        if seg:
            # A worktree checkout (…/<repo>/worktrees/<leaf>) is best labelled
            # by its parent project, not the throwaway leaf.
            if len(seg) >= 3 and seg[-2].lower() == "worktrees":
                return seg[-3].lower()
            return seg[-1].lower()
    # Fallback: the munged dir name (Claude Code's dir-mangled path). Lossy but
    # deterministic; only hit when a session has no cwd on any record.
    return (munged_dir or "unknown").lower()


def _first_text(v) -> str:
    """A tool_result / content field is str | list[block] | dict. Reduce it to a
    short plain-text summary WITHOUT storing the body (spine-only invariant)."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for b in v:
            if isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif isinstance(b.get("content"), (str, list)):
                    parts.append(_first_text(b["content"]))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    if isinstance(v, dict):
        if isinstance(v.get("text"), str):
            return v["text"]
        if v.get("content") is not None:
            return _first_text(v["content"])
    return ""


def _tool_stub(block: dict):
    """(tool, target, stub) one-liner for a tool_use block. NEVER stores the
    result body — just enough to recall what happened. e.g. 'Bash: npm test',
    'Read: src/server.py', 'Edit: parser.py'."""
    tool = str(block.get("name") or "tool")
    inp = block.get("input") or {}
    target = ""
    if isinstance(inp, dict):
        if isinstance(inp.get("command"), str):
            target = inp["command"]
        elif isinstance(inp.get("file_path"), str):
            target = inp["file_path"]
        elif isinstance(inp.get("path"), str):
            target = inp["path"]
        elif isinstance(inp.get("pattern"), str):
            target = inp["pattern"]
        elif isinstance(inp.get("url"), str):
            target = inp["url"]
        elif isinstance(inp.get("description"), str):
            target = inp["description"]
    target = " ".join(str(target).split())[:HISTORY_STUB_TARGET_CLIP]
    stub = f"{tool}: {target}".strip().rstrip(":") if target else tool
    return tool, target, stub


def _result_suffix(block: dict) -> str:
    """Compact ' -> …' suffix for a tool_result: error flag + line count. The
    body itself is discarded (spine-only)."""
    body = _first_text(block.get("content"))
    nlines = body.count("\n") + 1 if body else 0
    err = block.get("is_error")
    if err:
        return " -> error"
    if nlines:
        return f" -> {nlines} lines"
    return ""


def _iter_jsonl(fh):
    """Yield (record_dict, offset_after_this_line) for each COMPLETE line. A
    trailing partial line (no newline — the file is still being appended) is not
    yielded and its bytes are not counted, so the next run resumes at it. Bad
    JSON lines are skipped but still advance the offset (append-only: they never
    become valid later)."""
    offset = fh.tell()
    while True:
        raw = fh.readline()
        if not raw:
            return
        if not raw.endswith(b"\n"):
            return  # partial trailing line; leave offset where it was
        offset += len(raw)
        s = raw.strip()
        if not s:
            yield None, offset
            continue
        try:
            rec = json.loads(s)
        except (ValueError, UnicodeDecodeError):
            yield None, offset
            continue
        yield rec, offset


def _ingest_file(conn, path: str, munged_dir: str) -> dict:
    """Incrementally ingest one transcript file. Idempotent: resumes from the
    stored byte offset; a second run over an unchanged file does ~nothing. If the
    file shrank (rewritten), re-ingests from 0. Returns per-file counters."""
    sid = os.path.splitext(os.path.basename(path))[0]
    row = conn.execute(
        "SELECT src_offset, started_at, ended_at, scope, cwd, git_branch, model "
        "FROM sessions WHERE id=?", (sid,)).fetchone()
    size = os.path.getsize(path)
    offset = row["src_offset"] if row else 0
    if offset > size:                       # file was rewritten/truncated
        offset = 0
    if row and offset == 0:
        conn.execute("DELETE FROM turns WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM tool_events WHERE session_id=?", (sid,))
    if row and offset >= size:
        return {"turns": 0, "tools": 0, "new": False}

    turn_no = conn.execute(
        "SELECT COALESCE(MAX(turn_no), 0) FROM turns WHERE session_id=?",
        (sid,)).fetchone()[0]

    meta = {"scope": row["scope"] if row else None,
            "cwd": row["cwd"] if row else None,
            "git_branch": row["git_branch"] if row else None,
            "model": row["model"] if row else None,
            "started_at": row["started_at"] if row else None,
            "ended_at": row["ended_at"] if row else None}
    # Known limitation: this map lives only for one ingest pass, so a
    # tool_result that lands in a LATER append than its tool_use keeps a stub
    # with no "-> N lines"/error suffix. Cosmetic; the stub itself survives.
    tool_rowid_by_id: dict = {}
    added_turns = added_tools = 0
    last_offset = offset

    with open(path, "rb") as fh:
        fh.seek(offset)
        for rec, off in _iter_jsonl(fh):
            last_offset = off
            if rec is None:
                continue
            typ = rec.get("type")
            ts = rec.get("timestamp")
            if rec.get("cwd") and not meta["cwd"]:
                meta["cwd"] = rec.get("cwd")
            if rec.get("gitBranch") and not meta["git_branch"]:
                meta["git_branch"] = rec.get("gitBranch")
            if ts:
                if not meta["started_at"] or ts < meta["started_at"]:
                    meta["started_at"] = ts
                if not meta["ended_at"] or ts > meta["ended_at"]:
                    meta["ended_at"] = ts
            if rec.get("isSidechain"):
                continue                     # subagent/sidechain: skip (spine only)
            msg = rec.get("message")
            if typ == "user" and isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    text = content.strip()
                    if text:
                        turn_no += 1
                        conn.execute(
                            "INSERT INTO turns(session_id,turn_no,role,ts,text) "
                            "VALUES(?,?,?,?,?)", (sid, turn_no, "user", ts, text))
                        added_turns += 1
                elif isinstance(content, list):
                    for b in content:        # tool_result blocks: enrich the stub
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            rid = tool_rowid_by_id.get(b.get("tool_use_id"))
                            if rid is not None:
                                suf = _result_suffix(b)
                                if suf:
                                    conn.execute(
                                        "UPDATE tool_events SET stub=stub||? WHERE rowid=?",
                                        (suf, rid))
            elif typ == "assistant" and isinstance(msg, dict):
                if not meta["model"] and msg.get("model"):
                    meta["model"] = msg.get("model")
                blocks = msg.get("content")
                if not isinstance(blocks, list):
                    continue
                texts, tool_uses = [], []
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and isinstance(b.get("text"), str):
                        texts.append(b["text"])
                    elif bt == "tool_use":
                        tool_uses.append(b)
                    # 'thinking' / 'redacted_thinking' blocks: dropped entirely
                atext = "\n".join(t.strip() for t in texts if t.strip()).strip()
                if atext or tool_uses:
                    turn_no += 1
                    if atext:
                        conn.execute(
                            "INSERT INTO turns(session_id,turn_no,role,ts,text) "
                            "VALUES(?,?,?,?,?)", (sid, turn_no, "assistant", ts, atext))
                        added_turns += 1
                    for tu in tool_uses:
                        tool, target, stub = _tool_stub(tu)
                        cur = conn.execute(
                            "INSERT INTO tool_events(session_id,turn_no,tool,target,stub) "
                            "VALUES(?,?,?,?,?)", (sid, turn_no, tool, target, stub))
                        added_tools += 1
                        if tu.get("id"):
                            tool_rowid_by_id[tu["id"]] = cur.lastrowid
            # every other record type (attachment, summary, queue-operation,
            # last-prompt, unknown) is skipped, never fatal.

    if meta["scope"] is None:
        meta["scope"] = _history_scope(munged_dir, meta["cwd"] or "")
    # tags: distinct files touched (Edit/Write) + distinct tools used, capped.
    files = [r[0] for r in conn.execute(
        "SELECT DISTINCT target FROM tool_events WHERE session_id=? AND "
        "tool IN ('Edit','Write','NotebookEdit') AND target<>'' LIMIT 20", (sid,))]
    tags = ", ".join(dict.fromkeys(os.path.basename(f) for f in files))[:400]

    conn.execute("""
        INSERT INTO sessions(id,scope,cwd,git_branch,model,started_at,ended_at,
                             src_path,src_offset,tags)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            scope=excluded.scope, cwd=excluded.cwd, git_branch=excluded.git_branch,
            model=excluded.model, started_at=excluded.started_at,
            ended_at=excluded.ended_at, src_path=excluded.src_path,
            src_offset=excluded.src_offset, tags=excluded.tags
    """, (sid, meta["scope"], meta["cwd"], meta["git_branch"], meta["model"],
          meta["started_at"], meta["ended_at"], path, last_offset, tags))
    conn.commit()
    return {"turns": added_turns, "tools": added_tools, "new": row is None}


def cmd_history_ingest(args) -> int:
    root = _projects_root()
    if not os.path.isdir(root):
        print(f"(no transcript tree at {root}; nothing to ingest)")
        return 0
    try:
        conn = _history_connect()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    files = 0
    tot_turns = tot_tools = new_sessions = 0
    for munged in sorted(os.listdir(root)):
        d = os.path.join(root, munged)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(d, fn)
            try:
                r = _ingest_file(conn, path, munged)
            except Exception as e:      # one bad file never aborts the sweep
                print(f"  skip {fn}: {e}", file=sys.stderr)
                continue
            files += 1
            tot_turns += r["turns"]
            tot_tools += r["tools"]
            new_sessions += 1 if r["new"] else 0
    conn.close()
    print(f"history ingest: {files} files scanned, {new_sessions} new sessions, "
          f"+{tot_turns} turns, +{tot_tools} tool events -> {_history_db_path()}")
    return 0


def _parse_iso(ts: str):
    """Transcript timestamps are ISO-8601 UTC ('...Z'). -> aware datetime | None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# Stopwords for deterministic tag extraction — drop function words so the tags
# name what the session was ABOUT, not how English is spelled.
_TAG_STOP = frozenset(
    "the a an and or but if then this that these those with without for from into "
    "onto over under about above below of to in on at by as is are was were be been "
    "being do does did done have has had having will would should could can may might "
    "must not no yes it its it's you your we our they their he she his her i me my "
    "so just now here there when what which who whom how why all any some more most "
    "one two get got make made use used using run ran let out up off than too very "
    "user assistant session claude code okay ok yeah sure thanks please".split())


def _deterministic_summary(row, turns):
    """(summary, tags) computed mechanically from the session's turns — the first
    user turn IS what the session was about; tags are the most frequent content
    words across the spine. No model call."""
    first_user = next((t["text"] for t in turns if t["role"] == "user" and t["text"].strip()),
                      turns[0]["text"] if turns else "")
    summary = " ".join(first_user.split())[:400]
    freq: dict[str, int] = {}
    for t in turns:
        for w in re.findall(r"[a-z][a-z0-9_]{2,}", (t["text"] or "").lower()):
            if w in _TAG_STOP:
                continue
            freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    tags = ", ".join(w for w, _ in top)
    return summary, tags


def cmd_history_summarize(args) -> int:
    try:
        conn = _history_connect()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    rows = conn.execute(
        "SELECT * FROM sessions WHERE summary IS NULL ORDER BY ended_at DESC").fetchall()
    now = datetime.now(timezone.utc)
    done = skipped_idle = 0
    for row in rows:
        if args.limit and done >= args.limit:
            break
        ended = _parse_iso(row["ended_at"])
        if ended is None or (now - ended).total_seconds() < HISTORY_IDLE_SECS:
            skipped_idle += 1
            continue                    # still hot / undatable — leave for later
        turns = conn.execute(
            "SELECT role, text FROM turns WHERE session_id=? ORDER BY turn_no",
            (row["id"],)).fetchall()
        if not turns:
            continue
        summary, tags = _deterministic_summary(row, turns)
        if args.dry_run:
            print(f"--- would summarize {row['id']} (scope={row['scope']}, "
                  f"ended={row['ended_at']}) ---")
            print(summary)
            print(f"tags: {tags}")
            print("--- end ---\n")
            done += 1
            continue
        if not summary:
            continue
        ts = now.strftime("%Y-%m-%dT%H:%M")
        conn.execute("UPDATE sessions SET summary=?, tags=COALESCE(NULLIF(?,''),tags), "
                     "summarized_at=? WHERE id=?", (summary, tags, ts, row["id"]))
        conn.commit()
        done += 1
        print(f"  summarized {row['id']} [{row['scope']}]: {summary[:80]}")
    conn.close()
    verb = "would summarize" if args.dry_run else "summarized"
    print(f"history summarize: {verb} {done} session(s); {skipped_idle} skipped (idle < 24h)")
    return 0


def _fts_query(raw: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression: each alnum token
    double-quoted (so FTS operators in user text can't raise), implicit-AND."""
    toks = re.findall(r"[0-9A-Za-z_]+", raw)
    return " ".join(f'"{t}"' for t in toks)


def cmd_history_search(args) -> int:
    query = " ".join(args.query)
    try:
        conn = _history_connect()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    match = _fts_query(query)
    if not match:
        print("(empty query)")
        return 0
    try:
        rows = conn.execute("""
            SELECT s.id AS id, s.scope AS scope, s.started_at AS started_at,
                   s.summary AS summary,
                   snippet(turns_fts, 0, '[', ']', '…', 10) AS snip
            FROM turns_fts
            JOIN turns ON turns.rowid = turns_fts.rowid
            JOIN sessions s ON s.id = turns.session_id
            WHERE turns_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (match, HISTORY_SEARCH_HITS * 5)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"(search error: {e})", file=sys.stderr)
        conn.close()
        return 1
    # One hit per session: several matching turns in one conversation would
    # otherwise crowd distinct sessions out of the top 5. (snippet() cannot
    # run under GROUP BY, so dedup happens here, over an over-fetched window.)
    seen: set = set()
    hits = []
    for r_ in rows:
        if r_["id"] in seen:
            continue
        seen.add(r_["id"])
        hits.append(r_)
        if len(hits) >= HISTORY_SEARCH_HITS:
            break
    if not hits:
        print(f"(no transcript hits for {query!r})")
        conn.close()
        return 0
    lines = 0
    for h in hits:
        if lines >= HISTORY_SEARCH_MAXLINES:
            break
        date = (h["started_at"] or "")[:10]
        label = h["summary"]
        if not label:
            first = conn.execute(
                "SELECT text FROM turns WHERE session_id=? AND role='user' "
                "ORDER BY turn_no LIMIT 1", (h["id"],)).fetchone()
            label = (first["text"] if first else "").strip().splitlines()[0:1]
            label = (label[0] if label else "")[:80]
        snip = " ".join((h["snip"] or "").split())
        print(f"{date}  [{h['scope']}]  {label[:80]}")
        print(f"    …{snip[:160]}  ({h['id']})")
        lines += 2
    print(f"— {len(hits)} hit(s); `brain history show <id>` to expand —")
    conn.close()
    return 0


def cmd_history_show(args) -> int:
    try:
        conn = _history_connect()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    s = conn.execute("SELECT * FROM sessions WHERE id=?", (args.session_id,)).fetchone()
    if not s:
        print(f"(no session {args.session_id!r} in the archive)")
        conn.close()
        return 1
    n = args.n or HISTORY_SHOW_TURNS
    if args.around is not None:
        half = max(1, n // 2)
        lo, hi = args.around - half, args.around + (n - half - 1)
        turns = conn.execute(
            "SELECT turn_no, role, ts, text FROM turns WHERE session_id=? "
            "AND turn_no BETWEEN ? AND ? ORDER BY turn_no", (s["id"], lo, hi)).fetchall()
    else:
        turns = conn.execute(
            "SELECT turn_no, role, ts, text FROM turns WHERE session_id=? "
            "ORDER BY turn_no LIMIT ?", (s["id"], n)).fetchall()
    print(f"session {s['id']}  [{s['scope']}]  {(s['started_at'] or '')[:16]}"
          f"  branch={s['git_branch'] or '-'}  model={s['model'] or '-'}")
    if s["summary"]:
        print(f"summary: {s['summary']}")
    print("")
    for t in turns:
        text = " ".join((t["text"] or "").split())
        if len(text) > HISTORY_TURN_CLIP:
            text = text[:HISTORY_TURN_CLIP] + "…"
        print(f"#{t['turn_no']} {t['role']}: {text}")
    conn.close()
    return 0


def cmd_history(args) -> int:
    """Router: `history <ingest|summarize|show>` are subcommands; anything else
    is a search query (search is the default, so `brain history "did we discuss X"`
    Just Works). Flags are parsed per-subcommand off the raw remainder."""
    rest = list(args.rest or [])
    head = rest[0] if rest else ""
    if head == "ingest":
        return cmd_history_ingest(argparse.Namespace())
    if head == "summarize":
        sp = argparse.ArgumentParser(prog="brain history summarize")
        sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="print the summarizer prompt without calling the model")
        sp.add_argument("--limit", type=int, default=0, help="max sessions this run")
        return cmd_history_summarize(sp.parse_args(rest[1:]))
    if head == "show":
        sp = argparse.ArgumentParser(prog="brain history show")
        sp.add_argument("session_id")
        sp.add_argument("--around", type=int, help="center the window on this turn number")
        sp.add_argument("-n", dest="n", type=int, help="number of turns to show")
        return cmd_history_show(sp.parse_args(rest[1:]))
    if not rest:
        print("usage: brain history <ingest|summarize|show <id>|\"search query\">")
        return 0
    return cmd_history_search(argparse.Namespace(query=rest))


# ---------------------------------------------------------------- init / doctor

def _git_available() -> bool:
    return shutil.which("git") is not None


def _root_is_git_backed() -> bool:
    """True only when the data root is the TOP of its own git worktree. A root
    merely NESTED inside another repo (e.g. the default ./brain inside the
    package clone, where it is gitignored) is NOT protected — its writes never
    commit — so doctor must not claim a safety net that isn't there."""
    if not _git_available() or not os.path.isdir(KB_ROOT):
        return False
    try:
        r = subprocess.run(["git", "-C", KB_ROOT, "rev-parse", "--show-toplevel"],
                           capture_output=True, timeout=10)
        if r.returncode != 0:
            return False
        top = r.stdout.decode("utf-8", "replace").strip()
        return bool(top) and os.path.samefile(top, KB_ROOT)
    except Exception:
        return False


def cmd_init(args) -> int:
    """Create a fresh, empty instance at the data root: the directory scaffold,
    a _state/ git-ignore, and an optional git repo when git is available. Safe to
    re-run — never touches existing stream data."""
    created = not os.path.isdir(KB_ROOT)
    for sub in ("stream", "tree", "tickets", "registers", "_state"):
        os.makedirs(os.path.join(KB_ROOT, sub), exist_ok=True)
    # Keep the growing/binary runtime artefacts out of the stream's git.
    gi = os.path.join(KB_ROOT, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as f:
            f.write("# Agent Brain instance — runtime state is not the record.\n"
                    "_state/\n")
    want_git = not getattr(args, "no_git", False)
    if want_git and _git_available() and not _root_is_git_backed():
        try:
            subprocess.run(["git", "-C", KB_ROOT, "init", "-q"],
                           capture_output=True, timeout=15, check=True)
        except Exception as e:
            print(f"(git init skipped: {e})", file=sys.stderr)
    print(f"brain instance {'created' if created else 'ready'} at {KB_ROOT}")
    print(f"  config: BRAIN_ROOT / {_config_path()} / default ./brain")
    if not _root_is_git_backed():
        print("  NOTE: no git — the stream's safety net is the append-only spool "
              "files plus your folder-sync history (e.g. OneDrive versions), not "
              "commits. Run `brain doctor` for the full picture.")
    return 0


def cmd_doctor(_args) -> int:
    """Report on the instance: where the data root is, how it was resolved, and —
    critically — whether the stream has a git safety net or is running in the
    degraded no-git (folder-sync) profile."""
    print(f"data root : {KB_ROOT}")
    src = ("BRAIN_ROOT env" if os.environ.get("BRAIN_ROOT")
           else "config file" if _load_config().get("root")
           else "default (./brain next to brain.py)")
    print(f"resolved  : {src}")
    print(f"config    : {_config_path()}"
          + ("" if os.path.exists(_config_path()) else " (absent)"))
    exists = os.path.isdir(STREAM_DIR)
    print(f"stream dir: {STREAM_DIR}" + ("" if exists else "  (not initialized — run `brain init`)"))
    if exists:
        scopes = _scopes()
        print(f"scopes    : {len(scopes)}")
        # Shared (multi-writer) scopes: name the writer set of each and flag any
        # spool whose write timestamps run backwards (a sync arrived out of order
        # — harmless, the fold still merges deterministically, but worth seeing).
        shared = [s for s in scopes if _is_shared(s)]
        if shared:
            print(f"shared    : {len(shared)} scope(s) on the multi-writer sync-folder layout")
            for s in shared:
                writers = _spool_writers(s)
                frozen = len(_read_file_lines(_scope_path(s)))
                print(f"  {s}: writers [{', '.join(writers) or 'none yet'}], "
                      f"{frozen} frozen legacy entries")
                for w in writers:
                    tss = [(_parse(ln)[0] if _parse(ln) else "")
                           for ln in _read_file_lines(_spool_path(s, w))]
                    ooo = any(tss[i] and tss[i - 1] and tss[i] < tss[i - 1]
                              for i in range(1, len(tss)))
                    if ooo:
                        print(f"    ! spool {w}.log has out-of-order write timestamps "
                              "(sync lag — normal; the fold is still deterministic)")
    if _root_is_git_backed():
        print("git       : OK — every write commits; the stream is protected by git.")
        return 0
    if not _git_available():
        print("git       : ABSENT (git is not installed).")
    else:
        print("git       : ABSENT (the data root is not a git repo).")
    print("            DEGRADED PROFILE: the stream's safety net is the append-only")
    print("            spool files themselves plus your folder-sync version history")
    print("            (e.g. OneDrive). Writes still land; there are no commits to")
    print("            roll back to. This is expected on a locked-down machine.")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(prog="brain", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("note", help="append one typed entry")
    p.add_argument("--project", "-p", help="scope slug (default: global)")
    p.add_argument("--type", "-t", required=True)
    p.add_argument("--supersedes", help="entry number(s) this entry replaces, e.g. 7,9")
    p.add_argument("--asof", help="date the fact was TRUE (YYYY-MM-DD), when it "
                   "differs from now — backfilled facts must not read as newest")
    p.add_argument("--source", "--src", dest="source",
                   help="citation: the evidence behind this entry (commit sha, "
                   "file path, projects/<scope>/<topic>.md, url); one token, "
                   "comma-separate multiples")
    p.add_argument("--subsystem", help="hazard/invariant label (required for gotcha/invariant)")
    p.add_argument("--force-type", dest="force_type", action="store_true",
                   help="override the fix-language gate for a genuine gotcha")
    p.add_argument("--distinct", action="store_true",
                   help="override the near-duplicate gate: this is a separate fact")
    p.add_argument("--pin", action="store_true",
                   help="mark this an identity PIN: verbatim, always loaded at the top "
                   "of wake. Only valid with --type invariant, and still needs "
                   "--subsystem (use 'identity' for identity facts). Superseding a pin "
                   "does NOT carry the pin — re-pass --pin to keep it. Scope pins sum to "
                   "a 1200-char cap.")
    p.add_argument("text", nargs="+")
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("papercuts", help="open papercuts clustered by recurrence")
    p.add_argument("--scope", help="one scope (default: all)")
    p.set_defaults(fn=cmd_papercuts)

    p = sub.add_parser("recall", help="live rulings bearing on a topic (the conversational gate)")
    p.add_argument("--scope", help="one scope (default: all)")
    p.add_argument("--budget", type=int, default=0, help="hard char budget for the body")
    p.add_argument("--exclude", help="scope#id list already surfaced this session")
    p.add_argument("text", nargs="+")
    p.set_defaults(fn=cmd_recall)

    p = sub.add_parser("resolve", help="retire hazard #N (milestone + sup=N; index drops it)")
    p.add_argument("--project", "-p", help="scope slug (default: global)")
    p.add_argument("entry", help="hazard entry id to resolve (a number, or a shared "
                   "scope's <writer>:<seq> id)")
    p.add_argument("text", nargs="+", help="how it was fixed")
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("share", help="convert a scope to the multi-writer "
                       "(sync-folder) layout: freeze its log, route new notes to "
                       "a per-writer spool")
    p.add_argument("--scope", "--project", dest="scope",
                   help="scope slug to share (default: global)")
    p.set_defaults(fn=cmd_share)

    p = sub.add_parser("wake", help="root + newest raw tail + pull map")
    p.add_argument("--scope", help="scope slug (default: global)")
    p.set_defaults(fn=cmd_wake)

    p = sub.add_parser("hazards", help="gotchas/dead-ends verbatim, grouped by subsystem")
    p.add_argument("--scope", "--project", dest="scope")
    p.add_argument("--grep", help="only entries matching this regex")
    p.add_argument("--budget", type=int,
                   help="char budget: whole groups kept newest-first, rest digested")
    p.add_argument("--prefer", help="path/tokens: matching groups kept full-text first")
    p.add_argument("--match-path", dest="match_path",
                   help="only hazards whose text/label whole-word-match this file "
                        "path (just-in-time edit gate); silent when none match")
    p.add_argument("--exclude",
                   help="comma-separated entry #s to skip (session dedup for the gate)")
    p.set_defaults(fn=cmd_hazards)

    p = sub.add_parser("design", help="live invariants verbatim, grouped by subsystem")
    p.add_argument("--scope", "--project", dest="scope")
    p.add_argument("--grep", help="only entries matching this regex")
    p.add_argument("--budget", type=int,
                   help="char budget: whole groups kept newest-first, rest digested")
    p.set_defaults(fn=cmd_design)

    p = sub.add_parser("zoom", help="raw entries in range")
    p.add_argument("scope")
    p.add_argument("range", help="<lo>-<hi>, 1-based")
    p.set_defaults(fn=cmd_zoom)

    p = sub.add_parser("branches", help="chrono branch summaries")
    p.add_argument("scope")
    p.set_defaults(fn=cmd_branches)

    for name, typ in (
        ("decisions", "decision"),
        ("deadends", "dead-end"),
        ("gotchas", "gotcha"),
        ("milestones", "milestone"),
        ("questions", "question"),
        ("invariants", "invariant"),
    ):
        p = sub.add_parser(name, help=f"{typ} entries (verbatim, newest last)")
        p.add_argument("--scope", "--project", dest="scope")
        p.add_argument("--recent", "-n", type=int)
        p.set_defaults(fn=lambda a, t=typ: cmd_view(a, t))

    p = sub.add_parser("grep", help="regex over raw streams")
    p.add_argument("pattern")
    p.add_argument("--scope")
    p.set_defaults(fn=cmd_grep)


    # RETIRED — folded into the ticket board (tickets.py). Kept as a fail-loud
    # stub: it swallows any remaining args and exits 1 naming the replacement,
    # so an old `brain dispatch ...` habit fails visibly instead of silently.
    p = sub.add_parser("dispatch",
                       help="RETIRED — tracked work now lives on the ticket board (tickets.py)")
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="ignored — the dispatch board is retired")
    p.set_defaults(fn=cmd_dispatch)

    p = sub.add_parser("rebuild", help="regenerate branches + hazards + root")
    p.add_argument("--scope")
    p.add_argument("--all", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="ignore caches: rebuild every branch and the root")
    p.add_argument("--alert", action="store_true",
                   help="push a ntfy alert if any scope fails (scheduled runs)")
    p.set_defaults(fn=cmd_rebuild)

    p = sub.add_parser("history", help="durable transcript archive: ingest | "
                                       "summarize | show <id> | \"search query\"")
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="ingest | summarize [--dry-run] [--limit N] | "
                        "show <id> [--around T] [-n N] | <search query>")
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("status", help="per-scope counts, watermarks, injected footprint")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("lint", help="deterministic integrity audit (provenance, "
                                    "supersession, labels, budgets)")
    p.add_argument("--scope", "--project", dest="scope")
    p.set_defaults(fn=cmd_lint)

    p = sub.add_parser("sessions", help="entries grouped by capture session (sid)")
    p.add_argument("--scope", "--project", dest="scope")
    p.set_defaults(fn=cmd_sessions)

    p = sub.add_parser("init", help="create a fresh, empty instance at the data root")
    p.add_argument("--no-git", dest="no_git", action="store_true",
                   help="do not init a git repo (folder-sync / no-git profile)")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="report the instance's data root and git safety-net status")
    p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
