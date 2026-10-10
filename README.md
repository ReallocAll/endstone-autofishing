# Endstone AutoFishing

A pure-Python automatic fishing plugin for Minecraft Bedrock Dedicated Server (BDS) through Endstone. It uses event-driven, per-player fishing sessions and avoids scanning world entities or polling every connected player.

**Version:** 0.4.0 · **Python:** 3.10+ · **Endstone API:** 0.11 · **Verified protocol:** BDS 1.26.51.1 (2193). Protocol values for other BDS builds are configurable but not independently verified.

## Features

- Opt in once; the enabled preference persists across logins and server restarts.
- Manually cast a fishing rod anywhere to start a session. The plugin then reels in on a bite and casts again automatically.
- Movement, teleporting, death, or switching hotbar slots ends only the active session; the persistent preference remains enabled.
- Fishing-hook runtime IDs are tracked per player, with no world-wide entity scans.
- One-shot scheduling for recasts and retries; no repeating all-player polling.
- Packet handlers return quickly for unrelated traffic. Endstone 0.11 does **not** provide native packet-ID filters, so all registered packet events still cross the Python bridge.
- Optional packet/callback counters for profiling. Verbose per-cast popups are disabled to avoid additional outgoing packets.

## Install

Place the `endstone_autofishing-0.4.0-py3-none-any.whl` distribution in the server's `plugins/` directory and restart Endstone. Do not install the source ZIP in place of a wheel. A supported Endstone installation is required on the server.

The package registers the `endstone` entry point `autofishing = endstone_autofishing:AutoFishingPlugin`.

## Commands

| Command | Purpose |
| --- | --- |
| `/autofish on` | Enable persistent automatic fishing |
| `/autofish off` | Disable persistent automatic fishing |
| `/autofish status` | Show enabled and session state |
| `/autofish reset` | Clear the current session without disabling the preference |
| `/autofish stats` | Show counters (when enabled in configuration) |
| `/autofish` | Toggle the persistent preference |

`/af` is an alias. Command arguments offer `on`, `off`, `status`, `reset`, and `stats` completions. These commands can also be called by server menus such as ClockMenu.

## Configuration

The plugin copies its bundled defaults to `plugins/autofishing/config.toml` on first use. Common settings:

```toml
[general]
movement-tolerance = 0.45
vertical-movement-tolerance = 1.0
recast-delay-ticks = 12
hook-association-radius = 12.0
show-status = true
cast-retry-seconds = 4.0
manual-start-timeout-seconds = 2.0

[debug]
protocol-log = false
performance-counters = false
```

See [`src/endstone_autofishing/config.toml`](src/endstone_autofishing/config.toml) for packet IDs and additional comments. Player opt-ins are stored in the plugin data directory as `armed_players.json` under the existing `{"xuids": [...]}` format. **Never commit this runtime data file.**

## Development

```bash
uv sync --group dev
uv run ruff check src tests/test_regressions.py
uv run pytest -q
uv build
```

The `tests/checks/` scripts are the regression checks from the v0.4.0 source distribution. Pytest invokes each in an isolated Python process because the checks supply their own fake Endstone modules. Real BDS networking must still be tested on a non-production test server before interpreting these checks as live-server proof.

The GitHub Actions workflow runs lint, regression tests, wheel/sdist build, and installation/entry-point checks, and retains build artifacts without publishing a package or Release.

## License

GPL-3.0-only. See [`LICENSE`](LICENSE). The repository contains no vendored third-party code or bundled assets; Endstone is a separate runtime dependency.
