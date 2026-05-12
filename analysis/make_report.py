"""
Generate a rich HTML engagement report locally, pulling the trace data
for the requested window from the Jaeger server over SSH.

Examples:
    python3 make_report.py                                   # all time
    python3 make_report.py --since 7                         # last 7 days
    python3 make_report.py --start 2026-05-01
    python3 make_report.py --start 2026-05-01 --end 2026-05-10
    python3 make_report.py --since 30 --output ~/Desktop/mospi.html
    python3 make_report.py --since 7 --no-open               # don't auto-launch browser

Defaults:
    --host    jaeger  (ssh alias to the Jaeger server)
    --remote  ~/observability/archive/traces.jsonl  (server-side path)
    --output  ~/Desktop/mospi-report-<YYYY-MM-DD-HHMM>.html

The script SSHes to the server, filters traces by date window there, streams
matching JSONL lines back, renders inline-SVG HTML locally, and (on macOS)
opens the result in the default browser.
"""
import argparse
import html
import json
import math
import os
import re
import shlex
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ_IST = timezone(timedelta(hours=5, minutes=30))
EXCLUDED_QUERIES = {"health check", "healthcheck", "ping", "test"}

INK    = "#18181b"
SUBTLE = "#71717a"
MUTED  = "#a1a1aa"
LINE   = "#e7e5e4"
ACCENT = "#1e3a8a"
ACCENT_SOFT = "#dbeafe"

# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host",   default="jaeger",
                   help="SSH alias to the Jaeger server (default: jaeger).")
    p.add_argument("--remote", default="~/observability/archive/traces.jsonl",
                   help="Path to traces.jsonl on the server.")
    p.add_argument("--start",  help="Start date YYYY-MM-DD (IST, inclusive).")
    p.add_argument("--end",    help="End date YYYY-MM-DD (IST, inclusive).")
    p.add_argument("--since",  type=int, metavar="N",
                   help="Last N days from today (IST), inclusive.")
    p.add_argument("--output", default=None,
                   help="HTML output path. Default: ~/Desktop/mospi-report-<stamp>.html.")
    p.add_argument("--no-open", action="store_true",
                   help="Don't auto-open the result in a browser (macOS).")
    p.add_argument("--top",    type=int, default=12,
                   help="Cap for top-N lists (default 12).")
    return p.parse_args()


def resolve_window(args):
    today = datetime.now(TZ_IST).date()
    if args.since:
        return today - timedelta(days=args.since - 1), today
    start = date.fromisoformat(args.start) if args.start else None
    end   = date.fromisoformat(args.end)   if args.end   else today
    return start, end


def default_output_path():
    stamp = datetime.now(TZ_IST).strftime("%Y%m%d-%H%M")
    return Path.home() / "Desktop" / f"mospi-report-{stamp}.html"


# ----------------------------------------------------------------------------
# Fetch from server
# ----------------------------------------------------------------------------

REMOTE_FILTER = r"""
import json, os, sys
S, E, P = {start_us}, {end_us}, os.path.expanduser({path!r})
with open(P) as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try: t = json.loads(line)
        except Exception: continue
        for s in t.get("spans", []):
            if s.get("operationName", "").startswith("tool."):
                ts = s.get("startTime", 0)
                if S <= ts < E:
                    print(line); break
"""

REMOTE_ACTIVE_DATES = r"""
import json, os
from datetime import datetime, timezone, timedelta
TZ = timezone(timedelta(hours=5, minutes=30))
P = os.path.expanduser({path!r})
EXCLUDED = {{"health check", "healthcheck", "ping", "test"}}
days = set()
with open(P) as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try: t = json.loads(line)
        except Exception: continue
        # Find earliest tool span timestamp AND check if this trace has a real query
        earliest = None
        has_real_query = False
        for s in t.get("spans", []):
            op = s.get("operationName", "")
            if not op.startswith("tool."): continue
            ts = s.get("startTime", 0)
            if earliest is None or ts < earliest: earliest = ts
            tags = {{tg["key"]: tg["value"] for tg in s.get("tags", [])}}
            try: inp = json.loads(tags.get("tool.input","{{}}"))
            except Exception: inp = {{}}
            uq = (inp.get("user_query") or "").strip()
            if uq and uq.lower() not in EXCLUDED:
                has_real_query = True
        if has_real_query and earliest is not None:
            days.add(datetime.fromtimestamp(earliest/1e6, tz=TZ).date().isoformat())
for d in sorted(days):
    print(d)
"""

