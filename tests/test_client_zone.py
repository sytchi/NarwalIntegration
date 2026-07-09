"""Tests for the zone-clean payload (_build_zone_clean_payload) and start_zone."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import blackboxprotobuf

from narwal_client.client import NarwalClient
from narwal_client.const import CommandResult
from narwal_client.models import CommandResponse


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _task(payload: bytes) -> dict:
    decoded, _ = blackboxprotobuf.decode_message(payload)
    return decoded["1"]


def _items(task: dict) -> list[dict]:
    items = task["2"]
    return items if isinstance(items, list) else [items]


CAPTURED_RECT = (-21, -23, 29, 29)
CAPTURED_VERTICES = [
    {"1": -21, "2": 29},
    {"1": 29, "2": 29},
    {"1": 29, "2": -23},
    {"1": -21, "2": -23},
]


class TestBuildZoneCleanPayload:
    def test_matches_captured_zone_task(self) -> None:
        client = NarwalClient("127.0.0.1")
        task = _task(client._build_zone_clean_payload([CAPTURED_RECT], 1))
        assert task["1"] == 1
        assert task["5"] == 4
        assert task["3"] == {"1": 1}
        item = _items(task)[0]
        zone = item["1"]
        assert zone["1"] == 2
        assert zone["2"] == 1
        assert zone["3"] == CAPTURED_VERTICES
        assert len(zone["4"]) > 0
        assert item["3"] == 1
        assert item["2"]["1"] == 4
        assert item["2"].get("8") == 1 and item["2"].get("9") == 1

    def test_corner_order_normalized(self) -> None:
        client = NarwalClient("127.0.0.1")
        for rect in [(29, 29, -21, -23), (-21, 29, 29, -23), (29, -23, -21, 29)]:
            zone = _items(_task(client._build_zone_clean_payload([rect], 1)))[0]["1"]
            assert zone["3"] == CAPTURED_VERTICES, f"corners {rect}"

    def test_negative_coordinates_survive_roundtrip(self) -> None:
        client = NarwalClient("127.0.0.1")
        zone = _items(_task(client._build_zone_clean_payload([(-217, -47, 53, 152)], 1)))[0]["1"]
        assert {v.get("1", 0) for v in zone["3"]} == {-217, 53}
        assert {v.get("2", 0) for v in zone["3"]} == {-47, 152}

    def test_multiple_zones_ordered(self) -> None:
        client = NarwalClient("127.0.0.1")
        items = _items(_task(client._build_zone_clean_payload(
            [(0, 0, 10, 10), (20, 20, 30, 30)], 1)))
        assert len(items) == 2
        assert [i["1"]["2"] for i in items] == [1, 2]
        assert [i["3"] for i in items] == [1, 2]
        assert all(i["1"]["1"] == 2 for i in items)

    def test_param_settings_forwarded(self) -> None:
        client = NarwalClient("127.0.0.1")
        param = _items(_task(client._build_zone_clean_payload(
            [(0, 0, 5, 5)], 7, fan=3, water=3, mop_strength=2, passes=2)))[0]["2"]
        assert param["2"] == 3
        assert param["3"] == 2
        assert param["4"] == 3
        assert param["7"] == 2

    def test_coverage_fills_rectangle_interior(self) -> None:
        client = NarwalClient("127.0.0.1")
        cov = _items(_task(client._build_zone_clean_payload([(0, 0, 20, 20)], 1)))[0]["1"]["4"]
        assert len(cov) > 20
        for p in cov:
            assert 0 < p.get("1", 0) < 20 and 0 < p.get("2", 0) < 20

    def test_tiny_rectangle_has_one_point(self) -> None:
        assert len(NarwalClient("127.0.0.1")._zone_coverage(5, 5, 6, 6)) >= 1


class TestStartZone:
    def _client(self) -> NarwalClient:
        client = NarwalClient("127.0.0.1")
        client._ws = AsyncMock()
        client.state.map_data = MagicMock(map_id=1)
        return client

    def test_empty_zones_not_applicable(self) -> None:
        client = self._client()
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            resp = _run(client.start_zone([]))
        assert resp.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_called()

    def test_sends_clean_task_topic(self) -> None:
        client = self._client()
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = success
            resp = _run(client.start_zone([CAPTURED_RECT]))
        assert resp is success
        assert mock_send.await_args.args[0] == "clean/start_clean"
        zone = _items(_task(mock_send.await_args.kwargs["payload"]))[0]["1"]
        assert zone["1"] == 2
        assert zone["3"] == CAPTURED_VERTICES

    def test_missing_map_id_not_applicable(self) -> None:
        client = self._client()
        client.state.map_data = MagicMock(map_id=0)
        with patch.object(client, "get_map", new_callable=AsyncMock) as mock_map, \
             patch.object(client, "send_command", new_callable=AsyncMock) as mock_send:
            mock_map.return_value = MagicMock(map_id=0)
            resp = _run(client.start_zone([CAPTURED_RECT]))
        assert resp.result_code == CommandResult.NOT_APPLICABLE
        mock_send.assert_not_called()

    def test_retries_while_docked(self) -> None:
        client = self._client()
        client.state.update_from_base_status({"3": {"1": 10, "10": 1}})
        assert client.state.is_docked
        na = CommandResponse(result_code=CommandResult.NOT_APPLICABLE)
        success = CommandResponse(result_code=CommandResult.SUCCESS)
        with patch.object(client, "send_command", new_callable=AsyncMock) as mock_send, \
             patch("narwal_client.client.asyncio.sleep", new_callable=AsyncMock):
            mock_send.side_effect = [na, success]
            resp = _run(client.start_zone([CAPTURED_RECT]))
        assert mock_send.await_count == 2
        assert resp is success


class TestStartCleanMode:
    """Mode threading into the clean/start_clean payloads."""

    def _client(self):
        from narwal_client.client import NarwalClient

        return NarwalClient("127.0.0.1")

    def test_zone_payload_encodes_selected_mode(self) -> None:
        import blackboxprotobuf

        from narwal_client.const import CleanMode

        client = self._client()
        payload = client._build_zone_clean_payload(
            [(10, 10, 20, 20)], map_id=7, clean_mode=CleanMode.SWEEP
        )
        decoded, _ = blackboxprotobuf.decode_message(payload)
        task = decoded["1"]
        item = task["2"] if isinstance(task["2"], dict) else task["2"][0]
        assert item["2"]["1"] == 2, "CleanParam mode should be 2 (vacuum)"
        assert task["5"] == 1, "taskType should be 1 (VACUUM)"

    def test_zone_payload_default_is_vacuum_mop(self) -> None:
        import blackboxprotobuf

        client = self._client()
        payload = client._build_zone_clean_payload([(10, 10, 20, 20)], map_id=7)
        decoded, _ = blackboxprotobuf.decode_message(payload)
        task = decoded["1"]
        item = task["2"] if isinstance(task["2"], dict) else task["2"][0]
        assert item["2"]["1"] == 4
        assert task["5"] == 4

    def test_whole_payload_uses_zone_type_3_and_mode(self) -> None:
        import blackboxprotobuf

        from narwal_client.const import CleanMode

        client = self._client()
        payload = client._build_whole_clean_payload(
            map_id=7, clean_mode=CleanMode.SWEEP_THEN_MOP
        )
        decoded, _ = blackboxprotobuf.decode_message(payload)
        task = decoded["1"]
        item = task["2"] if isinstance(task["2"], dict) else task["2"][0]
        assert item["1"]["1"] == 3, "ZoneOption type should be 3 (whole map)"
        assert item["2"]["1"] == 5, "CleanParam mode should be 5 (vacuum-then-mop)"
        assert task["5"] == 3, "taskType should be 3 (VACUUM_THEN_MOP)"


class TestRoomStartClean:
    """Per-room clean/start_clean payload (ZoneOption type 1)."""

    def _client(self):
        from narwal_client.client import NarwalClient
        return NarwalClient("127.0.0.1")

    def test_room_payload_type1_and_mode(self) -> None:
        import blackboxprotobuf
        from narwal_client.const import CleanMode

        client = self._client()
        payload = client._build_room_startclean_payload(
            [6], map_id=1, clean_mode=CleanMode.MOP
        )
        decoded, _ = blackboxprotobuf.decode_message(payload)
        task = decoded["1"]
        item = task["2"] if isinstance(task["2"], dict) else task["2"][0]
        assert item["1"]["1"] == 1, "ZoneOption type should be 1 (room)"
        assert item["1"]["2"] == 6, "room_id should be in ZoneOption field 2"
        assert item["2"]["1"] == 3, "CleanParam mode should be 3 (mop)"
        assert task["5"] == 2, "taskType should be 2 (MOP)"

    def test_room_payload_multiple_rooms(self) -> None:
        import blackboxprotobuf

        client = self._client()
        payload = client._build_room_startclean_payload([2, 6], map_id=1)
        decoded, _ = blackboxprotobuf.decode_message(payload)
        items = decoded["1"]["2"]
        assert isinstance(items, list) and len(items) == 2
        assert {items[0]["1"]["2"], items[1]["1"]["2"]} == {2, 6}
