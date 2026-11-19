"""Analysis publisher — an independent process.

Two-phase publish with atomic commit and crash recovery:

  1. STAGE: compute the analysis, insert a 'staging' snapshot row and write
     the payload to next.json.tmp -> fsync -> rename to next.json
  2. COMMIT: atomically rename next.json -> current.json and mark the
     snapshot 'published' (previous published becomes 'superseded')

`--crash` kills the process (os._exit) between the phases, simulating a
publish interruption: the last published snapshot stays intact, and the
next run (or `--recover`) marks the orphaned staging snapshot failed.

Usage:
  python -m dq.publisher --db data/dq.db --dir data/published [--crash]
  python -m dq.publisher --db data/dq.db --dir data/published --recover
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import analysis
from .store import Store


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def publish(db: str, out_dir: str, crash: bool = False) -> dict:
    store = Store(db)
    store.fail_staging_snapshots()  # recover leftovers from a previous crash
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    payload = analysis.compute(store)
    sid = store.stage_snapshot(payload)
    _atomic_write(out / "next.json", json.dumps(payload, ensure_ascii=False, indent=2))
    store.event("publish", f"快照 {sid} 已暂存（staging），等待提交")

    if crash:
        store.event("publish", f"快照 {sid} 暂存后进程崩溃，提交未完成")
        store.close()
        os._exit(2)  # hard crash: no cleanup, staging row left behind

    os.replace(out / "next.json", out / "current.json")
    store.commit_snapshot(sid)
    store.close()
    return {"snapshot_id": sid, "status": "published"}


def recover(db: str, out_dir: str) -> dict:
    store = Store(db)
    failed = store.fail_staging_snapshots()
    leftover = Path(out_dir) / "next.json"
    if leftover.exists():
        leftover.unlink()
    pub = store.latest_published()
    store.close()
    return {"failed_staging": failed,
            "published_intact": pub["snapshot_id"] if pub else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dq.publisher")
    ap.add_argument("--db", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--crash", action="store_true")
    ap.add_argument("--recover", action="store_true")
    args = ap.parse_args(argv)
    if args.recover:
        print(json.dumps(recover(args.db, args.dir), ensure_ascii=False))
        return 0
    print(json.dumps(publish(args.db, args.dir, args.crash), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
