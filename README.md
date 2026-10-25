# agent-brain — the ticket board and the session-start hook

Two stdlib-only pieces:

- **`tickets.py`** — the multi-writer ticket board: the one home for tracked work and open
  questions across every project. Per-writer append-only event logs under
  `~/.claude/global-kb/tickets/events/` fold into a computed board, so it is conflict-free under
  folder sync. Spec: `docs/TICKETS-SPEC.md`.

  ```
  python tickets.py open --prefix <PROJECT> "title" --root ~/.claude/global-kb/tickets
  python tickets.py claim -t KEY --owner claude --root ...
  python tickets.py close -t KEY --source <evidence> --root ...
  python tickets.py board --root ...
  ```

  Read by ccdesk (`tickets_board.py`, the web board), life-ops (`heartbeat.py`, the daily push),
  bluestone and wholesale-ops (operator runs).

- **`hooks/session_start.py`** — the SessionStart hook: Tod's profile (`global-kb/user.md`), the
  tickets due today or overdue, and pending phone replies (`global-kb/inbox.md`). Silent inside
  delegated runs (`CCDESK_ROLE` set). The wired copy is `~/.claude/hooks/session_start.py`;
  copy the file there after editing it.

Tests: `python -m pytest tests/`.
