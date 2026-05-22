"""
Server-side comprehensive engagement report for the MoSPI MCP server.

Runs ON the Jaeger host (no SSH needed). Reads:
  - ~/observability/archive/traces.jsonl  (trace archive)
  - ~/syslog.jsonl                         (CPU/RAM samples, optional)

Produces one HTML report covering:
  - Per-client x per-tool call matrix (the main view)
  - CPU utilisation from syslog
  - Tool call distribution
  - Dataset usage (overall + per client, with mini bars)
  - Top user queries (cross-client)
  - Activity over time (hour, weekday, daily)
  - Reliability and latency (errors, p50/p95/p99, total bytes served)

Can also email the report to a recipient list via SMTP - suitable for a
weekly cron job.

Examples
--------
    python3 server_report.py
    python3 server_report.py --since 7
    python3 server_report.py --start 2026-05-10 --end 2026-05-19
    python3 server_report.py --since 7 \\
        --email alice@example.com,bob@example.com \\
        --smtp-host smtp.example.com --smtp-port 587 \\
        --smtp-user noreply@example.com
"""
import argparse
import csv
import html
import json
import math
import os
import re
import shutil
import smtplib
import socket
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path

TZ_IST = timezone(timedelta(hours=5, minutes=30))

EXCLUDED_QUERIES = {"health check", "healthcheck", "ping", "test"}

# Datasets known to be legitimate MoSPI sources (kept in a fixed list so
# we can distinguish them from accidental junk values like "M4BK").
KNOWN_DATASETS = {
    "PLFS","CPI","IIP","ASI","NAS","WPI","ENERGY","AISHE","ASUSE","GENDER",
    "NFHS","ENVSTATS","RBI","NSS77","NSS78","NSS79","NSS80","CPIALRL",
    "HCES","TUS","EC","UDISE","MNRE",
}

INK         = "#18181b"
SUBTLE      = "#71717a"
MUTED       = "#a1a1aa"
LINE        = "#e7e5e4"
ACCENT      = "#1e3a8a"
ACCENT_SOFT = "#dbeafe"
WARN        = "#9f1239"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--traces",  default="~/observability/archive/traces.jsonl",
                   help="Path to traces.jsonl on this host.")
    p.add_argument("--syslog",  default="~/syslog.jsonl",
                   help="Path to syslog.jsonl (CPU/RAM samples).")
    p.add_argument("--start",   help="Start date YYYY-MM-DD (IST, inclusive).")
    p.add_argument("--end",     help="End date YYYY-MM-DD (IST, inclusive).")
    p.add_argument("--since",   type=int, metavar="N",
                   help="Last N days from today (IST), inclusive. Default 7.")
    p.add_argument("--output",  default=None,
                   help="HTML output path. Default: ./mospi-full-report-<stamp>.html")
    p.add_argument("--no-syslog", action="store_true", help="Skip the CPU section.")
    p.add_argument("--no-pdf",  action="store_true",
                   help="Skip PDF rendering; email the HTML instead.")
    p.add_argument("--top",     type=int, default=12, help="Cap for top-N lists.")
    # email
    p.add_argument("--email",   default="",
                   help="Comma-separated recipient list. If empty, no email is sent.")
    p.add_argument("--smtp-host", default=os.environ.get("SMTP_HOST", "localhost"))
    p.add_argument("--smtp-port", type=int, default=int(os.environ.get("SMTP_PORT", "25")))
    p.add_argument("--smtp-user", default=os.environ.get("SMTP_USER", ""))
    p.add_argument("--smtp-pass", default=os.environ.get("SMTP_PASS", ""))
    p.add_argument("--from-addr", default=os.environ.get(
                       "FROM_ADDR", f"mospi-mcp@{socket.gethostname()}"))
    p.add_argument("--subject",  default=None,
                   help="Email subject (default uses date range).")
    return p.parse_args()


def resolve_window(args):
    today = datetime.now(TZ_IST).date()
    if args.since:
        return today - timedelta(days=args.since - 1), today
    if args.start:
        start = date.fromisoformat(args.start)
        end   = date.fromisoformat(args.end) if args.end else today
        return start, end
    # default: last 7 days
    return today - timedelta(days=6), today


# ---------------------------------------------------------------------------
# Client classification
# ---------------------------------------------------------------------------

CLIENT_RULES = [
    ("Claude Code CLI",   re.compile(r"claude-code", re.I)),
    ("Claude",            re.compile(r"claude", re.I)),
    ("ChatGPT (OpenAI)",  re.compile(r"openai", re.I)),
    ("Gemini (Google)",   re.compile(r"gemini|google", re.I)),
    ("Python script",     re.compile(r"python-(?:requests|httpx)|python/", re.I)),
    ("Node script",       re.compile(r"node-fetch|^node\b", re.I)),
    ("curl",              re.compile(r"^curl/", re.I)),
    ("VS Code",           re.compile(r"Visual Studio Code", re.I)),
    ("Browser / generic", re.compile(r"mozilla", re.I)),
]

def classify_client(ua):
    if not ua or ua == "-": return "Unknown (no UA)"
    for label, pat in CLIENT_RULES:
        if pat.search(ua):
            return label
    return "Other"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

STEP_NAMES = ["list_datasets", "get_indicators", "get_metadata", "get_data"]


