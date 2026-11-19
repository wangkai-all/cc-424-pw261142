"""Quality rule definitions and evaluation.

A rule is a versioned JSON definition: {kind, params}. Evaluation returns
True when the record VIOLATES the rule (a "hit"). `duplicate` needs a
dataset-wide context built once per executor run.
"""
from __future__ import annotations

import re
from datetime import date

EMAIL_RE = r"^[\w.+-]+@[\w-]+\.[\w.]{2,}$"
PHONE_RE = r"^\+\d{11,14}$"

# kind -> (param schema for the UI, human description)
KINDS = {
    "missing": {"field": "str"},
    "regex": {"field": "str", "pattern": "str"},
    "range": {"field": "str", "min": "num", "max": "num"},
    "in_list": {"field": "str", "values": "list"},
    "duplicate": {"field": "str"},
    "date_not_future": {"field": "str"},
}

DEFAULT_RULES = [
    {"rule_id": "email_format", "name": "邮箱格式非法",
     "kind": "regex", "params": {"field": "email", "pattern": EMAIL_RE}},
    {"rule_id": "phone_missing", "name": "电话缺失或过短",
     "kind": "regex", "params": {"field": "phone", "pattern": PHONE_RE}},
    {"rule_id": "age_range", "name": "年龄越界",
     "kind": "range", "params": {"field": "age", "min": 18, "max": 120}},
    {"rule_id": "email_duplicate", "name": "邮箱重复",
     "kind": "duplicate", "params": {"field": "email"}},
    {"rule_id": "country_invalid", "name": "国家代码非法",
     "kind": "in_list", "params": {"field": "country", "values_from": "countries"}},
    {"rule_id": "signup_date_bad", "name": "注册日期非法",
     "kind": "date_not_future", "params": {"field": "signup_date"}},
    {"rule_id": "plan_unknown", "name": "套餐未知",
     "kind": "in_list", "params": {"field": "plan", "values_from": "plans"}},
]


def build_context(rules: list[dict], records: list[dict], lookups: dict) -> dict:
    """Dataset-wide context: value counts for duplicate rules, list lookups."""
    ctx = {"dup": {}, "lookups": lookups}
    for rule in rules:
        if rule["kind"] == "duplicate":
            f = rule["params"]["field"]
            counts: dict[str, int] = {}
            for r in records:
                key = str(r.get(f) or "").strip().lower()
                if key:
                    counts[key] = counts.get(key, 0) + 1
            ctx["dup"][f] = counts
    return ctx


def evaluate(rule: dict, rec: dict, ctx: dict) -> bool:
    """True = record violates the rule."""
    kind = rule["kind"]
    p = rule["params"]
    v = rec.get(p.get("field"))
    if kind == "missing":
        return v is None or (isinstance(v, str) and v.strip() == "")
    if kind == "regex":
        return re.fullmatch(p["pattern"], str(v or "")) is None
    if kind == "range":
        try:
            x = float(v)
        except (TypeError, ValueError):
            return True
        return not (float(p["min"]) <= x <= float(p["max"]))
    if kind == "in_list":
        values = p.get("values")
        if values is None:  # resolved from a named lookup
            values = ctx["lookups"][p["values_from"]]
        return v not in values
    if kind == "duplicate":
        key = str(v or "").strip().lower()
        return bool(key) and ctx["dup"][p["field"]].get(key, 0) > 1
    if kind == "date_not_future":
        s = str(v or "")
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
        if not m:
            return True
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return True
        return d > date.today()
    raise ValueError(f"unknown rule kind: {kind}")