def fetch_window(host, remote_path, start_us, end_us):
    """SSH to host, run a date-filter on the remote archive, return JSONL lines."""
    script = REMOTE_FILTER.format(start_us=start_us, end_us=end_us, path=remote_path)
    cmd = ["ssh", host, f"python3 -c {shlex.quote(script)}"]
    print(f"[fetch] ssh {host} (window)...", file=sys.stderr)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(f"ssh fetch failed (exit {r.returncode})")
    print(f"[fetch] received {len(r.stdout):,} bytes", file=sys.stderr)
    return r.stdout


def fetch_active_dates(host, remote_path):
    """Cheap pre-pass: return sorted list of dates that had any real (non-probe) query."""
    script = REMOTE_ACTIVE_DATES.format(path=remote_path)
    cmd = ["ssh", host, f"python3 -c {shlex.quote(script)}"]
    print(f"[fetch] ssh {host} (active dates)...", file=sys.stderr)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(f"ssh active-dates failed (exit {r.returncode})")
    dates = [date.fromisoformat(s.strip()) for s in r.stdout.splitlines() if s.strip()]
    print(f"[fetch] {len(dates)} active dates", file=sys.stderr)
    return dates


def detect_continuous_start(active_dates, min_gap_days=7):
    """Returns (start_date, gap_size_days). gap_size=0 when data is contiguous."""
    if not active_dates:
        return None, 0
    active = sorted(active_dates)
    largest_gap = 0
    after_largest = active[0]
    for i in range(1, len(active)):
        gap = (active[i] - active[i-1]).days - 1
        if gap > largest_gap:
            largest_gap = gap
            after_largest = active[i]
    if largest_gap >= min_gap_days:
        return after_largest, largest_gap
    return active[0], 0


# ----------------------------------------------------------------------------
# Parse / extract
# ----------------------------------------------------------------------------

STEP_ORDER = ["list_datasets", "get_indicators", "get_metadata", "get_data"]


def step_num(op_name):
    name = op_name.replace("tool.", "")
    return STEP_ORDER.index(name) + 1 if name in STEP_ORDER else 99


THEME_PATTERNS = [
    ("Employment & Labour",
     r"\b(employ|labour|labor|workforce|unemploy|wages|workers?|job|plfs|payroll|epfo)\b"),
    ("Education",
     r"\b(education|literacy|school|college|university|enrol|aishe|teacher|student|udise)\b"),
    ("Gender & Women",
     r"\b(gender|women|female|girls?|maternal)\b"),
    ("Health & Demographics",
     r"\b(health|mortality|morbidity|nfhs|fertility|birth|disease|nutrition|infant)\b"),
    ("Prices & Inflation",
     r"\b(cpi|wpi|price|inflation|cost of living|wholesale)\b"),
    ("Income, Consumption & Poverty",
     r"\b(income|consumption|poverty|hces|expenditure|household|standard of living)\b"),
    ("Industry, Manufacturing & IIP",
     r"\b(industry|asi|manufactur|iip|production|factories|sector performance|steel)\b"),
    ("Banking, Finance & RBI",
     r"\b(bank|rbi|credit|money|finance|loan|deposit|demat|debt|exchange rate)\b"),
    ("National Accounts (NAS / GDP)",
     r"\b(nas|gdp|gva|national accounts|national income|gnp|economic growth)\b"),
    ("Environment & Energy",
     r"\b(environment|envstats?|energy|emission|forest|water|pollution|coastline|temperature|rainfall|climate|renewable)\b"),
    ("Time Use & Voluntary Work",
     r"\b(time use|tus|volunt|unpaid|caregiv)\b"),
    ("Digital Access & Connectivity",
     r"\b(mobile|smartphone|internet|digital|connectivity)\b"),
    ("Housing, Sanitation & Basic Amenities",
     r"\b(hous|sanitat|drinking water|piped water|toilet|electricity|cooking fuel)\b"),
]


def classify_themes(q):
    if not q: return []
    qq = q.lower()
    return [theme for theme, pat in THEME_PATTERNS if re.search(pat, qq)]