def parse_traces(path, start_date, end_date):
    """Stream traces.jsonl and emit one dict per tool span in the window."""
    start_us = int(datetime.combine(start_date, datetime.min.time(), TZ_IST).timestamp() * 1_000_000)
    end_us   = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), TZ_IST).timestamp() * 1_000_000)

    calls = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:    t = json.loads(line)
            except Exception: continue

            # The real client UA is http.user_agent on the inbound server span
            # for POST /mcp. Do NOT read user_agent.original: that is the MCP
            # server's OWN outbound UA when it fetches from api.mospi.gov.in
            # (it sends "Mozilla/5.0"), which would mis-attribute the trace.
            ua = ""
            for s in t.get("spans", []):
                stags = {tg["key"]: tg.get("value") for tg in s.get("tags", [])}
                if stags.get("span.kind") == "server" and stags.get("http.route") == "/mcp":
                    v = stags.get("http.user_agent")
                    if v:
                        ua = str(v)
                        break
            if not ua:
                # fallback: any http.user_agent tag (never user_agent.original)
                for s in t.get("spans", []):
                    for tag in s.get("tags", []):
                        if tag["key"] == "http.user_agent":
                            v = str(tag.get("value", "") or "")
                            if v:
                                ua = v
                                break
                    if ua: break

            for s in t.get("spans", []):
                op = s.get("operationName", "")
                if not op.startswith("tool."): continue
                ts = s["startTime"]
                if not (start_us <= ts < end_us): continue
                tags = {tg["key"]: tg.get("value") for tg in s.get("tags", [])}
                tool_name = tags.get("tool.name") or op.replace("tool.", "")
                try:    inp = json.loads(tags.get("tool.input", "{}"))
                except Exception: inp = {}
                uq = (inp.get("user_query") or "").strip()
                dataset = str(inp.get("dataset") or "").upper()
                is_probe = uq.lower() in EXCLUDED_QUERIES
                try:    out_size = int(tags.get("tool.output_size") or 0)
                except (TypeError, ValueError): out_size = 0
                calls.append({
                    "ts":          datetime.fromtimestamp(ts/1_000_000, tz=timezone.utc),
                    "tool":        tool_name,
                    "dataset":     dataset,
                    "user_query":  uq,
                    "ua":          ua,
                    "client":      classify_client(ua),
                    "is_probe":    is_probe,
                    "duration_ms": s.get("duration", 0) / 1000.0,
                    "out_bytes":   out_size,
                    "error":       tags.get("otel.status_code") == "ERROR",
                    "error_desc":  tags.get("otel.status_description", "") or "",
                })
    calls.sort(key=lambda c: c["ts"])
    return calls


def parse_syslog(path, start_date, end_date):
    if not path.exists(): return []
    def parse_gi(s):
        if not s: return None
        m = re.match(r"^\s*([\d.]+)\s*([KMG])i?\s*$", str(s))
        if not m: return None
        v, u = float(m.group(1)), m.group(2)
        return {"K": v/1024/1024, "M": v/1024, "G": v}[u]
    start_us = int(datetime.combine(start_date, datetime.min.time(), TZ_IST).timestamp() * 1_000_000)
    end_us   = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), TZ_IST).timestamp() * 1_000_000)
    leading = re.compile(r"(:\s*)\.(\d)")
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line: continue
            fixed = leading.sub(r"\g<1>0.\2", line)
            try:
                r = json.loads(fixed)
                dt = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
                ts_us = int(dt.timestamp() * 1_000_000)
                if not (start_us <= ts_us < end_us): continue
                out.append({
                    "ts":           dt,
                    "cpu":          float(r["cpu_used_pct"]),
                    "ram_used_gi":  parse_gi(r.get("ram_used")),
                    "ram_free_gi":  parse_gi(r.get("ram_free")),
                    "ram_total_gi": parse_gi(r.get("ram_total")),
                })
            except Exception:
                continue
    out.sort(key=lambda x: x["ts"])
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(calls):
    # All calls counted, no probe exclusion.
    real = calls
    if not real:
        return {"n_real": 0, "n_probes": 0}

    by_client_tool = Counter()
    by_client      = Counter()
    by_tool        = Counter()
    by_dataset     = Counter()
    by_client_ds   = Counter()
    by_tool_ds     = Counter()
    by_hour_ist    = Counter()
    by_dow_ist     = Counter()
    by_day         = Counter()
    junk_datasets  = Counter()
    queries_total  = Counter()
    queries_per_client = defaultdict(Counter)

    errors_by_tool   = Counter()
    errors_by_client = Counter()
    durations_ms     = []
    durations_per_tool = defaultdict(list)
    total_bytes      = 0
    bytes_per_tool   = Counter()
    bytes_per_client = Counter()

    for c in real:
        cl, tool, ds = c["client"], c["tool"], c["dataset"]
        by_client_tool[(cl, tool)] += 1
        by_client[cl] += 1
        by_tool[tool] += 1
        if ds:
            if ds in KNOWN_DATASETS:
                by_dataset[ds] += 1
                by_client_ds[(cl, ds)] += 1
                by_tool_ds[(tool, ds)] += 1
            else:
                junk_datasets[ds] += 1
        if c["user_query"]:
            queries_total[c["user_query"]] += 1
            queries_per_client[cl][c["user_query"]] += 1

        d_ist = c["ts"].astimezone(TZ_IST)
        by_hour_ist[d_ist.hour]           += 1
        by_dow_ist[d_ist.strftime("%a")]  += 1
        by_day[d_ist.strftime("%Y-%m-%d")] += 1

        if c["error"]:
            errors_by_tool[tool] += 1
            errors_by_client[cl] += 1
        durations_ms.append(c["duration_ms"])
        durations_per_tool[tool].append(c["duration_ms"])
        total_bytes      += c["out_bytes"]
        bytes_per_tool[tool] += c["out_bytes"]
        bytes_per_client[cl] += c["out_bytes"]

    def quantile(arr, q):
        if not arr: return 0
        a = sorted(arr)
        return a[int(q * (len(a) - 1))]

    latency_per_tool = {}
    for t, arr in durations_per_tool.items():
        if not arr: continue
        latency_per_tool[t] = {
            "n":   len(arr),
            "p50": quantile(arr, 0.5),
            "p95": quantile(arr, 0.95),
            "p99": quantile(arr, 0.99),
            "max": max(arr),
        }

    return {
        "n_total":          len(calls),
        "n_real":           len(real),
        "n_probes":         len(calls) - len(real),
        "n_errors":         sum(errors_by_tool.values()),
        "n_with_query":     sum(1 for c in real if c["user_query"]),
        "by_client_tool":   by_client_tool,
        "by_client":        by_client,
        "by_tool":          by_tool,
        "by_dataset":       by_dataset,
        "by_client_ds":     by_client_ds,
        "by_tool_ds":       by_tool_ds,
        "junk_datasets":    junk_datasets,
        "queries_total":    queries_total,
        "queries_per_client": queries_per_client,
        "by_hour_ist":      [by_hour_ist.get(h, 0) for h in range(24)],
        "by_dow_ist":       by_dow_ist,
        "by_day":           by_day,
        "errors_by_tool":   errors_by_tool,
        "errors_by_client": errors_by_client,
        "latency_overall":  {
            "p50": quantile(durations_ms, 0.5),
            "p95": quantile(durations_ms, 0.95),
            "p99": quantile(durations_ms, 0.99),
            "max": max(durations_ms) if durations_ms else 0,
        },
        "latency_per_tool": latency_per_tool,
        "total_bytes":      total_bytes,
        "bytes_per_tool":   bytes_per_tool,
        "bytes_per_client": bytes_per_client,
    }


