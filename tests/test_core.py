"""Core correctness tests: exact compressed sets, approximate sketches,
retraction epochs, late reports, deletion, publish crash recovery."""
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from dq import analysis, dataset as ds, rules as rule_mod
from dq.cli import init
from dq.executor import run_executor
from dq.hll import HLL
from dq.publisher import publish, recover
from dq.roaring import Roaring
from dq.store import Store


class TestRoaring(unittest.TestCase):
    def test_fuzz_against_python_sets(self):
        rng = random.Random(7)
        for _ in range(30):
            a = {rng.randrange(0, 300_000) for _ in range(rng.randrange(0, 3000))}
            b = {rng.randrange(0, 300_000) for _ in range(rng.randrange(0, 3000))}
            ra, rb = Roaring(a), Roaring(b)
            self.assertEqual(set(ra), a)
            self.assertEqual(len(ra), len(a))
            self.assertEqual(set(ra & rb), a & b)
            self.assertEqual(set(ra | rb), a | b)
            self.assertEqual(set(ra - rb), a - b)
            for v in rng.sample(list(a | b), min(50, len(a | b))):
                self.assertEqual(v in ra, v in a)
            # deletion
            victim = rng.choice(list(a)) if a else 0
            ra.discard(victim)
            self.assertNotIn(victim, ra)
            self.assertEqual(len(ra), len(a) - (1 if victim in a else 0))

    def test_serialization_roundtrip(self):
        rng = random.Random(3)
        vals = {rng.randrange(0, 1_000_000) for _ in range(20000)}  # forces bitmap containers
        r = Roaring(vals)
        self.assertEqual(set(Roaring.from_bytes(r.to_bytes())), vals)
        self.assertEqual(set(Roaring.from_b64(r.to_b64())), vals)
        self.assertLess(len(r.to_bytes()), 8 * len(vals))  # actually compressed


class TestHLL(unittest.TestCase):
    def test_estimate_within_bounds(self):
        h = HLL()
        n = 20000
        for i in range(n):
            h.add(i)
        est = h.estimate()
        self.assertAlmostEqual(est, n, delta=n * 3 * h.rel_error)
        self.assertNotEqual(est, float(n))  # approximate, never masquerades as exact


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "dq.db")
        init(self.db, 400, 1)
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp)

    def test_retraction_on_rule_edit(self):
        run_executor(self.db, None, "e1")
        before = len(self.store.active_hit_ids("age_range"))
        self.assertGreater(before, 0)
        # edit the rule -> old contribution withdrawn, state pending
        self.store.upsert_rule("age_range", "年龄越界",
                               {"kind": "range", "params": {"field": "age", "min": 18, "max": 99}})
        self.assertEqual(self.store.active_hit_ids("age_range"), set())
        self.assertEqual(self.store.rule_run_state("age_range", 2), "pending")
        # re-execute at v2 -> fresh contribution
        run_executor(self.db, "age_range", "e2")
        self.assertEqual(self.store.rule_run_state("age_range", 2), "reported")
        self.assertGreater(len(self.store.active_hit_ids("age_range")), 0)

    def test_late_report_rejected(self):
        run_executor(self.db, None, "e1")
        iv = self.store.input_version
        self.store.upsert_rule("age_range", "年龄越界",
                               {"kind": "range", "params": {"field": "age", "min": 18, "max": 99}})
        out = run_executor(self.db, "age_range", "late",
                           as_rule_version=1, as_input_version=iv)[0]
        self.assertEqual(out["state"], "stale")
        self.assertEqual(self.store.active_hit_ids("age_range"), set())

    def test_input_revision_retracts_everything(self):
        run_executor(self.db, None, "e1")
        recs = ds.revise(self.store.load_records(), seed=9)
        self.store.replace_records(recs, bump_input=True)
        for r in self.store.active_rules():
            self.assertEqual(self.store.active_hit_ids(r["rule_id"]), set())
        # stale report under old input version is rejected
        out = run_executor(self.db, "email_format", "late",
                           as_rule_version=1, as_input_version=1)[0]
        self.assertEqual(out["state"], "stale")

    def test_legitimate_deletion(self):
        run_executor(self.db, None, "e1")
        hits = self.store.active_hit_ids("email_format")
        victim = next(iter(hits))
        self.assertTrue(self.store.delete_record(victim))
        self.assertNotIn(victim, self.store.active_hit_ids("email_format"))
        n = self.store.db.execute(
            "SELECT COUNT(*) c FROM hits WHERE record_id=?", (victim,)).fetchone()["c"]
        self.assertEqual(n, 0)  # detail rows actually erased, not just hidden

    def test_analysis_union_not_sum(self):
        run_executor(self.db, None, "e1")
        payload = analysis.compute(self.store)
        ov = payload["overall"]
        self.assertGreater(ov["naive_sum_invalid_total"], ov["union_exact"])
        # brute-force union from raw detail
        all_ids = set()
        for r in self.store.active_rules():
            all_ids |= self.store.active_hit_ids(r["rule_id"])
        self.assertEqual(ov["union_exact"], len(all_ids))
        # combination counts partition the union exactly
        self.assertEqual(sum(c["exact_count"] for c in payload["combinations"]),
                         ov["union_exact"])
        # hll estimate is close but the payload flags it as approximate
        self.assertTrue(ov["estimate_is_approximate"])
        self.assertAlmostEqual(ov["union_hll_estimate"], ov["union_exact"],
                               delta=max(5, ov["union_exact"] * 0.05))

    def test_publish_crash_and_recovery(self):
        run_executor(self.db, None, "e1")
        out_dir = str(self.tmp / "pub")
        first = publish(self.db, out_dir)
        self.assertTrue((Path(out_dir) / "current.json").exists())
        # crash mid-publish: process dies, staging left behind
        import subprocess, sys
        r = subprocess.run(
            [sys.executable, "-m", "dq.publisher", "--db", self.db,
             "--dir", out_dir, "--crash"],
            capture_output=True, cwd=Path(__file__).resolve().parent.parent)
        self.assertNotEqual(r.returncode, 0)
        # published snapshot is still the first one
        self.assertEqual(self.store.latest_published()["snapshot_id"],
                         first["snapshot_id"])
        rec = recover(self.db, out_dir)
        self.assertEqual(rec["failed_staging"], 1)
        self.assertEqual(rec["published_intact"], first["snapshot_id"])
        # next publish succeeds
        second = publish(self.db, out_dir)
        self.assertEqual(self.store.latest_published()["snapshot_id"],
                         second["snapshot_id"])

    def test_intersection_tracing(self):
        run_executor(self.db, None, "e1")
        ids = analysis.intersection_ids(self.store, ["email_format", "phone_missing"])
        expect = (self.store.active_hit_ids("email_format")
                  & self.store.active_hit_ids("phone_missing"))
        self.assertEqual(set(ids), expect)


if __name__ == "__main__":
    unittest.main()
