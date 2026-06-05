# MoSPI MCP Observability

Captures OpenTelemetry traces from the MoSPI Model-Context-Protocol (MCP)
server, persists them to disk, and produces engagement reports for any
date window. The weekly report runs server-side via cron and emails a
PDF + CSV to a recipient list.

This README is also the operator runbook. Read it once and you can run
the system, add a new cadence (monthly, daily, whatever), or hand it off.

## How the system fits together

```
   MoSPI MCP server (10.24.89.20)             Jaeger host (10.24.89.149, alias `jaeger`)
   - emits OTLP traces           --(4317)-->  jaeger container, Badger storage
   - emits ~/syslog.jsonl        --(rsync)->  ~/syslog.jsonl
                                              every 10 min, systemd user timer
                                              |
                                              | jaeger-archiver.timer (every 15 min)
                                              v
                                              ~/observability/archive/traces.jsonl
                                              |
                                              | cron Mon 08:00 IST
                                              v
                                              weekly_report.sh
                                              -> server_report.py
                                                 - renders HTML
                                                 - Chromium -> PDF
                                                 - writes queries CSV
                                                 - SMTP -> recipients
```

There are two ways to get a report.

1. **Scheduled, server-side**: `server_report.py` runs on the Jaeger host, rendered to PDF, emailed via Gmail SMTP. Driven by cron.
2. **Ad-hoc, from your Mac**: `analysis/make_report.py` SSHes into the Jaeger host, pulls a window's worth of traces, and renders an HTML report on your Desktop.

Both read the same `~/observability/archive/traces.jsonl` and the same `~/syslog.jsonl`.

## What's deployed where

On the Jaeger host (`jaeger` = `ubun@10.24.89.149`):

| Path | What |
|---|---|
| `~/observability/server_report.py` | Server-side report generator |
| `~/observability/archive_traces.py` | Trace archiver |
| `~/observability/weekly_report.sh` | Cron wrapper for the weekly run (recipients + env vars live here) |
| `~/observability/archive/traces.jsonl` | The trace archive (append-only) |
| `~/syslog.jsonl` | CPU/RAM samples synced from the MCP host |
| `~/.mospi-mail.env` | SMTP credentials (chmod 600) |
| `~/.config/systemd/user/jaeger-archiver.{service,timer}` | Archiver every 15 min |
| `~/.config/systemd/user/syslog-sync.{service,timer}` | Pulls syslog from MCP host every 10 min |
| `crontab -l` | `0 8 * * 1 /home/ubun/observability/weekly_report.sh` |

On the MCP host (`ubun@10.24.89.20`):
- The MCP server, with OTLP exporter pointed at `jaeger:4317`.
- A process that appends to `~/observability/syslog/syslog.jsonl` every ~10 minutes.

On your Mac:
- `analysis/make_report.py` (ad-hoc reports).
- `~/.ssh/config` entry for `Host jaeger` with the matching SSH key.

## Operator runbook

### Add or remove a recipient

Edit one line in the wrapper script:

```bash
ssh jaeger 'nano ~/observability/weekly_report.sh'
```

Find the `--email` line, edit the comma-separated list inside the `${REPORT_RECIPIENTS:-...}` default. Save. No restart needed - the next cron run will pick it up.

Verify:

```bash
ssh jaeger 'grep -o "REPORT_RECIPIENTS:-[^}]*" ~/observability/weekly_report.sh | tr "," "\n"'
```

### Change credentials

```bash
ssh jaeger 'nano ~/.mospi-mail.env'
```

Quote values with spaces (e.g. Gmail app passwords have spaces).

```
SMTP_HOST="smtp.gmail.com"
SMTP_PORT="587"
SMTP_USER="dirccmospi@gmail.com"
SMTP_PASS="wpkt ndba qudk eyzv"
FROM_ADDR="dirccmospi@gmail.com"
```

### Send a one-off report (any window, any recipients, does NOT touch cron)