def aggregate_cpu(samples):
    if not samples: return {}
    cpus = sorted(s["cpu"] for s in samples)
    n = len(cpus)
    peak = max(samples, key=lambda s: s["cpu"])
    spikes = sorted([s for s in samples if s["cpu"] >= 50], key=lambda s: -s["cpu"])[:5]
    return {
        "n":         n,
        "min":       cpus[0],
        "max":       cpus[-1],
        "mean":      sum(cpus) / n,
        "median":    cpus[n//2],
        "p95":       cpus[int(0.95 * (n-1))],
        "peak_time": peak["ts"],
        "spikes":    spikes,
    }


# ---------------------------------------------------------------------------
# Inline SVG
# ---------------------------------------------------------------------------

def esc(s): return html.escape(str(s))
def fmt_int(n): return f"{int(n):,}"
def fmt_bytes(b):
    if b >= 1024*1024*1024: return f"{b/1024/1024/1024:.2f} GiB"
    if b >= 1024*1024:      return f"{b/1024/1024:.2f} MiB"
    if b >= 1024:           return f"{b/1024:.2f} KiB"
    return f"{int(b)} B"


def bar_chart_h(items, width=760, bar_h=22, gap=8, label_w=240, value_w=70, accent=ACCENT):
    if not items: return '<div class="chart-empty">No data.</div>'
    n = len(items)
    chart_h = n * (bar_h + gap) + 12
    plot_w  = width - label_w - value_w - 12
    vmax    = max(v for _, v in items) or 1
    parts = []
    for i, (lab, v) in enumerate(items):
        y = 6 + i * (bar_h + gap)
        bw = (v / vmax) * plot_w
        parts.append(
            f'<text x="{label_w-10}" y="{y+bar_h*0.7:.1f}" text-anchor="end" '
            f'font-family="Inter,sans-serif" font-size="13" fill="{INK}">{esc(lab)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{bw:.1f}" height="{bar_h}" fill="{accent}" rx="2"/>'
            f'<text x="{label_w + bw + 6:.1f}" y="{y+bar_h*0.7:.1f}" '
            f'font-family="Inter,sans-serif" font-size="12" fill="{SUBTLE}">{fmt_int(v)}</text>'
        )
    return f'<svg viewBox="0 0 {width} {chart_h}" xmlns="http://www.w3.org/2000/svg" class="chart">{"".join(parts)}</svg>'


def column_chart(values, labels, width=760, height=240, show_x_every=1):
    if not values: return '<div class="chart-empty">No data.</div>'
    pad_l, pad_r, pad_t, pad_b = 44, 16, 14, 38
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(values)
    vmax = max(values) or 1

    def nice(v):
        if v <= 0: return 1
        exp = 10 ** math.floor(math.log10(v))
        for k in (1, 2, 5, 10):
            if k * exp >= v: return k * exp
        return v
    vmax_nice = nice(vmax)
    bar_w = plot_w / n * 0.7
    gap   = plot_w / n * 0.3
    bars, xlabs = [], []
    for i, v in enumerate(values):
        h = (v / vmax_nice) * plot_h if vmax_nice else 0
        x = pad_l + i * (plot_w / n) + gap/2
        y = pad_t + plot_h - h
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="{ACCENT}" rx="1">'
                    f'<title>{esc(labels[i])}: {v}</title></rect>')
    for i, lab in enumerate(labels):
        if i % show_x_every: continue
        x = pad_l + i * (plot_w / n) + (plot_w / n) / 2
        xlabs.append(f'<text x="{x:.1f}" y="{height - pad_b + 16}" text-anchor="middle" '
                     f'font-family="Inter,sans-serif" font-size="11" fill="{SUBTLE}">{esc(lab)}</text>')
    grid = []
    for ratio in (0, 0.5, 1.0):
        v = vmax_nice * ratio
        y = pad_t + plot_h - ratio * plot_h
        grid.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y:.1f}" y2="{y:.1f}" stroke="{LINE}" stroke-width="1"/>')
        grid.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" text-anchor="end" '
                    f'font-family="Inter,sans-serif" font-size="11" fill="{SUBTLE}">{fmt_int(v)}</text>')
    return f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="chart">{"".join(grid)}{"".join(bars)}{"".join(xlabs)}</svg>'


