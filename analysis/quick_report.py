"""
Quick report: real user-queries on MoSPI MCP over a date range.

Reads a Jaeger trace archive (JSONL, one trace per line) and prints a
plain-text summary: totals, top datasets, most-asked questions, daily
activity. Filters out automated probes (health check / ping / test).

Examples:
    python3 quick_report.py                                  # all time
    python3 quick_report.py --since 7                        # last 7 days (IST)
    python3 quick_report.py --start 2026-03-25
    python3 quick_report.py --start 2026-03-25 --end 2026-04-07
    python3 quick_report.py --data /path/to/traces.jsonl
    python3 quick_report.py --since 30 --top 15
    python3 quick_report.py --since 7 --json
"""
import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ_IST = timezone(timedelta(hours=5, minutes=30))
EXCLUDED = {"health check", "healthcheck", "ping", "test"}


def parse_args():
    p = argparse.ArgumentParser(
        description="Print a basic engagement report from a Jaeger JSONL archive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Default to local archive when run from project root, server archive when not.
    default_data = "archive/traces.jsonl"
    if not Path(default_data).exists() and Path(os.path.expanduser("~/observability/archive/traces.jsonl")).exists():
        default_data = os.path.expanduser("~/observability/archive/traces.jsonl")
    p.add_argument("--data", default=default_data,
                   help=f"Path to traces.jsonl (default: {default_data})")
    p.add_argument("--start", help="Start date YYYY-MM-DD, IST, inclusive.")
    p.add_argument("--end",   help="End date YYYY-MM-DD, IST, inclusive.")
    p.add_argument("--since", type=int, metavar="N",
                   help="Look at the last N days (IST), inclusive of today.")
    p.add_argument("--top",   type=int, default=10, metavar="N",
                   help="Items to show in top lists (default 10).")
    p.add_argument("--json",  action="store_true",
                   help="Emit JSON instead of formatted text.")
    return p.parse_args()


def resolve_window(args):
    today = datetime.now(TZ_IST).date()
    if args.since:
        start = today - timedelta(days=args.since - 1)
        end   = today
    else:
        start = date.fromisoformat(args.start) if args.start else None
        end   = date.fromisoformat(args.end)   if args.end   else today
    return start, end


def load_queries(path: Path, start_date, end_date):
    """Returns list of session dicts (one per trace with a real user_query)."""
    if start_date is not None:
        start_us = int(datetime.combine(start_date, datetime.min.time(), TZ_IST).timestamp() * 1_000_000)
    else:
        start_us = 0
    end_us = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), TZ_IST).timestamp() * 1_000_000)

    by_trace = {}  # traceID -> (earliest_ts_us, dataset, query)
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line: continue
            t = json.loads(line)
            for s in t["spans"]:
                if not s["operationName"].startswith("tool."):
                    continue
                ts = s["startTime"]
                if not (start_us <= ts < end_us):
                    continue
                tags = {tg["key"]: tg["value"] for tg in s["tags"]}
                try:    inp = json.loads(tags.get("tool.input", "{}"))
                except Exception: inp = {}
                uq = (inp.get("user_query") or "").strip()
                if not uq or uq.lower() in EXCLUDED:
                    continue
                tid = t["traceID"]
                existing = by_trace.get(tid)
                if existing is None or ts < existing[0]:
                    by_trace[tid] = (ts, str(inp.get("dataset") or "").upper(), uq)

    sessions = []
    for tid, (ts, ds, uq) in by_trace.items():
        d_ist = datetime.fromtimestamp(ts/1_000_000, tz=timezone.utc).astimezone(TZ_IST).date()
        sessions.append({"trace_id": tid, "ts_us": ts, "date": d_ist, "dataset": ds, "query": uq})
    sessions.sort(key=lambda s: s["ts_us"])
    return sessions


