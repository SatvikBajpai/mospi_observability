# MoSPI MCP Observability

Tooling for capturing OpenTelemetry traces from the MoSPI Model-Context-Protocol
(MCP) server, persisting them to disk, and generating engagement reports.

## What this is for

The MoSPI MCP server is queried by Claude (and other agents) on behalf of human
users who want answers from official statistics. Each query becomes a Jaeger
trace. This repo:

1. Captures those traces persistently (so they survive Jaeger restarts).
2. Lets you generate an HTML report for any date window with one local command.

## Quick start

```bash
python3 analysis/make_report.py
```

That single command will:

1. SSH into the Jaeger server (alias `jaeger`).
2. Pull only the traces in the current data window.
3. Render a rich HTML report locally.
4. Write it to `~/Desktop/mospi-report-<timestamp>.html`.
5. Open it in your default browser (macOS).

## Report CLI

```bash
# Default: window auto-starts at the continuous-capture date
python3 analysis/make_report.py

# Last N days
python3 analysis/make_report.py --since 7

# Specific window
python3 analysis/make_report.py --start 2026-05-08 --end 2026-05-11

# Custom output path
python3 analysis/make_report.py --output ~/Desktop/may-report.html

# Don't auto-open
python3 analysis/make_report.py --no-open
```

| Flag | Default | What it does |
|------|---------|---|
| `--since N` | unset | Last N days from today (IST), inclusive |
| `--start YYYY-MM-DD` | continuous start | Window start, inclusive |
| `--end YYYY-MM-DD` | today | Window end, inclusive |
| `--host` | `jaeger` | SSH alias for the Jaeger server |
| `--remote` | `~/observability/archive/traces.jsonl` | Path to the JSONL archive on the server |
| `--output` | `~/Desktop/mospi-report-<stamp>.html` | Where to write the HTML |
| `--no-open` | off | Skip auto-launching the browser |
| `--top` | `12` | Cap for top-N lists in the report |

## Date-window behaviour

The report auto-detects the date when continuous data capture began (currently
06 May 2026, after Jaeger was switched to persistent Badger storage).

| Your range | What happens |
|---|---|
| Fully on or after continuous start | Report rendered as requested |
| Straddles the continuous start | Window is clipped to start at the continuous date; caveat says "Showing from this date onwards (your range began earlier)" |
| Fully before the continuous start | Empty-state HTML: "No data available. Please try a range from DD Month YYYY onwards." |

Every report shows a single italic line near the title: *Data available since DD Month YYYY.*

## Architecture

```
   MoSPI MCP server (wherever it runs)
       |
       | OTLP gRPC (port 4317)
       v
   Jaeger container on the server
       |
       | Badger persistent storage (disk)
       |
       +--> Jaeger UI on :16686
       |
       | every 15 min, systemd user timer fires
       v
   archive_traces.py
       |
       | appends new trace IDs to JSONL
       v
   ~/observability/archive/traces.jsonl
       ^
       | SSH + remote date-filter
       |
   analysis/make_report.py (on your Mac)
       |
       v
   HTML report on ~/Desktop
```

The archiver is append-only and deduplicates by trace ID, so it is safe to run
on any schedule. A fcntl lock prevents concurrent runs from corrupting the file.

## Setup from scratch

### 1. Jaeger with persistent storage

On the Jaeger host:

```bash
docker run -d --name jaeger --restart=unless-stopped \
  -p 4317:4317 -p 16686:16686 \
  -e SPAN_STORAGE_TYPE=badger \
  -e BADGER_EPHEMERAL=false \
  -e BADGER_DIRECTORY_VALUE=/badger/data \
  -e BADGER_DIRECTORY_KEY=/badger/key \
  -v /home/ubun/jaeger-badger:/badger \
  jaegertracing/all-in-one:latest
```

### 2. Deploy the archiver on the Jaeger server

```bash
# from this repo on your Mac
ssh jaeger 'mkdir -p ~/observability/archive ~/.config/systemd/user'
scp archive_traces.py jaeger:~/observability/
scp systemd/jaeger-archiver.service jaeger:~/.config/systemd/user/
scp systemd/jaeger-archiver.timer   jaeger:~/.config/systemd/user/

ssh jaeger 'loginctl enable-linger ubun; \
  systemctl --user daemon-reload; \
  systemctl --user enable --now jaeger-archiver.timer; \
  systemctl --user list-timers jaeger-archiver.timer'
```

### 3. Local SSH config

Add to your `~/.ssh/config`:

```
Host jaeger
    HostName 10.24.89.149
    User ubun
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
```

Then `ssh jaeger 'hostname; uptime'` should work without prompting for a password.

## Files

```
.
├── README.md                            this file
├── requirements.txt                     just `requests` (for archive_traces.py)
├── .gitignore
├── archive_traces.py                    server-side archiver (runs every 15 min)
├── analysis/
│   └── make_report.py                   local report generator
└── systemd/
    ├── jaeger-archiver.service          systemd user service definition
    └── jaeger-archiver.timer            fires every 15 min
```

| File | Where it runs | When |
|------|---------------|------|
| `archive_traces.py` | Jaeger server | Every 15 min via systemd timer |
| `analysis/make_report.py` | Your Mac | When you run it |
| `systemd/*` | Jaeger server | Installed under `~/.config/systemd/user/` |

## Inspect what the archiver is doing

```bash
# Last few archive runs
ssh jaeger 'tail ~/observability/archive/archive.log'

# Current state snapshot
ssh jaeger 'cat ~/observability/archive/state.json'

# Timer status
ssh jaeger 'systemctl --user list-timers jaeger-archiver.timer'

# Total traces captured
ssh jaeger 'wc -l ~/observability/archive/traces.jsonl'
```

## Environment variables for the archiver

The archiver reads a few env vars (configured in
`systemd/jaeger-archiver.service`):

| Variable | Default | What it does |
|----------|---------|---|
| `JAEGER_URL` | `http://localhost:16686` | Jaeger HTTP endpoint |
| `JAEGER_SERVICE` | `mospi-mcp-server` | Service name to query |
| `LOOKBACK_HOURS` | `24` | How far back to look on each run |
| `PER_OP_LIMIT` | `5000` | Cap per operation per run |
| `HTTP_TIMEOUT` | `180` | Seconds before giving up on Jaeger |

## Data model and filtering

Each tool span carries tags including `tool.name`, `tool.input`, `tool.output`,
`tool.output_size`. The natural-language user question is on
`tool.input.user_query`. The report:

- Drops sessions whose query is `health check`, `healthcheck`, `ping`, or `test`
  (these are automated probes, not engagement).
- Treats one Jaeger trace as one user query, even if multiple tool spans hit
  the MCP on its behalf.
- Counts "tool calls" as the total number of tool spans across qualifying
  sessions (i.e. on behalf of real users).

## Notes

Trace JSONL files are gitignored and never committed: they are large and may
contain user query text and client IPs. Only code and config live in this repo.
