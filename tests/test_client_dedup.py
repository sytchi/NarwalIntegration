"""Tests for the WebSocket listener hot path.

Covers two performance fixes in narwal_client.client:

* `start_listening` yields to the event loop between frames, so a burst of
  buffered frames is not handled inside a single loop iteration.
* `_handle_message` drops broadcasts whose payload repeats the previous one
  byte for byte, before the protobuf decode and the entity fan-out.
"""

from __future__ import annotations

import asyncio

import pytest

from narwal_client.client import DEDUP_CACHE_MAX_TOPICS, NarwalClient
from narwal_client.protocol import build_frame

PREFIX = "/QoEsI5qYXO/test_device_id_000000000000000"


@pytest.fixture(autouse=True, scope="module")
def _leave_a_running_loop_behind():
    """Hand a fresh event loop back to whatever module runs next.

    pytest-asyncio clears the current event loop after the last async test
    in this module, and some older test modules still call
    `asyncio.get_event_loop().run_until_complete(...)`, which then raises.
    """
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


# field 3 = varint 1, decodes fine and is stable across calls
BASE_STATUS_A = b"\x1a\x02\x08\x01"
BASE_STATUS_B = b"\x1a\x02\x08\x02"


def frame(short_topic: str, payload: bytes) -> bytes:
    return build_frame(f"{PREFIX}/{short_topic}", payload)


def make_client() -> tuple[NarwalClient, list, list]:
    """Client instrumented with decode and push counters."""
    client = NarwalClient("10.0.0.1", device_id="dev")
    decoded: list[bytes] = []
    pushed: list[object] = []

    original = client._decode_protobuf

    def counting_decode(payload: bytes):
        decoded.append(payload)
        return original(payload)

    client._decode_protobuf = counting_decode  # type: ignore[method-assign]
    client.on_state_update = pushed.append
    return client, decoded, pushed


class TestDuplicateBroadcastSuppression:
    """Repeated payloads must not reach the decoder or the coordinator."""

    async def test_repeated_payload_decoded_once(self) -> None:
        client, decoded, pushed = make_client()
        f = frame("status/robot_base_status", BASE_STATUS_A)

        for _ in range(5):
            await client._handle_message(f)

        assert len(decoded) == 1
        assert len(pushed) == 1

    async def test_changed_payload_passes_through(self) -> None:
        client, decoded, pushed = make_client()

        await client._handle_message(frame("status/robot_base_status", BASE_STATUS_A))
        await client._handle_message(frame("status/robot_base_status", BASE_STATUS_A))
        await client._handle_message(frame("status/robot_base_status", BASE_STATUS_B))
        await client._handle_message(frame("status/robot_base_status", BASE_STATUS_A))

        assert len(decoded) == 3
        assert len(pushed) == 3

    async def test_dedup_is_per_topic(self) -> None:
        """The same bytes on two topics are two distinct updates."""
        client, decoded, _ = make_client()

        await client._handle_message(frame("status/download_status", b"\x08\x01"))
        await client._handle_message(frame("upgrade/upgrade_status", b"\x08\x01"))
        await client._handle_message(frame("status/download_status", b"\x08\x01"))

        assert len(decoded) == 2

    async def test_state_unchanged_by_suppression(self) -> None:
        """Skipping a repeat leaves exactly the state the repeat would set."""
        client, _, _ = make_client()
        f = frame("status/download_status", b"\x08\x07")

        await client._handle_message(f)
        expected = client.state.download_status
        for _ in range(4):
            await client._handle_message(f)

        assert client.state.download_status == expected == 7


class TestLivenessBookkeeping:
    """Suppressed frames still prove the robot is alive."""

    async def test_duplicate_marks_robot_awake(self) -> None:
        client, _, _ = make_client()
        f = frame("status/robot_base_status", BASE_STATUS_A)

        await client._handle_message(f)
        client._robot_awake = False
        client._last_broadcast_time = 0.0

        await client._handle_message(f)  # duplicate -> suppressed

        assert client.robot_awake is True
        assert client._last_broadcast_time > 0.0

    async def test_duplicate_refreshes_broadcast_time(self) -> None:
        client, _, _ = make_client()
        f = frame("status/upgrade_status", BASE_STATUS_A)

        await client._handle_message(f)
        first = client._last_broadcast_time
        await asyncio.sleep(0.01)
        await client._handle_message(f)

        assert client._last_broadcast_time > first

    async def test_duplicate_still_reaches_on_message(self) -> None:
        """The raw-frame hook keeps seeing every broadcast."""
        client, decoded, _ = make_client()
        seen: list[object] = []
        client.on_message = seen.append
        f = frame("status/robot_base_status", BASE_STATUS_A)

        await client._handle_message(f)
        await client._handle_message(f)

        assert len(seen) == 2
        assert len(decoded) == 1


