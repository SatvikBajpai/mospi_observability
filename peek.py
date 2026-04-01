"""
MoSPI MCP Session Inspector
Peeks inside Jaeger traces to show full session detail:
user query → dataset → filters → API response data in tables.

Usage:
    python inspect.py                        # list all sessions (last 24h)
    python inspect.py --hours 72             # last 72 hours
    python inspect.py --session 3            # full detail for session #3
    python inspect.py --session 3 --full     # include raw API response records
    python inspect.py --dataset CPI          # filter sessions by dataset
    python inspect.py --jaeger http://IP:16686
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from collections import defaultdict

import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.syntax import Syntax
from rich import box
from rich.columns import Columns

console = Console()

JAEGER_DEFAULT = "http://10.24.89.149:16686"
SERVICE        = "mospi-mcp-server"

STEP_ORDER = [
    "list_datasets",
    "get_indicators",
    "get_metadata",
    "get_data",
]

def step_num(tool_name: str) -> int:
    for i, s in enumerate(STEP_ORDER):
        if tool_name == s:
            return i + 1
    return 99


# ─────────────────────────────────────────────────────────────
# Jaeger API
# ─────────────────────────────────────────────────────────────

TOOL_OPERATIONS = [
    "tool.list_datasets",
    "tool.get_indicators",
    "tool.get_metadata",
    "tool.get_data",
]

def fetch_traces(jaeger: str, hours: int, limit: int = 500) -> list:
    now_us   = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    start_us = now_us - hours * 3600 * 1_000_000
    seen_ids = set()
    all_traces = []
    for op in TOOL_OPERATIONS:
        try:
            r = requests.get(
                f"{jaeger}/api/traces",
                params={"service": SERVICE, "operation": op,
                        "start": start_us, "end": now_us, "limit": limit},
                timeout=30,
            )
            r.raise_for_status()
            for trace in r.json().get("data", []):
                if trace["traceID"] not in seen_ids:
                    seen_ids.add(trace["traceID"])
                    all_traces.append(trace)
        except Exception:
            continue
    if not all_traces:
        console.print("[red]No tool traces found. Check Jaeger connection.[/red]")
        sys.exit(1)
    return all_traces


def get_tag(tags: list, key: str, default=None):
    for t in tags:
        if t["key"] == key:
            return t["value"]
    return default


# ─────────────────────────────────────────────────────────────
# Session Extraction
# ─────────────────────────────────────────────────────────────

def extract_sessions(traces: list) -> list:
    """
    Each trace with ≥1 tool.* span = one session.
    Returns list of session dicts sorted by time ascending.
    """
    sessions = []

    for trace in traces:
        tool_spans = [
            s for s in trace["spans"]
            if s["operationName"].startswith("tool.")
        ]
        if not tool_spans:
            continue

        # Sort tool spans by start time
        tool_spans.sort(key=lambda s: s["startTime"])

        steps = []
        for span in tool_spans:
            tags       = {t["key"]: t["value"] for t in span["tags"]}
            tool_name  = tags.get("tool.name", span["operationName"].replace("tool.", ""))
            inp_raw    = tags.get("tool.input", "{}")
            out_raw    = tags.get("tool.output", "{}")
            out_size   = int(tags.get("tool.output_size", 0))
            client_ip  = tags.get("client.ip", "unknown")
            user_agent = tags.get("client.user_agent", "unknown")
            error      = tags.get("otel.status_code", "") == "ERROR" or tags.get("error", False)

            try:
                inp = json.loads(inp_raw)
            except Exception:
                inp = {}
            try:
                out = json.loads(out_raw)
            except Exception:
                out = {}

            steps.append({
                "tool":        tool_name,
                "step_num":    step_num(tool_name),
                "input":       inp,
                "output":      out,
                "output_raw":  out_raw,
                "output_size": out_size,
                "duration_ms": span["duration"] / 1000,
                "client_ip":   client_ip,
                "user_agent":  user_agent,
                "error":       error,
                "start_us":    span["startTime"],
            })

        steps.sort(key=lambda s: s["step_num"])

        # Extract key session metadata
        user_query = ""
        dataset    = ""
        filters    = {}
        api_data   = []
        api_error  = ""
        total_records = 0

        for s in steps:
            if "user_query" in s["input"] and s["input"]["user_query"]:
                user_query = s["input"]["user_query"]
            if "dataset" in s["input"] and s["input"]["dataset"]:
                dataset = s["input"]["dataset"].upper()
            if "filters" in s["input"]:
                filters = s["input"]["filters"]
            if s["step_num"] == 4:
                out = s["output"]
                if isinstance(out, dict):
                    if "data" in out and isinstance(out["data"], list):
                        api_data = out["data"]
                        total_records = out.get("meta_data", {}).get("totalRecords", len(api_data))
                    if "error" in out:
                        api_error = out["error"]

        start_dt = datetime.fromtimestamp(
            min(s["start_us"] for s in steps) / 1_000_000,
            tz=timezone.utc
        )
        total_ms = sum(s["duration_ms"] for s in steps)
        has_error = any(s["error"] for s in steps) or bool(api_error)

        sessions.append({
            "trace_id":      trace["traceID"],
            "start_dt":      start_dt,
            "steps":         steps,
            "user_query":    user_query,
            "dataset":       dataset,
            "filters":       filters,
            "api_data":      api_data,
            "api_error":     api_error,
            "total_records": total_records,
            "total_ms":      total_ms,
            "client_ip":     steps[0]["client_ip"] if steps else "unknown",
            "user_agent":    steps[0]["user_agent"] if steps else "unknown",
            "has_error":     has_error,
            "n_steps":       len(steps),
        })

    sessions.sort(key=lambda s: s["start_dt"])
    return sessions


# ─────────────────────────────────────────────────────────────
# Session List View
# ─────────────────────────────────────────────────────────────

def print_session_list(sessions: list, dataset_filter: str = ""):
    if dataset_filter:
        sessions = [s for s in sessions if s["dataset"] == dataset_filter.upper()]

    if not sessions:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    console.print(Panel(
        f"[bold cyan]MoSPI MCP — Session Inspector[/bold cyan]\n"
        f"[dim]{len(sessions)} sessions found[/dim]",
        box=box.DOUBLE_EDGE
    ))

    t = Table(box=box.SIMPLE_HEAVY, show_lines=True)
    t.add_column("#",          style="dim",    width=4,  no_wrap=True)
    t.add_column("Time (UTC)", style="cyan",   width=17, no_wrap=True)
    t.add_column("User Query", style="white",  min_width=35, max_width=55, no_wrap=True)
    t.add_column("Dataset",    style="yellow", width=8,  no_wrap=True)
    t.add_column("Steps",      justify="center", width=5, no_wrap=True)
    t.add_column("ms",         justify="right",  width=7, no_wrap=True)
    t.add_column("Status",     justify="center", width=7, no_wrap=True)
    t.add_column("Client IP",  style="dim",    width=15, no_wrap=True)

    for i, s in enumerate(sessions, 1):
        status = "[red]ERR[/red]" if s["has_error"] else "[green]OK[/green]"
        q = s["user_query"]
        query  = (q[:52] + "…") if len(q) > 52 else (q or "[dim]—[/dim]")
        t.add_row(
            str(i),
            s["start_dt"].strftime("%m-%d %H:%M:%S"),
            query,
            s["dataset"] or "[dim]—[/dim]",
            str(s["n_steps"]),
            f"{s['total_ms']:.0f}",
            status,
            s["client_ip"],
        )

    console.print(t)
    console.print(f"\n[dim]Run with [bold]--session N[/bold] to inspect a session in detail[/dim]")


# ─────────────────────────────────────────────────────────────
# Session Detail View
# ─────────────────────────────────────────────────────────────

def print_step_detail(step: dict, full: bool):
    num  = step["step_num"]
    name = step["tool"]
    dur  = step["duration_ms"]
    err  = step["error"]

    color  = "red" if err else "green"
    status = "✗ ERROR" if err else "✓"

    console.print(f"\n  [bold {color}]Step {num}: {name}[/bold {color}]  "
                  f"[dim]{dur:.0f}ms[/dim]  [{color}]{status}[/{color}]")

    # ── Input
    inp = step["input"]
    if inp:
        console.print("  [bold]Input:[/bold]")
        inp_t = Table(box=box.MINIMAL, show_header=False, padding=(0, 1))
        inp_t.add_column("key",   style="cyan")
        inp_t.add_column("value", style="white")
        for k, v in inp.items():
            val = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            if len(val) > 120:
                val = val[:120] + "…"
            inp_t.add_row(k, val)
        console.print("  ", inp_t)

    # ── Output
    out = step["output"]
    if not out:
        return

    if "error" in out:
        console.print(f"  [red]Error:[/red] {out['error'][:300]}")
        return

    # Step 1 — dataset overview
    if num == 1:
        datasets = out.get("datasets", {})
        console.print(f"  [bold]Output:[/bold] {len(datasets)} datasets available: "
                      f"[cyan]{', '.join(datasets.keys())}[/cyan]")

    # Step 2 — indicators
    elif num == 2:
        console.print(f"  [bold]Output:[/bold] Available options:")
        out_t = Table(box=box.MINIMAL, show_header=False, padding=(0,1))
        out_t.add_column("key",  style="cyan")
        out_t.add_column("vals", style="white")
        data = out.get("data", out)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    vals = ", ".join(
                        str(item.get(list(item.keys())[0], item)) if isinstance(item, dict) else str(item)
                        for item in v[:8]
                    )
                    out_t.add_row(k, vals + ("…" if len(v) > 8 else ""))
        console.print("  ", out_t)

    # Step 3 — metadata / filters available
    elif num == 3:
        data = out.get("data", out)
        console.print(f"  [bold]Output:[/bold] Filter options returned:")
        out_t = Table(box=box.MINIMAL, show_header=False, padding=(0,1))
        out_t.add_column("filter", style="cyan")
        out_t.add_column("values", style="white")
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list) and v:
                    first = v[0]
                    sample_key = list(first.keys())[0] if isinstance(first, dict) else None
                    if sample_key:
                        vals = ", ".join(str(item.get(sample_key, "")) for item in v[:6])
                    else:
                        vals = ", ".join(str(x) for x in v[:6])
                    out_t.add_row(k, vals + (f"  [dim]({len(v)} total)[/dim]" if len(v) > 6 else ""))
        console.print("  ", out_t)

    # Step 4 — API data response
    elif num == 4:
        data_rows = out.get("data", [])
        meta      = out.get("meta_data", {})
        msg       = out.get("msg", "")
        total     = meta.get("totalRecords", len(data_rows))

        console.print(f"  [bold]Output:[/bold] [green]{msg}[/green]  "
                      f"[dim]({total} total records, showing {len(data_rows)})[/dim]")

        if data_rows:
            _print_data_table(data_rows, full)


def _print_data_table(rows: list, full: bool):
    """Print API data records as a Rich table."""
    if not rows:
        return

    # Determine columns — exclude null-heavy ones unless full
    all_cols = list(rows[0].keys())
    if not full:
        # Filter out columns that are >80% null
        non_null = {
            col: sum(1 for r in rows if r.get(col) is not None and r.get(col) != "")
            for col in all_cols
        }
        cols = [c for c in all_cols if non_null.get(c, 0) > len(rows) * 0.2]
    else:
        cols = all_cols

    show_rows = rows if full else rows[:15]

    t = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    for col in cols:
        t.add_column(col, style="white", overflow="fold", max_width=20)

    for row in show_rows:
        t.add_row(*[str(row.get(c, "")) if row.get(c) is not None else "[dim]—[/dim]" for c in cols])

    console.print("  ", t)

    if not full and len(rows) > 15:
        console.print(f"  [dim]… {len(rows) - 15} more rows. Use --full to see all.[/dim]")


def print_session_detail(session: dict, full: bool):
    status_str = "[red]FAILED[/red]" if session["has_error"] else "[green]SUCCESS[/green]"

    console.print(Panel(
        f"[bold cyan]Session Detail[/bold cyan]  {status_str}\n"
        f"[dim]Trace ID: {session['trace_id']}[/dim]",
        box=box.DOUBLE_EDGE
    ))

    # ── Header metadata
    meta = Table(box=box.MINIMAL, show_header=False, padding=(0, 2))
    meta.add_column("k", style="cyan bold")
    meta.add_column("v", style="white")
    meta.add_row("Time (UTC)",  session["start_dt"].strftime("%Y-%m-%d %H:%M:%S"))
    meta.add_row("Client IP",  session["client_ip"])
    meta.add_row("User Agent", (session["user_agent"][:80] + "…") if len(session["user_agent"]) > 80 else session["user_agent"])
    meta.add_row("User Query", session["user_query"] or "[dim]not captured[/dim]")
    meta.add_row("Dataset",    session["dataset"] or "[dim]—[/dim]")
    meta.add_row("Total Time", f"{session['total_ms']:.0f}ms")
    if session["filters"]:
        meta.add_row("Filters Used", json.dumps(session["filters"], ensure_ascii=False)[:200])
    console.print(meta)

    # ── Step-by-step breakdown
    console.rule("[bold]Step-by-Step Breakdown[/bold]")
    for step in session["steps"]:
        print_step_detail(step, full)

    # ── Timing summary
    console.rule("[bold]Timing[/bold]")
    t = Table(box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Step",     style="cyan")
    t.add_column("Duration", justify="right")
    t.add_column("% of total", justify="right")
    t.add_column("Output Size", justify="right")
    total_ms = session["total_ms"] or 1
    for step in session["steps"]:
        pct = f"{step['duration_ms'] / total_ms * 100:.1f}%"
        t.add_row(
            step["tool"],
            f"{step['duration_ms']:.0f}ms",
            pct,
            f"{step['output_size'] / 1024:.1f}KB" if step["output_size"] else "—",
        )
    t.add_row("[bold]TOTAL[/bold]", f"[bold]{session['total_ms']:.0f}ms[/bold]", "100%", "")
    console.print(t)

    if session["api_error"]:
        console.print(f"\n[red bold]API Error:[/red bold] {session['api_error']}")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MoSPI MCP Session Inspector")
    parser.add_argument("--jaeger",   default=JAEGER_DEFAULT)
    parser.add_argument("--hours",    type=int, default=24)
    parser.add_argument("--limit",    type=int, default=2000)
    parser.add_argument("--session",  type=int, default=None, help="Session number to inspect")
    parser.add_argument("--dataset",  default="", help="Filter by dataset (e.g. CPI, PLFS)")
    parser.add_argument("--full",     action="store_true", help="Show all records and columns")
    args = parser.parse_args()

    console.print(f"[cyan]Fetching traces (last {args.hours}h)…[/cyan]")
    traces   = fetch_traces(args.jaeger, args.hours, args.limit)
    sessions = extract_sessions(traces)

    if args.dataset:
        sessions = [s for s in sessions if s["dataset"] == args.dataset.upper()]

    if not sessions:
        console.print("[yellow]No sessions with tool calls found.[/yellow]")
        return

    if args.session is None:
        print_session_list(sessions)
    else:
        idx = args.session - 1
        if idx < 0 or idx >= len(sessions):
            console.print(f"[red]Session {args.session} not found. Valid range: 1–{len(sessions)}[/red]")
            sys.exit(1)
        print_session_detail(sessions[idx], args.full)


if __name__ == "__main__":
    main()
