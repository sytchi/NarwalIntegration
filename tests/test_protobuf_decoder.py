"""Tests for narwal_client.protobuf_decoder.

The decoder replaces blackboxprotobuf on the hot broadcast path.  Its whole
contract is "produce exactly what bbpb produced", so the tests are built around
that rather than around hand-written expectations:

* :class:`TestGoldenFrames` replays 325 real broadcast payloads captured off a
  live Narwal Flow and asserts the decoder still produces the recorded output,
  digest for digest.  It also re-derives those digests with bbpb, so the
  fixture cannot silently drift away from the library it mirrors.
* :class:`TestWireFormat` pins the individual bbpb quirks the golden frames
  happen to exercise, so a regression says *which* rule broke.
* :class:`TestFallback` covers the payloads the decoder refuses.
"""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

import pytest

from narwal_client.protobuf_decoder import ProtobufDecodeError, decode
from tests.decoder_digest import canonical, digest

FIXTURE = Path(__file__).parent / "fixtures" / "narwal_broadcast_frames.jsonl.gz"


def _frames() -> list[dict[str, str]]:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


FRAMES = _frames()
TOPICS = sorted({frame["topic"] for frame in FRAMES})


def _tag(field_number: int, wire_type: int) -> bytes:
    """Encode a protobuf field tag (field number + wire type)."""
    value = (field_number << 3) | wire_type
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def _uvarint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def _varint(field_number: int, value: int) -> bytes:
    return _tag(field_number, 0) + _uvarint(value)


def _lendelim(field_number: int, body: bytes) -> bytes:
    return _tag(field_number, 2) + _uvarint(len(body)) + body


def _fixed32(field_number: int, raw: int) -> bytes:
    return _tag(field_number, 5) + raw.to_bytes(4, "little")


class TestGoldenFrames:
    """Real payloads from a live robot — the regression net."""

    def test_fixture_is_present_and_covers_every_topic(self) -> None:
        assert len(FRAMES) > 300, "golden fixture looks truncated"
        # The topics whose decoding cost drove this work.
        assert "map/display_map" in TOPICS
        assert "status/point_navi_plan_traj" in TOPICS
        assert "status/robot_base_status" in TOPICS

    @pytest.mark.parametrize("topic", TOPICS)
    def test_decoder_matches_recorded_output(self, topic: str) -> None:
        """Every captured frame still decodes to exactly what was recorded."""
        frames = [f for f in FRAMES if f["topic"] == topic]
        assert frames
        for frame in frames:
            payload = base64.b64decode(frame["payload_b64"])
            got = decode(payload)
            assert digest(got) == frame["digest"], (
                f"{topic}: decoder output changed for a {len(payload)}-byte "
                f"frame\n  got: {canonical(got)[:400]}"
            )

    @pytest.mark.parametrize("topic", TOPICS)
    def test_blackboxprotobuf_agrees(self, topic: str) -> None:
        """The recorded digests really are what bbpb produces.

        Without this the fixture could drift into recording *our* bugs.
        """
        blackboxprotobuf = pytest.importorskip("blackboxprotobuf")
        for frame in (f for f in FRAMES if f["topic"] == topic):
            payload = base64.b64decode(frame["payload_b64"])
            reference, _ = blackboxprotobuf.decode_message(payload)
            assert digest(reference) == frame["digest"]

    def test_no_real_frame_needs_the_fallback(self) -> None:
        """The fast path has to actually cover production traffic."""
        for frame in FRAMES:
            payload = base64.b64decode(frame["payload_b64"])
            decode(payload)  # must not raise