def detect_script(text):
    if not text: return "Unknown"
    counts = Counter()
    for ch in text:
        cp = ord(ch)
        if 0x0041 <= cp <= 0x007A or 0x00C0 <= cp <= 0x024F: counts["English (Latin)"] += 1
        elif 0x0980 <= cp <= 0x09FF: counts["Bengali"] += 1
        elif 0x0900 <= cp <= 0x097F: counts["Devanagari"] += 1
        elif 0x0B80 <= cp <= 0x0BFF: counts["Tamil"] += 1
        elif 0x0C00 <= cp <= 0x0C7F: counts["Telugu"] += 1
        elif ch.isalpha(): counts["Other"] += 1
    if not counts: return "Unknown"
    return counts.most_common(1)[0][0]


def normalise_query(q):
    q = (q or "").strip()
    q = re.sub(r"\s+", " ", q)
    if q and q[0].isalpha() and q[0].islower():
        q = q[0].upper() + q[1:]
    return q


def load_sessions(jsonl_text):
    """Build session list from JSONL text (one trace per line)."""
    sessions = []
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line: continue
        try: t = json.loads(line)
        except Exception: continue
        tool_spans = [s for s in t.get("spans", []) if s["operationName"].startswith("tool.")]
        if not tool_spans: continue
        tool_spans.sort(key=lambda s: s["startTime"])

        steps, user_query, dataset, filters = [], "", "", {}
        for s in tool_spans:
            tags = {tg["key"]: tg["value"] for tg in s["tags"]}
            try:    inp = json.loads(tags.get("tool.input","{}"))
            except: inp = {}
            steps.append({
                "step": step_num(s["operationName"]),
                "input": inp,
                "output_size": int(tags.get("tool.output_size", 0) or 0),
                "duration_us": s["duration"],
                "start_us":    s["startTime"],
                "error":       tags.get("otel.status_code") == "ERROR",
            })
            if inp.get("user_query"): user_query = inp["user_query"]
            if inp.get("dataset"):    dataset = str(inp["dataset"]).upper()
            if isinstance(inp.get("filters"), dict): filters = inp["filters"]

        uq_norm = (user_query or "").strip().lower()
        if not uq_norm or uq_norm in EXCLUDED_QUERIES:
            continue

        start_dt = datetime.fromtimestamp(min(s["start_us"] for s in steps)/1_000_000, tz=timezone.utc)
        sessions.append({
            "trace_id":   t["traceID"],
            "start_dt":   start_dt,
            "user_query": user_query.strip(),
            "user_query_norm": normalise_query(user_query),
            "dataset":    dataset,
            "filters":    filters,
            "n_steps":    len(steps),
            "max_step":   max((s["step"] for s in steps), default=0),
            "had_error":  any(s["error"] for s in steps),
        })
    sessions.sort(key=lambda s: s["start_dt"])
    return sessions


