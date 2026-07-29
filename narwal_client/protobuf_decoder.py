"""Targeted protobuf wire-format decoder for Narwal broadcast payloads.

Why this module exists
======================
The integration used ``blackboxprotobuf`` (bbpb) to decode *every* broadcast
frame.  bbpb is a schemaless reverse-engineering library: on top of decoding it
builds a re-encodable type definition, keeps a set of alternate type candidates
per field and allocates a small object graph for every field it walks.  We pay
for rediscovering a schema we have known for months.

Measured on the Home Assistant host (x86_64, CPython 3.14, no JIT), mean over
the real frame mix of a captured cleaning session:

    topic                          avg B    bbpb     this    speedup
    map/display_map                 1518  4.878ms  0.704ms      6.9x
    status/point_navi_plan_traj      345  3.635ms  0.519ms      7.0x
    status/robot_base_status         141  2.552ms  0.256ms     10.0x
    upgrade/upgrade_status            30  0.318ms  0.050ms      6.3x
    status/download_status             2  0.077ms  0.012ms      6.5x

That work runs inline in the event loop at ~3 frames/s, which made
``_decode_protobuf`` 58% of the time ``_handle_message`` blocked the loop.

Note the speedup is ~7x, not the 25-50x a *field-targeted* decoder would give.
That is the deliberate trade: this decoder reproduces the entire dict bbpb
would have produced, so `models.py` and `select.py` (which reads
`raw_base_status["29"]`) keep working untouched. A decoder that only extracted
the handful of fields we actually read would be faster and far easier to break.

This module does the decoding half only, in a single pass over the buffer, and
is **bit-for-bit compatible** with ``blackboxprotobuf.decode_message()`` for
every payload it accepts.  Whenever it cannot guarantee that, it raises
:class:`ProtobufDecodeError` and the caller falls back to bbpb -- see
``NarwalClient._decode_protobuf``.

The output shape (which ``models.py`` depends on) is bbpb's:

* keys are field numbers rendered as **strings** (``"1"``, ``"12"``),
  in first-appearance order,
* a field seen once is a scalar, a field seen twice or more is a ``list``,
* wire type 0 (varint)     -> ``int``, interpreted as **signed** 64-bit,
* wire type 5 (fixed32)    -> ``int``, the raw **unsigned** 32-bit pattern
  (``models._to_float32`` turns it into a float where needed),
* wire type 1 (fixed64)    -> ``int``, raw unsigned 64-bit,
* wire type 2 (len-delim)  -> ``dict`` if the bytes parse as a nested message,
  else ``str`` if they are valid UTF-8, else ``bytes``.

Protobuf wire format, for reference
===================================
A message is a flat sequence of fields.  Each field is::

    tag = varint((field_number << 3) | wire_type)
    payload, whose length depends on wire_type:
        0  varint          base-128, little-endian groups of 7 bits
        1  fixed64         8 bytes, little-endian
        2  length-delim    varint length, then that many bytes
        5  fixed32         4 bytes, little-endian
        3/4 start/end group  deprecated; bbpb rejects them, so do we

There is no framing that tells a bytes field from a nested message -- both are
wire type 2.  That ambiguity is why the "try message, then string, then bytes"
ladder below exists, and why it has to match bbpb's ladder exactly.

Compatibility notes (the sharp edges)
=====================================
1.  **Strict varints.**  bbpb re-encodes every varint it reads and rejects the
    value if the bytes are not the canonical minimal encoding, or if it does
    not fit in 64 bits.  This is not pedantry: it is the main reason random
    binary blobs (zlib-compressed map cells, packed float arrays) *fail* to
    parse as nested messages and come back as bytes.  Loosening it would
    silently reinterpret real payloads.

2.  **Field number 0 is accepted.**  Real protobuf forbids it, bbpb does not,
    and live ``display_map`` frames contain blobs that bbpb decodes into
    messages with a field ``"0"``.  Rejecting it would change the output.

3.  **All-or-nothing per field number.**  When a length-delimited field number
    occurs several times, bbpb decides message/string/bytes for the whole
    group: if *any* occurrence fails to parse as a message, *every* occurrence
    is re-decoded as string (or bytes).

4.  **"Repeated" is sticky across siblings.**  When bbpb decodes the second
    occurrence of a repeated message field, it reuses the type definition it
    built from the first one.  If a sub-field was repeated in occurrence #1, it
    stays a ``list`` in occurrence #2 even when it appears only once there.
    We model that with the small ``state`` tree threaded through the decode
    (field key -> ``[resolved_type, seen_repeated, child_state]``) -- without
    it we would return a bare dict where bbpb returns ``[dict]``.

5.  **Alternate type ids -> we bail.**  If reusing that type definition fails,
    bbpb does not merge: it stores the odd occurrence under a *different* key,
    ``"12-1"`` instead of ``"12"``.  Emulating that faithfully would mean
    reimplementing bbpb's whole typedef engine.  Instead we detect the
    situation (a field number whose resolved type changes between sibling
    occurrences) and raise :class:`ProtobufDecodeError` so the caller replays
    the payload through bbpb.  It never happened in 574 captured live frames.
"""

