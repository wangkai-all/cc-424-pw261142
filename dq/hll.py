"""HyperLogLog — APPROXIMATE cardinality only.

Trade-off vs the exact Roaring set (dq/roaring.py):
  + tiny fixed memory, mergeable
  - approximate (relative error ~ 1.04/sqrt(m)); must NEVER be presented
    as an exact count
  - no member enumeration (no intersection tracing back to records)
  - no deletion: a legitimate (privacy) deletion cannot be removed from a
    register; the sketch must be rebuilt from the exact source of truth
"""
from __future__ import annotations

import hashlib
import math


class HLL:
    def __init__(self, p: int = 14):
        if not 4 <= p <= 20:
            raise ValueError("p out of range")
        self.p = p
        self.m = 1 << p
        self.reg = bytearray(self.m)

    def add(self, item) -> None:
        h = int.from_bytes(hashlib.sha1(str(item).encode()).digest()[:8], "big")
        idx = h >> (64 - self.p)
        rest = h & ((1 << (64 - self.p)) - 1)
        width = 64 - self.p
        rank = width - rest.bit_length() + 1 if rest else width + 1
        if rank > self.reg[idx]:
            self.reg[idx] = rank

    def merge(self, other: "HLL") -> None:
        if other.p != self.p:
            raise ValueError("p mismatch")
        for i in range(self.m):
            if other.reg[i] > self.reg[i]:
                self.reg[i] = other.reg[i]

    def estimate(self) -> float:
        """Approximate cardinality — always label as an estimate."""
        m = self.m
        alpha = 0.7213 / (1 + 1.079 / m)
        z = sum(2.0 ** -r for r in self.reg)
        e = alpha * m * m / z
        zeros = self.reg.count(0)
        if e <= 2.5 * m and zeros:
            e = m * math.log(m / zeros)  # linear counting, small range
        return e

    @property
    def rel_error(self) -> float:
        return 1.04 / math.sqrt(self.m)