def aggregate(sessions, start_date, end_date):
    by_day      = Counter()
    by_hour_ist = Counter()
    by_dow_ist  = Counter()
    datasets    = Counter()
    themes      = Counter()
    scripts     = Counter()
    repeated    = Counter()
    samples_per_theme = defaultdict(list)
    total_tool_calls = 0

    for s in sessions:
        d_ist = s["start_dt"].astimezone(TZ_IST)
        by_day[d_ist.strftime("%Y-%m-%d")] += 1
        by_hour_ist[d_ist.hour] += 1
        by_dow_ist[d_ist.strftime("%a")] += 1
        total_tool_calls += s["n_steps"]

        if s["dataset"]: datasets[s["dataset"]] += 1

        scripts[detect_script(s["user_query"])] += 1
        for th in classify_themes(s["user_query"]):
            themes[th] += 1
            nice = s["user_query_norm"]
            if nice not in samples_per_theme[th] and len(samples_per_theme[th]) < 6:
                samples_per_theme[th].append(nice)
        repeated[s["user_query"].strip().lower()] += 1

    # Peak day (objective: highest count) - neutral framing, no judgement
    peak = max(by_day.items(), key=lambda kv: kv[1]) if by_day else (None, 0)

    # Detect the largest no-activity gap; if material, treat the day after as
    # the start of "continuous" capture
    sorted_active = sorted(by_day.keys())
    continuous_since = None
    largest_gap_days = 0
    if len(sorted_active) >= 2:
        biggest_gap_end = sorted_active[0]
        for i in range(1, len(sorted_active)):
            prev = date.fromisoformat(sorted_active[i-1])
            cur  = date.fromisoformat(sorted_active[i])
            gap_days = (cur - prev).days - 1
            if gap_days > largest_gap_days:
                largest_gap_days = gap_days
                biggest_gap_end = sorted_active[i]
        if largest_gap_days >= 7:
            continuous_since = biggest_gap_end

    return {
        "n_queries":          len(sessions),
        "n_tool_calls":       total_tool_calls,
        "n_datasets":         len(datasets),
        "n_days_in_window":   (end_date - start_date).days + 1 if start_date else len(by_day),
        "first_active":       sorted_active[0] if sorted_active else None,
        "last_active":        sorted_active[-1] if sorted_active else None,
        "continuous_since":   continuous_since,
        "largest_gap_days":   largest_gap_days,
        "peak_day":           peak,
        "by_day":             by_day,
        "by_hour_ist":        [by_hour_ist.get(h, 0) for h in range(24)],
        "by_dow_ist":         by_dow_ist,
        "datasets":           datasets,
        "themes":             themes,
        "scripts":            scripts,
        "samples_per_theme":  dict(samples_per_theme),
        "repeated":           repeated,
    }


# ----------------------------------------------------------------------------
# Charts (inline SVG)
# ----------------------------------------------------------------------------

def esc(s): return html.escape(str(s))
def fmt_int(n): return f"{int(n):,}"


def bar_chart_h(items, width=760, bar_h=22, gap=8, label_w=240, value_w=64):
    if not items:
        return '<div class="chart-empty">No data.</div>'
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


def column_chart(values, labels, width=760, height=260, show_x_every=1):
    if not values:
        return '<div class="chart-empty">No data.</div>'
    pad_l, pad_r, pad_t, pad_b = 44, 16, 14, 42
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
        xlabs.append(f'<text x="{x:.1f}" y="{height - pad_b + 18}" text-anchor="middle" '
                     f'font-family="Inter, sans-serif" font-size="11" fill="{SUBTLE}">{esc(lab)}</text>')
    grid = []
    for ratio in (0, 0.5, 1.0):
        v = vmax_nice * ratio
        y = pad_t + plot_h - ratio * plot_h
        grid.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y:.1f}" y2="{y:.1f}" stroke="{LINE}" stroke-width="1"/>')
        grid.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" text-anchor="end" '
                    f'font-family="Inter, sans-serif" font-size="11" fill="{SUBTLE}">{fmt_int(v)}</text>')
    return f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="chart">{"".join(grid)}{"".join(bars)}{"".join(xlabs)}</svg>'


# ----------------------------------------------------------------------------
# Render
# ----------------------------------------------------------------------------

def kpi(label, value, sub=""):
    return f'''<div class="kpi"><div class="kpi-value">{value}</div>
      <div class="kpi-label">{label}</div>
      {f'<div class="kpi-sub">{sub}</div>' if sub else ''}</div>'''


