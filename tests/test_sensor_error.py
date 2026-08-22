"""Tests for the error sensor's slug mapping and attributes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal import sensor as sensor_mod  # noqa: E402
from custom_components.narwal.const import ERROR_CODE_SLUGS  # noqa: E402
from custom_components.narwal.sensor import NarwalErrorSensor  # noqa: E402

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "narwal"

# Live-captured fault: 0x02310031, 机器人被抱起悬空 (robot lifted off the floor)
ROBOT_LIFTED = 36765745
# Live-captured fault: 0x02110040, mop drying interrupted (robot left the
# dock >5 min during drying)
MOP_DRYING_INTERRUPTED = 34668608
# Live-captured fault: 0x02020030, 扫地时右边刷异常 (side brush fault while
# sweeping) — undocumented, absent from the help-center language packs
SIDE_BRUSH_SWEEPING = 33685552


def _make_sensor(error_code: int, message: str = "", severity: int = 2) -> NarwalErrorSensor:
    coordinator = MagicMock()
    state = MagicMock()
    state.error_code = error_code
    state.error_message = message
    state.error_severity = severity
    coordinator.data = state
    sensor = NarwalErrorSensor.__new__(NarwalErrorSensor)
    sensor.coordinator = coordinator
    return sensor


class TestErrorSlugs:
    def test_no_error(self) -> None:
        assert _make_sensor(0).native_value == "no_error"
        assert _make_sensor(0).extra_state_attributes is None

    def test_known_codes_map_to_slugs(self) -> None:
        assert _make_sensor(ROBOT_LIFTED).native_value == "robot_lifted"
        assert (
            _make_sensor(MOP_DRYING_INTERRUPTED).native_value
            == "mop_drying_interrupted"
        )

    def test_live_undocumented_side_brush_code(self) -> None:
        assert _make_sensor(SIDE_BRUSH_SWEEPING).native_value == "side_brush_error"

    def test_unknown_code_falls_back_to_translatable_slug(self) -> None:
        """Unknown codes must not leak a bare number into the UI."""
        assert _make_sensor(12345678).native_value == "unknown_error"

    def test_unknown_code_keeps_the_number_in_attributes(self) -> None:
        attrs = _make_sensor(12345678).extra_state_attributes
        assert attrs["code"] == 12345678
        assert attrs["code_hex"] == "0x00BC614E"

    def test_unknown_code_logged_once(self, caplog) -> None:
        sensor_mod._LOGGED_UNKNOWN_CODES.discard(12345678)
        with caplog.at_level(logging.WARNING, logger=sensor_mod.__name__):
            values = [_make_sensor(12345678, message="whatever").native_value for _ in range(3)]
        assert values == ["unknown_error"] * 3
        warnings = [r for r in caplog.records if "12345678" in r.getMessage()]
        assert len(warnings) == 1

    def test_none_state(self) -> None:
        sensor = _make_sensor(0)
        sensor.coordinator.data = None
        assert sensor.native_value is None
        assert sensor.extra_state_attributes is None

    def test_attributes_expose_code_and_hex(self) -> None:
        attrs = _make_sensor(ROBOT_LIFTED, message="机器人被抱起悬空").extra_state_attributes
        assert attrs == {
            "code": ROBOT_LIFTED,
            "code_hex": "0x02310031",
            "message": "机器人被抱起悬空",
            "severity": 2,
            "help_url": (
                "https://help.narwal.com/helpcenter/vall/#/p2/question/all"
                "?eType=1&code=02310031&lang=en-US"
            ),
        }

    def test_slugs_are_valid_translation_keys(self) -> None:
        for slug in ERROR_CODE_SLUGS.values():
            assert slug == slug.lower()
            assert all(c.isalnum() or c == "_" for c in slug), slug


class TestErrorTranslations:
    """Every slug must be translated in strings.json and all translations."""

    FILES = [
        "strings.json",
        "translations/en.json",
        "translations/pl.json",
        "translations/fr.json",
    ]

    def test_all_slugs_translated_everywhere(self) -> None:
        expected = set(ERROR_CODE_SLUGS.values()) | {"no_error", "unknown_error"}
        for rel in self.FILES:
            data = json.loads((COMPONENT_DIR / rel).read_text())
            states = data["entity"]["sensor"]["error"].get("state", {})
            missing = expected - set(states)
            assert not missing, f"{rel} missing error states: {sorted(missing)}"
            for slug, text in states.items():
                assert text.strip(), f"{rel}: empty translation for {slug}"