def compute_metrics(sessions, start_date, end_date):
    per_day  = Counter(s["date"] for s in sessions)
    datasets = Counter(s["dataset"] for s in sessions if s["dataset"])
    repeats  = Counter(s["query"].strip().lower() for s in sessions)
    counts   = sorted(per_day.values(), reverse=True)

    if start_date is None:
        # Anchor to the first observed activity if no explicit start given
        start_date = min(per_day) if per_day else end_date

    window_days = (end_date - start_date).days + 1
    active_days = len(per_day)

    metrics = {
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat(), "days": window_days},
        "total_queries":       len(sessions),
        "distinct_questions":  len({s["query"].strip().lower() for s in sessions}),
        "datasets_reached":    len(datasets),
        "active_days":         active_days,
        "avg_active":          statistics.mean(counts) if counts else 0.0,
        "median_active":       statistics.median(counts) if counts else 0.0,
        "good_day_avg":        statistics.mean(counts[:max(len(counts)//2, 1)]) if counts else 0.0,
        "best_day":            (max(per_day.items(), key=lambda kv: kv[1])[0].isoformat(),
                                max(counts)) if counts else (None, 0),
        "per_day":             {d.isoformat(): n for d, n in sorted(per_day.items())},
        "top_datasets":        datasets.most_common(),
        "top_repeated":        [(q, n) for q, n in repeats.most_common() if n > 1],
    }
    return metrics, start_date


def render_text(metrics, sessions, top_n):
    out = []
    push = out.append
    w = metrics["window"]
    push("Claude Queries on MoSPI MCP")
    push(f"{w['start']} to {w['end']}  ({w['days']} days)")
    push("")
    if metrics["total_queries"] == 0:
        push("No real user queries in this window.")
        return "\n".join(out)

    push(f"  Total real user queries        {metrics['total_queries']:>6}")
    push(f"  Distinct questions             {metrics['distinct_questions']:>6}")
    push(f"  Datasets reached               {metrics['datasets_reached']:>6}")
    push(f"  Active days                    {metrics['active_days']:>6} of {w['days']}")
    push(f"  Average / active day           {metrics['avg_active']:>6.1f}")
    push(f"  Median / active day            {metrics['median_active']:>6.1f}")
    push(f"  Good-day average (top half)    {metrics['good_day_avg']:>6.1f}")
    if metrics["best_day"][0]:
        push(f"  Best day                       {metrics['best_day'][1]:>6}   ({metrics['best_day'][0]})")
    push("")

    # Top datasets bar chart
    datasets = metrics["top_datasets"][:top_n]
    if datasets:
        push(f"Top {len(datasets)} datasets")
        vmax = max(n for _, n in datasets)
        for ds, n in datasets:
            bar = "#" * int(n / vmax * 38) or "#"
            push(f"  {ds:<10}  {n:>4}  {bar}")
        push("")

    # Most-asked questions (only those asked > 1x)
    repeats = metrics["top_repeated"][:top_n]
    if repeats:
        push("Most-asked questions")
        # Look up an original-cased instance to display
        for qlow, n in repeats:
            example = next(s["query"] for s in sessions if s["query"].strip().lower() == qlow)
            push(f"  {n:>3}  {example[:80]}")
        push("")

    # Daily activity bar
    per_day = metrics["per_day"]
    if per_day:
        push("Daily activity")
        start = date.fromisoformat(w["start"])
        end   = date.fromisoformat(w["end"])
        vmax  = max(per_day.values()) or 1
        cur = start
        while cur <= end:
            c = per_day.get(cur.isoformat(), 0)
            bar = "#" * int(c / vmax * 40) if c > 0 else ""
            push(f"  {cur.isoformat()}  {c:>4}  {bar}")
            cur += timedelta(days=1)
        push("")

    return "\n".join(out)


def main():
    args = parse_args()
    start_date, end_date = resolve_window(args)

    path = Path(args.data)
    if not path.exists():
        print(f"ERROR: data file not found: {path}", file=sys.stderr)
        sys.exit(1)

    sessions = load_queries(path, start_date, end_date)
    metrics, resolved_start = compute_metrics(sessions, start_date, end_date)

    if args.json:
        # Strip non-JSON-friendly tuple
        m = dict(metrics)
        m["best_day"] = {"date": metrics["best_day"][0], "count": metrics["best_day"][1]}
        print(json.dumps(m, indent=2))
    else:
        print(render_text(metrics, sessions, args.top))


if __name__ == "__main__":
    main()
