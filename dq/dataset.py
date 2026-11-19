"""Deterministic fictional customer dataset with injected quality defects.

Nothing here is real data. Defects are injected at controlled rates so that
records frequently violate several rules at once (overlap is the point of
the whole system). `revise` produces an input revision: some defects fixed,
some new ones introduced — which must retract all prior hit contributions.
"""
from __future__ import annotations

import random

FIRST = ["Wei", "Fang", "Lei", "Na", "Min", "Yong", "Tao", "Juan", "Chao", "Yan",
         "Alex", "Sam", "Pat", "Kim", "Jo", "Rin", "Aya", "Leo", "Mia", "Noa"]
LAST = ["Wang", "Li", "Zhang", "Liu", "Chen", "Yang", "Zhao", "Huang", "Zhou", "Wu",
        "Smith", "Brown", "Lee", "Kim", "Park", "Sato", "Muller", "Rossi", "Diaz", "Chen"]
DOMAINS = ["example.com", "mail.test", "demo.org", "sample.net"]
VALID_COUNTRIES = ["CN", "US", "GB", "DE", "FR", "JP", "KR", "SG", "AU", "CA",
                   "IN", "BR", "NL", "SE", "CH", "ES", "IT", "MX", "PL", "IE"]
PLANS = ["free", "basic", "pro", "enterprise"]


def _email(rng: random.Random, name: str) -> str:
    return f"{name.lower()}.{rng.choice(LAST).lower()}{rng.randint(1, 99)}@{rng.choice(DOMAINS)}"


def generate(n: int = 2000, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    records = []
    emails_pool: list[str] = []
    for i in range(n):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        rec = {
            "id": i + 1,
            "name": f"{first} {last}",
            "email": _email(rng, first),
            "phone": f"+86{rng.randint(13000000000, 19999999999)}",
            "age": rng.randint(18, 75),
            "country": rng.choice(VALID_COUNTRIES),
            "signup_date": f"202{rng.randint(1, 4)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            "plan": rng.choice(PLANS),
        }
        # ~6% of records share an email with someone else (duplicates)
        if emails_pool and rng.random() < 0.06:
            rec["email"] = rng.choice(emails_pool)
        else:
            emails_pool.append(rec["email"])
        records.append(rec)

    def pick(rate: float):
        return [r for r in records if rng.random() < rate]

    for r in pick(0.07):  # invalid / missing email
        defect = rng.random()
        if defect < 0.4:
            r["email"] = r["email"].split("@")[0]          # missing domain
        elif defect < 0.7:
            r["email"] = ""                               # empty
        else:
            r["email"] = r["email"].replace(".", "@", 1)  # double @ / no dot
    for r in pick(0.09):  # missing or short phone
        r["phone"] = "" if rng.random() < 0.6 else f"+86{rng.randint(1000, 9999)}"
    for r in pick(0.06):  # age out of range / non-numeric
        r["age"] = rng.choice([-3, 0, 12, 145, 230, "unknown", None])
    for r in pick(0.05):  # invalid country code
        r["country"] = rng.choice(["XX", "ZZ", "USA", "uk", "", "N/A"])
    for r in pick(0.06):  # malformed or future signup date
        r["signup_date"] = rng.choice(["2023-13-45", "not-a-date", "", "2029-01-01", "2031-06-30"])
    for r in pick(0.04):  # unknown plan
        r["plan"] = rng.choice(["trial", "vip", "", "pro+"])
    # a "messy cluster": some records break 3+ rules at once -> rich overlap
    for r in rng.sample(records, 60):
        r["email"] = ""
        r["phone"] = ""
        r["country"] = "XX"
        if rng.random() < 0.5:
            r["age"] = None
    return records


def revise(records: list[dict], seed: int, count: int = 40) -> list[dict]:
    """Input revision: fix some defects, introduce some new ones."""
    rng = random.Random(seed)
    out = [dict(r) for r in records]
    for r in rng.sample(out, min(count, len(out))):
        action = rng.random()
        if action < 0.35:  # fix a defect
            if not r["email"] or "@" not in str(r["email"]):
                r["email"] = _email(rng, r["name"].split()[0])
            if not r["phone"]:
                r["phone"] = f"+86{rng.randint(13000000000, 19999999999)}"
            if r["country"] not in VALID_COUNTRIES:
                r["country"] = rng.choice(VALID_COUNTRIES)
        elif action < 0.7:  # introduce a new defect
            field = rng.choice(["email", "phone", "age", "country", "signup_date"])
            r[field] = {"email": "", "phone": "", "age": 999,
                        "country": "XX", "signup_date": "2030-12-31"}[field]
        else:  # legitimate change of an attribute
            r["plan"] = rng.choice(PLANS)
            r["age"] = rng.randint(18, 75)
    return out
