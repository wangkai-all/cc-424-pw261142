"""Orchestration CLI: init / demo / reset.

  python -m dq.cli init   --db data/dq.db --records 2000 --seed 42
  python -m dq.cli demo   --db data/dq.db --dir data/published
  python -m dq.cli reset  --db data/dq.db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import dataset as ds
from . import rules as rule_mod
from .executor import run_executor
from .publisher import publish, recover
from .store import Store


def init(db: str, n: int, seed: int) -> None:
    store = Store(db)
    store.replace_records(ds.generate(n, seed), bump_input=False)
    for r in rule_mod.DEFAULT_RULES:
        store.upsert_rule(r["rule_id"], r["name"],
                          {"kind": r["kind"], "params": r["params"]})
    store.event("init", f"初始化：{n} 条虚构记录，{len(rule_mod.DEFAULT_RULES)} 条规则")
    store.close()


def reset(db: str) -> None:
    p = Path(db)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()


def demo(db: str, out_dir: str) -> None:
    """Full headless pipeline from an empty state, with simulations."""
    reset(db)
    init(db, 2000, 42)
    print("== 执行全部规则 ==")
    for res in run_executor(db, None, "exec-all"):
        print(f"  {res['rule_id']}: {res['state']} hits={res['hits']}")
    print("== 发布分析 ==")
    print(" ", publish(db, out_dir))

    store = Store(db)
    print("== 模拟：规则修订后旧执行器迟到回报 ==")
    store.upsert_rule("age_range", "年龄越界",
                      {"kind": "range", "params": {"field": "age", "min": 18, "max": 99}})
    out = run_executor(db, "age_range", "exec-late", delay=0,
                       as_rule_version=1, as_input_version=store.input_version)
    print("  迟到报告结果:", out[0]["state"], "-", out[0]["why"])
    print("== 模拟：发布中断与恢复 ==")
    import subprocess
    r = subprocess.run(
        [sys.executable, "-m", "dq.publisher", "--db", db,
         "--dir", out_dir, "--crash"],
        capture_output=True, text=True)
    print(f"  发布进程崩溃退出码: {r.returncode}")
    print("  崩溃后已发布快照:", (store.latest_published() or {}).get("snapshot_id"))
    print("  恢复:", recover(db, out_dir))
    print("== 重新执行 + 重新发布 ==")
    run_executor(db, "age_range", "exec-2")
    print(" ", publish(db, out_dir))
    store.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dq.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--db", required=True)
    p.add_argument("--records", type=int, default=2000); p.add_argument("--seed", type=int, default=42)
    p = sub.add_parser("demo"); p.add_argument("--db", required=True); p.add_argument("--dir", required=True)
    p = sub.add_parser("reset"); p.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "init":
        init(args.db, args.records, args.seed)
    elif args.cmd == "demo":
        demo(args.db, args.dir)
    elif args.cmd == "reset":
        reset(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
