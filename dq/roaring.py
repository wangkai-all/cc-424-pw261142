"""Roaring-lite: exact compressed bitmap set (stdlib only).

Container-based roaring bitmap over 32-bit integers:
  - high 16 bits select a container
  - sparse containers are sorted arrays, dense containers are 8192-byte bitmaps
  - containers flip representation at ARRAY_THRESHOLD

Exact, compressed, and supports intersection (tracing) and removal
(legitimate deletion) — unlike approximate structures such as HLL.
"""
from __future__ import annotations

import base64
import struct
from bisect import bisect_left, insort

CHUNK = 1 << 16
ARRAY_THRESHOLD = 4096
_BITMAP_BYTES = CHUNK // 8


def _is_bitmap(cont) -> bool:
    return isinstance(cont, bytearray)


def _bitmap_card(cont: bytearray) -> int:
    return int.from_bytes(cont, "little").bit_count()


def _card(cont) -> int:
    return _bitmap_card(cont) if _is_bitmap(cont) else len(cont)


def _to_bitmap(arr: list) -> bytearray:
    bm = bytearray(_BITMAP_BYTES)
    for v in arr:
        bm[v >> 3] |= 1 << (v & 7)
    return bm


def _to_array(bm: bytearray) -> list:
    out = []
    for i in range(0, _BITMAP_BYTES, 8):
        word = int.from_bytes(bm[i:i + 8], "little")
        base = i << 3
        while word:
            lsb = word & (-word)
            out.append(base + lsb.bit_length() - 1)
            word ^= lsb
    return out


def _iter_cont(cont):
    if _is_bitmap(cont):
        for v in _to_array(cont):
            yield v
    else:
        yield from cont


class Roaring:
    """Exact compressed set of uint32 record numbers."""

    __slots__ = ("c",)

    def __init__(self, values=()):
        self.c: dict[int, object] = {}
        if values:
            for v in values:
                self.add(v)

    # -- mutation ---------------------------------------------------------
    def add(self, v: int) -> None:
        hi, lo = v >> 16, v & 0xFFFF
        cont = self.c.get(hi)
        if cont is None:
            self.c[hi] = [lo]
        elif _is_bitmap(cont):
            cont[lo >> 3] |= 1 << (lo & 7)
        else:
            i = bisect_left(cont, lo)
            if i == len(cont) or cont[i] != lo:
                insort(cont, lo)
                if len(cont) > ARRAY_THRESHOLD:
                    self.c[hi] = _to_bitmap(cont)

    def discard(self, v: int) -> None:
        """Remove a member — legitimate deletion is supported (exact)."""
        hi, lo = v >> 16, v & 0xFFFF
        cont = self.c.get(hi)
        if cont is None:
            return
        if _is_bitmap(cont):
            cont[lo >> 3] &= ~(1 << (lo & 7)) & 0xFF
            if _bitmap_card(cont) <= ARRAY_THRESHOLD // 2:
                arr = _to_array(cont)
                if arr:
                    self.c[hi] = arr
                else:
                    del self.c[hi]
        else:
            i = bisect_left(cont, lo)
            if i < len(cont) and cont[i] == lo:
                cont.pop(i)
                if not cont:
                    del self.c[hi]

    # -- queries ----------------------------------------------------------
    def __contains__(self, v: int) -> bool:
        hi, lo = v >> 16, v & 0xFFFF
        cont = self.c.get(hi)
        if cont is None:
            return False
        if _is_bitmap(cont):
            return bool(cont[lo >> 3] >> (lo & 7) & 1)
        i = bisect_left(cont, lo)
        return i < len(cont) and cont[i] == lo

    def __len__(self) -> int:
        return sum(_card(c) for c in self.c.values())

    def __iter__(self):
        for hi in sorted(self.c):
            base = hi << 16
            for lo in _iter_cont(self.c[hi]):
                yield base | lo

    # -- set algebra (exact intersections/unions for tracing) -------------
    def __and__(self, other: "Roaring") -> "Roaring":
        out = Roaring()
        for hi in self.c.keys() & other.c.keys():
            a, b = self.c[hi], other.c[hi]
            if _is_bitmap(a) and _is_bitmap(b):
                wa = int.from_bytes(a, "little") & int.from_bytes(b, "little")
                cont = wa.to_bytes(_BITMAP_BYTES, "little")
                if wa:
                    out.c[hi] = bytearray(cont) if _bitmap_card(bytearray(cont)) > ARRAY_THRESHOLD else _to_array(bytearray(cont))
            elif _is_bitmap(a):
                arr = [x for x in b if a[x >> 3] >> (x & 7) & 1]
                if arr:
                    out.c[hi] = arr
            elif _is_bitmap(b):
                arr = [x for x in a if b[x >> 3] >> (x & 7) & 1]
                if arr:
                    out.c[hi] = arr
            else:
                arr = sorted(set(a) & set(b))
                if arr:
                    out.c[hi] = arr
        return out

    def __or__(self, other: "Roaring") -> "Roaring":
        out = Roaring()
        for hi in self.c.keys() | other.c.keys():
            a, b = self.c.get(hi), other.c.get(hi)
            if a is None:
                out.c[hi] = bytearray(b) if _is_bitmap(b) else list(b)
            elif b is None:
                out.c[hi] = bytearray(a) if _is_bitmap(a) else list(a)
            elif _is_bitmap(a) or _is_bitmap(b):
                ba = a if _is_bitmap(a) else _to_bitmap(a)
                bb = b if _is_bitmap(b) else _to_bitmap(b)
                w = int.from_bytes(ba, "little") | int.from_bytes(bb, "little")
                out.c[hi] = bytearray(w.to_bytes(_BITMAP_BYTES, "little"))
            else:
                arr = sorted(set(a) | set(b))
                out.c[hi] = _to_bitmap(arr) if len(arr) > ARRAY_THRESHOLD else arr
        return out

    def __sub__(self, other: "Roaring") -> "Roaring":
        out = Roaring()
        for hi, a in self.c.items():
            b = other.c.get(hi)
            if b is None:
                out.c[hi] = bytearray(a) if _is_bitmap(a) else list(a)
                continue
            aset = set(_iter_cont(a))
            aset -= set(_iter_cont(b))
            if aset:
                arr = sorted(aset)
                out.c[hi] = _to_bitmap(arr) if len(arr) > ARRAY_THRESHOLD else arr
        return out

    # -- serialization ----------------------------------------------------
    def to_bytes(self) -> bytes:
        parts = [struct.pack("<I", len(self.c))]
        for hi in sorted(self.c):
            cont = self.c[hi]
            if _is_bitmap(cont):
                parts.append(struct.pack("<HBI", hi, 1, _bitmap_card(cont)))
                parts.append(bytes(cont))
            else:
                parts.append(struct.pack("<HBI", hi, 0, len(cont)))
                parts.append(struct.pack(f"<{len(cont)}H", *cont))
        return b"".join(parts)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Roaring":
        out = cls()
        off = 0
        (n,) = struct.unpack_from("<I", data, off)
        off += 4
        for _ in range(n):
            hi, typ, card = struct.unpack_from("<HBI", data, off)
            off += 7
            if typ == 1:
                out.c[hi] = bytearray(data[off:off + _BITMAP_BYTES])
                off += _BITMAP_BYTES
            else:
                out.c[hi] = list(struct.unpack_from(f"<{card}H", data, off))
                off += 2 * card
        return out

    def to_b64(self) -> str:
        return base64.b64encode(self.to_bytes()).decode()

    @classmethod
    def from_b64(cls, s: str) -> "Roaring":
        return cls.from_bytes(base64.b64decode(s))
