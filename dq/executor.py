"""Rule executor — an independent process.

Loads the current rules + input records from the store, evaluates real rule
logic, and reports the hit set back as one run. The store decides whether
the run is accepted, retracts older runs, or rejects the report as stale
(when this executor was late and the rule/input already moved on).

Usage:
  python -m dq.executor --db data/dq.db --rule email_format
      [--executor-id exec-1] [--delay 8]
      [--as-rule-version N] [--as-input-version N]   # simulate a late report
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import dataset as ds
from . import rules as rule_mod
from .store import Store

LOOKUPS = {"countries": ds.VALID_COUNTRIES, "plans": ds.PLANS}


def run_executor(db: str, rule_id: str | None, executor_id: str,
                 delay: float = 0.0,
                 as_rule_version: int | None = None,
                 as_input_version: int | None = None) -> list[dict]:
    store = Store(db)
    active = store.active_rules()
    targets = [r for r in active if rule_id is None or r["rule_id"] == rule_id]
    if not targets:
        print(json.dumps({"error": f"no active rule {rule_id!r}"}))
        return []
    records = store.load_records()
    definitions = [{**json.loads(r["definition"]), "rule_id": r["rule_id"]}
                   for r in targets]
    ctx = rule_mod.build_context(definitions, records, LOOKUPS)
    results = []
    for r, defn in zip(targets, definitions):
        hits = [rec["id"] for rec in records if rule_mod.evaluate(defn, rec, ctx)]
        rv = as_rule_version if as_rule_version is not None else r["version"]
        iv = as_input_version if as_input_version is not None else store.input_version
        if delay:
            time.sleep(delay)  # still "computing" while the world moves on
        outcome = store.report_run(r["rule_id"], rv, iv, executor_id, hits)
        results.append({"rule_id": r["rule_id"], **outcome})
    store.close()
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dq.executor")
    ap.add_argument("--db", required=True)
    ap.add_argument("--rule", default=None)
    ap.add_argument("--executor-id", default="exec-1")
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--as-rule-version", type=int, default=None)
    ap.add_argument("--as-input-version", type=int, default=None)
    args = ap.parse_args(argv)
    results = run_executor(args.db, args.rule, args.executor_id, args.delay,
                           args.as_rule_version, args.as_input_version)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