```bash
ssh jaeger 'cd ~/observability && set -a && . ~/.mospi-mail.env && set +a && \
  python3 server_report.py \
    --start 2026-05-06 --end 2026-05-31 \
    --email "person@example.com,other@example.com" \
    --subject "MoSPI MCP Engagement Report (06 to 31 May 2026)" \
    --output ~/observability/may-2026.html'
```

Skip `--email` if you only want the files written (PDF + CSV + HTML).

### Worked example: add a monthly report

Goal: on the **1st of every month at 08:00 IST**, email a report for the **previous calendar month**.

1. Create the wrapper on the server:

```bash
ssh jaeger 'cat > ~/observability/monthly_report.sh' <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG=/home/ubun/observability/monthly-cron.log
exec >> "$LOG" 2>&1
echo "===== $(date) ====="
cd /home/ubun/observability
set -a
. /home/ubun/.mospi-mail.env
set +a

# Previous calendar month
START=$(date -d "$(date +%Y-%m-01) -1 month" +%Y-%m-%d)
END=$(date -d "$(date +%Y-%m-01) -1 day" +%Y-%m-%d)
TAG=$(date -d "$START" +%Y-%m)
OUT=/home/ubun/observability/monthly-${TAG}.html

python3 server_report.py \
  --start "$START" --end "$END" \
  --email "${MONTHLY_RECIPIENTS:-satvikbajpai@gmail.com}" \
  --subject "MoSPI MCP Monthly Engagement Report (${START} to ${END})" \
  --output "$OUT"
EOF

ssh jaeger 'chmod +x ~/observability/monthly_report.sh'
```