from __future__ import annotations

import struct
from typing import Any

__all__ = ["ProtobufDecodeError", "decode"]

# Pre-built struct readers: ~2.3x cheaper than int.from_bytes() on a slice,
# because they read straight out of the buffer without allocating one.
# Unsigned on purpose -- that is what bbpb's default fixed32/fixed64 types are.
_read_fixed32 = struct.Struct("<I").unpack_from
_read_fixed64 = struct.Struct("<Q").unpack_from

# --- wire types -----------------------------------------------------------
_WT_VARINT = 0
_WT_FIXED64 = 1
_WT_LEN = 2
_WT_FIXED32 = 5

# --- resolved types for length-delimited fields ---------------------------
# Kept distinct from the wire-type numbers above so a single "what did this
# field resolve to" tag can cover both.
_T_MESSAGE = 10
_T_STRING = 11
_T_BYTES = 12

_MAX_UINT64 = (1 << 64) - 1
_SIGN_BIT64 = 1 << 63
_TWO_POW_64 = 1 << 64

# bbpb keys its output by the field number rendered as a string.  Rendering it
# is a measurable slice of the cost when a payload holds a few hundred small
# sub-messages (point_navi_plan_traj does), so pre-render the plausible range.
_KEY_CACHE = tuple(str(i) for i in range(512))
_KEY_CACHE_LEN = len(_KEY_CACHE)

# Real payloads nest 6 levels at most.  The cap only exists so a malformed
# buffer cannot walk us into a RecursionError; hitting it means "we don't
# know", not "invalid", hence _AmbiguousError.
_MAX_DEPTH = 64


class ProtobufDecodeError(Exception):
    """The payload could not be decoded bbpb-compatibly.

    Callers should treat this as "use blackboxprotobuf instead", not as
    "the payload is broken" -- both causes raise it.
    """


class _WireError(ProtobufDecodeError):
    """These bytes are not a well-formed protobuf message.

    Raised (and caught) while probing whether a length-delimited field holds a
    nested message.  bbpb reaches the same verdict for the same bytes.
    """


class _AmbiguousError(ProtobufDecodeError):
    """We cannot promise bbpb would produce the same dict -- hand it over.

    Deliberately *not* a subclass of :class:`_WireError` so that it escapes the
    "try as a nested message" handler instead of being swallowed into a
    string/bytes fallback.
    """


def decode(payload: bytes) -> dict[str, Any]:
    """Decode a schemaless protobuf payload into blackboxprotobuf's dict shape.

    Args:
        payload: Raw protobuf bytes (the part of the frame after the topic).

    Returns:
        Field number (as ``str``) -> value.  An empty payload gives ``{}``.

    Raises:
        ProtobufDecodeError: The payload is malformed, or decoding it exactly
            like bbpb would require bbpb's typedef machinery.
    """
    return _decode_message(payload, 0, len(payload), None, 0)


