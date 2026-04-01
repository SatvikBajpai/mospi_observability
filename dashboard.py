"""
MoSPI MCP — Jaeger Web Dashboard
FastAPI backend + single-page frontend with Plotly charts.

Run:
    pip install fastapi uvicorn
    python dashboard.py

Open: http://localhost:7777
"""

import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="MoSPI MCP Dashboard")

JAEGER  = "http://10.24.89.149:16686"
SERVICE = "mospi-mcp-server"

STEP_ORDER = [
    "list_datasets",
    "get_indicators",
    "get_metadata",
    "get_data",
]

def step_num(name):
    for i, s in enumerate(STEP_ORDER):
        if name == s:
            return i + 1
    return 99

def step_label(name):
    n = step_num(name)
    labels = {1: "list_datasets", 2: "get_indicators", 3: "get_metadata", 4: "get_data"}
    return labels.get(n, name)


# ─────────────────────────────────────────────────────────────
# Jaeger helpers
# ─────────────────────────────────────────────────────────────

TOOL_OPERATIONS = [
    "tool.list_datasets",
    "tool.get_indicators",
    "tool.get_metadata",
    "tool.get_data",
]

def fetch_traces(hours: int = 24, limit: int = 500) -> list:
    """Fetch traces by querying each tool operation individually.
    This avoids drowning in HTTP-only traces (1000+/hour) and correctly
    surfaces sessions across longer time windows."""
    now_us   = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    start_us = now_us - hours * 3600 * 1_000_000

    seen_ids = set()
    all_traces = []

    for op in TOOL_OPERATIONS:
        try:
            r = requests.get(
                f"{JAEGER}/api/traces",
                params={
                    "service":   SERVICE,
                    "operation": op,
                    "start":     start_us,
                    "end":       now_us,
                    "limit":     limit,
                },
                timeout=30,
            )
            r.raise_for_status()
            for trace in r.json().get("data", []):
                if trace["traceID"] not in seen_ids:
                    seen_ids.add(trace["traceID"])
                    all_traces.append(trace)
        except Exception:
            continue

    return all_traces


def get_tag(tags, key, default=None):
    for t in tags:
        if t["key"] == key:
            return t["value"]
    return default


def _parse_tool_span(span):
    """Parse a single tool span into a step dict."""
    tags      = {t["key"]: t["value"] for t in span["tags"]}
    tool_name = tags.get("tool.name", span["operationName"].replace("tool.", ""))
    inp_raw   = tags.get("tool.input", "{}")
    out_raw   = tags.get("tool.output", "{}")

    try:    inp = json.loads(inp_raw)
    except: inp = {}
    try:    out = json.loads(out_raw)
    except: out = {}

    api_err = ""
    error   = tags.get("otel.status_code", "") == "ERROR" or bool(tags.get("error", False))
    if isinstance(out, dict) and "error" in out:
        api_err = str(out["error"])[:300]
        error   = True
    if error and not api_err:
        api_err = (
            tags.get("otel.status_description") or
            tags.get("exception.message") or
            tags.get("error.message") or
            "span marked ERROR (no message captured)"
        )

    return {
        "tool":        tool_name,
        "step_num":    step_num(tool_name),
        "step_label":  step_label(tool_name),
        "input":       inp,
        "input_raw":   inp_raw[:300],
        "output":      out,
        "output_size": int(tags.get("tool.output_size", 0)),
        "duration_ms": round(span["duration"] / 1000, 1),
        "error":       error,
        "api_error":   api_err,
        "start_us":    span["startTime"],
        "trace_id":    span.get("traceID", ""),
    }