2. Add the cron entry (append, don't overwrite the weekly one):

```bash
ssh jaeger '(crontab -l 2>/dev/null; echo "0 8 1 * * /home/ubun/observability/monthly_report.sh") | crontab -'
ssh jaeger 'crontab -l'
```

3. Test it once now (does NOT wait for the 1st):

```bash
ssh jaeger '/home/ubun/observability/monthly_report.sh'
ssh jaeger 'tail -15 /home/ubun/observability/monthly-cron.log'
```

If the email lands, you're done. The cron pattern `0 8 1 * *` means "minute 0, hour 8, day-of-month 1, every month, any weekday" - so it fires once a month at 08:00 IST on the 1st.

To change the recipient list for the monthly without touching the weekly:

```bash
ssh jaeger 'MONTHLY_RECIPIENTS="boss@example.com,team@example.com" \
  /home/ubun/observability/monthly_report.sh'   # one-off override
```

Or edit the default inside `monthly_report.sh`.

### Add a different cadence in general

Cron syntax cheat sheet (all times = server's local timezone, which is **IST** here):

| Cron expression | Fires |
|---|---|
| `0 8 * * 1` | Mon 08:00 (the weekly) |
| `0 8 1 * *` | 1st of every month, 08:00 (the monthly above) |
| `0 8 * * *` | every day at 08:00 |
| `0 */6 * * *` | every 6 hours |
| `30 9 * * 1-5` | Mon-Fri at 09:30 |

The pattern is always: one wrapper script + one cron line. The wrapper sources `~/.mospi-mail.env`, computes the window, and runs `server_report.py --email ... --subject ... --output ...`.

### Ad-hoc reports from your Mac

```bash
python3 analysis/make_report.py                              # default window (since 06 May 2026)
python3 analysis/make_report.py --since 7                    # last 7 days
python3 analysis/make_report.py --start 2026-05-08 --end 2026-05-11
python3 analysis/make_report.py --output ~/Desktop/x.html --no-open
```

This SSHes into `jaeger`, fetches only the date-windowed slice, renders HTML locally, and auto-opens it on macOS. No email, no cron, no production impact.

### Inspect what's running

```bash
# weekly cron log
ssh jaeger 'tail -30 /home/ubun/observability/weekly-cron.log'

# archiver state
ssh jaeger 'cat ~/observability/archive/state.json'

# timers status
ssh jaeger 'systemctl --user list-timers jaeger-archiver.timer syslog-sync.timer --no-pager'

# cron entries
ssh jaeger 'crontab -l'

# trace archive size + freshness
ssh jaeger 'wc -l ~/observability/archive/traces.jsonl; tail -1 ~/observability/archive/traces.jsonl | python3 -c "import json,sys; t=json.loads(sys.stdin.read()); import datetime; print(datetime.datetime.fromtimestamp(max(s[\"startTime\"] for s in t[\"spans\"])/1e6))"'

# syslog freshness
ssh jaeger 'wc -l ~/syslog.jsonl; tail -1 ~/syslog.jsonl'
```

### Common things that go wrong

| Symptom | Likely cause |
|---|---|
| Cron ran but no email arrived | Check the SMTP creds: `ssh jaeger 'cat ~/.mospi-mail.env'`. App password must be quoted (it has spaces). Watch the cron log: `tail -30 ~/observability/weekly-cron.log` |
| PDF not generated, email arrives with HTML attachment instead | `google-chrome` missing. Install: `sudo apt install google-chrome-stable`. Verify: `which google-chrome` |
| CPU section is empty in the report | `~/syslog.jsonl` on jaeger is stale or missing. Check `syslog-sync.timer` status; the MCP host (10.24.89.20) must be reachable from jaeger over SSH key auth |
| Report shows 0 tool calls | Archiver is not pulling. Check `state.json.last_run_utc` and `archive.log`. Could be: Jaeger restarted, or service name / operation names changed in FastMCP version |
| Client distribution looks wrong (e.g. one bucket suspiciously huge) | Likely a UA classification change. The fix from May 2026 reads `http.user_agent` only from the inbound `/mcp` server span, never `user_agent.original`. If FastMCP swaps to new OTel semantic conventions, the tag name itself may need updating |

## Setup from scratch

Follow this only if you're deploying onto a brand-new server. Otherwise skip to the operator runbook above.

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

### 2. SSH keys

From your Mac:

```bash
# (one-time) create a key if you don't have one
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519

# install on the Jaeger host
ssh-copy-id -i ~/.ssh/id_ed25519.pub ubun@<JAEGER-IP>

# add to ~/.ssh/config so `ssh jaeger` works
cat >> ~/.ssh/config <<EOF
Host jaeger
    HostName <JAEGER-IP>
    User ubun
    IdentityFile ~/.ssh/id_ed25519
EOF
```

Then enable jaeger to reach the MCP host (for syslog rsync):

```bash
ssh jaeger 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519'
ssh -t jaeger 'ssh-copy-id -i ~/.ssh/id_ed25519.pub ubun@<MCP-IP>'
```

### 3. Deploy the archiver

```bash
ssh jaeger 'mkdir -p ~/observability/archive ~/.config/systemd/user'
scp archive_traces.py jaeger:~/observability/
scp systemd/jaeger-archiver.service jaeger:~/.config/systemd/user/
scp systemd/jaeger-archiver.timer   jaeger:~/.config/systemd/user/

ssh jaeger 'loginctl enable-linger ubun; \
  systemctl --user daemon-reload; \
  systemctl --user enable --now jaeger-archiver.timer'
```

### 4. Deploy the syslog sync

On jaeger, create the service + timer:

```bash
ssh jaeger "cat > ~/.config/systemd/user/syslog-sync.service" <<'EOF'
[Unit]
Description=Pull syslog.jsonl from MCP host into Jaeger host
[Service]
Type=oneshot
ExecStart=/usr/bin/rsync -a --partial -e "ssh -o StrictHostKeyChecking=accept-new" ubun@<MCP-IP>:/home/ubun/observability/syslog/syslog.jsonl /home/ubun/syslog.jsonl
EOF

ssh jaeger "cat > ~/.config/systemd/user/syslog-sync.timer" <<'EOF'
[Unit]
Description=Pull syslog.jsonl every 10 minutes
[Timer]
OnBootSec=1min
OnUnitActiveSec=10min
Unit=syslog-sync.service
Persistent=true
[Install]
WantedBy=timers.target
EOF

ssh jaeger 'systemctl --user daemon-reload; \
  systemctl --user enable --now syslog-sync.timer'
```

### 5. Deploy the report generator + email creds + cron

```bash
scp analysis/server_report.py jaeger:~/observability/

# credentials (chmod 600), edit values to match your SMTP provider
ssh jaeger 'umask 077; cat > ~/.mospi-mail.env' <<'EOF'
SMTP_HOST="smtp.gmail.com"
SMTP_PORT="587"
SMTP_USER="your-sender@gmail.com"
SMTP_PASS="your app password with spaces"
FROM_ADDR="your-sender@gmail.com"
EOF

# wrapper script (replace the recipient list)
ssh jaeger 'cat > ~/observability/weekly_report.sh' <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG=/home/ubun/observability/weekly-cron.log
exec >> "$LOG" 2>&1
echo "===== $(date) ====="
cd /home/ubun/observability
set -a
. /home/ubun/.mospi-mail.env
set +a

START=$(date -d "7 days ago" +%Y-%m-%d)
END=$(date +%Y-%m-%d)
OUT=/home/ubun/observability/weekly-$(date +%Y%m%d).html

python3 server_report.py --since 7 \
  --email "${REPORT_RECIPIENTS:-you@example.com}" \
  --subject "MoSPI MCP Weekly Engagement Report (${START} to ${END})" \
  --output "$OUT"
EOF
ssh jaeger 'chmod +x ~/observability/weekly_report.sh'

# install the weekly cron (Mon 08:00 IST)
ssh jaeger '(crontab -l 2>/dev/null | grep -v weekly_report.sh; echo "0 8 * * 1 /home/ubun/observability/weekly_report.sh") | crontab -'

# install Chrome for PDF rendering (one time, needs sudo)
ssh jaeger 'sudo apt-get install -y google-chrome-stable || true'

# smoke-test once
ssh jaeger '/home/ubun/observability/weekly_report.sh'
ssh jaeger 'tail -10 /home/ubun/observability/weekly-cron.log'
```

## Files in this repo

```
.
+- README.md                            this guide
+- requirements.txt                     `requests` for archive_traces.py
+- .gitignore
+- archive_traces.py                    server-side trace archiver
+- analysis/
|   +- server_report.py                 server-side scheduled report generator
|   +- make_report.py                   ad-hoc Mac-side report generator
+- systemd/
    +- jaeger-archiver.service          archiver systemd unit
    +- jaeger-archiver.timer            fires the archiver every 15 min
```

## Data and security notes

- Trace JSONL files are gitignored. They are large and contain user query text. Only code and config live in this repo.
- SMTP credentials live in `~/.mospi-mail.env` on the Jaeger host (chmod 600). Never commit them.
- The PDF and CSV produced for each weekly run are archived under `~/observability/weekly-YYYYMMDD.{html,pdf,csv}`. Clean these up periodically if disk space matters.
- `~/observability/weekly-cron.log` accumulates run history. Truncate it occasionally if it gets large.

## Quick reference

```bash
# fire an ad-hoc weekly report manually (uses default recipients)
ssh jaeger '/home/ubun/observability/weekly_report.sh'

# ad-hoc custom-window report from your Mac, no email
python3 analysis/make_report.py --since 14

# server-side report for any window, emailed to one person, no cron impact
ssh jaeger 'cd ~/observability && set -a && . ~/.mospi-mail.env && set +a && \
  python3 server_report.py --start 2026-05-06 --end 2026-05-31 \
    --email "person@example.com" \
    --subject "Custom report" \
    --output /tmp/custom.html'
```
