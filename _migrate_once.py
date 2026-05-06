"""
One-shot migration: all_traces.json -> archive/traces.jsonl

Safety properties:
- all_traces.json is opened read-only and never overwritten.
- Output written to archive/traces.jsonl.tmp first, fsync'd, then verified
  against the source by comparing parsed dicts trace-by-trace.
- Only after verification does the temp file get atomically renamed.
- Aborts loudly if archive/traces.jsonl already exists.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC  = HERE / "all_traces.json"
DST_DIR = HERE / "archive"
DST  = DST_DIR / "traces.jsonl"
TMP  = DST_DIR / "traces.jsonl.tmp"


def die(msg: str, code: int = 1) -> None:
    print(f"ABORT: {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    if not SRC.exists():
        die(f"{SRC} missing")
    DST_DIR.mkdir(exist_ok=True)
    if DST.exists():
        die(f"{DST} already exists ({DST.stat().st_size:,} bytes). Refusing to overwrite.")
    if TMP.exists():
        TMP.unlink()

    print(f"[1/5] Loading {SRC} ({SRC.stat().st_size:,} bytes)...")
    with SRC.open("rb") as f:
        src_bytes = f.read()
    src_data = json.loads(src_bytes)
    if not isinstance(src_data, list):
        die(f"source is not a JSON array (got {type(src_data).__name__})")
    print(f"      parsed {len(src_data):,} traces")

    print("[2/5] Building source ID set + per-trace hashes...")
    src_ids = []
    src_hashes = {}
    for i, t in enumerate(src_data):
        tid = t.get("traceID")
        if not tid:
            die(f"trace #{i} missing traceID")
        src_ids.append(tid)
        h = hashlib.sha256(json.dumps(t, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        src_hashes[tid] = h
    if len(src_ids) != len(set(src_ids)):
        from collections import Counter
        dups = [tid for tid, n in Counter(src_ids).items() if n > 1]
        die(f"source has {len(dups)} duplicate trace IDs (e.g. {dups[:3]})")
    print(f"      {len(src_ids):,} unique IDs, hashes computed")

    print(f"[3/5] Writing {TMP}...")
    with TMP.open("w") as f:
        for t in src_data:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print(f"      wrote {TMP.stat().st_size:,} bytes")

    print("[4/5] Verifying every line of temp file vs source...")
    seen_ids = set()
    line_no = 0
    with TMP.open("r") as f:
        for line_no, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line:
                die(f"empty line at {line_no}")
            try:
                t = json.loads(line)
            except Exception as e:
                die(f"line {line_no} not valid JSON: {e}")
            tid = t.get("traceID")
            if not tid:
                die(f"line {line_no} missing traceID")
            if tid in seen_ids:
                die(f"duplicate traceID at line {line_no}: {tid}")
            seen_ids.add(tid)
            h = hashlib.sha256(json.dumps(t, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if h != src_hashes.get(tid):
                die(f"hash mismatch at line {line_no} (traceID {tid})")
    if line_no != len(src_data):
        die(f"line count {line_no} != source trace count {len(src_data)}")
    if seen_ids != set(src_ids):
        missing = set(src_ids) - seen_ids
        extra   = seen_ids - set(src_ids)
        die(f"ID set mismatch. missing={len(missing)} extra={len(extra)}")
    print(f"      verified {line_no:,} lines, all hashes match")

    print(f"[5/5] Atomic rename {TMP.name} -> {DST.name}")
    os.rename(TMP, DST)
    print(f"      OK: {DST} ({DST.stat().st_size:,} bytes)")
    print(f"      Source preserved: {SRC} still {SRC.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