def _build_session(steps):
    """Convert a list of step dicts into a session dict."""
    steps.sort(key=lambda s: (s["step_num"], s["start_us"]))

    user_query, dataset, filters = "", "", {}
    api_error, total_records = "", 0
    completed_steps = set()
    trace_ids = set()

    for s in steps:
        completed_steps.add(s["step_num"])
        if s.get("trace_id"):
            trace_ids.add(s["trace_id"])
        if "user_query" in s["input"] and s["input"]["user_query"]:
            user_query = s["input"]["user_query"]
        if "dataset" in s["input"] and s["input"]["dataset"]:
            dataset = s["input"]["dataset"].upper()
        if "filters" in s["input"]:
            filters = s["input"]["filters"]
        if s["step_num"] == 4 and not s["api_error"]:
            out = s["output"]
            if isinstance(out, dict) and "data" in out and isinstance(out["data"], list):
                total_records = out.get("meta_data", {}).get("totalRecords", len(out["data"]))
        if s["api_error"] and not api_error:
            api_error = s["api_error"]

    start_dt  = datetime.fromtimestamp(
        min(s["start_us"] for s in steps) / 1_000_000, tz=timezone.utc
    )
    total_ms  = round(sum(s["duration_ms"] for s in steps), 1)
    has_error = any(s["error"] for s in steps)
    is_complete = {1, 2, 3, 4}.issubset(completed_steps)

    step_timing = {}
    for s in steps:
        n = s["step_num"]
        if n not in step_timing:
            step_timing[n] = {"duration_ms": 0, "label": s["step_label"], "error": False}
        step_timing[n]["duration_ms"] += s["duration_ms"]
        if s["error"]:
            step_timing[n]["error"] = True

    ist_dt = start_dt + timedelta(hours=5, minutes=30)
    trace_id = sorted(trace_ids)[0] if trace_ids else "unknown"

    return {
        "trace_id":       trace_id,
        "start_dt":       start_dt.isoformat(),
        "start_ts":       start_dt.timestamp(),
        "start_display":  start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "start_display_ist": ist_dt.strftime("%Y-%m-%d %H:%M IST"),
        "hour":           start_dt.strftime("%m-%d %H:00"),
        "steps":          steps,
        "step_timing":    step_timing,
        "user_query":     user_query,
        "dataset":        dataset,
        "filters":        filters,
        "total_ms":       total_ms,
        "total_records":  total_records,
        "has_error":      has_error,
        "api_error":      api_error,
        "n_steps":        len(steps),
        "completed_steps": sorted(completed_steps),
        "is_complete":    is_complete,
        "error_details": [
            {
                "tool":      s["tool"],
                "step":      s["step_num"],
                "error":     s["api_error"] or "span error",
                "input":     s["input_raw"],
            }
            for s in steps if s["error"]
        ],
    }


def reconstruct_sessions(traces, gap_threshold_s=60):
    """Reconstruct logical sessions from single-step traces using time proximity.

    In stateless mode each tool call creates a separate trace, so we flatten
    all tool spans, sort chronologically, and group into sessions when:
    - The gap between consecutive spans exceeds gap_threshold_s, OR
    - A step1 call appears (signals a new query cycle)
    """
    # 1. Flatten all tool spans across all traces
    all_tool_spans = []
    for trace in traces:
        for span in trace["spans"]:
            if span["operationName"].startswith("tool."):
                span["traceID"] = trace["traceID"]
                all_tool_spans.append(span)

    if not all_tool_spans:
        return []

    # 2. Sort by startTime
    all_tool_spans.sort(key=lambda s: s["startTime"])

    # 3. Group into sessions by time gap or step1 boundary
    sessions = []
    current_group = []

    for span in all_tool_spans:
        parsed = _parse_tool_span(span)
        if current_group:
            gap_s = (span["startTime"] - current_group[-1]["start_us"]) / 1_000_000
            if gap_s > gap_threshold_s or parsed["step_num"] == 1:
                sessions.append(_build_session(current_group))
                current_group = []
        current_group.append(parsed)

    if current_group:
        sessions.append(_build_session(current_group))

    sessions.sort(key=lambda s: s["start_ts"], reverse=True)
    return sessions