class TestDedupExemptions:
    """Topics that stamp an arrival time must never be suppressed."""

    async def test_display_map_never_deduplicated(self) -> None:
        client, decoded, _ = make_client()
        f = frame("map/display_map", b"\x08\x01")

        await client._handle_message(f)
        await client._handle_message(f)

        assert len(decoded) == 2

    async def test_planned_trajectory_never_deduplicated(self) -> None:
        client, decoded, _ = make_client()
        f = frame("status/point_navi_plan_traj", b"\x08\x01")

        await client._handle_message(f)
        await client._handle_message(f)

        assert len(decoded) == 2

    async def test_display_map_freshness_keeps_advancing(self) -> None:
        client, _, _ = make_client()
        f = frame("map/display_map", b"\x08\x01")

        await client._handle_message(f)
        first = client._last_display_map_time
        await asyncio.sleep(0.01)
        await client._handle_message(f)

        assert client._last_display_map_time > first

    async def test_field5_response_never_deduplicated(self) -> None:
        """Command responses are routed to the queue, dedup must not eat them."""
        client, _, _ = make_client()
        raw = bytearray(frame("cmd/start_clean", b"\x08\x01"))
        raw[2] = 0x2A  # field5 tag

        await client._handle_message(bytes(raw))
        await client._handle_message(bytes(raw))

        assert client._response_queue.qsize() == 2
        assert client._last_payloads == {}


class TestDedupCache:
    """The payload cache must stay bounded and reset across reconnects."""

    async def test_cache_is_bounded(self) -> None:
        client, _, _ = make_client()

        for i in range(DEDUP_CACHE_MAX_TOPICS + 10):
            await client._handle_message(frame(f"status/topic_{i}", b"\x08\x01"))

        assert len(client._last_payloads) <= DEDUP_CACHE_MAX_TOPICS

    async def test_eviction_is_oldest_first(self) -> None:
        client, _, _ = make_client()

        for i in range(DEDUP_CACHE_MAX_TOPICS):
            await client._handle_message(frame(f"status/topic_{i}", b"\x08\x01"))
        assert "status/topic_0" in client._last_payloads

        await client._handle_message(frame("status/newcomer", b"\x08\x01"))

        assert "status/topic_0" not in client._last_payloads
        assert "status/newcomer" in client._last_payloads

    async def test_cache_cleared_on_listener_teardown(self) -> None:
        """After a reconnect the first frame of each topic must get through."""
        client, decoded, _ = make_client()
        f = frame("status/robot_base_status", BASE_STATUS_A)

        await client._handle_message(f)
        assert client._last_payloads

        await _run_listener(client, [])  # a full connect/disconnect cycle
        assert client._last_payloads == {}

        await client._handle_message(f)
        assert len(decoded) == 2


class _FakeWebSocket:
    """Async-iterable socket that hands over buffered frames without awaiting."""

    def __init__(self, frames: list[bytes], client: NarwalClient) -> None:
        self._frames = frames
        self._client = client

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for f in self._frames:
            yield f
        self._client._should_reconnect = False

    async def ping(self) -> None:  # pragma: no cover - not exercised here
        return None

    async def close(self) -> None:  # pragma: no cover - not exercised here
        return None


async def _run_listener(client: NarwalClient, frames: list[bytes]) -> None:
    client._ws = _FakeWebSocket(frames, client)
    client._connected.set()
    await client.start_listening()


class TestListenerYieldsBetweenFrames:
    """A burst of buffered frames must not monopolise one loop iteration."""

    async def test_other_tasks_run_between_frames(self) -> None:
        client, _, _ = make_client()
        frames = [
            frame("status/robot_base_status", bytes([0x1A, 0x02, 0x08, i]))
            for i in range(1, 6)
        ]

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        task = asyncio.create_task(ticker())
        try:
            await _run_listener(client, frames)
        finally:
            task.cancel()

        # One scheduling slot per frame at minimum; without the yield the
        # ticker would get at most a single turn for the whole burst.
        assert ticks >= len(frames)

    async def test_all_frames_still_processed_in_order(self) -> None:
        client, _, pushed = make_client()
        frames = [
            frame("status/robot_base_status", bytes([0x1A, 0x02, 0x08, i]))
            for i in range(1, 6)
        ]

        await _run_listener(client, frames)

        assert len(pushed) == len(frames)
        assert client.state.working_status.value == 5
