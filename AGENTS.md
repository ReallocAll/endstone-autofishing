# Development notes

- Preserve the v0.4.0 `endstone` entry point, commands, config keys, and `armed_players.json` format.
- Do not alter the packet 30 -> packet 144 transaction conversion without protocol tests. BDS 1.26.51.1 (2193) is the verified baseline.
- Keep persistent enabled preferences separate from ephemeral fishing sessions. Movement must not remove opt-in; a new manual cast must work after any stop.
- Do not introduce world entity scans, per-player per-tick polling, or unfiltered extra high-frequency Python event subscriptions.
- Preserve O(1) hook runtime-ID tracking, per-session one-shot tasks, and multiplayer isolation.
- Keep user data and server configuration out of Git. Never deploy to production or publish Releases in CI.
- Run `uv run ruff check src tests/test_regressions.py`, `uv run pytest -q`, and `uv build` before proposing a release.