def polyline_chart(points, width=760, height=240, y_max=None):
    if not points: return '<div class="chart-empty">No data.</div>'
    pad_l, pad_r, pad_t, pad_b = 44, 16, 14, 38
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    times  = [p[0] for p in points]
    values = [p[1] for p in points]
    t_min, t_max = min(times), max(times)
    t_range = max((t_max - t_min).total_seconds(), 1.0)
    if y_max is None:
        raw = max(values) if values else 1
        def nice(v):
            if v <= 0: return 1
            exp = 10 ** math.floor(math.log10(v))
            for k in (1, 2, 5, 10):
                if k * exp >= v: return k * exp
            return v
        y_max = max(nice(raw), 1)
    def xpos(t): return pad_l + (t - t_min).total_seconds() / t_range * plot_w
    def ypos(v): return pad_t + plot_h - (v / y_max if y_max else 0) * plot_h
    pts = " ".join(f"{xpos(t):.1f},{ypos(v):.1f}" for t, v in points)
    area_pts = f"{pad_l:.1f},{pad_t+plot_h:.1f} {pts} {pad_l + plot_w:.1f},{pad_t+plot_h:.1f}"
    grid = []
    for ratio in (0, 0.5, 1.0):
        v = y_max * ratio
        y = pad_t + plot_h - ratio * plot_h
        grid.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y:.1f}" y2="{y:.1f}" stroke="{LINE}" stroke-width="1"/>')
        grid.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" text-anchor="end" '
                    f'font-family="Inter,sans-serif" font-size="11" fill="{SUBTLE}">{v:.0f}%</text>')
    xlabs = [
        f'<text x="{pad_l:.0f}" y="{height-pad_b+16}" text-anchor="start" '
        f'font-family="Inter,sans-serif" font-size="11" fill="{SUBTLE}">{esc(t_min.astimezone(TZ_IST).strftime("%d %b %H:%M"))}</text>',
        f'<text x="{(width-pad_r):.0f}" y="{height-pad_b+16}" text-anchor="end" '
        f'font-family="Inter,sans-serif" font-size="11" fill="{SUBTLE}">{esc(t_max.astimezone(TZ_IST).strftime("%d %b %H:%M"))}</text>',
    ]
    return (f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="chart">'
            + "".join(grid)
            + f'<polygon points="{area_pts}" fill="{ACCENT_SOFT}" opacity="0.5"/>'
            + f'<polyline points="{pts}" stroke="{ACCENT}" stroke-width="1.5" fill="none"/>'
            + "".join(xlabs)
            + '</svg>')


def kpi(label, value, sub=""):
    return f'''<div class="kpi"><div class="kpi-value">{value}</div>
      <div class="kpi-label">{label}</div>
      {f'<div class="kpi-sub">{sub}</div>' if sub else ''}</div>'''


