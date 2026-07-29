"""Canonical digest of a decoded protobuf message.

Used by both the golden fixture builder and the decoder tests.  It deliberately
folds in things a plain ``==`` comparison would let slide:

* the **python type** of every leaf -- ``b"12"``, ``"12"`` and ``12`` are three
  different decodings and only one of them is right,
* the **key order** -- blackboxprotobuf emits fields in first-appearance order,
  and models.py has been written against that.
"""

from __future__ import annotations

import hashlib
from typing import Any


def canonical(value: Any) -> str:
    """Render a decoded value as a string that encodes its type and order."""
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}={canonical(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, bytes):
        return "b:" + value.hex()
    if isinstance(value, str):
        return "s:" + value
    if isinstance(value, bool):  # not expected, but never silently equal an int
        return "?:" + repr(value)
    if isinstance(value, int):
        return "i:" + str(value)
    if isinstance(value, float):
        return "f:" + repr(value)
    raise TypeError(f"unexpected decoded value type: {type(value).__name__}")


def digest(value: Any) -> str:
    """128-bit hex digest of :func:`canonical`."""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:32]
