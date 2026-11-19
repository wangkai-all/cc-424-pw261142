"""Live analysis over the ACTIVE hit sets.

Principles enforced here:
  * per-rule counts AND the deduplicated union are shown side by side;
    the naive sum of per-rule counts is computed only to be displayed as
    an explicitly-invalid total (records hit by several rules)
  * exact numbers come from full hit detail via exact compressed sets
    (Roaring); intersections are traceable back to record ids
  * HyperLogLog gives an APPROXIMATE union estimate, always labelled as
    such, never mixed into exact figures; it cannot support deletion, so
    after a legitimate deletion it is rebuilt and flagged
"""
from __future__ import annotations

import itertools
import json
import time

from .hll import HLL
from .roaring import Roaring
from .store import Store

MAX_COMBO_RULES = 12  # 2^k combination explosion guard


def compute(store: Store) -> dict:
    rules = store.active_rules()
    iv = store.input_version
    sets: dict[str, Roaring] = {}
    rule_infos = []
    for r in rules:
        rid = r["rule_id"]
        state = store.rule_run_state(rid, r["version"])
        s = Roaring(store.active_hit_ids(rid)) if state == "reported" else Roaring()
        sets[rid] = s
        rule_infos.append({
            "rule_id": rid, "name": r["name"], "version": r["version"],
            "run_state": state,  # reported | pending (never ran / retracted)
            "exact_count": len(s) if state == "reported" else None,
        })

    fresh = [i["rule_id"] for i in rule_infos if i["run_state"] == "reported"]

    union = Roaring()
    for rid in fresh:
        union = union | sets[rid]
    naive_sum = sum(len(sets[rid]) for rid in fresh)
    union_exact = len(union)

    # exact-only membership combinations (a record's rule signature)
    combos: dict[tuple, int] = {}
    for rec_id in union:
        sig = tuple(rid for rid in fresh if rec_id in sets[rid])
        combos[sig] = combos.get(sig, 0) + 1
    combinations = [
        {"rules": list(sig), "exact_count": n}
        for sig, n in sorted(combos.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    pairwise = []
    for a, b in itertools.combinations(fresh, 2):
        inter = len(sets[a] & sets[b])
        if inter:
            pairwise.append({"a": a, "b": b, "intersection_exact": inter})

    # APPROXIMATE union cardinality via HLL — labelled, never exact.
    hll = HLL()
    for rec_id in union:
        hll.add(rec_id)
    hll_est = hll.estimate()
    # deletion flag: HLL cannot remove members, so a legitimate deletion
    # forces a rebuild from the exact source of truth (done above, every
    # compute); report that a rebuild was required, then clear the flag
    hll_stale = _has_meta(store, "hll_stale") and store._meta("hll_stale") == "1"
    if hll_stale:
        store._set_meta("hll_stale", "0")
        store.db.commit()

    # compression trade-off: raw id list vs exact compressed Roaring bytes
    raw_bytes = sum(len(json.dumps(sorted(sets[r]))) for r in fresh)
    roar_bytes = sum(len(sets[r].to_bytes()) for r in fresh)

    return {
        "generated_at": time.time(),
        "input_version": iv,
        "rules": rule_infos,
        "overall": {
            "naive_sum_invalid_total": naive_sum,
            "union_exact": union_exact,
            "double_counted": naive_sum - union_exact,
            "union_hll_estimate": round(hll_est, 1),
            "hll_relative_error": round(hll.rel_error, 5),
            "hll_rebuilt_after_deletion": hll_stale,
            "estimate_is_approximate": True,
        },
        "combinations": combinations,
        "pairwise": pairwise,
        "compression": {
            "raw_id_list_bytes": raw_bytes,
            "roaring_exact_bytes": roar_bytes,
            "ratio": round(roar_bytes / raw_bytes, 3) if raw_bytes else None,
            "note": "Roaring 为精确压缩：支持交集追溯与合法删除；HLL 为近似：不可删除、不可枚举成员",
        },
    }


def _has_meta(store: Store, k: str) -> bool:
    row = store.db.execute("SELECT 1 FROM meta WHERE k=?", (k,)).fetchone()
    return row is not None


def intersection_ids(store: Store, rule_ids: list[str]) -> list[int]:
    """Trace an overlap combination back to concrete record ids (exact)."""
    acc = None
    for rid in rule_ids:
        s = Roaring(store.active_hit_ids(rid))
        acc = s if acc is None else acc & s
    return sorted(acc) if acc else []