def render_html(sessions, agg, start_date, end_date, top_n):
    if not sessions:
        period = f"{start_date or 'all time'} to {end_date}"
        return f"<!doctype html><html><body><p>No real user queries in {esc(period)}.</p></body></html>"

    actual_start = sessions[0]["start_dt"].astimezone(TZ_IST).date()
    actual_end   = sessions[-1]["start_dt"].astimezone(TZ_IST).date()
    window_start = start_date or actual_start
    window_end   = end_date

    # Daily series with calendar gap-filling
    cur = window_start; daily_labs, daily_vals = [], []
    while cur <= window_end:
        daily_labs.append(cur.strftime("%m-%d"))
        daily_vals.append(agg["by_day"].get(cur.strftime("%Y-%m-%d"), 0))
        cur += timedelta(days=1)

    hour_chart    = column_chart(agg["by_hour_ist"], [f"{h:02d}" for h in range(24)], show_x_every=2)
    dow_order     = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    dow_chart     = column_chart([agg["by_dow_ist"].get(d, 0) for d in dow_order], dow_order)
    daily_chart   = column_chart(daily_vals, daily_labs, show_x_every=max(1, len(daily_labs)//12))
    theme_chart   = bar_chart_h(agg["themes"].most_common(top_n))
    dataset_chart = bar_chart_h(agg["datasets"].most_common(top_n), label_w=110)

    # Common questions table
    common_top = [(q, n) for q, n in agg["repeated"].most_common(20) if n > 1][:top_n]
    common_html = ""
    if common_top:
        rows = "".join(
            f'<tr><td class="num">{n}</td><td class="q">{esc(normalise_query(q))}</td></tr>'
            for q, n in common_top
        )
        common_html = f'<table class="data"><thead><tr><th>Asked</th><th>Question</th></tr></thead><tbody>{rows}</tbody></table>'

    # Sample queries by theme
    theme_blocks = []
    for theme, qs_in_theme in sorted(agg["samples_per_theme"].items(), key=lambda kv: -agg["themes"][kv[0]]):
        if not qs_in_theme: continue
        items = "".join(f'<li>{esc(q)}</li>' for q in qs_in_theme[:5])
        theme_blocks.append(
            f'<div class="theme-block"><h4>{esc(theme)} <span class="count">· {agg["themes"][theme]} queries</span></h4>'
            f'<ul class="q-list">{items}</ul></div>'
        )
    theme_blocks_html = "\n".join(theme_blocks)

    # Recent queries timeline (last 25)
    recent = sessions[-25:][::-1]
    recent_rows = "".join(
        f'<tr>'
        f'<td>{s["start_dt"].astimezone(TZ_IST).strftime("%d %b %H:%M")}</td>'
        f'<td>{esc(s["dataset"] or "-")}</td>'
        f'<td class="q">{esc(s["user_query_norm"])}</td>'
        f'</tr>'
        for s in recent
    )
    recent_html = f'<table class="data"><thead><tr><th>When (IST)</th><th>Dataset</th><th>Query</th></tr></thead><tbody>{recent_rows}</tbody></table>'

    # Multilingual
    biling = [s for s in sessions if detect_script(s["user_query"]) not in ("English (Latin)", "Unknown")]
    biling_html = ""
    if biling:
        rows = "".join(
            f'<tr><td>{esc(detect_script(s["user_query"]))}</td>'
            f'<td class="q">{esc(s["user_query_norm"])}</td>'
            f'<td>{esc(s["dataset"] or "-")}</td>'
            f'<td>{s["start_dt"].astimezone(TZ_IST).strftime("%d %b")}</td></tr>'
            for s in biling[:10]
        )
        biling_html = (
            '<table class="data"><thead><tr><th>Script</th><th>Verbatim</th><th>Dataset</th><th>When</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    # Notable queries - selected dynamically from the current window by an
    # objective score (length, research-style phrasing, multi-dataset mentions).
    # Common repeated questions are excluded because they already appear above.
    common_lookup = {q for q, n in agg["repeated"].items() if n >= 3}
    RESEARCH_VERBS = (
        "assess", "compare", "track", "quantify", "estimate", "map ",
        "cross-reference", "benchmark", "analyze", "examine", "identify",
        "need to verify", "find ", "trend in", "what is",
    )
    DS_NAMES = ("plfs", "cpi", "iip", "asi", "nas", "wpi", "energy", "aishe",
                "asuse", "gender", "nfhs", "envstats", "rbi", "nss", "cpialrl",
                "hces", "tus", "udise", "mnre")

    def score(s):
        q = s["user_query"].strip()
        ql = q.lower()
        sc = len(q)
        if any(ql.startswith(v) for v in RESEARCH_VERBS):
            sc += 30
        sc += 5 * sum(1 for w in DS_NAMES if w in ql)
        return sc

    candidates = sorted(
        (s for s in sessions if s["user_query"].strip().lower() not in common_lookup),
        key=score, reverse=True,
    )
    notable, seen = [], set()
    for s in candidates:
        if s["user_query_norm"] in seen: continue
        seen.add(s["user_query_norm"])
        notable.append(s)
        if len(notable) >= 12: break

    notable_html = "".join(
        f'<div class="hl-row"><div class="hl-q">{esc(s["user_query_norm"])}</div>'
        f'<div class="hl-meta">{esc(s["dataset"] or "-")} · {s["start_dt"].astimezone(TZ_IST).strftime("%d %b").upper()}</div>'
        f'</div>'
        for s in notable
    )

    generated = datetime.now(TZ_IST).strftime("%d %B %Y, %H:%M IST")
    period_str = f"{window_start.strftime('%d %B %Y')} to {window_end.strftime('%d %B %Y')}"

    # Continuous-data caveat - one short line; if the window was clipped, add a note
    caveat = ""
    cs = agg.get("continuous_since")
    if cs:
        cs_str = date.fromisoformat(cs).strftime('%d %B %Y')
        caveat = f"Data available since {cs_str}."
        if agg.get("clipped") and agg.get("requested_start"):
            rs = date.fromisoformat(agg["requested_start"]).strftime('%d %B %Y')
            caveat += f" Showing from this date onwards (your range began earlier, on {rs})."

    peak_str = ""
    if agg["peak_day"][0]:
        peak_date = date.fromisoformat(agg["peak_day"][0]).strftime("%d %b")
        peak_str = f'<span style="color:var(--muted);font-size:14px;font-weight:400">  on {peak_date}</span>'
    peak_value = f'{fmt_int(agg["peak_day"][1])}{peak_str}'

    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Queries on MoSPI MCP - Rich Report</title>
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
                font-family: 'Inter', system-ui, sans-serif; line-height: 1.55;
                -webkit-font-smoothing: antialiased; }}
  .page {{ max-width: 940px; margin: 0 auto; padding: 64px 64px 80px; background: var(--paper);
           box-shadow: 0 1px 0 var(--line); }}
  h1, h2, h3, h4 {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; color: var(--ink); margin: 0; letter-spacing: -0.01em; }}
  h1 {{ font-size: 38px; line-height: 1.1; margin-bottom: 10px; }}
  h2 {{ font-size: 26px; margin-top: 56px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }}
  h3 {{ font-size: 19px; margin-top: 28px; margin-bottom: 10px; }}
  h4 {{ font-size: 13px; margin-top: 18px; margin-bottom: 6px; font-family: 'Inter', sans-serif;
        font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--subtle); }}
  h4 .count {{ color: var(--muted); font-weight: 400; text-transform: none; letter-spacing: 0; font-size: 12px; }}
  p, li {{ font-size: 15px; color: var(--ink); }}
  .meta {{ color: var(--subtle); font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; margin: 0; }}

  /* Hero */
  .hero {{ margin: 32px 0 24px; padding: 28px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
  .hero-value {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; font-size: 70px; line-height: 1; color: var(--ink); letter-spacing: -0.02em; }}
  .hero-label {{ font-size: 14px; color: var(--subtle); margin-top: 10px; }}

  /* KPI strip */
  .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; margin: 0 0 22px;
           border-bottom: 1px solid var(--line); }}
  .caveat {{ font-size: 12px; color: var(--subtle); margin-top: 6px; font-style: italic; }}
  .kpi {{ padding: 18px 14px 20px 0; border-right: 1px solid var(--line); }}
  .kpi:last-child {{ border-right: 0; padding-left: 14px; }}
  .kpi-value {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; font-size: 26px; line-height: 1.1; }}
  .kpi-label {{ font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--subtle); margin-top: 6px; }}
  .kpi-sub  {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}

  /* Charts */
  .chart {{ width: 100%; height: auto; max-width: 820px; }}
  .chart-empty {{ font-size: 13px; color: var(--subtle); padding: 14px 0; }}

  /* Tables */
  table.data {{ width: 100%; border-collapse: collapse; margin: 12px 0 8px; font-size: 14px; }}
  table.data th, table.data td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }}
  table.data th {{ font-weight: 500; color: var(--subtle); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }}
  table.data td.num {{ font-variant-numeric: tabular-nums; text-align: right; width: 1%; white-space: nowrap; color: var(--ink); }}
  table.data td.q {{ font-family: 'EB Garamond', Georgia, serif; font-size: 16px; color: var(--ink); }}

  /* Highlights */
  .highlights {{ margin: 14px 0 20px; border-top: 1px solid var(--line); }}
  .hl-row {{ padding: 14px 0; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; gap: 24px; align-items: baseline; }}
  .hl-q {{ font-family: 'EB Garamond', Georgia, serif; font-size: 18px; line-height: 1.4; color: var(--ink); flex: 1; }}
  .hl-meta {{ font-size: 12px; color: var(--subtle); white-space: nowrap; letter-spacing: 0.04em; text-transform: uppercase; }}

  /* Theme blocks */
  .theme-block {{ break-inside: avoid; padding: 4px 0 14px; }}
  .theme-block h4 {{ color: var(--ink); text-transform: none; letter-spacing: 0; font-size: 14px; font-weight: 600; }}
  .q-list {{ list-style: none; padding: 0; margin: 8px 0 16px; }}
  .q-list li {{ font-family: 'EB Garamond', Georgia, serif; font-size: 16px; color: var(--ink); padding: 5px 0; border-bottom: 1px dashed var(--line); }}
  .q-list li:last-child {{ border-bottom: 0; }}

  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 36px; }}
  .grid2 > * {{ min-width: 0; }}

  .fv-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 24px; margin-top: 12px; }}

  .footer {{ margin-top: 56px; padding-top: 16px; border-top: 1px solid var(--line);
             font-size: 11px; color: var(--muted); letter-spacing: 0.04em; }}

  @media print {{
    body, html, .page {{ background: white; box-shadow: none; }}
    .page {{ max-width: none; padding: 24px; }}
    h2 {{ break-before: page; }} h2:first-of-type {{ break-before: auto; }}
  }}
