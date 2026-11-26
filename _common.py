"""Small helpers for tickets.py: filename-safe writer ids, the home writer name,
and an append held under a lock, so concurrent writers never lose a line."""
from __future__ import annotations

import contextlib
import os
import re
import time


def safe_name(name: str) -> str:
    """Keep a writer id/name usable as a filename (no slashes, spaces, etc.).

    Non-alphanumeric characters (outside ``-_.``) become ``-``; the result is
    stripped of leading/trailing ``-`` and falls back to ``"unknown"`` when it
    would be empty.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name).strip("-")
    return cleaned or "unknown"


def home_writer_name() -> str:
    """The last segment of the home path (C:\\Users\\jsmith -> 'jsmith').

    Split on both slash styles so a Windows-style home path gives the same
    answer on any platform (os.path.basename would not). Returned raw — the
    caller applies its own sanitizer.
    """
    home = os.path.expanduser("~")
    parts = [p for p in re.split(r"[\\/]+", home) if p]
    return parts[-1] if parts else ""


@contextlib.contextmanager
def _held(path: str):
    """Hold an OS lock while the block runs (msvcrt on Windows, flock elsewhere): appends from several processes to
    one file are not atomic on Windows, so two at once can lose or tear a line. The lock is one file beside the
    folder (``events/x.jsonl`` -> ``events.lock``), so nothing extra lands among the folder's files."""
    with open(os.path.dirname(os.path.abspath(path)) + ".lock", "a+b") as fh:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)   # retries ~10 s itself, then raises
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def append_with_retry(path: str, line: str, attempts: int = 5) -> None:
    """Append one line to a file under its lock (:func:`_held`), riding out transient contention on open."""
    for attempt in range(attempts):
        try:
            with _held(path), open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