def mini_bar(value, vmax, color=ACCENT):
    """Tiny inline horizontal bar for use inside table cells."""
    if vmax <= 0: return ""
    pct = max(2, min(100, int(value / vmax * 100)))
    return (f'<span style="display:inline-block;width:{pct}%;height:6px;'
            f'background:{color};border-radius:2px;vertical-align:middle"></span>')


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_html(calls, agg, syslog_samples, start_date, end_date, top_n):
    if agg.get("n_real", 0) == 0:
        period = f"{start_date} to {end_date}"
        return (f"<!doctype html><html><body><p>No tool calls in "
                f"{esc(period)} (excluding probes).</p></body></html>")

    period_str = f"{start_date.strftime('%d %B %Y')} to {end_date.strftime('%d %B %Y')}"
    generated  = datetime.now(TZ_IST).strftime("%d %B %Y, %H:%M IST")

    # --- per-client x per-tool matrix (Section 1 - the main view) ----------
    # Sort: Claude family first, then ChatGPT, then everyone else by call count.
    PRIORITY = ["Claude", "Claude Code CLI", "ChatGPT (OpenAI)", "Gemini (Google)"]
    HIGHLIGHTED = set(PRIORITY)
    others = sorted(
        (c for c in agg["by_client"] if c not in HIGHLIGHTED),
        key=lambda c: -agg["by_client"][c],
    )
    clients_ranked = [c for c in PRIORITY if c in agg["by_client"]] + others

    tool_order = STEP_NAMES + sorted(set(agg["by_tool"]) - set(STEP_NAMES))
    tool_order = [t for t in tool_order if agg["by_tool"].get(t, 0) > 0]

    head_cells = "".join(f'<th>{esc(t)}</th>' for t in tool_order) + '<th>Total</th>'
    body_rows = []
    grand_total = sum(agg["by_client"].values())
    for cl in clients_ranked:
        row_class = ' class="hi"' if cl in HIGHLIGHTED else ''
        cells = [f'<td><strong>{esc(cl)}</strong></td>']
        for t in tool_order:
            v = agg["by_client_tool"].get((cl, t), 0)
            cells.append(f'<td class="num">{fmt_int(v) if v else "&middot;"}</td>')
        total = agg["by_client"][cl]
        share_pct = total/grand_total*100 if grand_total else 0
        cells.append(f'<td class="num"><strong>{fmt_int(total)}</strong> '
                     f'<span class="meta">{share_pct:.1f}%</span></td>')
        body_rows.append(f'<tr{row_class}>' + ''.join(cells) + '</tr>')
    matrix_html = (
        '<table class="data wide"><thead>'
        f'<tr><th>Client</th>{head_cells}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table>'
    )

    # --- tool-name overall distribution (small) ----------------------------
    tool_chart = bar_chart_h(
        [(t, agg["by_tool"].get(t, 0)) for t in tool_order], label_w=160)

    # --- dataset usage (verified) ------------------------------------------
    dataset_chart = bar_chart_h(agg["by_dataset"].most_common(), label_w=110)
    junk_note = ""  # silently exclude junk dataset names; no commentary

    # Per-client dataset detail with inline mini-bars
    ds_per_client_rows = []
    for cl in clients_ranked:
        per = Counter({ds: agg["by_client_ds"][(cl, ds)] for ds in agg["by_dataset"]
                       if (cl, ds) in agg["by_client_ds"]})
        if not per: continue
        top = per.most_common(6)
        vmax = top[0][1] if top else 1
        chips = "".join(
            f'<div class="ds-chip">'
            f'<span class="ds-name">{esc(ds)}</span>'
            f'<span class="ds-bar">{mini_bar(n, vmax)}</span>'
            f'<span class="ds-num">{fmt_int(n)}</span>'
            f'</div>' for ds, n in top
        )
        ds_per_client_rows.append(f'<tr><td><strong>{esc(cl)}</strong></td>'
                                  f'<td><div class="ds-grid">{chips}</div></td></tr>')
    ds_per_client_html = (
        '<table class="data"><thead><tr><th>Client</th><th>Top datasets</th></tr></thead>'
        f'<tbody>{"".join(ds_per_client_rows)}</tbody></table>'
    )

    # --- top queries: longest 50, deduped, probes filtered out of this list --
    queries_html = ""
    candidates = [
        (q, n) for q, n in agg["queries_total"].items()
        if q.strip().lower() not in EXCLUDED_QUERIES
    ]
    candidates.sort(key=lambda kv: -len(kv[0]))   # longest first
    top_queries = candidates[:50]

    if top_queries:
        rows = []
        for q, n in top_queries:
            clients_for_q = []
            for cl, qs in agg["queries_per_client"].items():
                if q in qs:
                    clients_for_q.append(f"{cl} ({qs[q]})")
            rows.append(
                f'<tr><td class="q">{esc(q)}</td>'
                f'<td class="meta">{esc(", ".join(clients_for_q))}</td></tr>'
            )
        queries_html = (
            '<table class="data"><thead>'
            '<tr><th>Query (longest first)</th><th>Asked by</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )
    else:
        queries_html = '<p class="meta">No natural-language queries in this window.</p>'

    # --- time charts -------------------------------------------------------
    hour_chart  = column_chart(agg["by_hour_ist"], [f"{h:02d}" for h in range(24)], show_x_every=2)
    dow_order   = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    dow_chart   = column_chart([agg["by_dow_ist"].get(d, 0) for d in dow_order], dow_order)
    if agg["by_day"]:
        cur = start_date; daily_labs, daily_vals = [], []
        while cur <= end_date:
            daily_labs.append(cur.strftime("%m-%d"))
            daily_vals.append(agg["by_day"].get(cur.strftime("%Y-%m-%d"), 0))
            cur += timedelta(days=1)
        daily_chart = column_chart(daily_vals, daily_labs, show_x_every=max(1, len(daily_labs)//12))
    else:
        daily_chart = '<div class="chart-empty">No data.</div>'

    # Section numbering - CPU goes at the END. Dataset usage section removed.
    _s = 1
    sec_matrix    = _s; _s += 1
    sec_tooldist  = _s; _s += 1
    sec_queries   = _s; _s += 1
    sec_time      = _s; _s += 1
    sec_reliab    = _s; _s += 1
    sec_cpu       = _s if syslog_samples else None

    # --- CPU section (only if samples) ------------------------------------
    cpu_section_html = ""
    if syslog_samples:
        cc = aggregate_cpu(syslog_samples)
        cpu_line = polyline_chart([(s["ts"], s["cpu"]) for s in syslog_samples], y_max=100)
        per_hr = defaultdict(list)
        for s in syslog_samples:
            per_hr[s["ts"].astimezone(TZ_IST).hour].append(s["cpu"])
        hr_means = [(sum(per_hr[h])/len(per_hr[h]) if per_hr.get(h) else 0) for h in range(24)]
        cpu_hour_chart = column_chart(hr_means, [f"{h:02d}" for h in range(24)], show_x_every=2)
        peak_when = cc["peak_time"].astimezone(TZ_IST).strftime("%d %b %H:%M")

        spike_html = ""
        if cc["spikes"]:
            sr = "".join(
                f'<tr><td>{s["ts"].astimezone(TZ_IST).strftime("%d %b %H:%M:%S")}</td>'
                f'<td class="num">{s["cpu"]:.1f}%</td>'
                f'<td>{(s["ram_used_gi"] or 0):.2f} GiB</td></tr>'
                for s in cc["spikes"]
            )
            spike_html = ('<h3>Top CPU spikes (&gt;= 50%)</h3>'
                          '<table class="data"><thead>'
                          '<tr><th>When (IST)</th><th>CPU</th><th>RAM used</th></tr>'
                          f'</thead><tbody>{sr}</tbody></table>')

        free_vals = [s["ram_free_gi"] for s in syslog_samples if s["ram_free_gi"] is not None]
        ram_note = ""
        if free_vals:
            tot = next((s["ram_total_gi"] for s in syslog_samples if s["ram_total_gi"]), None)
            mu  = max(s["ram_used_gi"] for s in syslog_samples if s["ram_used_gi"] is not None)
            ram_note = (f'<p class="meta" style="margin-top:14px">'
                        f'RAM: lowest free <strong>{min(free_vals):.2f} GiB</strong> of '
                        f'{tot:.1f} GiB; max used <strong>{mu:.2f} GiB</strong>.</p>')

        cpu_section_html = f'''
  <h2>{{cpu_sn}} &middot; MCP server CPU usage</h2>
  <div class="kpis">
    {kpi("Median CPU", f"{cc['median']:.1f}%")}
    {kpi("Mean CPU",   f"{cc['mean']:.1f}%")}
    {kpi("Peak",       f"{cc['max']:.1f}%", esc(peak_when))}
    {kpi("Samples",    fmt_int(cc['n']))}
  </div>
  <h3>CPU% over time</h3>
  {cpu_line}
  <h3>Mean CPU by hour of day (IST)</h3>
  {cpu_hour_chart}
  {spike_html}
  {ram_note}
'''
        cpu_section_html = cpu_section_html.format(cpu_sn=sec_cpu)

    # --- reliability + latency --------------------------------------------
    lat = agg["latency_overall"]
    lat_rows = []
    for t in tool_order:
        if t not in agg["latency_per_tool"]: continue
        L = agg["latency_per_tool"][t]
        err_n = agg["errors_by_tool"].get(t, 0)
        n = L["n"]
        err_rate = (err_n/n*100) if n else 0
        lat_rows.append(
            f'<tr><td><span class="op">{esc(t)}</span></td>'
            f'<td class="num">{fmt_int(n)}</td>'
            f'<td class="num">{L["p50"]:.0f} ms</td>'
            f'<td class="num">{L["p95"]:.0f} ms</td>'
            f'<td class="num">{L["p99"]:.0f} ms</td>'
            f'<td class="num">{L["max"]:.0f} ms</td>'
            f'<td class="num">{err_n} <span class="meta">({err_rate:.2f}%)</span></td>'
            f'<td class="num">{fmt_bytes(agg["bytes_per_tool"].get(t, 0))}</td></tr>'
        )
    lat_table = (
        '<table class="data wide"><thead>'
        '<tr><th>Tool</th><th>Calls</th><th>p50</th><th>p95</th><th>p99</th>'
        '<th>max</th><th>Errors</th><th>Bytes returned</th></tr></thead>'
        f'<tbody>{"".join(lat_rows)}</tbody></table>'
    )

    probe_note = ""  # probes are no longer excluded


    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MoSPI MCP - Full Engagement Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=EB+Garamond:wght@400;500;600;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: {INK}; --subtle: {SUBTLE}; --muted: {MUTED}; --line: {LINE};
    --bg: #fbfaf7; --paper: #ffffff; --accent: {ACCENT}; --accent-soft: {ACCENT_SOFT}; --warn: {WARN};
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ background: var(--bg); color: var(--ink); margin: 0; padding: 0;
                font-family: 'Inter', system-ui, sans-serif; line-height: 1.55; -webkit-font-smoothing: antialiased; }}
  .page {{ max-width: 960px; margin: 0 auto; padding: 56px 56px 80px; background: var(--paper); box-shadow: 0 1px 0 var(--line); }}
  h1, h2, h3, h4 {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; color: var(--ink); margin: 0; letter-spacing: -0.01em; }}
  h1 {{ font-size: 36px; line-height: 1.1; margin-bottom: 8px; }}
  .period {{ font-family: 'EB Garamond', Georgia, serif; font-size: 22px; color: var(--ink);
             font-weight: 500; margin: 0 0 4px; }}
  h2 {{ font-size: 24px; margin-top: 52px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }}
  h3 {{ font-size: 18px; margin-top: 24px; margin-bottom: 10px; }}
  p, li {{ font-size: 15px; color: var(--ink); }}
  .meta {{ color: var(--subtle); font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; margin: 0; }}

  .hero {{ margin: 30px 0 22px; padding: 24px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
  .hero-value {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; font-size: 64px; line-height: 1; letter-spacing: -0.02em; }}
  .hero-label {{ font-size: 14px; color: var(--subtle); margin-top: 8px; }}

  .kpis {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 0; margin: 0 0 20px; border-bottom: 1px solid var(--line); }}
  .kpi {{ padding: 16px 12px 18px 0; border-right: 1px solid var(--line); }}
  .kpi:last-child {{ border-right: 0; padding-left: 12px; }}
  .kpi-value {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; font-size: 24px; line-height: 1.1; }}
  .kpi-label {{ font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--subtle); margin-top: 5px; }}
  .kpi-sub  {{ font-size: 11px; color: var(--muted); margin-top: 3px; }}

  .chart {{ width: 100%; height: auto; max-width: 880px; }}
  .chart-empty {{ font-size: 13px; color: var(--subtle); padding: 14px 0; }}

  table.data {{ width: 100%; border-collapse: collapse; margin: 12px 0 8px; font-size: 14px; }}
  table.data th, table.data td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }}
  table.data th {{ font-weight: 500; color: var(--subtle); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }}
  table.data td.num {{ font-variant-numeric: tabular-nums; text-align: right; width: 1%; white-space: nowrap; color: var(--ink); }}
  table.data td.q {{ font-family: 'EB Garamond', Georgia, serif; font-size: 16px; color: var(--ink); }}
  table.data.wide {{ font-size: 13px; }}
  table.data.wide td {{ padding: 8px 8px; }}
  table.data tr.hi td {{ background: var(--accent-soft); }}
  table.data tr.hi td:first-child strong {{ color: var(--accent); }}
  .op {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--accent); }}

  .ds-grid {{ display: grid; grid-template-columns: minmax(0,1fr); row-gap: 4px; }}
  .ds-chip {{ display: grid; grid-template-columns: 80px 1fr 48px; align-items: center; gap: 8px; font-size: 13px; }}
  .ds-name {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--ink); }}
  .ds-bar  {{ display: block; height: 6px; }}
  .ds-num  {{ font-variant-numeric: tabular-nums; text-align: right; color: var(--subtle); font-size: 12px; }}

  .footer {{ margin-top: 56px; padding-top: 16px; border-top: 1px solid var(--line); font-size: 11px; color: var(--muted); letter-spacing: 0.04em; }}