</style>
</head>
<body>
<div class="page">

  <header>
    <h1>Claude Queries on MoSPI MCP</h1>
    <p class="meta">{esc(period_str)}  ·  Generated {esc(generated)}</p>
    {f'<p class="caveat">{esc(caveat)}</p>' if caveat else ''}
  </header>

  <div class="hero">
    <div class="hero-value">{fmt_int(agg['n_queries'])}</div>
    <div class="hero-label">user queries</div>
  </div>

  <div class="kpis">
    {kpi("Tool calls", fmt_int(agg["n_tool_calls"]))}
    {kpi("Datasets queried", fmt_int(agg["n_datasets"]))}
    {kpi("Peak day", peak_value)}
    {kpi("Period", f"{agg['n_days_in_window']} days")}
  </div>

  <h2>1 · Most-asked questions</h2>
  {common_html or '<p class="meta">No question was asked more than once in this window.</p>'}

  <h2>2 · Themes</h2>
  {theme_chart}

  <h2>3 · Datasets</h2>
  {dataset_chart}

  <h2>4 · Queries by theme</h2>
  {theme_blocks_html}

  <h2>5 · When</h2>

  <h3>By hour of day (IST)</h3>
  {hour_chart}

  <h3>By day of week</h3>
  {dow_chart}

  <h3>Daily volume</h3>
  {daily_chart}

  <h2>6 · Recent queries</h2>
  {recent_html}

  {(f'<h2>7 · Multilingual queries</h2>' + biling_html) if biling_html else ''}

  {(f'<h2>{8 if biling_html else 7} · Detailed queries</h2><div class="highlights">' + notable_html + '</div>') if notable_html else ''}

  <div class="footer">
    {esc(period_str)} · Generated {esc(generated)}
  </div>
