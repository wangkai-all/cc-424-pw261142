"""Hit detail store (SQLite). Shared by three independent components:

  * rule executors   -> report runs of hit details
  * analysis/publisher -> read active hits, stage/commit snapshots
  * web server       -> query + trigger

Epoch & retraction model
------------------------
Every run is stamped with (rule_version, input_version). A run's hits
contribute to analysis only while the run is in state 'reported'.

  * rule edited   -> rule_version bumps, old runs are RETRACTED
                     (their contribution is withdrawn immediately)
  * input revised -> input_version bumps, ALL runs are RETRACTED
  * a run reporting with an older (rule_version, input_version) than
    current is rejected as STALE — a late executor cannot corrupt the
    current analysis
  * legitimate deletion removes the record's hit details from ALL runs
    (full detail is the source of truth; compressed sets are rederived)

Full hit detail rows are never rewritten by retraction — only run state
changes — so history stays auditable.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS rules(
  rule_id TEXT, version INT, name TEXT, definition TEXT,
  status TEXT, PRIMARY KEY(rule_id, version));
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, rule_id TEXT, rule_version INT, input_version INT,
  executor_id TEXT, state TEXT, reported_at REAL);
CREATE TABLE IF NOT EXISTS hits(
  run_id TEXT, record_id INT, PRIMARY KEY(run_id, record_id));
CREATE TABLE IF NOT EXISTS records(
  record_id INT PRIMARY KEY, payload TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS snapshots(
  snapshot_id TEXT PRIMARY KEY, status TEXT, created_at REAL,
  published_at REAL, payload TEXT);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('input_version','1')")
        self.db.commit()

    # -- helpers ----------------------------------------------------------
    def _meta(self, k: str) -> str:
        return self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()[0]

    def _set_meta(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))

    @property
    def input_version(self) -> int:
        return int(self._meta("input_version"))

    def event(self, kind: str, message: str) -> None:
        self.db.execute("INSERT INTO events VALUES(NULL,?,?,?)",
                        (time.time(), kind, message))
        self.db.commit()

    def events(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- records ----------------------------------------------------------
    def load_records(self) -> list[dict]:
        rows = self.db.execute("SELECT payload FROM records ORDER BY record_id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def replace_records(self, records: list[dict], bump_input: bool) -> None:
        with self.db:
            self.db.execute("DELETE FROM records")
            self.db.executemany(
                "INSERT INTO records VALUES(?,?)",
                [(r["id"], json.dumps(r, ensure_ascii=False)) for r in records])
            if bump_input:
                iv = self.input_version + 1
                self._set_meta("input_version", str(iv))
                n = self._retract_all_runs()
                self._event_nolock(
                    "input",
                    f"输入修订为 v{iv}：{n} 个旧运行被撤回，全部规则需重新执行")

    def delete_record(self, record_id: int) -> bool:
        """Legitimate deletion: erase the record and ALL of its hit details."""
        with self.db:
            cur = self.db.execute("DELETE FROM records WHERE record_id=?", (record_id,))
            if cur.rowcount == 0:
                return False
            hits = self.db.execute(
                "DELETE FROM hits WHERE record_id=?", (record_id,)).rowcount
            self._event_nolock(
                "deletion",
                f"合法删除记录 #{record_id}：抹除记录本体与 {hits} 条命中明细；"
                f"精确集合已同步收缩，近似结构(HLL)无法增量删除、已标记需重建")
            self._set_meta("hll_stale", "1")
        return True

    # -- rules ------------------------------------------------------------
    def active_rules(self) -> list[dict]:
        rows = self.db.execute("""
            SELECT r.* FROM rules r
            JOIN (SELECT rule_id, MAX(version) v FROM rules GROUP BY rule_id) m
              ON r.rule_id=m.rule_id AND r.version=m.v
            WHERE r.status='active' ORDER BY r.rule_id""").fetchall()
        return [dict(r) for r in rows]

    def rule_version(self, rule_id: str) -> int | None:
        row = self.db.execute(
            "SELECT MAX(version) v FROM rules WHERE rule_id=? AND status='active'",
            (rule_id,)).fetchone()
        return row["v"]

    def upsert_rule(self, rule_id: str, name: str, definition: dict) -> int:
        """Insert or edit a rule. Editing bumps the version and retracts the
        old runs — their hit contribution is withdrawn at once."""
        with self.db:
            cur_v = self.rule_version(rule_id)
            new_v = (cur_v or 0) + 1
            self.db.execute(
                "INSERT INTO rules VALUES(?,?,?,?, 'active')",
                (rule_id, new_v, name, json.dumps(definition, ensure_ascii=False)))
            n = 0
            if cur_v is not None:
                n = self._retract_rule_runs(rule_id)
            self._event_nolock(
                "rule",
                f"规则 {rule_id} 更新到 v{new_v}：撤回 {n} 个旧运行的命中贡献，"
                f"等待执行器按新版本重算")
            return new_v

    def retire_rule(self, rule_id: str) -> None:
        with self.db:
            row = self.db.execute(
                "SELECT MAX(version) v FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
            if row and row["v"] is not None:
                self.db.execute(
                    "UPDATE rules SET status='retired' WHERE rule_id=? AND version=?",
                    (rule_id, row["v"]))
                n = self._retract_rule_runs(rule_id)
                self._event_nolock(
                    "rule", f"规则 {rule_id} 已删除：撤回 {n} 个运行的全部命中贡献")

    # -- runs / hits ------------------------------------------------------
    def _retract_rule_runs(self, rule_id: str) -> int:
        cur = self.db.execute(
            "UPDATE runs SET state='retracted' WHERE rule_id=? AND state='reported'",
            (rule_id,))
        return cur.rowcount

    def _retract_all_runs(self) -> int:
        cur = self.db.execute(
            "UPDATE runs SET state='retracted' WHERE state='reported'")
        return cur.rowcount

    def _event_nolock(self, kind: str, message: str) -> None:
        self.db.execute("INSERT INTO events VALUES(NULL,?,?,?)",
                        (time.time(), kind, message))

    def report_run(self, rule_id: str, rule_version: int, input_version: int,
                   executor_id: str, hit_ids: list[int]) -> dict:
        """Executor reports a run. Returns the outcome the store decided."""
        with self.db:
            rv = self.rule_version(rule_id)
            iv = self.input_version
            run_id = uuid.uuid4().hex[:12]
            if rv is None:
                state = "stale"
                why = f"规则 {rule_id} 已不存在"
            elif rule_version != rv or input_version != iv:
                state = "stale"
                why = (f"迟到/过期报告被拒绝：运行基于 规则v{rule_version}/输入v{input_version}，"
                       f"当前为 规则v{rv}/输入v{iv}")
            else:
                state = "reported"
                why = "accepted"
            self.db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                (run_id, rule_id, rule_version, input_version,
                 executor_id, state, time.time()))
            if state == "reported":
                self.db.executemany(
                    "INSERT OR IGNORE INTO hits VALUES(?,?)",
                    [(run_id, h) for h in hit_ids])
                n = self.db.execute(
                    "UPDATE runs SET state='retracted' WHERE rule_id=? AND state='reported' AND run_id<>?",
                    (rule_id, run_id)).rowcount
                self._event_nolock(
                    "run",
                    f"执行器 {executor_id} 报告规则 {rule_id} v{rule_version}（输入v{input_version}）："
                    f"{len(hit_ids)} 条命中已生效，撤回 {n} 个旧运行")
            else:
                self._event_nolock("run", f"执行器 {executor_id} 的报告被标记为 stale：{why}")
            return {"run_id": run_id, "state": state, "why": why, "hits": len(hit_ids)}

    def active_hit_ids(self, rule_id: str) -> set[int]:
        rows = self.db.execute("""
            SELECT h.record_id FROM hits h
            JOIN runs r ON h.run_id = r.run_id
            WHERE r.rule_id=? AND r.state='reported'""", (rule_id,)).fetchall()
        return {row[0] for row in rows}

    def runs(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM runs ORDER BY reported_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def rule_run_state(self, rule_id: str, rule_version: int) -> str:
        row = self.db.execute("""
            SELECT state FROM runs WHERE rule_id=? AND rule_version=? AND input_version=?
            ORDER BY reported_at DESC LIMIT 1""",
            (rule_id, rule_version, self.input_version)).fetchone()
        if row is None:
            return "pending"
        return row["state"]

    # -- snapshots (publisher) -------------------------------------------
    def stage_snapshot(self, payload: dict) -> str:
        sid = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?)",
                (sid, "staging", time.time(), None,
                 json.dumps(payload, ensure_ascii=False)))
        return sid

    def commit_snapshot(self, sid: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE snapshots SET status='superseded' WHERE status='published'")
            self.db.execute(
                "UPDATE snapshots SET status='published', published_at=? WHERE snapshot_id=?",
                (time.time(), sid))
            self._event_nolock("publish", f"分析快照 {sid} 已提交发布")

    def fail_staging_snapshots(self) -> int:
        with self.db:
            cur = self.db.execute(
                "UPDATE snapshots SET status='failed' WHERE status='staging'")
            if cur.rowcount:
                self._event_nolock(
                    "publish", f"恢复：{cur.rowcount} 个中断的暂存快照被标记为 failed，已发布版本保持原样")
            return cur.rowcount

    def latest_published(self) -> dict | None:
        row = self.db.execute("""
            SELECT * FROM snapshots WHERE status='published'
            ORDER BY published_at DESC LIMIT 1""").fetchone()
        return dict(row) if row else None

    def snapshots(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT snapshot_id,status,created_at,published_at FROM snapshots "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.db.close()
