"""
Incremental Jaeger trace archiver.

Append-only archive at archive/traces.jsonl (one trace per line).
Designed to be safe to invoke on any cadence (cron / launchd).
Will not run two copies concurrently (fcntl.flock on archive/.lock).

Per-run flow:
  1. Acquire exclusive lock on archive/.lock (skip if another copy holds it).
  2. Load existing trace IDs from archive/traces.jsonl into memory.
  3. For each tool operation, query Jaeger for the last LOOKBACK_HOURS window.
  4. Append only traces whose IDs aren't already in the archive.
  5. Update archive/state.json with run metadata for monitoring.

Configuration via env vars:
  JAEGER_URL       (default http://10.24.89.149:16686)
  JAEGER_SERVICE   (default mospi-mcp-server)
  LOOKBACK_HOURS   (default 24)
  PER_OP_LIMIT     (default 2000)
  HTTP_TIMEOUT     (default 120)
"""
import errno
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

JAEGER         = os.environ.get("JAEGER_URL", "http://10.24.89.149:16686")
SERVICE        = os.environ.get("JAEGER_SERVICE", "mospi-mcp-server")
OPERATIONS     = [
    "tool.list_datasets",
    "tool.get_indicators",
    "tool.get_metadata",
    "tool.get_data",
]
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
PER_OP_LIMIT   = int(os.environ.get("PER_OP_LIMIT", "2000"))
HTTP_TIMEOUT   = int(os.environ.get("HTTP_TIMEOUT", "120"))

HERE        = Path(__file__).resolve().parent
ARCHIVE_DIR = HERE / "archive"
JSONL_PATH  = ARCHIVE_DIR / "traces.jsonl"
LOCK_PATH   = ARCHIVE_DIR / ".lock"
STATE_PATH  = ARCHIVE_DIR / "state.json"
LOG_PATH    = ARCHIVE_DIR / "archive.log"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}  {msg}"
    print(line, flush=True)
    ARCHIVE_DIR.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def load_seen_ids() -> set:
    seen = set()
    if not JSONL_PATH.exists():
        return seen
    with JSONL_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                seen.add(json.loads(line)["traceID"])
            except Exception:
                continue
    return seen


def fetch_recent(hours: int) -> list:
    now_us   = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    start_us = now_us - hours * 3600 * 1_000_000
    out = []
    seen_in_batch = set()
    per_op_counts = {}
    for op in OPERATIONS:
        try:
            r = requests.get(
                f"{JAEGER}/api/traces",
                params={
                    "service":   SERVICE,
                    "operation": op,
                    "start":     start_us,
                    "end":       now_us,
                    "limit":     PER_OP_LIMIT,
                },
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            traces = r.json().get("data", [])
            per_op_counts[op] = len(traces)
            for trace in traces:
                tid = trace.get("traceID")
                if not tid or tid in seen_in_batch:
                    continue
                seen_in_batch.add(tid)
                out.append(trace)
        except Exception as e:
            per_op_counts[op] = f"ERR: {e}"
            log(f"WARN  fetch {op} failed: {e}")
    log(f"      per-op: {per_op_counts}")
    return out


def append_traces(new_traces: list) -> int:
    if not new_traces:
        return 0
    ARCHIVE_DIR.mkdir(exist_ok=True)
    with JSONL_PATH.open("a") as f:
        for trace in new_traces:
            f.write(json.dumps(trace, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return len(new_traces)


def write_state(payload: dict) -> None:
    ARCHIVE_DIR.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2)
    os.rename(tmp, STATE_PATH)


def acquire_lock():
    ARCHIVE_DIR.mkdir(exist_ok=True)
    fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return None
        raise
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def main() -> int:
    started = time.time()
    lock = acquire_lock()
    if lock is None:
        log("SKIP   another archiver run already in progress")
        return 0

    try:
        seen_before = load_seen_ids()
        log(f"START  existing={len(seen_before)} traces  lookback={LOOKBACK_HOURS}h  jaeger={JAEGER}")

        fetched = fetch_recent(LOOKBACK_HOURS)
        new = [t for t in fetched if t["traceID"] not in seen_before]
        added = append_traces(new)

        elapsed = time.time() - started
        log(f"DONE   fetched={len(fetched)}  new={added}  total={len(seen_before)+added}  elapsed={elapsed:.1f}s")

        write_state({
            "last_run_utc":   datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "fetched":        len(fetched),
            "new":            added,
            "archive_total":  len(seen_before) + added,
            "lookback_hours": LOOKBACK_HOURS,
            "jaeger_url":     JAEGER,
            "service":        SERVICE,
        })
        return 0
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