def _read_uvarint(buf: bytes, pos: int, end: int) -> tuple[int, int]:
    """Read a base-128 varint at ``pos``; return ``(value, new_pos)``.

    Mirrors bbpb's strict reader: the encoding must be canonical (the minimal
    number of bytes) and must fit in 64 bits.  See compatibility note 1.
    """
    value = 0
    shift = 0
    while pos < end:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:  # continuation bit clear -> last byte
            # b"\x80\x00" and b"\x00" both mean 0; only the latter is
            # canonical, so a multi-byte varint may not end in a zero byte.
            if byte == 0 and shift:
                raise _WireError("non-canonical varint")
            if value > _MAX_UINT64:
                raise _WireError("varint wider than 64 bit")
            return value, pos
        shift += 7
        if shift > 63:
            # 10 bytes is the most a 64-bit value can take.
            raise _WireError("varint longer than 10 bytes")
    raise _WireError("varint runs past the end of the buffer")


def _decode_message(
    buf: bytes,
    pos: int,
    end: int,
    state: dict[int, list] | None,
    depth: int,
) -> dict[str, Any]:
    """Decode the message occupying ``buf[pos:end]``.

    Args:
        buf: Buffer holding the message (never sliced -- we carry offsets so
            nested messages cost no copies).
        pos, end: Half-open range of this message inside ``buf``.
        state: Shared per-field bookkeeping when this message is one of several
            occurrences of the same repeated field, else ``None``.  Maps field
            number to ``[resolved_type, seen_repeated, child_state]`` -- our
            stand-in for bbpb's type definition (compatibility notes 4 and 5).
        depth: Nesting level, for the recursion guard.
    """
    if depth > _MAX_DEPTH:
        raise _AmbiguousError(f"message nested deeper than {_MAX_DEPTH} levels")

    # --- pass 1: walk the fields -------------------------------------------
    # Scalars go straight into ``out`` (their type is unambiguous).  Length-
    # delimited fields only get their slot reserved: deciding what they hold
    # needs every occurrence of that field number, so they are resolved below.
    out: dict[str, Any] = {}
    wire_of: dict[str, int] = {}  # field key -> wire type; also "already seen?"
    spans_of: dict[str, list] | None = None  # field key -> [(start, stop), ...]

    while pos < end:
        # tag = (field_number << 3) | wire_type
        byte = buf[pos]
        if byte < 0x80:
            # Fast path: field numbers 1..15 with any wire type fit in one byte
            # and cover the overwhelming majority of real fields.
            tag = byte
            pos += 1
        else:
            tag, pos = _read_uvarint(buf, pos, end)
        wire_type = tag & 0x07
        field_number = tag >> 3
        key = (
            _KEY_CACHE[field_number]
            if field_number < _KEY_CACHE_LEN
            else str(field_number)
        )

        seen = wire_of.get(key)
        if seen is None:
            wire_of[key] = wire_type
        elif seen != wire_type:
            # bbpb refuses to merge a field number that switches wire type
            # mid-message and treats the whole buffer as "not a message".
            raise _WireError(f"field {key} has mismatched wire types")

        if wire_type == _WT_LEN:
            length, pos = _read_uvarint(buf, pos, end)
            if length & _SIGN_BIT64:
                # bbpb reads this length with its *signed* varint decoder, so a
                # value this large would come out negative and make it slice
                # backwards.  No real field looks like that; reject.
                raise _WireError("negative length prefix")
            new_pos = pos + length
            if new_pos > end:
                raise _WireError("length-delimited field runs past the end")
            if seen is None:
                # Reserve the slot now so the output keeps bbpb's ordering
                # (first appearance), even though the value lands in pass 2.
                out[key] = None
                if spans_of is None:
                    spans_of = {key: [(pos, new_pos)]}
                else:
                    spans_of[key] = [(pos, new_pos)]
            else:
                spans_of[key].append((pos, new_pos))
            pos = new_pos
            continue

        if wire_type == _WT_VARINT:
            value, pos = _read_uvarint(buf, pos, end)
            # bbpb's default type for wire type 0 is "int", i.e. the 64-bit
            # pattern read as a *signed* value.
            if value & _SIGN_BIT64:
                value -= _TWO_POW_64
        elif wire_type == _WT_FIXED32:
            new_pos = pos + 4
            if new_pos > end:
                raise _WireError("fixed32 runs past the end")
            # bbpb's default for wire type 5 is "fixed32": the raw unsigned
            # bit pattern, NOT a float.  models._to_float32 reinterprets it.
            value = _read_fixed32(buf, pos)[0]
            pos = new_pos
        elif wire_type == _WT_FIXED64:
            new_pos = pos + 8
            if new_pos > end:
                raise _WireError("fixed64 runs past the end")
            value = _read_fixed64(buf, pos)[0]
            pos = new_pos
        else:
            # 3/4 are the deprecated group markers, 6/7 are undefined.
            raise _WireError(f"unsupported wire type {wire_type}")

        if seen is None:
            out[key] = value
        else:
            # Scalar values are always ints here, so a list in ``out`` can only
            # mean "we already saw this field number more than once".
            previous = out[key]
            if type(previous) is list:
                previous.append(value)
            else:
                out[key] = [previous, value]

    # --- pass 2: resolve the length-delimited fields -----------------------
    if state is None:
        # Common case: this message is decoded on its own, so there is no
        # earlier occurrence whose type definition bbpb would be reusing.
        if spans_of is not None:
            for key, spans in spans_of.items():
                values, _ = _decode_len_group(buf, spans, None, depth)
                out[key] = values[0] if len(values) == 1 else values
        return out

    # --- rare case: one of several occurrences of the same repeated field --
    # bbpb carries a type definition over from the previous occurrence, which
    # changes two things: a sub-field that repeated before stays a list here
    # even when it appears once, and a sub-field whose type changed gets filed
    # under a different key.  Mirror the first, bail out on the second.
    for key, wire_type in wire_of.items():
        field_state = state.get(key)
        if field_state is None:
            field_state = [None, False, None]
            state[key] = field_state

        if wire_type == _WT_LEN:
            values, resolved = _decode_len_group(
                buf, spans_of[key], field_state, depth
            )
            # bbpb only marks a length-delimited field as repeated when it
            # decoded as a message; its string/bytes fallback never does.
            markable = resolved is _T_MESSAGE
        else:
            value = out[key]
            values = value if type(value) is list else [value]
            resolved = wire_type
            markable = True

        if field_state[0] is None:
            field_state[0] = resolved
        elif field_state[0] != resolved:
            raise _AmbiguousError(
                f"field {key} changed type between repeated occurrences"
            )
        if markable and len(values) > 1:
            field_state[1] = True

        # bbpb collapses a one-element list back to a scalar, unless the field
        # was already known to repeat.
        out[key] = values[0] if len(values) == 1 and not field_state[1] else values

    return out