</div>
</body>
</html>'''


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def render_empty_html(requested_start, requested_end, continuous_start):
    """Render a clear empty-state page when the requested window has no data."""
    cs_str = continuous_start.strftime("%d %B %Y") if continuous_start else "06 May 2026"
    req = f"{requested_start.strftime('%d %B %Y')} to {requested_end.strftime('%d %B %Y')}" \
          if requested_start else f"up to {requested_end.strftime('%d %B %Y')}"
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Queries on MoSPI MCP - No data</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=EB+Garamond:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  html, body {{ background: #fbfaf7; color: {INK}; margin: 0; padding: 0; font-family: 'Inter', system-ui, sans-serif; line-height: 1.55; }}
  .page {{ max-width: 720px; margin: 0 auto; padding: 96px 64px 80px; background: #fff; box-shadow: 0 1px 0 {LINE}; }}
  h1 {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; font-size: 36px; line-height: 1.1; margin: 0 0 12px; color: {INK}; }}
  .meta {{ font-size: 12px; color: {SUBTLE}; letter-spacing: 0.06em; text-transform: uppercase; }}
  .caveat {{ font-size: 13px; color: {SUBTLE}; font-style: italic; margin-top: 6px; }}
  .empty {{ margin-top: 48px; padding: 36px 0; border-top: 1px solid {LINE}; border-bottom: 1px solid {LINE}; }}
  .empty-h {{ font-family: 'EB Garamond', Georgia, serif; font-weight: 600; font-size: 48px; color: {INK}; line-height: 1; letter-spacing: -0.02em; }}
  .empty-sub {{ font-size: 14px; color: {SUBTLE}; margin-top: 14px; }}
  .hint {{ margin-top: 28px; font-family: 'EB Garamond', Georgia, serif; font-size: 17px; color: {INK}; }}
</style></head><body><div class="page">
  <h1>Claude Queries on MoSPI MCP</h1>
  <p class="meta">Requested window: {esc(req)}</p>
  <p class="caveat">Data available since {esc(cs_str)}.</p>
  <div class="empty">
    <div class="empty-h">No data</div>
    <div class="empty-sub">No queries recorded in this window.</div>
  </div>
  <p class="hint">Please try a range from <strong>{esc(cs_str)}</strong> onwards.</p>
</div></body></html>'''


