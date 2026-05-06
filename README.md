# MoSPI MCP Observability

Tooling for inspecting and archiving Jaeger traces emitted by the MoSPI MCP server.

## Files

| File | Purpose |
|---|---|
| `peek.py` | CLI session inspector. Lists or drills into individual MCP sessions from Jaeger. |
| `dashboard.py` | FastAPI + Plotly dashboard on `http://localhost:7777`. |
| `archive_traces.py` | Incremental archiver. Appends new traces to `archive/traces.jsonl`. |
| `_migrate_once.py` | One-shot script that seeds `archive/traces.jsonl` from a full `all_traces.json` dump with hash verification. |
| `requirements.txt` | Python deps. |

Trace dumps (`all_traces.json`, `traces.jsonl`, `archive/`) are gitignored: large and contain user query text / client IPs.

## Setup

```bash
pip install -r requirements.txt
```

By default everything points at `http://10.24.89.149:16686` (the internal Jaeger). Override per process:

```bash
JAEGER_URL=http://other-host:16686 python3 archive_traces.py
```

## Inspecting traces

```bash
python3 peek.py                  # list recent sessions
python3 peek.py --session 3      # detail on session #3
python3 peek.py --hours 168      # widen the window
```

```bash
python3 dashboard.py             # http://localhost:7777
```

## Archiving traces

`archive_traces.py` is safe to run on any cadence. It:

- locks `archive/.lock` so two copies never overlap;
- loads existing trace IDs from `archive/traces.jsonl`;
- queries Jaeger for the last `LOOKBACK_HOURS` (default 24h);
- appends only traces whose IDs aren't already archived;
- writes a `archive/state.json` summary for monitoring.

Env vars: `JAEGER_URL`, `JAEGER_SERVICE`, `LOOKBACK_HOURS`, `PER_OP_LIMIT`, `HTTP_TIMEOUT`.

### Schedule (macOS launchd)

A launchd plist lives at `~/Library/LaunchAgents/com.satvik.jaegerarchive.plist` and runs the archiver every 15 minutes (15 * 60 = 900s). Reload after editing:

```bash
launchctl unload ~/Library/LaunchAgents/com.satvik.jaegerarchive.plist
launchctl load   ~/Library/LaunchAgents/com.satvik.jaegerarchive.plist
launchctl list | grep jaegerarchive
tail -f archive/archive.log
```

## SSH access to the Jaeger host

`~/.ssh/config` has a `jaeger` alias (key auth, user `ubun`):

```bash
ssh jaeger 'hostname; uptime'
```

## Initial seed

If you have a one-shot dump (`all_traces.json`) and want to seed `archive/traces.jsonl` from it, run:

```bash
python3 _migrate_once.py
```

The script aborts if `archive/traces.jsonl` already exists, never touches the source file, and verifies every trace's hash before atomically renaming the temp file into place.