def _decode_len_group(
    buf: bytes,
    spans: list[tuple[int, int]],
    field_state: list | None,
    depth: int,
) -> tuple[list[Any], int]:
    """Decide what one length-delimited field number holds, for all its
    occurrences at once.

    bbpb's ladder, reproduced verbatim: try to parse *every* occurrence as a
    nested message; if any fails, decode *every* occurrence as UTF-8; if that
    fails too, hand back raw bytes (which cannot fail).

    Returns:
        ``(values, resolved_type)`` where ``resolved_type`` is one of
        ``_T_MESSAGE`` / ``_T_STRING`` / ``_T_BYTES``.
    """
    # Sub-messages of one field share a type definition in bbpb, so they need
    # shared state -- but only when there is more than one of them, or when
    # this whole message is itself a repeated occurrence.
    if field_state is not None:
        child_state = field_state[2]
        if child_state is None:
            child_state = field_state[2] = {}
    elif len(spans) > 1:
        child_state = {}
    else:
        child_state = None  # a single occurrence has nothing to stay consistent with

    depth += 1
    try:
        return [
            _decode_message(buf, start, stop, child_state, depth)
            for start, stop in spans
        ], _T_MESSAGE
    except _WireError:
        # Not a message (or not all of them were).  _AmbiguousError deliberately
        # escapes here: it means "ask bbpb", not "these are bytes".
        pass

    try:
        return [str(buf[start:stop], "utf-8") for start, stop in spans], _T_STRING
    except UnicodeDecodeError:
        pass

    return [buf[start:stop] for start, stop in spans], _T_BYTES
