"""
Server-side comprehensive engagement report for the MoSPI MCP server.

Runs ON the Jaeger host (no SSH needed). Reads:
  - ~/observability/archive/traces.jsonl   (trace archive)
  - ~/syslog.jsonl                          (CPU/RAM samples, optional)

Produces a single HTML report covering:
  - Tool calls across all clients (Claude, ChatGPT/OpenAI, scripts, etc.)
    identified from the user_agent header.
  - Distribution of tool calls (list_datasets / get_indicators /
    get_metadata / get_data) overall and per client.
  - Dataset usage, overall and per client.
  - CPU utilisation from syslog (line chart, hour-of-day, peak spikes).
  - Activity over time (hour, weekday, daily).
  - Recent tool-call timeline.

CLI:
    python3 server_report.py                                  # since continuous start
    python3 server_report.py --since 7
    python3 server_report.py --start 2026-05-10 --end 2026-05-19
    python3 server_report.py --output /tmp/report.html --no-syslog
"""
import argparse
import html
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ_IST = timezone(timedelta(hours=5, minutes=30))

# Queries that are automated probes, not user engagement.
EXCLUDED_QUERIES = {"health check", "healthcheck", "ping", "test"}

INK         = "#18181b"
SUBTLE      = "#71717a"
MUTED       = "#a1a1aa"
LINE        = "#e7e5e4"
ACCENT      = "#1e3a8a"
ACCENT_SOFT = "#dbeafe"

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
                   help="Last N days from today (IST), inclusive.")
    p.add_argument("--output",  default=None,
                   help="HTML output path. Default: ./mospi-full-report-<stamp>.html")
    p.add_argument("--no-syslog", action="store_true", help="Skip the CPU section.")
    p.add_argument("--top",     type=int, default=12, help="Cap for top-N lists.")
    return p.parse_args()


def resolve_window(args):
    today = datetime.now(TZ_IST).date()
    if args.since:
        return today - timedelta(days=args.since - 1), today
    start = date.fromisoformat(args.start) if args.start else None
    end   = date.fromisoformat(args.end)   if args.end   else today
    return start, end


# ---------------------------------------------------------------------------
# Client classification (header-based)
# ---------------------------------------------------------------------------

# Order matters: more specific first.
CLIENT_RULES = [
    ("Claude Code CLI",   re.compile(r"claude-code", re.I)),
    ("Claude",            re.compile(r"claude", re.I)),
    ("ChatGPT (OpenAI)",  re.compile(r"openai", re.I)),
    ("Gemini (Google)",   re.compile(r"gemini|google", re.I)),
    ("Python script",     re.compile(r"python-(?:requests|httpx)|python/", re.I)),
    ("Node script",       re.compile(r"node-fetch|^node\b", re.I)),
    ("curl",              re.compile(r"^curl/", re.I)),
    ("Browser / generic", re.compile(r"mozilla", re.I)),
]

def classify_client(ua: str) -> str:
    if not ua or ua == "-":
        return "Unknown (no UA)"
    for label, pat in CLIENT_RULES:
        if pat.search(ua):
            return label
    return "Other"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

STEP_NAMES = ["list_datasets", "get_indicators", "get_metadata", "get_data"]


def parse_traces(path: Path, start_date, end_date):
    """Stream traces.jsonl, return a list of tool-call dicts in window.

    Each tool call is one tool.* span. We attribute it to its trace's
    user_agent (taken from any http.user_agent / user_agent.original tag
    seen in the trace's spans).
    """
    if start_date:
        start_us = int(datetime.combine(start_date, datetime.min.time(), TZ_IST).timestamp() * 1_000_000)
    else:
        start_us = 0
    end_us = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), TZ_IST).timestamp() * 1_000_000)

    calls = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:    t = json.loads(line)
            except Exception: continue

            # Trace-level: find a user_agent string anywhere in the trace.
            ua = None
            for s in t.get("spans", []):
                for tag in s.get("tags", []):
                    k = tag["key"]
                    if k in ("http.user_agent", "user_agent.original"):
                        ua = ua or str(tag.get("value", "") or "")
                        if ua: break
                if ua: break

            for s in t.get("spans", []):
                op = s.get("operationName", "")
                if not op.startswith("tool."):
                    continue
                ts = s["startTime"]
                if not (start_us <= ts < end_us):
                    continue
                tags = {tg["key"]: tg["value"] for tg in s.get("tags", [])}
                tool_name = tags.get("tool.name") or op.replace("tool.", "")
                try:    inp = json.loads(tags.get("tool.input", "{}"))
                except Exception: inp = {}
                uq = (inp.get("user_query") or "").strip()
                dataset = str(inp.get("dataset") or "").upper()
                is_probe = uq.lower() in EXCLUDED_QUERIES
                calls.append({
                    "ts":         datetime.fromtimestamp(ts/1_000_000, tz=timezone.utc),
                    "tool":       tool_name,
                    "dataset":    dataset,
                    "user_query": uq,
                    "ua":         ua or "",
                    "client":     classify_client(ua),
                    "is_probe":   is_probe,
                    "trace_id":   t.get("traceID", ""),
                })
    calls.sort(key=lambda c: c["ts"])
    return calls