</style>
</head>
<body>
<div class="page">

  <header>
    <h1>MoSPI MCP - Full Engagement Report</h1>
    <p class="period">{esc(period_str)}</p>
    <p class="meta">Generated {esc(generated)}</p>
  </header>

  <div class="hero">
    <div class="hero-value">{fmt_int(agg["n_real"])}</div>
    <div class="hero-label">tool calls between {esc(start_date.strftime("%d %b %Y"))} and {esc(end_date.strftime("%d %b %Y"))}</div>
  </div>

  <h2>{sec_matrix} &middot; Tool calls by client</h2>
  {matrix_html}

  <h2>{sec_tooldist} &middot; Tool distribution overall</h2>
  {tool_chart}

  <h2>{sec_queries} &middot; Top user queries</h2>
  {queries_html}

  <h2>{sec_time} &middot; Activity over time</h2>
  <h3>By hour of day (IST)</h3>
  {hour_chart}
  <h3>By day of week</h3>
  {dow_chart}
  <h3>Daily volume</h3>
  {daily_chart}

  <h2>{sec_reliab} &middot; Reliability and latency</h2>
  {lat_table}

  {cpu_section_html}

  <div class="footer">
    {esc(period_str)} &middot; Generated {esc(generated)}
  </div>
</div>
</body>
</html>'''


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def html_to_pdf(html_path, pdf_path):
    """Render an HTML file to PDF. Tries headless Chromium, then wkhtmltopdf.
    Returns pdf_path on success, None if no renderer is available / it fails."""
    html_abs = os.path.abspath(html_path)
    pdf_abs  = os.path.abspath(pdf_path)

    # 1. Headless Chromium / Chrome
    candidates = ["chromium", "chromium-browser", "google-chrome",
                  "google-chrome-stable", "/snap/bin/chromium"]
    for name in candidates:
        exe = name if (os.path.isabs(name) and os.path.exists(name)) else shutil.which(name)
        if not exe:
            continue
        for headless in ("--headless=new", "--headless"):
            try:
                r = subprocess.run(
                    [exe, headless, "--disable-gpu", "--no-sandbox",
                     "--no-pdf-header-footer", "--virtual-time-budget=10000",
                     f"--print-to-pdf={pdf_abs}", f"file://{html_abs}"],
                    capture_output=True, timeout=120,
                )
                if r.returncode == 0 and os.path.exists(pdf_abs) and os.path.getsize(pdf_abs) > 0:
                    print(f"[pdf] rendered via {exe} {headless}", file=sys.stderr)
                    return pdf_path
            except Exception:
                continue

    # 2. wkhtmltopdf fallback
    wk = shutil.which("wkhtmltopdf")
    if wk:
        try:
            r = subprocess.run(
                [wk, "--quiet", "--enable-local-file-access", html_abs, pdf_abs],
                capture_output=True, timeout=120,
            )
            if r.returncode == 0 and os.path.exists(pdf_abs) and os.path.getsize(pdf_abs) > 0:
                print(f"[pdf] rendered via wkhtmltopdf", file=sys.stderr)
                return pdf_path
        except Exception:
            pass

    print("[pdf] no working HTML-to-PDF renderer found", file=sys.stderr)
    return None


def send_email(args, body, body_subtype, subject, attachment_paths):
    """body_subtype: 'plain' or 'html'.
    attachment_paths: list of (path, subtype) tuples."""
    recipients = [r.strip() for r in args.email.split(",") if r.strip()]
    if not recipients:
        return False
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = args.from_addr
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(body, body_subtype, "utf-8"))
    for path, subtype in attachment_paths or []:
        if not path: continue
        with open(path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype=subtype)
        att.add_header("Content-Disposition", "attachment",
                       filename=Path(path).name)
        msg.attach(att)
    try:
        with smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=30) as s:
            s.ehlo()
            if args.smtp_port == 587:
                s.starttls(); s.ehlo()
            if args.smtp_user:
                s.login(args.smtp_user, args.smtp_pass)
            s.sendmail(args.from_addr, recipients, msg.as_string())
        print(f"[email] sent to {len(recipients)} recipient(s) via "
              f"{args.smtp_host}:{args.smtp_port}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[email] FAILED: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def default_output_path():
    stamp = datetime.now(TZ_IST).strftime("%Y%m%d-%H%M")
    return Path.cwd() / f"mospi-full-report-{stamp}.html"


def main():
    args = parse_args()
    start_date, end_date = resolve_window(args)

    traces_path = Path(os.path.expanduser(args.traces))
    syslog_path = Path(os.path.expanduser(args.syslog))

    if not traces_path.exists():
        sys.exit(f"ERROR: traces archive not found: {traces_path}")

    print(f"[load] {traces_path}", file=sys.stderr)
    calls = parse_traces(traces_path, start_date, end_date)
    print(f"[load] {len(calls):,} tool calls in window "
          f"({start_date} to {end_date})", file=sys.stderr)

    agg = aggregate(calls)
    print(f"[aggregate] {agg.get('n_real',0):,} real, "
          f"{agg.get('n_probes',0):,} probes, "
          f"{len(agg.get('by_client',{}))} clients, "
          f"{len(agg.get('by_dataset',{}))} datasets",
          file=sys.stderr)

    syslog_samples = []
    if not args.no_syslog:
        syslog_samples = parse_syslog(syslog_path, start_date, end_date)
        print(f"[syslog] {len(syslog_samples)} samples in window", file=sys.stderr)

    html_doc = render_html(calls, agg, syslog_samples, start_date, end_date, args.top)

    out_path = Path(os.path.expanduser(args.output)) if args.output else default_output_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc)
    print(f"[write] {out_path}  ({out_path.stat().st_size:,} bytes)", file=sys.stderr)

    # CSV companion: just the natural-language queries, no automated probes
    csv_path = out_path.with_suffix(".csv")
    n_query_rows = 0
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_ist", "client", "dataset", "user_query"])
        for c in calls:
            if not c["user_query"]:
                continue
            if c["user_query"].strip().lower() in EXCLUDED_QUERIES:
                continue
            w.writerow([
                c["ts"].astimezone(TZ_IST).strftime("%Y-%m-%d %H:%M:%S"),
                c["client"], c["dataset"], c["user_query"],
            ])
            n_query_rows += 1
    print(f"[write] {csv_path}  ({csv_path.stat().st_size:,} bytes, {n_query_rows} queries)", file=sys.stderr)

    # Render a PDF of the report (unless disabled)
    pdf_path = None
    if not args.no_pdf:
        pdf_path = html_to_pdf(out_path, out_path.with_suffix(".pdf"))
        if pdf_path:
            print(f"[write] {pdf_path}  ({Path(pdf_path).stat().st_size:,} bytes)", file=sys.stderr)

    if args.email:
        subject = args.subject or (
            f"MoSPI MCP weekly report - {start_date} to {end_date}"
        )
        if pdf_path:
            # PDF mode: plain-text body, PDF + CSV attached
            body = (
                f"MoSPI MCP weekly engagement report for {start_date} to {end_date}.\n\n"
                f"Attached:\n"
                f"  - {Path(pdf_path).name}  (the report)\n"
                f"  - {csv_path.name}  (all user queries in the window)\n\n"
                f"This is an automated email.\n"
            )
            send_email(args, body, "plain", subject,
                       [(pdf_path, "pdf"), (csv_path, "csv")])
        else:
            # Fallback: no PDF renderer available, send HTML as before
            print("[email] PDF unavailable, falling back to HTML body + attachment",
                  file=sys.stderr)
            send_email(args, html_doc, "html", subject,
                       [(out_path, "html"), (csv_path, "csv")])


if __name__ == "__main__":
    main()
