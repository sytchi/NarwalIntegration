"""Tests for the camera zone overlay: active_zones set/clear + render."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from narwal_client.client import NarwalClient
from narwal_client.const import CommandResult
from narwal_client.models import CommandResponse


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client() -> NarwalClient:
    c = NarwalClient("127.0.0.1")
    c._ws = AsyncMock()
    c.state.map_data = MagicMock(map_id=1)
    return c


def test_start_zone_success_sets_active_zones() -> None:
    c = _client()
    with patch.object(c, "send_command", new_callable=AsyncMock) as send:
        send.return_value = CommandResponse(result_code=CommandResult.SUCCESS)
        _run(c.start_zone([(29, 29, -21, -23)]))
    assert c.state.active_zones == [(-21, -23, 29, 29)]  # normalized


def test_start_zone_failure_leaves_zones_empty() -> None:
    c = _client()
    c.state.update_from_base_status({"3": {"1": 1}})  # not docked -> no retry
    with patch.object(c, "send_command", new_callable=AsyncMock) as send:
        send.return_value = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        _run(c.start_zone([(0, 0, 10, 10)]))
    assert c.state.active_zones == []


def test_return_to_base_clears_zones() -> None:
    c = _client()
    c.state.active_zones = [(0, 0, 10, 10)]
    with patch.object(c, "send_command", new_callable=AsyncMock) as send:
        send.return_value = CommandResponse(result_code=CommandResult.SUCCESS)
        _run(c.return_to_base())
    assert c.state.active_zones == []


def test_stop_clears_zones() -> None:
    c = _client()
    c.state.active_zones = [(0, 0, 10, 10)]
    with patch.object(c, "send_command", new_callable=AsyncMock) as send:
        send.return_value = CommandResponse(result_code=CommandResult.SUCCESS)
        _run(c.stop())
    assert c.state.active_zones == []


def test_start_clean_whole_clears_zones() -> None:
    # Whole-house clean is the primary path on fw v01.07+ and must drop the
    # stale zone preview, otherwise the amber overlay from a previous zone
    # clean persists on the camera map (regression: task 18043f5).
    c = _client()
    c.state.active_zones = [(0, 0, 10, 10)]
    with patch.object(c, "send_command", new_callable=AsyncMock) as send:
        send.return_value = CommandResponse(result_code=CommandResult.SUCCESS)
        _run(c.start_clean_whole())
    assert c.state.active_zones == []


def test_start_clean_rooms_clears_zones() -> None:
    c = _client()
    c.state.active_zones = [(0, 0, 10, 10)]
    with patch.object(c, "send_command", new_callable=AsyncMock) as send:
        send.return_value = CommandResponse(result_code=CommandResult.SUCCESS)
        _run(c.start_clean_rooms([1]))
    assert c.state.active_zones == []


def test_render_overlay_with_zone_returns_png() -> None:
    from PIL import Image

    from narwal_client.map_renderer import render_overlay
    base = Image.new("RGB", (60, 80), (20, 20, 20))
    png = render_overlay(base, 60, 80, zones=[(5, 5, 30, 40)])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # Amber fill must tint pixels inside the zone (grid (17, 22) is inside;
    # image y = 80 - 1 - 22 = 57)
    import io as _io
    img = Image.open(_io.BytesIO(png))
    assert img.getpixel((17, 57)) != (20, 20, 20)