# ─────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(hours: int = Query(24)):
    traces   = fetch_traces(hours)
    sessions = reconstruct_sessions(traces)

    # All-time session count (for the overview card)
    if hours < 8760:
        all_time_traces = fetch_traces(hours=8760)
        all_time_count = len(reconstruct_sessions(all_time_traces))
    else:
        all_time_count = len(sessions)

    total_sessions   = len(sessions)
    complete_sessions = sum(1 for s in sessions if s["is_complete"])
    error_sessions   = sum(1 for s in sessions if s["has_error"])
    error_rate       = round(error_sessions / total_sessions * 100, 1) if total_sessions else 0
    completion_rate  = round(complete_sessions / total_sessions * 100, 1) if total_sessions else 0
    avg_duration     = round(sum(s["total_ms"] for s in sessions) / total_sessions, 0) if total_sessions else 0

    # Durations for p50/p95
    durations = sorted(s["total_ms"] for s in sessions)
    p50 = durations[len(durations) // 2] if durations else 0
    p95 = durations[int(len(durations) * 0.95)] if durations else 0

    # Tool metrics
    tool_counts    = defaultdict(int)
    tool_errors    = defaultdict(int)
    tool_durations = defaultdict(list)
    dataset_counts = defaultdict(int)
    query_counts   = defaultdict(int)
    hourly_counts  = defaultdict(int)

    # Funnel: how many sessions reached each step
    funnel = defaultdict(int)

    for s in sessions:
        hourly_counts[s["hour"]] += 1
        if s["dataset"]:
            dataset_counts[s["dataset"]] += 1
        if s["user_query"]:
            query_counts[s["user_query"]] += 1
        for step_n in s["completed_steps"]:
            funnel[step_n] += 1
        for step in s["steps"]:
            tool_counts[step["step_label"]] += 1
            tool_durations[step["step_label"]].append(step["duration_ms"])
            if step["error"]:
                tool_errors[step["step_label"]] += 1

    tool_avg_dur = {
        t: round(sum(v) / len(v), 1) for t, v in tool_durations.items()
    }
    tool_p95_dur = {
        t: round(sorted(v)[int(len(v) * 0.95)], 1)
        for t, v in tool_durations.items() if v
    }
    tool_error_rate = {
        t: round(tool_errors[t] / tool_counts[t] * 100, 1)
        for t in tool_counts
    }

    # Recent errors — rich detail
    recent_errors = []
    for s in sessions:
        if not s["has_error"]:
            continue
        for ed in s["error_details"]:
            recent_errors.append({
                "time":       s["start_display_ist"],
                "dataset":    s["dataset"] or "—",
                "user_query": s["user_query"][:80] or "—",
                "step":       ed["step"],
                "tool":       ed["tool"],
                "error":      ed["error"][:200],
                "input":      ed["input"][:200],
                "trace_id":   s["trace_id"][:16] + "…",
            })

    # Sessions list for table
    sessions_list = [
        {
            "time":         s["start_display_ist"],
            "user_query":   s["user_query"][:80] or "—",
            "dataset":      s["dataset"] or "—",
            "n_steps":      s["n_steps"],
            "duration_ms":  s["total_ms"],
            "records":      s["total_records"],
            "status":       "ERROR" if s["has_error"] else ("OK" if s["is_complete"] else "PARTIAL"),
            "step_timing":  {str(k): v for k, v in s["step_timing"].items()},
            "trace_id":     s["trace_id"],
        }
        for s in sessions
    ]

    # Top queries
    top_queries = [
        {"query": q, "count": c}
        for q, c in sorted(query_counts.items(), key=lambda x: -x[1])
    ]

    return {
        "overview": {
            "total_sessions":    total_sessions,
            "complete_sessions": complete_sessions,
            "completion_rate":   completion_rate,
            "error_sessions":    error_sessions,
            "error_rate":        error_rate,
            "avg_duration_ms":   avg_duration,
            "p50_ms":            p50,
            "p95_ms":            p95,
            "hours":             hours,
            "all_time_sessions": all_time_count,
        },
        "tool_counts":     dict(tool_counts),
        "tool_avg_dur":    tool_avg_dur,
        "tool_p95_dur":    tool_p95_dur,
        "tool_error_rate": tool_error_rate,
        "dataset_counts":  dict(dataset_counts),
        "hourly_counts":   sorted(hourly_counts.items()),
        "funnel":          {str(k): funnel[k] for k in sorted(funnel)},
        "top_queries":     top_queries,
        "sessions":        sessions_list,
        "errors":          recent_errors[:100],
    }


# ─────────────────────────────────────────────────────────────
# Frontend
# ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MoSPI MCP · Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
:root {
  --bg:#0d1117; --surface:#161b22; --surface2:#1c2128; --border:#30363d;
  --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --green:#3fb950;
  --red:#f85149; --yellow:#e3b341; --purple:#bc8cff; --orange:#f0883e;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}

/* Header */
header{display:flex;align-items:center;justify-content:space-between;padding:14px 24px;
  border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;z-index:100}
header h1{font-size:16px;font-weight:700;color:var(--accent);letter-spacing:-.01em}
header h1 span{color:var(--muted);font-weight:400}
.controls{display:flex;gap:10px;align-items:center}
select,button{background:var(--bg);border:1px solid var(--border);color:var(--text);
  padding:5px 12px;border-radius:6px;font-size:13px;cursor:pointer}
button:hover,select:hover{border-color:var(--accent)}
#status{font-size:12px;color:var(--muted)}

/* Layout */
main{padding:20px 24px;max-width:1600px;margin:0 auto}
section{margin-bottom:24px}
.section-title{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* Stat cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.card .label{font-size:11px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em}
.card .value{font-size:26px;font-weight:700;line-height:1}
.card .sub{font-size:11px;color:var(--muted);margin-top:5px}
.c-blue .value{color:var(--accent)}
.c-green .value{color:var(--green)}
.c-red .value{color:var(--red)}
.c-purple .value{color:var(--purple)}
.c-yellow .value{color:var(--yellow)}
.c-orange .value{color:var(--orange)}

/* Charts */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.chart-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:4px;min-height:300px}
.chart-card.span2{grid-column:span 2}
.chart-card.span3{grid-column:span 3}

/* Tabs */
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--border);margin-bottom:16px}
.tab{padding:8px 18px;cursor:pointer;font-size:13px;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:500}
.tab-pane{display:none}
.tab-pane.active{display:block}

