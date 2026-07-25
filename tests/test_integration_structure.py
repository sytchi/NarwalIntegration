"""Tests for integration structural requirements (CLN-01, CLN-02).

CLN-01: camera.py is the sole map entity using CameraEntity (MJPEG streaming)
CLN-02: narwal_client embedded copy is in sync with canonical copy
"""

from __future__ import annotations

import filecmp
import os
from pathlib import Path

import tests.ha_stubs

tests.ha_stubs.install()


ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "custom_components" / "narwal"


class TestCLN01PlatformRegistration:
    """CLN-01: camera.py is the sole map entity using CameraEntity."""

    def test_platform_camera_in_platforms(self) -> None:
        """Platform.CAMERA is registered in PLATFORMS list."""
        from homeassistant.const import Platform

        from custom_components.narwal.const import PLATFORMS

        assert Platform.CAMERA in PLATFORMS

    def test_platform_image_not_in_platforms(self) -> None:
        """Platform.IMAGE is NOT in PLATFORMS (CameraEntity is used, not ImageEntity)."""
        from homeassistant.const import Platform

        from custom_components.narwal.const import PLATFORMS

        assert Platform.IMAGE not in PLATFORMS

    def test_camera_py_exists(self) -> None:
        """camera.py exists as the map entity module."""
        assert (INTEGRATION / "camera.py").is_file()

    def test_image_py_not_present(self) -> None:
        """image.py does not exist (CameraEntity is used instead of ImageEntity)."""
        assert not (INTEGRATION / "image.py").is_file()

    def test_camera_inherits_camera_entity(self) -> None:
        """NarwalMapCamera inherits from Camera (CameraEntity) — verified via AST."""
        import ast

        source = (INTEGRATION / "camera.py").read_text()
        tree = ast.parse(source)

        # Find the NarwalMapCamera class and check its bases
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "NarwalMapCamera":
                base_names = []
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        base_names.append(base.id)
                    elif isinstance(base, ast.Attribute):
                        base_names.append(base.attr)
                assert "Camera" in base_names or "NarwalEntity" in base_names, (
                    f"NarwalMapCamera bases: {base_names}, expected Camera or NarwalEntity"
                )
                return
        raise AssertionError("NarwalMapCamera class not found in camera.py")

    def test_no_other_map_entities(self) -> None:
        """Only camera.py provides map entities — no image.py or duplicate."""
        map_modules = [
            f for f in os.listdir(INTEGRATION)
            if f.endswith(".py") and f.startswith("image")
        ]
        assert map_modules == [], f"Unexpected map entity modules: {map_modules}"


class TestCLN02NarwalClientSync:
    """CLN-02: narwal_client embedded copy matches canonical copy."""

    CANONICAL = ROOT / "narwal_client"
    EMBEDDED = INTEGRATION / "narwal_client"

    def test_canonical_exists(self) -> None:
        """Canonical narwal_client/ directory exists."""
        assert self.CANONICAL.is_dir()

    def test_embedded_exists(self) -> None:
        """Embedded narwal_client/ directory exists."""
        assert self.EMBEDDED.is_dir()

    def test_same_file_list(self) -> None:
        """Both copies have the same set of files (code + bundled fonts)."""
        canonical_py = sorted(
            str(f.relative_to(self.CANONICAL)) for f in self.CANONICAL.rglob("*")
            if f.is_file() and "__pycache__" not in str(f)
            and f.suffix != ".pyc"
        )
        embedded_py = sorted(
            str(f.relative_to(self.EMBEDDED)) for f in self.EMBEDDED.rglob("*")
            if f.is_file() and "__pycache__" not in str(f)
            and f.suffix != ".pyc"
        )
        assert canonical_py == embedded_py, (
            f"File list mismatch:\n"
            f"  Canonical only: {set(canonical_py) - set(embedded_py)}\n"
            f"  Embedded only: {set(embedded_py) - set(canonical_py)}"
        )

    def test_files_byte_identical(self) -> None:
        """All files (code + bundled fonts) are byte-for-byte identical."""
        canonical_files = sorted(
            f for f in self.CANONICAL.rglob("*")
            if f.is_file() and "__pycache__" not in str(f)
            and f.suffix != ".pyc"
        )
        for canonical_file in canonical_files:
            relative = canonical_file.relative_to(self.CANONICAL)
            embedded_file = self.EMBEDDED / relative
            assert embedded_file.is_file(), f"Missing embedded file: {relative}"
            assert filecmp.cmp(
                str(canonical_file), str(embedded_file), shallow=False
            ), f"Content mismatch: {relative}"
