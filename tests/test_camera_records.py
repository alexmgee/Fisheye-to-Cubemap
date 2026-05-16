"""Tests for the per-sensor PINHOLE camera record builders in
metashape_cameras_to_colmap.py — both cubeface (one entry per fisheye
lens) and ERP (one entry per ERP sensor).

These tests pin the Method E refactor: cubeface no longer emits one
shared camera entry across all lenses; ERP no longer emits one entry
per view-slot.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load the exporter module via importlib to mirror the test_exporter_manifest
# pattern and avoid coupling to working-directory state.
_spec = importlib.util.spec_from_file_location(
    "exporter_for_camera_records",
    PROJECT_ROOT / "metashape_cameras_to_colmap.py",
)
exporter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(exporter)


# ─────────────────────────────────────────────────────────────────────────────
# Cubeface camera records
# ─────────────────────────────────────────────────────────────────────────────

def _lens(lens_label: str, face_width: int, face_height: int | None = None,
          image_count: int = 5) -> dict:
    """Build a minimal lens dict shaped like one entry in discovery['lenses']."""
    h = face_height if face_height is not None else face_width
    return {
        "lens_label": lens_label,
        "source_lens_label": lens_label,
        "path": f"/fake/{lens_label}",
        "layout": "station",
        "images": tuple(),
        "image_count": image_count,
        "mask_count": image_count,
        "stems": tuple(),
        "face_size_set": ((face_width, h),),
        "suffix_counts": {},
        "run_report": None,
    }


def _discovery(lenses: list[dict]) -> dict:
    return {
        "root": "/fake/processing",
        "lenses": tuple(lenses),
        "lens_count": len(lenses),
        "image_count": sum(int(l["image_count"]) for l in lenses),
        "layout_counts": {},
    }


def test_cubeface_camera_records_empty_discovery():
    cams, ids_by_lens, next_id = exporter.build_cubeface_camera_records(
        _discovery([]), start_camera_id=1,
    )
    assert cams == []
    assert ids_by_lens == {}
    assert next_id == 1


def test_cubeface_camera_records_single_lens():
    discovery = _discovery([_lens("sensor_3", 2048)])
    cams, ids_by_lens, next_id = exporter.build_cubeface_camera_records(
        discovery, start_camera_id=1,
    )
    assert len(cams) == 1
    assert cams[0]["camera_id"] == 1
    assert cams[0]["model"] == "PINHOLE"
    assert cams[0]["width"] == 2048
    assert cams[0]["height"] == 2048
    assert cams[0]["params"] == (1024.0, 1024.0, 1024.0, 1024.0)
    assert ids_by_lens == {"sensor_3": 1}
    assert next_id == 2


def test_cubeface_camera_records_two_lenses_same_face_width():
    """The Insta360 X5 case: two lenses, same face_width. After Method E,
    each lens still gets its own PINHOLE entry with identical intrinsics."""
    discovery = _discovery([_lens("sensor_3", 2048), _lens("sensor_4", 2048)])
    cams, ids_by_lens, next_id = exporter.build_cubeface_camera_records(
        discovery, start_camera_id=1,
    )
    assert len(cams) == 2
    assert ids_by_lens == {"sensor_3": 1, "sensor_4": 2}
    assert cams[0]["params"] == cams[1]["params"]  # identical intrinsics
    assert cams[0]["camera_id"] != cams[1]["camera_id"]  # distinct IDs
    assert next_id == 3


def test_cubeface_camera_records_two_lenses_different_face_widths():
    """The case Method E unlocks: two lenses at different face_widths.
    Previously errored with 'Expected exactly one cubeface image size'."""
    discovery = _discovery([_lens("sensor_3", 2048), _lens("sensor_4", 1024)])
    cams, ids_by_lens, next_id = exporter.build_cubeface_camera_records(
        discovery, start_camera_id=1,
    )
    assert len(cams) == 2
    assert cams[0]["width"] == 2048 and cams[0]["params"][0] == 1024.0
    assert cams[1]["width"] == 1024 and cams[1]["params"][0] == 512.0
    assert ids_by_lens == {"sensor_3": 1, "sensor_4": 2}
    assert next_id == 3


def test_cubeface_camera_records_start_camera_id_offset():
    """When called with start_camera_id > 1 (e.g. after ERP cameras
    already allocated), IDs should pick up from there."""
    discovery = _discovery([_lens("sensor_3", 2048), _lens("sensor_4", 2048)])
    cams, ids_by_lens, next_id = exporter.build_cubeface_camera_records(
        discovery, start_camera_id=5,
    )
    assert ids_by_lens == {"sensor_3": 5, "sensor_4": 6}
    assert cams[0]["camera_id"] == 5
    assert next_id == 7


def test_cubeface_camera_records_rejects_non_square():
    discovery = _discovery([_lens("sensor_3", 2048, face_height=1024)])
    with pytest.raises(exporter.ValidationError, match="must be square"):
        exporter.build_cubeface_camera_records(discovery, start_camera_id=1)


def test_cubeface_camera_records_rejects_multiple_face_sizes_within_lens():
    """If a single lens directory somehow contains faces at different sizes
    (e.g. corrupted output), the validator catches it."""
    lens = _lens("sensor_3", 2048)
    lens["face_size_set"] = ((2048, 2048), (1024, 1024))
    with pytest.raises(exporter.ValidationError, match="multiple cubeface image sizes"):
        exporter.build_cubeface_camera_records(_discovery([lens]), start_camera_id=1)


# ─────────────────────────────────────────────────────────────────────────────
# Cubeface image records use the per-lens map
# ─────────────────────────────────────────────────────────────────────────────

def test_cubeface_image_records_uses_per_lens_camera_id():
    """Image records stamped via the camera_id_by_lens map look up each
    image's lens_label and reference the right camera_id."""
    images = [
        {
            "lens_label": "sensor_3",
            "source_lens_label": "sensor_3",
            "stem": "img001",
            "suffix": "_dir_plusZ",
            "internal_face": "+Z",
            "filename_face_dir": "dir_plusZ",
            "extension": ".png",
            "image_path": "/fake/processing/sensor_3/images/img001_dir_plusZ.png",
            "image_name": "sensor_3/images/img001_dir_plusZ.png",
            "mask_path": None,
            "width": 2048,
            "height": 2048,
        },
        {
            "lens_label": "sensor_4",
            "source_lens_label": "sensor_4",
            "stem": "img001",
            "suffix": "_dir_plusZ",
            "internal_face": "+Z",
            "filename_face_dir": "dir_plusZ",
            "extension": ".png",
            "image_path": "/fake/processing/sensor_4/images/img001_dir_plusZ.png",
            "image_name": "sensor_4/images/img001_dir_plusZ.png",
            "mask_path": None,
            "width": 2048,
            "height": 2048,
        },
    ]
    discovery = {
        "root": "/fake/processing",
        "lenses": (
            {"lens_label": "sensor_3", "images": (images[0],), "face_size_set": ((2048, 2048),)},
            {"lens_label": "sensor_4", "images": (images[1],), "face_size_set": ((2048, 2048),)},
        ),
        "lens_count": 2,
        "image_count": 2,
    }
    records = exporter.build_cubeface_image_records(
        discovery,
        camera_id_by_lens={"sensor_3": 7, "sensor_4": 11},
        placeholder_poses=True,
    )
    by_lens = {r["image_name"]: r["camera_id"] for r in records}
    assert by_lens["sensor_3/images/img001_dir_plusZ.png"] == 7
    assert by_lens["sensor_4/images/img001_dir_plusZ.png"] == 11