/* Tables */
.tbl-wrap{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.tbl-toolbar{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tbl-toolbar input,.tbl-toolbar select{
  background:var(--bg);border:1px solid var(--border);color:var(--text);
  padding:5px 10px;border-radius:6px;font-size:13px}
.tbl-toolbar input{flex:1;min-width:160px}
.tbl-scroll{overflow-x:auto;max-height:520px;overflow-y:auto}
table{width:100%;border-collapse:collapse}
th{background:var(--bg);color:var(--muted);font-size:11px;text-transform:uppercase;
  letter-spacing:.05em;padding:8px 12px;text-align:left;border-bottom:1px solid var(--border);
  position:sticky;top:0;white-space:nowrap}
td{padding:8px 12px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(88,166,255,.04)}

/* Badges */
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap}
.b-ok{background:rgba(63,185,80,.15);color:var(--green)}
.b-err{background:rgba(248,81,73,.15);color:var(--red)}
.b-partial{background:rgba(227,179,65,.15);color:var(--yellow)}
.b-ds{background:rgba(88,166,255,.12);color:var(--accent)}

/* Step bars */
.step-bars{display:flex;gap:3px;margin-top:4px}
.step-bar{height:4px;border-radius:2px;flex:1}
.step-bar.done{background:var(--green)}
.step-bar.err{background:var(--red)}
.step-bar.empty{background:var(--border)}

/* Error detail */
.err-msg{font-family:monospace;font-size:11px;color:var(--red);word-break:break-all;max-width:400px}
.inp-snippet{font-family:monospace;font-size:11px;color:var(--muted);word-break:break-all;max-width:300px}

/* Query row */
.q-bar-bg{height:6px;background:var(--border);border-radius:3px;margin-top:4px}
.q-bar{height:6px;background:var(--accent);border-radius:3px}

/* Loading */
#loading{text-align:center;padding:80px;color:var(--muted)}
.spinner{display:inline-block;width:20px;height:20px;border:2px solid var(--border);
  border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}

@media(max-width:900px){
  .grid-2,.grid-3{grid-template-columns:1fr}
  .chart-card.span2,.chart-card.span3{grid-column:span 1}
}
</style>
</head>
<body>

<header>
  <h1>MoSPI MCP <span>· Observability Dashboard</span></h1>
  <div class="controls">
    <select id="hours-sel" onchange="load()">
      <option value="1">Last 1h</option>
      <option value="6">Last 6h</option>
      <option value="24" selected>Last 24h</option>
      <option value="72">Last 3d</option>
      <option value="168">Last 7d</option>
      <option value="8760">All time</option>
    </select>
    <button onclick="load()">⟳ Refresh</button>
    <span id="status">—</span>
  </div>
</header>

<main>
<div id="loading"><span class="spinner"></span>Fetching traces from Jaeger…</div>
<div id="app" style="display:none">

<!-- ── Overview cards ── -->
<section>
  <div class="section-title">Overview</div>
  <div class="cards">
    <div class="card c-blue">
      <div class="label">Sessions</div>
      <div class="value" id="ov-sessions">—</div>
      <div class="sub" id="ov-sessions-sub"></div>
    </div>
    <div class="card c-green">
      <div class="label">All-Time Sessions</div>
      <div class="value" id="ov-alltime">—</div>
      <div class="sub">since first trace</div>
    </div>
    <div class="card" id="ov-errcard">
      <div class="label">Error Rate</div>
      <div class="value" id="ov-errrate">—</div>
      <div class="sub" id="ov-errsub"></div>
    </div>
    <div class="card c-purple">
      <div class="label">Total Tool Calls</div>
      <div class="value" id="ov-calls">—</div>
      <div class="sub">across all sessions</div>
    </div>
    <div class="card c-yellow">
      <div class="label">Avg Duration</div>
      <div class="value" id="ov-avg">—</div>
      <div class="sub" id="ov-p95">ms end-to-end</div>
    </div>
  </div>
</section>

<!-- ── Tabs ── -->
<div class="tabs">
  <div class="tab active" onclick="tab('overview')">Overview</div>
  <div class="tab" onclick="tab('sessions')">Sessions</div>
  <div class="tab" onclick="tab('errors')">Errors <span id="err-badge"></span></div>
  <div class="tab" onclick="tab('queries')">User Queries</div>
</div>

<!-- ═══════════════ OVERVIEW TAB ═══════════════ -->
<div id="pane-overview" class="tab-pane active">

  <!-- Timeline -->
  <section>
    <div class="section-title">Sessions Over Time</div>
    <div class="chart-card"><div id="ch-timeline"></div></div>
  </section>

  <!-- Dataset Usage -->
  <section>
    <div class="section-title">Dataset Usage</div>
    <div class="chart-card"><div id="ch-datasets"></div></div>
  </section>

  <!-- Tool perf -->
  <section>
    <div class="section-title">Tool Performance</div>
    <div class="grid-3">
      <div class="chart-card"><div id="ch-tool-counts"></div></div>
      <div class="chart-card"><div id="ch-tool-dur"></div></div>
      <div class="chart-card"><div id="ch-tool-err"></div></div>
    </div>
  </section>

</div>

<!-- ═══════════════ SESSIONS TAB ═══════════════ -->
<div id="pane-sessions" class="tab-pane">
  <div class="tbl-wrap">
    <div class="tbl-toolbar">
      <input id="sess-search" type="text" placeholder="Search query, dataset…" oninput="filterSessions()"/>
      <select id="sess-ds" onchange="filterSessions()"><option value="">All datasets</option></select>
      <select id="sess-status" onchange="filterSessions()">
        <option value="">All statuses</option>
        <option value="OK">OK</option>
        <option value="ERROR">ERROR</option>
        <option value="PARTIAL">PARTIAL</option>
      </select>
    </div>
    <div class="tbl-scroll">
      <table>
        <thead>
          <tr>
            <th>Time (IST)</th>
            <th>User Query</th>
            <th>Dataset</th>
            <th>Steps</th>
            <th>Duration</th>
            <th>Records</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="sess-body"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══════════════ ERRORS TAB ═══════════════ -->
<div id="pane-errors" class="tab-pane">
  <div class="tbl-wrap">
    <div class="tbl-toolbar">
      <input type="text" placeholder="Search errors, tools, queries…" oninput="filterTbl('err-body', this.value)"/>
    </div>
    <div class="tbl-scroll">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Dataset</th>
            <th>User Query</th>
            <th>Failed Tool</th>
            <th>Error</th>
            <th style="width:30px"></th>
          </tr>
        </thead>
        <tbody id="err-body"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══════════════ QUERIES TAB ═══════════════ -->
<div id="pane-queries" class="tab-pane">
  <div class="tbl-wrap">
    <div class="tbl-toolbar">
      <input type="text" placeholder="Search queries…" oninput="filterTbl('q-body', this.value)"/>
    </div>
    <div class="tbl-scroll">
      <table>
        <thead>
          <tr>
            <th style="width:40px">#</th>
            <th>User Query</th>
            <th style="width:80px">Count</th>
            <th style="width:180px">Frequency</th>
          </tr>
        </thead>
        <tbody id="q-body"></tbody>
      </table>
    </div>
  </div>
</div>

</div><!-- #app -->
</main>

<script>
const LAYOUT = {
  paper_bgcolor:'#161b22', plot_bgcolor:'#161b22',
  font:{color:'#e6edf3',size:11},
  margin:{t:32,b:32,l:44,r:12},
  legend:{bgcolor:'transparent'},
  xaxis:{gridcolor:'#30363d',zerolinecolor:'#30363d'},
  yaxis:{gridcolor:'#30363d',zerolinecolor:'#30363d'},
};
const C = ['#58a6ff','#3fb950','#bc8cff','#e3b341','#f0883e','#f85149','#79c0ff','#56d364'];
const CFG = {responsive:true,displayModeBar:false};

let _sessions = [], _errors = [], _queries = [];

// ── Tab switching
function tab(name) {
  document.querySelectorAll('.tab').forEach((el,i) => {
    el.classList.toggle('active', ['overview','sessions','errors','queries'][i] === name);
  });
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-' + name).classList.add('active');
}

// ── Generic table filter
function filterTbl(tbodyId, q) {
  q = q.toLowerCase();
  document.querySelectorAll('#' + tbodyId + ' tr').forEach(r => {
    r.style.display = !q || r.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

// ── Session filter (multi-filter)
function filterSessions() {
  const q  = document.getElementById('sess-search').value.toLowerCase();
  const ds = document.getElementById('sess-ds').value.toLowerCase();
  const st = document.getElementById('sess-status').value.toLowerCase();
  document.querySelectorAll('#sess-body tr').forEach(r => {
    const txt = r.textContent.toLowerCase();
    const ok = (!q || txt.includes(q)) && (!ds || txt.includes(ds)) && (!st || txt.includes(st));
    r.style.display = ok ? '' : 'none';
  });
}

// ── Step progress bars
function stepBars(stepTiming) {
  return [1,2,3,4].map(n => {
    const s = stepTiming[n];
    const cls = !s ? 'empty' : (s.error ? 'err' : 'done');
    return `<div class="step-bar ${cls}" title="${s ? s.label+' '+s.duration_ms+'ms' : 'not reached'}"></div>`;
  }).join('');
}

// ── Render overview cards
function renderCards(ov) {
  document.getElementById('ov-sessions').textContent    = ov.total_sessions;
  document.getElementById('ov-sessions-sub').textContent = ov.hours >= 8760 ? 'all time' : `last ${ov.hours}h`;
  document.getElementById('ov-alltime').textContent     = ov.all_time_sessions;
  document.getElementById('ov-errrate').textContent     = ov.error_rate + '%';
  document.getElementById('ov-errsub').textContent      = ov.error_sessions + ' sessions failed';
  document.getElementById('ov-errcard').className       = 'card ' + (ov.error_sessions > 0 ? 'c-red' : 'c-green');

  // Sum tool counts from API
  document.getElementById('ov-calls').textContent       = '—';
  document.getElementById('ov-avg').textContent         = ov.avg_duration_ms + 'ms';
  document.getElementById('ov-p95').textContent         = `p50: ${ov.p50_ms}ms · p95: ${ov.p95_ms}ms`;

  const n = ov.error_sessions;
  document.getElementById('err-badge').textContent = n > 0 ? ` (${n})` : '';
  document.getElementById('status').textContent =
    'Updated ' + new Date().toLocaleTimeString();
}

// ── Render charts
function renderCharts(data) {
  // Total calls
  const tcTotal = Object.values(data.tool_counts).reduce((a,b)=>a+b,0);
  document.getElementById('ov-calls').textContent = tcTotal;

  // Timeline
  const hrs = data.hourly_counts.map(x=>x[0]);
  const hcn = data.hourly_counts.map(x=>x[1]);
  Plotly.newPlot('ch-timeline',[{
    x:hrs, y:hcn, type:'scatter', mode:'lines+markers',
    fill:'tozeroy', line:{color:'#58a6ff',width:2},
    fillcolor:'rgba(88,166,255,.1)', name:'Sessions',
    hovertemplate:'%{x}<br><b>%{y} session(s)</b><extra></extra>',
  }],{...LAYOUT, title:{text:'Sessions / Hour',font:{size:12}}, hovermode:'x unified'},CFG);

  // Dataset pie
  const dsk = Object.keys(data.dataset_counts);
  const dsv = dsk.map(k=>data.dataset_counts[k]);
  Plotly.newPlot('ch-datasets',[{
    labels:dsk, values:dsv, type:'pie',
    marker:{colors:C}, textinfo:'label+percent', hole:0.45,
  }],{...LAYOUT, title:{text:'Dataset Usage',font:{size:12}},showlegend:false},CFG);

  // Tool counts
  const tc = Object.keys(data.tool_counts);
  const tv = tc.map(t=>data.tool_counts[t]);
  Plotly.newPlot('ch-tool-counts',[{
    x:tc, y:tv, type:'bar', marker:{color:C},
    text:tv, textposition:'outside',
    hovertemplate:'%{x}: <b>%{y}</b><extra></extra>',
  }],{...LAYOUT, title:{text:'Tool Call Counts',font:{size:12}}, showlegend:false,
    height:300, xaxis:{...LAYOUT.xaxis, tickangle:-30}},CFG);

  // Tool duration
  const td = Object.keys(data.tool_avg_dur);
  Plotly.newPlot('ch-tool-dur',[
    {x:td, y:td.map(t=>data.tool_avg_dur[t]),   type:'bar', name:'Avg', marker:{color:'#3fb950'},
     hovertemplate:'%{x} avg: <b>%{y}ms</b><extra></extra>'},
    {x:td, y:td.map(t=>data.tool_p95_dur[t]||0), type:'bar', name:'P95', marker:{color:'#bc8cff'},
     hovertemplate:'%{x} p95: <b>%{y}ms</b><extra></extra>'},
  ],{...LAYOUT, title:{text:'Tool Duration (ms)',font:{size:12}}, barmode:'group',
    height:300, xaxis:{...LAYOUT.xaxis, tickangle:-30}},CFG);

  // Tool error rate
  const te = Object.keys(data.tool_error_rate);
  const tev = te.map(t=>data.tool_error_rate[t]);
  Plotly.newPlot('ch-tool-err',[{
    x:te, y:tev, type:'bar',
    marker:{color:tev.map(v=>v>0?'#f85149':'#3fb950')},
    text:tev.map(v=>v+'%'), textposition:'outside',
    hovertemplate:'%{x}: <b>%{y}%</b><extra></extra>',
  }],{...LAYOUT, title:{text:'Error Rate per Tool (%)',font:{size:12}}, showlegend:false,
    height:300, xaxis:{...LAYOUT.xaxis, tickangle:-30}},CFG);
}

// ── Render sessions table
function renderSessions(sessions) {
  // Populate dataset filter
  const dsSel = document.getElementById('sess-ds');
  const existing = new Set([...dsSel.options].map(o=>o.value));
  const allDS = [...new Set(sessions.map(s=>s.dataset).filter(Boolean))].sort();
  allDS.forEach(ds => {
    if(!existing.has(ds.toLowerCase())){
      const o = document.createElement('option');
      o.value = ds.toLowerCase(); o.textContent = ds;
      dsSel.appendChild(o);
    }
  });

  document.getElementById('sess-body').innerHTML = sessions.map(s => {
    const statusCls = {OK:'b-ok', ERROR:'b-err', PARTIAL:'b-partial'}[s.status] || 'b-ok';
    return `<tr>
      <td style="white-space:nowrap;color:var(--muted);font-size:12px">${s.time}</td>
      <td style="max-width:320px">
        <div>${s.user_query}</div>
        <div class="step-bars">${stepBars(s.step_timing)}</div>
      </td>
      <td><span class="badge b-ds">${s.dataset}</span></td>
      <td style="text-align:center;color:var(--muted)">${s.n_steps}</td>
      <td style="text-align:right;white-space:nowrap">${s.duration_ms}ms</td>
      <td style="text-align:right;color:var(--muted)">${s.records||'—'}</td>
      <td><span class="badge ${statusCls}">${s.status}</span></td>
    </tr>`;
  }).join('');
}

// ── Render errors table
function renderErrors(errors) {
  const body = document.getElementById('err-body');
  if(!errors.length){
    body.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--green);padding:32px">
      ✓ No errors in this time range</td></tr>`;
    return;
  }
  body.innerHTML = errors.map((e,i) => `
    <tr style="cursor:pointer" onclick="toggleErrDetail(${i})">
      <td style="white-space:nowrap;color:var(--muted);font-size:12px">${e.time}</td>
      <td><span class="badge b-ds">${e.dataset}</span></td>
      <td style="max-width:200px;color:var(--muted);font-size:12px">${e.user_query}</td>
      <td style="font-family:monospace;color:var(--purple);font-size:12px">${e.tool}</td>
      <td><div class="err-msg" style="max-width:300px">${e.error}</div></td>
      <td style="color:var(--muted);font-size:18px;text-align:center" id="err-arrow-${i}">▶</td>
    </tr>
    <tr id="err-detail-${i}" style="display:none">
      <td colspan="6" style="background:var(--bg);padding:0">
        <div style="padding:16px 20px;display:grid;grid-template-columns:1fr 1fr;gap:16px">
          <div>
            <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Input Sent to API</div>
            <pre style="background:#0d1117;border:1px solid var(--border);border-radius:6px;padding:12px;font-size:12px;color:#e6edf3;white-space:pre-wrap;word-break:break-all;margin:0">${formatJson(e.input)}</pre>
          </div>
          <div>
            <div style="font-size:11px;color:var(--red);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Error Detail</div>
            <pre style="background:#0d1117;border:1px solid rgba(248,81,73,.3);border-radius:6px;padding:12px;font-size:12px;color:#f85149;white-space:pre-wrap;word-break:break-all;margin:0">${e.error}</pre>
            <div style="margin-top:8px;font-size:11px;color:var(--muted)">Step: ${e.step} · Tool: ${e.tool} · Trace: ${e.trace_id}</div>
          </div>
        </div>
      </td>
    </tr>`).join('');
}

function toggleErrDetail(i) {
  const row = document.getElementById('err-detail-' + i);
  const arrow = document.getElementById('err-arrow-' + i);
  const open = row.style.display !== 'none';
  row.style.display = open ? 'none' : 'table-row';
  arrow.textContent = open ? '▶' : '▼';
}

function formatJson(str) {
  try { return JSON.stringify(JSON.parse(str), null, 2); }
  catch { return str; }
}

// ── Render queries table
function renderQueries(queries) {
  const max = queries.length ? queries[0].count : 1;
  document.getElementById('q-body').innerHTML = queries.map((q,i) => `<tr>
    <td style="color:var(--muted)">${i+1}</td>
    <td>${q.query}</td>
    <td style="text-align:right;color:var(--accent);font-weight:600">${q.count}</td>
    <td>
      <div class="q-bar-bg"><div class="q-bar" style="width:${Math.round(q.count/max*100)}%"></div></div>
    </td>
  </tr>`).join('');
}

// ── Main load
async function load() {
  const hours = document.getElementById('hours-sel').value;
  document.getElementById('status').textContent = 'Loading…';

  try {
    const res  = await fetch('/api/stats?hours=' + hours);
    const data = await res.json();

    renderCards(data.overview);
    renderCharts(data);
    renderSessions(data.sessions);
    renderErrors(data.errors);
    renderQueries(data.top_queries);

    document.getElementById('loading').style.display = 'none';
    document.getElementById('app').style.display     = 'block';
  } catch(e) {
    document.getElementById('loading').innerHTML = '❌ Failed to load: ' + e.message;
  }
}

load();
setInterval(load, 60000);
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7777)