class TestWireFormat:
    """The individual blackboxprotobuf behaviours the decoder mirrors."""

    def test_empty_payload(self) -> None:
        assert decode(b"") == {}

    def test_keys_are_field_numbers_as_strings(self) -> None:
        assert decode(_varint(3, 7)) == {"3": 7}

    def test_keys_keep_first_appearance_order(self) -> None:
        payload = _varint(9, 1) + _varint(2, 2) + _varint(5, 3)
        assert list(decode(payload)) == ["9", "2", "5"]

    def test_varint_is_signed(self) -> None:
        """bbpb's default type for wire type 0 is a signed 64-bit int."""
        assert decode(_varint(1, (1 << 64) - 1)) == {"1": -1}
        assert decode(_varint(1, (1 << 63))) == {"1": -(1 << 63)}

    def test_fixed32_is_the_raw_unsigned_pattern_not_a_float(self) -> None:
        """models._to_float32 reinterprets the int; it must not arrive as a float."""
        raw = 1118175232  # IEEE-754 float32 83.0, the battery encoding
        decoded = decode(_fixed32(2, raw))
        assert decoded == {"2": raw}
        assert type(decoded["2"]) is int

    def test_fixed64_is_the_raw_unsigned_pattern(self) -> None:
        decoded = decode(_tag(1, 1) + (2**64 - 1).to_bytes(8, "little"))
        assert decoded == {"1": 2**64 - 1}

    def test_single_occurrence_is_a_scalar_repeat_is_a_list(self) -> None:
        assert decode(_varint(1, 5)) == {"1": 5}
        assert decode(_varint(1, 5) + _varint(1, 6)) == {"1": [5, 6]}

    def test_nested_message_becomes_a_dict(self) -> None:
        inner = _varint(1, 11) + _varint(2, 22)
        assert decode(_lendelim(4, inner)) == {"4": {"1": 11, "2": 22}}

    def test_empty_length_delimited_field_is_an_empty_message(self) -> None:
        """bbpb parses zero bytes as an empty message, not as b""."""
        decoded = decode(_lendelim(4, b""))
        assert decoded == {"4": {}}
        assert type(decoded["4"]) is dict

    def test_utf8_bytes_become_str_and_others_stay_bytes(self) -> None:
        # "hello" is not parseable as a message, but is valid UTF-8
        assert decode(_lendelim(7, b"hello")) == {"7": "hello"}
        # 0xff can neither start a field nor be UTF-8
        assert decode(_lendelim(7, b"\xff\xfe\xfd")) == {"7": b"\xff\xfe\xfd"}

    def test_field_number_zero_is_accepted(self) -> None:
        """Real protobuf forbids it; bbpb does not, and live display_map blobs
        decode into messages that use it."""
        assert decode(_varint(0, 4)) == {"0": 4}

    def test_non_canonical_varint_is_rejected(self) -> None:
        """b"\\x80\\x00" is a non-minimal encoding of 0.

        This strictness is what stops random blobs from being mistaken for
        nested messages, so it has to stay.
        """
        with pytest.raises(ProtobufDecodeError):
            decode(_tag(1, 0) + b"\x80\x00")
        # ... and inside a length-delimited field it makes the blob stay bytes
        body = _tag(1, 0) + b"\x80\x00"
        assert decode(_lendelim(3, body)) == {"3": body}

    def test_message_decision_is_taken_per_field_group(self) -> None:
        """If one occurrence is not a message, none of them are decoded as one."""
        good = _varint(1, 1)
        assert decode(_lendelim(5, good) + _lendelim(5, good)) == {
            "5": [{"1": 1}, {"1": 1}]
        }
        # second occurrence is not a valid message -> both fall back to bytes
        decoded = decode(_lendelim(5, good) + _lendelim(5, b"\xff\xff"))
        assert decoded == {"5": [good, b"\xff\xff"]}

    def test_repeated_flag_is_sticky_across_occurrences(self) -> None:
        """bbpb reuses the type definition of the first occurrence, so a
        sub-field that repeated there stays a list in the next one."""
        first = _varint(2, 1) + _varint(2, 2)  # sub-field 2 twice
        second = _varint(2, 3)  # ... and once here
        decoded = decode(_lendelim(12, first) + _lendelim(12, second))
        assert decoded == {"12": [{"2": [1, 2]}, {"2": [3]}]}

    def test_mismatched_wire_types_reject_the_message(self) -> None:
        payload = _varint(1, 1) + _fixed32(1, 7)
        with pytest.raises(ProtobufDecodeError):
            decode(payload)

    def test_truncated_field_is_rejected(self) -> None:
        assert decode(_lendelim(1, b"ab")) == {"1": "ab"}
        with pytest.raises(ProtobufDecodeError):
            decode(_tag(1, 2) + b"\x08ab")  # claims 8 bytes, supplies 2

    def test_group_wire_types_are_rejected(self) -> None:
        with pytest.raises(ProtobufDecodeError):
            decode(_tag(1, 3))  # START_GROUP, deprecated

    def test_deep_nesting_raises_instead_of_recursing(self) -> None:
        payload = _varint(1, 1)
        for _ in range(200):
            payload = _lendelim(1, payload)
        with pytest.raises(ProtobufDecodeError):
            decode(payload)


# Field 1 repeated three times as a nested message, where sub-field 1 is a
# varint in the first two occurrences and a fixed32 in the third.  bbpb cannot
# reuse the type definition for the third one and files it under the key "1-1";
# the fast path refuses the payload instead of guessing.
AMBIGUOUS_PAYLOAD = (
    _lendelim(1, _varint(1, 105))
    + _lendelim(1, _varint(1, 220))
    + _lendelim(1, _fixed32(1, 0xE0962200))
)


class TestFallback:
    """Payloads the decoder deliberately refuses."""

    def test_type_change_between_occurrences_bails_out(self) -> None:
        with pytest.raises(ProtobufDecodeError):
            decode(AMBIGUOUS_PAYLOAD)

    def test_blackboxprotobuf_really_does_something_we_cannot(self) -> None:
        """Document why the bail-out exists: bbpb invents a second key."""
        blackboxprotobuf = pytest.importorskip("blackboxprotobuf")
        decoded, _ = blackboxprotobuf.decode_message(AMBIGUOUS_PAYLOAD)
        assert any("-" in key for key in decoded), decoded

    def test_client_falls_back_to_blackboxprotobuf(self) -> None:
        """_decode_protobuf must not drop a frame it cannot fast-path."""
        pytest.importorskip("blackboxprotobuf")
        from narwal_client.client import NarwalClient

        client = NarwalClient("127.0.0.1")
        decoded = client._decode_protobuf(AMBIGUOUS_PAYLOAD)
        assert client._decode_fallbacks == 1
        assert decoded  # bbpb result, not an empty dict

    def test_client_fast_path_does_not_count_as_fallback(self) -> None:
        from narwal_client.client import NarwalClient

        client = NarwalClient("127.0.0.1")
        assert client._decode_protobuf(_varint(3, 9)) == {"3": 9}
        assert client._decode_fallbacks == 0