def test_cubeface_image_records_rejects_unmapped_lens():
    image = {
        "lens_label": "sensor_3",
        "stem": "img001",
        "suffix": "_dir_plusZ",
        "internal_face": "+Z",
        "image_path": "/fake/img001_dir_plusZ.png",
        "image_name": "img001_dir_plusZ.png",
        "mask_path": None,
        "width": 2048,
        "height": 2048,
    }
    discovery = {
        "root": "/fake",
        "lenses": ({"lens_label": "sensor_3", "images": (image,), "face_size_set": ((2048, 2048),)},),
        "image_count": 1,
    }
    with pytest.raises(exporter.ValidationError, match="No camera_id allocated for lens"):
        exporter.build_cubeface_image_records(
            discovery,
            camera_id_by_lens={"sensor_99": 1},  # wrong lens
            placeholder_poses=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# ERP camera records: one per sensor
# ─────────────────────────────────────────────────────────────────────────────

def _erp_view(label: str, fx: float = 512.0, width: int = 1024) -> dict:
    return {
        "label": label,
        "dir_name": f"view_{label}",
        "images_dir": f"/fake/erp/{label}/images",
        "masks_dir": f"/fake/erp/{label}/masks",
        "width": width,
        "height": width,
        "fx": fx,
        "fy": fx,
        "cx": width / 2.0,
        "cy": width / 2.0,
    }


def test_erp_camera_records_single_sensor_reframe():
    """16 reframe views should collapse to 1 PINHOLE camera record."""
    erp_view_map = {
        "sensors": (
            {
                "sensor_id": 2,
                "views": tuple(_erp_view(f"v{i:02d}") for i in range(16)),
            },
        ),
    }
    cams, ids, next_id = exporter.build_erp_camera_records(erp_view_map, start_camera_id=1)
    assert len(cams) == 1
    assert cams[0]["camera_id"] == 1
    assert cams[0]["width"] == 1024
    assert ids == {2: 1}
    assert next_id == 2


def test_erp_camera_records_two_sensors():
    erp_view_map = {
        "sensors": (
            {"sensor_id": 2, "views": tuple(_erp_view(f"a{i:02d}") for i in range(6))},
            {"sensor_id": 5, "views": tuple(_erp_view(f"b{i:02d}", width=2048, fx=1024.0) for i in range(16))},
        ),
    }
    cams, ids, next_id = exporter.build_erp_camera_records(erp_view_map, start_camera_id=1)
    assert len(cams) == 2
    assert ids == {2: 1, 5: 2}
    assert cams[0]["width"] == 1024
    assert cams[1]["width"] == 2048
    assert next_id == 3


def test_erp_camera_records_validates_intrinsic_agreement():
    """If two views within a sensor disagree on intrinsics (should never
    happen given preset-driven geometry, but the check is cheap), error."""
    erp_view_map = {
        "sensors": (
            {
                "sensor_id": 2,
                "views": (
                    _erp_view("a", width=1024, fx=512.0),
                    _erp_view("b", width=2048, fx=1024.0),  # different
                ),
            },
        ),
    }
    with pytest.raises(exporter.ValidationError, match="disagree on intrinsic"):
        exporter.build_erp_camera_records(erp_view_map, start_camera_id=1)


def test_erp_camera_records_empty_sensors():
    cams, ids, next_id = exporter.build_erp_camera_records({"sensors": ()}, start_camera_id=1)
    assert cams == [] and ids == {} and next_id == 1


def test_erp_camera_records_skips_sensor_with_no_views():
    erp_view_map = {
        "sensors": (
            {"sensor_id": 2, "views": ()},
            {"sensor_id": 5, "views": (_erp_view("x"),)},
        ),
    }
    cams, ids, next_id = exporter.build_erp_camera_records(erp_view_map, start_camera_id=1)
    assert ids == {5: 1}
    assert len(cams) == 1
    assert next_id == 2