def main():
    args = parse_args()
    user_specified = bool(args.since or args.start or args.end)
    requested_start, end_date = resolve_window(args)
    today = datetime.now(TZ_IST).date()

    # Always determine the date when continuous capture started.
    active_dates = fetch_active_dates(args.host, args.remote)
    continuous_start, gap_days = detect_continuous_start(active_dates)

    # Resolve the effective window and figure out which case we're in.
    clipped = False
    empty_pre_data = False

    if not user_specified:
        # Default: continuous_start → today
        start_date = continuous_start
        end_date   = today
        print(f"[window] default: {start_date} to {end_date}", file=sys.stderr)
    else:
        # Treat --end alone as "from continuous_start to --end"
        if requested_start is None:
            requested_start = continuous_start or end_date

        if continuous_start is None:
            empty_pre_data = True
            start_date = requested_start
        elif end_date < continuous_start:
            # Entire requested window is before any data
            empty_pre_data = True
            start_date = requested_start
            print(f"[window] requested {requested_start}..{end_date} is entirely before {continuous_start}",
                  file=sys.stderr)
        elif requested_start < continuous_start:
            # Partial overlap - clip start to continuous_start
            start_date = continuous_start
            clipped = True
            print(f"[window] clipped: requested {requested_start} -> {continuous_start} (continuous start)",
                  file=sys.stderr)
        else:
            start_date = requested_start

    # Render path: empty state vs normal report
    out_path = Path(args.output).expanduser() if args.output else default_output_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if empty_pre_data:
        html_doc = render_empty_html(requested_start, end_date, continuous_start)
        out_path.write_text(html_doc)
        print(f"[write] {out_path}  ({out_path.stat().st_size:,} bytes) [empty-state]", file=sys.stderr)
        if not args.no_open and sys.platform == "darwin":
            subprocess.run(["open", str(out_path)])
        return

    start_us = int(datetime.combine(start_date, datetime.min.time(), TZ_IST).timestamp() * 1_000_000) if start_date else 0
    end_us   = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), TZ_IST).timestamp() * 1_000_000)

    jsonl_text = fetch_window(args.host, args.remote, start_us, end_us)
    sessions = load_sessions(jsonl_text)
    print(f"[parse] {len(sessions):,} real user queries in window", file=sys.stderr)

    if start_date is None:
        start_date = sessions[0]["start_dt"].astimezone(TZ_IST).date() if sessions else end_date

    agg = aggregate(sessions, start_date, end_date)
    agg["continuous_since"] = continuous_start.isoformat() if continuous_start else None
    agg["clipped"]          = clipped
    agg["requested_start"]  = requested_start.isoformat() if (clipped and requested_start) else None
    html_doc = render_html(sessions, agg, start_date, end_date, args.top)

    out_path.write_text(html_doc)
    print(f"[write] {out_path}  ({out_path.stat().st_size:,} bytes)", file=sys.stderr)
    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", str(out_path)])
        print(f"[open] opened in default browser", file=sys.stderr)


if __name__ == "__main__":
    main()
