"""Analysis/query web endpoint + UI host.

The server itself does NOT execute rules or publish — those stay
independent processes (dq.executor / dq.publisher) spawned per request,
coordinating only through the store. The server reads the store, serves
live analysis, and exposes the simulation controls.

  python -m dq.server --db data/dq.db --dir data/published --port 8000
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"

from . import analysis, dataset as ds
from .cli import init as cli_init, reset as cli_reset
from .executor import LOOKUPS
from .rules import KINDS
from .store import Store

CTX = {"db": "data/dq.db", "dir": "data/published"}


def store() -> Store:
    return Store(CTX["db"])


def spawn(module: str, *args: str) -> None:
    """Fire an independent component process; results arrive via the store."""
    subprocess.Popen([sys.executable, "-m", module, "--db", CTX["db"], *args],
                     cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_initialized() -> None:
    if not Path(CTX["db"]).exists():
        cli_init(CTX["db"], 2000, 42)
        spawn("dq.executor", "--executor-id", "exec-boot")
        # wait briefly so first page load has data
        deadline = time.time() + 15
        s = store()
        while time.time() < deadline:
            if all(s.rule_run_state(r["rule_id"], r["version"]) == "reported"
                   for r in s.active_rules()):
                break
            time.sleep(0.2)
        s.close()
        spawn("dq.publisher", "--dir", CTX["dir"])


def live_state() -> dict:
    s = store()
    try:
        payload = analysis.compute(s)
        rules = []
        info_by_id = {i["rule_id"]: i for i in payload["rules"]}
        for r in s.active_rules():
            info = info_by_id[r["rule_id"]]
            rules.append({
                "rule_id": r["rule_id"], "name": r["name"],
                "version": r["version"], "definition": json.loads(r["definition"]),
                "run_state": info["run_state"], "exact_count": info["exact_count"],
            })
        pub = s.latest_published()
        published = None
        if pub:
            cur = Path(CTX["dir"]) / "current.json"
            published = {
                "snapshot_id": pub["snapshot_id"], "published_at": pub["published_at"],
                "payload": json.loads(cur.read_text()) if cur.exists()
                else json.loads(pub["payload"]),
            }
        return {
            "input_version": s.input_version,
            "rules": rules,
            "live": payload,
            "published": published,
            "snapshots": s.snapshots(),
            "runs": s.runs(30),
            "events": s.events(80),
            "kinds": KINDS,
            "record_count": len(s.load_records()),
        }
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    # -- helpers ----------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _static(self, path: str):
        f = UI / ("index.html" if path == "/" else path.lstrip("/"))
        if not f.is_file() or UI not in f.resolve().parents:
            return self._json({"error": "not found"}, 404)
        ctype = {".html": "text/html", ".js": "text/javascript",
                 ".css": "text/css"}[f.suffix]
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/" or u.path.endswith((".js", ".css")):
                return self._static(u.path)
            if u.path == "/api/state":
                return self._json(live_state())
            if u.path == "/api/records":
                return self._records(parse_qs(u.query))
            if u.path.startswith("/api/record/"):
                return self._record(int(u.path.rsplit("/", 1)[1]))
            if u.path == "/api/overlap":
                return self._overlap(parse_qs(u.query))
            self._json({"error": "not found"}, 404)
        except Exception as e:  # surface, don't hang the UI
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/execute":
                args = ["--executor-id", b.get("executor_id", "exec-ui")]
                if b.get("rule_id"):
                    args += ["--rule", b["rule_id"]]
                if b.get("delay"):
                    args += ["--delay", str(float(b["delay"]))]
                if b.get("as_rule_version"):
                    args += ["--as-rule-version", str(int(b["as_rule_version"]))]
                if b.get("as_input_version"):
                    args += ["--as-input-version", str(int(b["as_input_version"]))]
                spawn("dq.executor", *args)
                return self._json({"ok": True, "note": "执行器进程已启动，结果经命中明细库异步到达"})
            if u.path == "/api/publish":
                args = ["--dir", CTX["dir"]] + (["--crash"] if b.get("crash") else [])
                spawn("dq.publisher", *args)
                return self._json({"ok": True,
                                   "note": "发布进程已启动" + ("（将在暂存后崩溃）" if b.get("crash") else "")})
            if u.path == "/api/publish/recover":
                spawn("dq.publisher", "--dir", CTX["dir"], "--recover")
                return self._json({"ok": True})
            if u.path == "/api/rules":
                return self._rule_add(b)
            if u.path.startswith("/api/rules/"):
                rid = u.path.rsplit("/", 1)[1]
                if rid.endswith("/delete"):
                    return self._rule_retire(rid[:-7])
                return self._rule_edit(rid, b)
            if u.path == "/api/records/delete":
                s = store()
                ok = s.delete_record(int(b["id"]))
                s.close()
                return self._json({"ok": ok})
            if u.path == "/api/input/revise":
                s = store()
                recs = ds.revise(s.load_records(), seed=int(time.time()))
                s.replace_records(recs, bump_input=True)
                iv = s.input_version
                s.close()
                return self._json({"ok": True, "input_version": iv})
            if u.path == "/api/reset":
                cli_reset(CTX["db"])
                cli_init(CTX["db"], 2000, 42)
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # -- endpoint logic ---------------------------------------------------
    def _records(self, q: dict):
        s = store()
        try:
            off, lim = int(q.get("offset", [0])[0]), min(int(q.get("limit", [50])[0]), 200)
            recs = s.load_records()[off:off + lim]
            active = {r["rule_id"]: s.active_hit_ids(r["rule_id"])
                      for r in s.active_rules()}
            for r in recs:
                r["hits"] = [rid for rid, ids in active.items() if r["id"] in ids]
            return self._json({"records": recs, "offset": off})
        finally:
            s.close()

    def _record(self, rid: int):
        s = store()
        try:
            row = s.db.execute("SELECT payload FROM records WHERE record_id=?",
                               (rid,)).fetchone()
            if not row:
                return self._json({"error": "记录不存在（可能已被合法删除）"}, 404)
            rec = json.loads(row[0])
            rec["hits"] = [r["rule_id"] for r in s.active_rules()
                           if rid in s.active_hit_ids(r["rule_id"])]
            return self._json(rec)
        finally:
            s.close()

    def _overlap(self, q: dict):
        rule_ids = [r for r in q.get("rules", [""])[0].split(",") if r]
        if not rule_ids:
            return self._json({"error": "rules required"}, 400)
        s = store()
        try:
            ids = analysis.intersection_ids(s, rule_ids)[:200]
            recs = []
            for rid in ids:
                row = s.db.execute("SELECT payload FROM records WHERE record_id=?",
                                   (rid,)).fetchone()
                if row:
                    recs.append(json.loads(row[0]))
            return self._json({"ids": ids, "records": recs, "exact": True})
        finally:
            s.close()

    def _rule_add(self, b: dict):
        s = store()
        v = s.upsert_rule(b["rule_id"], b["name"],
                          {"kind": b["kind"], "params": b["params"]})
        s.close()
        return self._json({"ok": True, "version": v})

    def _rule_edit(self, rid: str, b: dict):
        s = store()
        v = s.upsert_rule(rid, b["name"],
                          {"kind": b["kind"], "params": b["params"]})
        s.close()
        return self._json({"ok": True, "version": v,
                           "note": "旧命中贡献已撤回，等待重新执行"})

    def _rule_retire(self, rid: str):
        s = store()
        s.retire_rule(rid)
        s.close()
        return self._json({"ok": True})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dq.server")
    ap.add_argument("--db", default="data/dq.db")
    ap.add_argument("--dir", default="data/published")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    CTX.update(db=args.db, dir=args.dir)
    ensure_initialized()
    print(f"dq 分析端已启动: http://localhost:{args.port}  (db={args.db})")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
