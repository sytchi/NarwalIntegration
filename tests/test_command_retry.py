"""Tests for command recovery: reconnect, force a wake, re-send once.

Covers the failure mode from 2026-08-22: the WebSocket library dropped the
connection on a late pong, `ws.send()` raised straight through the vacuum
service, and the resume script died. Nothing retried, so the robot only moved
again after a human pressed "wake robot" and then resume a second time.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets.exceptions

from narwal_client.client import (
    NarwalClient,
    NarwalCommandError,
    NarwalConnectionError,
    NarwalTransportError,
)
from narwal_client.const import (
    TOPIC_CMD_RESUME,
    TOPIC_CMD_START_CLEAN,
    CommandResult,
)
from narwal_client.protocol import PROTOBUF_FIELD5_TAG, build_frame

PREFIX = "/QoEsI5qYXO/dev"

# field 1 = varint 1 (CommandResult.SUCCESS), in a field5 response frame
SUCCESS_RESPONSE = build_frame(
    f"{PREFIX}/{TOPIC_CMD_RESUME}", b"\x08\x01", header_byte=PROTOBUF_FIELD5_TAG
)


@pytest.fixture(autouse=True, scope="module")
def _leave_a_running_loop_behind():
    """Hand a fresh event loop back to whatever module runs next."""
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


class FakeWebSocket:
    """Socket that fails the first N sends, then accepts everything.

    A send that lands answers like the real listener loop does: by pushing a
    field5 response onto the client's queue. `respond_from` says which
    successful send is the first to get an answer, so a test can make an
    attempt time out without touching the client internals.
    """

    def __init__(
        self,
        fail_sends: int = 0,
        respond_from: int = 1,
        exc: Exception | None = None,
    ) -> None:
        self.fail_sends = fail_sends
        self.respond_from = respond_from
        self.exc = exc or websockets.exceptions.ConnectionClosedError(None, None)
        self.sent: list[bytes] = []
        self.client: NarwalClient | None = None

    async def send(self, frame: bytes) -> None:
        if self.fail_sends > 0:
            self.fail_sends -= 1
            raise self.exc
        self.sent.append(frame)
        if self.client is not None and len(self.sent) >= self.respond_from:
            feed_response(self.client)

    async def close(self) -> None:
        pass


def make_client(ws: FakeWebSocket) -> NarwalClient:
    """Client wired to a fake socket, as if the listener were running."""
    client = NarwalClient("10.0.0.1", device_id="dev")
    client._ws = ws
    client._connected.set()
    client._listener_active = True
    client._robot_awake = True
    ws.client = client
    return client


def feed_response(client: NarwalClient, count: int = 1) -> None:
    """Queue field5 responses the way the listener loop would."""
    from narwal_client.protocol import parse_frame

    for _ in range(count):
        client._response_queue.put_nowait(parse_frame(SUCCESS_RESPONSE))


class TestTransportFailureRetries:
    """A frame that never left the socket is always safe to re-send."""

    async def test_retries_after_send_fails(self) -> None:
        ws = FakeWebSocket(fail_sends=1)
        client = make_client(ws)
        woken: list[bool] = []

        async def fake_wake(timeout: float = 0.0, force: bool = False) -> bool:
            woken.append(force)
            return True

        client.wake = fake_wake  # type: ignore[method-assign]

        resp = await client.send_command(TOPIC_CMD_RESUME, timeout=1.0)

        assert resp.result_code == CommandResult.SUCCESS
        assert len(ws.sent) == 1  # the retry landed
        assert woken == [True]  # and it forced the wake burst

    async def test_retries_a_non_idempotent_command_too(self) -> None:
        """start_clean is unsafe to repeat only when it may have been executed."""
        ws = FakeWebSocket(fail_sends=1)
        client = make_client(ws)
        client.wake = _always_awake  # type: ignore[method-assign]

        await client.send_command(TOPIC_CMD_START_CLEAN, timeout=1.0)

        assert len(ws.sent) == 1

    async def test_gives_up_when_both_attempts_fail(self) -> None:
        ws = FakeWebSocket(fail_sends=2)
        client = make_client(ws)
        client.wake = _always_awake  # type: ignore[method-assign]

        with pytest.raises(NarwalTransportError):
            await client.send_command(TOPIC_CMD_RESUME, timeout=1.0)

    async def test_reconnect_failure_raises_the_original_error(self) -> None:
        """No socket and no listener to bring one back — fail without hanging."""
        client = NarwalClient("10.0.0.1", device_id="dev")
        client._ws = None
        client.wake = _fail_if_called  # type: ignore[method-assign]

        with pytest.raises(NarwalConnectionError):
            await client.send_command(TOPIC_CMD_RESUME, timeout=0.05)


class TestAmbiguousFailure:
    """A silent robot may still have acted, so only safe topics repeat."""

    async def test_retries_idempotent_command_on_timeout(self) -> None:
        # only the second send is answered, so the first attempt times out
        ws = FakeWebSocket(respond_from=2)
        client = make_client(ws)
        client.wake = _always_awake  # type: ignore[method-assign]

        resp = await client.send_command(TOPIC_CMD_RESUME, timeout=0.1)

        assert resp.result_code == CommandResult.SUCCESS
        assert len(ws.sent) == 2  # sent twice — resume is safe to repeat

    async def test_does_not_retry_clean_start_on_timeout(self) -> None:
        ws = FakeWebSocket(respond_from=99)  # nothing is ever answered
        client = make_client(ws)
        client.wake = _always_awake  # type: ignore[method-assign]

        with pytest.raises(NarwalCommandError):
            await client.send_command(TOPIC_CMD_START_CLEAN, timeout=0.05)

        assert len(ws.sent) == 1  # never repeated — could start a second job


class TestEnsureAwake:
    """The robot_awake flag alone is not evidence the robot is listening."""

    async def test_stale_broadcast_forces_a_wake_burst(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="dev")
        client._robot_awake = True
        client._last_broadcast_time = _monotonic_ago(30.0)
        forced: list[bool] = []

        async def fake_wake(timeout: float = 0.0, force: bool = False) -> bool:
            forced.append(force)
            return True

        client.wake = fake_wake  # type: ignore[method-assign]

        assert await client.ensure_awake(timeout=1.0) is True
        assert forced == [True]

    async def test_fresh_broadcast_skips_the_burst(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="dev")
        client._robot_awake = True
        client._last_broadcast_time = _monotonic_ago(0.5)
        client.wake = _fail_if_called  # type: ignore[method-assign]

        assert await client.ensure_awake(timeout=1.0) is True

    async def test_asleep_robot_gets_a_burst(self) -> None:
        client = NarwalClient("10.0.0.1", device_id="dev")
        client._robot_awake = False
        called: list[bool] = []

        async def fake_wake(timeout: float = 0.0, force: bool = False) -> bool:
            called.append(force)
            return False

        client.wake = fake_wake  # type: ignore[method-assign]

        assert await client.ensure_awake(timeout=1.0) is False
        assert called == [False]  # wake() bursts anyway when the flag is False


def _monotonic_ago(seconds: float) -> float:
    import time

    return time.monotonic() - seconds


async def _always_awake(timeout: float = 0.0, force: bool = False) -> bool:
    return True


async def _fail_if_called(timeout: float = 0.0, force: bool = False) -> bool:
    raise AssertionError("wake burst sent while the robot was broadcasting")
