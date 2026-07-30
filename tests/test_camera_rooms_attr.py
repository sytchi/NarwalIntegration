"""The `rooms` attribute must never be computed on the event loop.

compute_room_outlines decodes the whole saved-map grid and traces every
room contour - hundreds of ms on typical HA hardware. It used to run
inline in the `extra_state_attributes` property, blocking the loop each
time the saved map changed. The property must now only read a cache that
the render path fills through an executor.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.camera import NarwalMapCamera  # noqa: E402


def _camera(map_data) -> NarwalMapCamera:
    coordinator = MagicMock()
    coordinator.config_entry.data = {"device_id": "dev1"}
    coordinator.client.state.map_data = map_data
    return NarwalMapCamera(coordinator, scale=4)


def _map(created_at: int = 111) -> MagicMock:
    m = MagicMock()
    m.width = 20
    m.height = 30
    m.origin_x = -47
    m.origin_y = -217
    m.created_at = created_at
    m.compressed_map = b"whatever"
    m.rooms = []
    return m


def test_attributes_never_call_compute_room_outlines() -> None:
    """The property must not touch the expensive tracer at all."""
    cam = _camera(_map())
    with patch(
        "custom_components.narwal.narwal_client.map_renderer.compute_room_outlines"
    ) as tracer:
        attrs = cam.extra_state_attributes
    tracer.assert_not_called()
    # Cache empty -> key absent rather than a misleading empty dict
    assert "rooms" not in attrs
    assert "calibration_points" in attrs
    assert attrs["render_count"] == 0


def test_attributes_return_cached_rooms() -> None:
    cam = _camera(_map())
    cached = {"1": {"name": "Kuchnia", "outline": [[0, 0]], "x": 1, "y": 2}}
    cam._rooms_attr = cached
    cam._rooms_attr_ts = 111

    with patch(
        "custom_components.narwal.narwal_client.map_renderer.compute_room_outlines"
    ) as tracer:
        attrs = cam.extra_state_attributes
    tracer.assert_not_called()
    assert attrs["rooms"] is cached


def test_compute_is_pure_and_leaves_cache_alone() -> None:
    """The executor-side helper must not mutate entity state.

    It runs in a worker thread, so writing the cache from there would be
    a cross-thread mutation; the awaiting coroutine assigns it instead.
    """
    static_map = _map()
    static_map.rooms = [MagicMock(room_id=1, display_name="Kuchnia")]
    cam = _camera(static_map)

    with patch(
        "custom_components.narwal.narwal_client.map_renderer.compute_room_outlines",
        return_value={"1": {"outline": [[0, 0]], "x": 1, "y": 2}},
    ):
        rooms = cam._compute_rooms_attribute(static_map)

    assert rooms == {"1": {"outline": [[0, 0]], "x": 1, "y": 2, "name": "Kuchnia"}}
    assert cam._rooms_attr is None
    assert cam._rooms_attr_ts == -1


def test_compute_survives_a_broken_map() -> None:
    static_map = _map()
    cam = _camera(static_map)
    with patch(
        "custom_components.narwal.narwal_client.map_renderer.compute_room_outlines",
        side_effect=ValueError("corrupt grid"),
    ):
        assert cam._compute_rooms_attribute(static_map) == {}