def parse_syslog(path: Path, start_date, end_date):
    """Returns list of {ts, cpu, ram_used_gi, ram_free_gi, ram_total_gi}."""
    if not path.exists():
        return []

    def parse_gi(s):
        if not s: return None
        m = re.match(r"^\s*([\d.]+)\s*([KMG])i?\s*$", str(s))
        if not m: return None
        v, u = float(m.group(1)), m.group(2)
        return {"K": v/1024/1024, "M": v/1024, "G": v}[u]

    if start_date:
        start_us = int(datetime.combine(start_date, datetime.min.time(), TZ_IST).timestamp() * 1_000_000)
    else:
        start_us = 0
    end_us = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), TZ_IST).timestamp() * 1_000_000)

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
    # Bucket real (non-probe) calls separately
    real = [c for c in calls if not c["is_probe"]]

    by_client_tool = Counter()           # (client, tool) -> n
    by_client      = Counter()
    by_tool        = Counter()
    by_dataset     = Counter()
    by_client_ds   = Counter()           # (client, dataset)
    by_tool_ds     = Counter()           # (tool, dataset)
    distinct_ua_per_client = defaultdict(set)
    by_hour_ist    = Counter()
    by_dow_ist     = Counter()
    by_day         = Counter()
    queries_per_client = defaultdict(Counter)

    for c in real:
        client = c["client"]
        tool   = c["tool"]
        ds     = c["dataset"]
        by_client_tool[(client, tool)] += 1
        by_client[client] += 1
        by_tool[tool]     += 1
        if ds: by_dataset[ds] += 1
        if ds: by_client_ds[(client, ds)] += 1
        if ds: by_tool_ds[(tool, ds)] += 1
        if c["ua"]: distinct_ua_per_client[client].add(c["ua"])
        if c["user_query"]:
            queries_per_client[client][c["user_query"]] += 1

        d_ist = c["ts"].astimezone(TZ_IST)
        by_hour_ist[d_ist.hour]              += 1
        by_dow_ist[d_ist.strftime("%a")]     += 1
        by_day[d_ist.strftime("%Y-%m-%d")]   += 1

    return {
        "n_total":          len(calls),
        "n_real":           len(real),
        "n_probes":         len(calls) - len(real),
        "by_client_tool":   by_client_tool,
        "by_client":        by_client,
        "by_tool":          by_tool,
        "by_dataset":       by_dataset,
        "by_client_ds":     by_client_ds,
        "by_tool_ds":       by_tool_ds,
        "distinct_ua":      distinct_ua_per_client,
        "by_hour_ist":      [by_hour_ist.get(h, 0) for h in range(24)],
        "by_dow_ist":       by_dow_ist,
        "by_day":           by_day,
        "queries_per_client": queries_per_client,
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
# Inline SVG charts
# ---------------------------------------------------------------------------

def esc(s): return html.escape(str(s))
def fmt_int(n): return f"{int(n):,}"


def bar_chart_h(items, width=760, bar_h=22, gap=8, label_w=240, value_w=70):
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
            f'font-family="Inter, sans-serif" font-size="13" fill="{INK}">{esc(lab)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{bw:.1f}" height="{bar_h}" fill="{ACCENT}" rx="2"/>'
            f'<text x="{label_w + bw + 6:.1f}" y="{y+bar_h*0.7:.1f}" '
            f'font-family="Inter, sans-serif" font-size="12" fill="{SUBTLE}">{fmt_int(v)}</text>'
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
                     f'font-family="Inter, sans-serif" font-size="11" fill="{SUBTLE}">{esc(lab)}</text>')
    grid = []
    for ratio in (0, 0.5, 1.0):
        v = vmax_nice * ratio
        y = pad_t + plot_h - ratio * plot_h
        grid.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y:.1f}" y2="{y:.1f}" stroke="{LINE}" stroke-width="1"/>')
        grid.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" text-anchor="end" '
                    f'font-family="Inter, sans-serif" font-size="11" fill="{SUBTLE}">{fmt_int(v)}</text>')
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


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_html(calls, agg, syslog_samples, start_date, end_date, top_n):
    real_calls = [c for c in calls if not c["is_probe"]]
    if not real_calls:
        return f"<!doctype html><html><body><p>No tool calls in {start_date} to {end_date}.</p></body></html>"

    actual_start = real_calls[0]["ts"].astimezone(TZ_IST).date()
    actual_end   = real_calls[-1]["ts"].astimezone(TZ_IST).date()
    window_start = start_date or actual_start
    window_end   = end_date

    period_str = f"{window_start.strftime('%d %B %Y')} to {window_end.strftime('%d %B %Y')}"
    generated  = datetime.now(TZ_IST).strftime("%d %B %Y, %H:%M IST")

    # Charts: clients, tools, datasets
    client_chart  = bar_chart_h(agg["by_client"].most_common(top_n), label_w=200)
    tool_order = STEP_NAMES + sorted(set(agg["by_tool"]) - set(STEP_NAMES))
    tool_chart    = bar_chart_h([(t, agg["by_tool"].get(t, 0)) for t in tool_order
                                  if agg["by_tool"].get(t, 0) > 0], label_w=200)
    dataset_chart = bar_chart_h(agg["by_dataset"].most_common(top_n), label_w=110)

    # Per-client x per-tool table (matrix)
    clients_ranked = [c for c, _ in agg["by_client"].most_common()]
    matrix_html = ""
    if clients_ranked:
        header = "<tr><th>Client</th>" + "".join(f"<th>{esc(t)}</th>" for t in tool_order) + "<th>Total</th></tr>"
        rows = []
        for cl in clients_ranked:
            row = [f"<td>{esc(cl)}</td>"]
            for t in tool_order:
                v = agg["by_client_tool"].get((cl, t), 0)
                row.append(f'<td class="num">{fmt_int(v) if v else "&middot;"}</td>')
            row.append(f'<td class="num"><strong>{fmt_int(agg["by_client"][cl])}</strong></td>')
            rows.append("<tr>" + "".join(row) + "</tr>")
        matrix_html = (
            '<table class="data"><thead>' + header + '</thead><tbody>'
            + "".join(rows) + '</tbody></table>'
        )

    # Distinct UAs per client (so the reader can see raw strings)
    ua_table_html = ""
    rows = []
    for cl in clients_ranked:
        uas = sorted(agg["distinct_ua"].get(cl, []))
        if not uas: continue
        rows.append(f'<tr><td>{esc(cl)}</td><td>{esc(", ".join(uas[:6]))}'
                    + (f' <span class="meta">+{len(uas)-6} more</span>' if len(uas) > 6 else '')
                    + f'</td></tr>')
    if rows:
        ua_table_html = (
            '<table class="data"><thead><tr><th>Client bucket</th>'
            '<th>Raw user_agent strings</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table>'
        )

    # Datasets per client (top 3 datasets for each top client)
    ds_per_client_rows = []
    for cl in clients_ranked[:8]:
        per = Counter({ds: agg["by_client_ds"][(cl, ds)] for ds in agg["by_dataset"]
                       if (cl, ds) in agg["by_client_ds"]})
        if not per: continue
        top_ds = ", ".join(f"{ds} ({n})" for ds, n in per.most_common(5))
        ds_per_client_rows.append(f'<tr><td>{esc(cl)}</td><td>{esc(top_ds)}</td></tr>')
    ds_per_client_html = ""
    if ds_per_client_rows:
        ds_per_client_html = (
            '<table class="data"><thead><tr><th>Client</th>'
            '<th>Top datasets (count)</th></tr></thead><tbody>'
            + "".join(ds_per_client_rows) + '</tbody></table>'
        )

    # Time charts
    hour_chart  = column_chart(agg["by_hour_ist"], [f"{h:02d}" for h in range(24)], show_x_every=2)
    dow_order   = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    dow_chart   = column_chart([agg["by_dow_ist"].get(d, 0) for d in dow_order], dow_order)

    # Daily volume
    if agg["by_day"]:
        cur = window_start; daily_labs, daily_vals = [], []
        while cur <= window_end:
            daily_labs.append(cur.strftime("%m-%d"))
            daily_vals.append(agg["by_day"].get(cur.strftime("%Y-%m-%d"), 0))
            cur += timedelta(days=1)
        daily_chart = column_chart(daily_vals, daily_labs, show_x_every=max(1, len(daily_labs)//12))
    else:
        daily_chart = '<div class="chart-empty">No data.</div>'

    # CPU section (optional)
    cpu_section_html = ""
    if syslog_samples:
        cc = aggregate_cpu(syslog_samples)
        cpu_line  = polyline_chart([(s["ts"], s["cpu"]) for s in syslog_samples], y_max=100)
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
                f'<td>{s["ram_used_gi"]:.2f} GiB</td></tr>'
                for s in cc["spikes"]
            )
            spike_html = (
                '<h3>Top CPU spikes (&gt;= 50%)</h3>'
                '<table class="data"><thead>'
                '<tr><th>When (IST)</th><th>CPU</th><th>RAM used</th></tr>'
                f'</thead><tbody>{sr}</tbody></table>'
            )
        free_vals = [s["ram_free_gi"] for s in syslog_samples if s["ram_free_gi"] is not None]
        ram_note = ""
        if free_vals:
            tot = next((s["ram_total_gi"] for s in syslog_samples if s["ram_total_gi"]), None)
            mu  = max(s["ram_used_gi"] for s in syslog_samples if s["ram_used_gi"] is not None)
            ram_note = (
                f'<p class="meta" style="margin-top:14px">'
                f'RAM: lowest free <strong>{min(free_vals):.2f} GiB</strong> of '
                f'{tot:.1f} GiB; max used <strong>{mu:.2f} GiB</strong>.'
                f'</p>'
            )
        cpu_section_html = f'''
  <h2>5 &middot; MCP server CPU usage</h2>
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

    # Recent calls timeline
    recent = list(reversed(real_calls[-30:]))
    recent_rows = "".join(
        f'<tr><td>{c["ts"].astimezone(TZ_IST).strftime("%d %b %H:%M:%S")}</td>'
        f'<td>{esc(c["client"])}</td>'
        f'<td><span class="op">{esc(c["tool"])}</span></td>'
        f'<td>{esc(c["dataset"] or "-")}</td>'
        f'<td class="q">{esc((c["user_query"] or "")[:80])}</td></tr>'
        for c in recent
    )
    recent_html = (
        '<table class="data"><thead>'
        '<tr><th>When (IST)</th><th>Client</th><th>Tool</th>'
        '<th>Dataset</th><th>Query</th></tr></thead>'
        f'<tbody>{recent_rows}</tbody></table>'
    )

    # Probe note
    probe_note = ""
    if agg["n_probes"]:
        probe_note = (f'<p class="meta" style="margin-top:6px">'
                      f'{fmt_int(agg["n_probes"])} automated health-check calls excluded from these counts.'
                      f'</p>')

    cpu_section_num = 5 if syslog_samples else None
    next_sec        = 6 if cpu_section_num else 5

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
    --bg: #fbfaf7; --paper: #ffffff; --accent: {ACCENT}; --accent-soft: {ACCENT_SOFT};
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ background: var(--bg); color: var(--ink); margin: 0; padding: 0;
                font-family: 'Inter', system-ui, sans-serif; line-height: 1.55; -webkit-font-smoothing: antialiased; }}
  .page {{ max-width: 940px; margin: 0 auto; padding: 64px 64px 80px; background: var(--paper); box-shadow: 0 1px 0 var(--line); }}
  h1, h2, h3, h4 {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; color: var(--ink); margin: 0; letter-spacing: -0.01em; }}
  h1 {{ font-size: 38px; line-height: 1.1; margin-bottom: 10px; }}
  h2 {{ font-size: 26px; margin-top: 56px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }}
  h3 {{ font-size: 19px; margin-top: 28px; margin-bottom: 10px; }}
  p, li {{ font-size: 15px; color: var(--ink); }}
  .meta {{ color: var(--subtle); font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; margin: 0; }}

  .hero {{ margin: 32px 0 24px; padding: 28px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
  .hero-value {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; font-size: 70px; line-height: 1; letter-spacing: -0.02em; }}
  .hero-label {{ font-size: 14px; color: var(--subtle); margin-top: 10px; }}

  .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; margin: 0 0 22px; border-bottom: 1px solid var(--line); }}
  .kpi {{ padding: 18px 14px 20px 0; border-right: 1px solid var(--line); }}
  .kpi:last-child {{ border-right: 0; padding-left: 14px; }}
  .kpi-value {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; font-size: 26px; line-height: 1.1; }}
  .kpi-label {{ font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--subtle); margin-top: 6px; }}
  .kpi-sub  {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}

  .chart {{ width: 100%; height: auto; max-width: 820px; }}
  .chart-empty {{ font-size: 13px; color: var(--subtle); padding: 14px 0; }}

  table.data {{ width: 100%; border-collapse: collapse; margin: 12px 0 8px; font-size: 14px; }}
  table.data th, table.data td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }}
  table.data th {{ font-weight: 500; color: var(--subtle); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }}
  table.data td.num {{ font-variant-numeric: tabular-nums; text-align: right; width: 1%; white-space: nowrap; color: var(--ink); }}
  table.data td.q {{ font-family: 'EB Garamond', Georgia, serif; font-size: 15px; color: var(--ink); }}
  .op {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--accent); }}

  .footer {{ margin-top: 56px; padding-top: 16px; border-top: 1px solid var(--line); font-size: 11px; color: var(--muted); letter-spacing: 0.04em; }}
</style>
</head>
<body>
<div class="page">

  <header>
    <h1>MoSPI MCP - Full Engagement Report</h1>
    <p class="meta">{esc(period_str)}  &middot;  Generated {esc(generated)}</p>
  </header>

  <div class="hero">
    <div class="hero-value">{fmt_int(agg["n_real"])}</div>
    <div class="hero-label">tool calls in this window (excluding automated health checks)</div>
    {probe_note}
  </div>

  <div class="kpis">
    {kpi("Distinct clients", fmt_int(len(agg["by_client"])))}
    {kpi("Datasets queried", fmt_int(len(agg["by_dataset"])))}
    {kpi("Active days", fmt_int(len(agg["by_day"])))}
    {kpi("Period", f"{(window_end - window_start).days + 1} days")}
  </div>

  <h2>1 &middot; Clients</h2>
  <p>Tool calls grouped by client, derived from the <span class="op">http.user_agent</span> /
     <span class="op">user_agent.original</span> tags on each trace.</p>
  {client_chart}

  <h3>What each bucket looked like (raw user_agent strings)</h3>
  {ua_table_html}

  <h2>2 &middot; Tool calls</h2>
  <p>Distribution across the four MCP tools.</p>
  {tool_chart}

  <h3>Per-client breakdown</h3>
  {matrix_html}

  <h2>3 &middot; Dataset usage</h2>
  <p>Datasets touched across all real tool calls.</p>
  {dataset_chart}

  <h3>Top datasets per client</h3>
  {ds_per_client_html}

  <h2>4 &middot; Activity over time</h2>

  <h3>By hour of day (IST)</h3>
  {hour_chart}

  <h3>By day of week</h3>
  {dow_chart}

  <h3>Daily volume</h3>
  {daily_chart}

  {cpu_section_html}

  <h2>{next_sec} &middot; Recent tool calls</h2>
  {recent_html}

  <div class="footer">
    {esc(period_str)} &middot; Generated {esc(generated)}
  </div>
</div>
</body>
</html>'''


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def default_output_path():
    stamp = datetime.now(TZ_IST).strftime("%Y%m%d-%H%M")
    return Path.cwd() / f"mospi-full-report-{stamp}.html"


def main():
    args = parse_args()
    start_date, end_date = resolve_window(args)
    if start_date is None:
        start_date = (end_date - timedelta(days=7))   # last 7 days by default

    traces_path = Path(os.path.expanduser(args.traces))
    syslog_path = Path(os.path.expanduser(args.syslog))

    if not traces_path.exists():
        sys.exit(f"ERROR: traces archive not found: {traces_path}")

    print(f"[load] {traces_path}", file=sys.stderr)
    calls = parse_traces(traces_path, start_date, end_date)
    print(f"[load] {len(calls):,} tool calls in window "
          f"({start_date} to {end_date})", file=sys.stderr)

    agg = aggregate(calls)
    print(f"[aggregate] {agg['n_real']:,} real, {agg['n_probes']:,} probes, "
          f"{len(agg['by_client'])} clients", file=sys.stderr)

    syslog_samples = []
    if not args.no_syslog:
        syslog_samples = parse_syslog(syslog_path, start_date, end_date)
        print(f"[syslog] {len(syslog_samples)} samples in window", file=sys.stderr)

    html_doc = render_html(calls, agg, syslog_samples, start_date, end_date, args.top)

    out_path = Path(os.path.expanduser(args.output)) if args.output else default_output_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc)
    print(f"[write] {out_path}  ({out_path.stat().st_size:,} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
