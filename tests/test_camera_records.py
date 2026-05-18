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


def test_cubeface_asset_packaging_keeps_jpg_faces_and_png_masks(tmp_path):
    source_image = (
        tmp_path / "processing" / "sensor_0" / "images" / "IMG_0001"
        / "IMG_0001_dir_plusZ.jpg"
    )
    source_mask = (
        tmp_path / "processing" / "sensor_0" / "masks"
        / "IMG_0001_dir_plusZ_mask.png"
    )
    source_image.parent.mkdir(parents=True)
    source_mask.parent.mkdir(parents=True)
    source_image.write_bytes(b"jpg")
    source_mask.write_bytes(b"mask")
    image = {
        "lens_label": "sensor_0",
        "stem": "IMG_0001",
        "suffix": "_dir_plusZ",
        "extension": ".jpg",
        "image_path": str(source_image),
        "mask_path": str(source_mask),
    }
    discovery = {
        "root": str(tmp_path / "processing"),
        "lenses": ({
            "lens_label": "sensor_0",
            "images": (image,),
            "face_size_set": ((128, 128),),
        },),
        "image_count": 1,
    }
    records = [{
        "kind": "cubeface",
        "image_name": "sensor_0/images/IMG_0001_dir_plusZ.jpg",
        "image_path": str(source_image),
        "camera_id": 1,
        "qvec": (1.0, 0.0, 0.0, 0.0),
        "tvec": (0.0, 0.0, 0.0),
    }]

    final, _report_lines, image_count, mask_count = exporter._package_cubeface_assets(
        tmp_path / "colmap",
        discovery,
        records,
        package_assets=True,
        used_asset_names=set(),
    )

    assert image_count == 1
    assert mask_count == 1
    assert final[0]["image_name"].endswith(".jpg")
    assert final[0]["mask_name"].endswith(".png")
    assert final[0]["mask_output_path"].endswith(".png")
    assert (tmp_path / "colmap" / "images" / final[0]["image_name"]).is_file()
    assert (tmp_path / "colmap" / "masks" / final[0]["mask_name"]).is_file()


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


# ─────────────────────────────────────────────────────────────────────────────
# Adaptive single-pinhole camera/image records
# ─────────────────────────────────────────────────────────────────────────────

def _adaptive_map(tmp_path, sensor_id: int = 7, f_target: float = 1024.5,
                  w_out: int = 2856) -> dict:
    root = tmp_path / f"adaptive_sensor_{sensor_id}"
    images = root / "images"
    masks = root / "masks"
    images.mkdir(parents=True)
    masks.mkdir(parents=True)
    return {
        "sensors": (
            {
                "sensor_id": sensor_id,
                "f_target": f_target,
                "w_out": w_out,
                "images_dir": str(images),
                "masks_dir": str(masks),
            },
        ),
    }


def _adaptive_document(sensor_id: int = 7) -> dict:
    return {
        "cameras": {
            11: {
                "sensor_id": sensor_id,
                "label": "IMG_0001",
                "transform": (
                    1.0, 0.0, 0.0, 1.0,
                    0.0, 1.0, 0.0, 2.0,
                    0.0, 0.0, 1.0, 3.0,
                    0.0, 0.0, 0.0, 1.0,
                ),
            },
            12: {
                "sensor_id": sensor_id,
                "label": "IMG_0002",
                "transform": (
                    1.0, 0.0, 0.0, 4.0,
                    0.0, 1.0, 0.0, 5.0,
                    0.0, 0.0, 1.0, 6.0,
                    0.0, 0.0, 0.0, 1.0,
                ),
            },
        },
        "sensors": {},
    }


def test_adaptive_camera_records_single_sensor(tmp_path):
    adaptive_map = _adaptive_map(tmp_path)
    cams, ids_by_sensor, next_id = exporter.build_adaptive_camera_records(
        adaptive_map,
        start_camera_id=5,
    )

    assert len(cams) == 1
    assert ids_by_sensor == {7: 5}
    assert next_id == 6
    assert cams[0]["model"] == "PINHOLE"
    assert cams[0]["width"] == 2856
    assert cams[0]["height"] == 2856
    assert cams[0]["params"] == (1024.5, 1024.5, 1428.0, 1428.0)


def test_adaptive_camera_records_require_intrinsics(tmp_path):
    adaptive_map = _adaptive_map(tmp_path)
    sensor = dict(adaptive_map["sensors"][0])
    sensor["f_target"] = None
    adaptive_map["sensors"] = (sensor,)

    with pytest.raises(exporter.ValidationError, match="requires f_target and w_out"):
        exporter.build_adaptive_camera_records(adaptive_map, start_camera_id=1)


def test_adaptive_image_records_use_direct_source_pose_and_skip_missing(tmp_path):
    adaptive_map = _adaptive_map(tmp_path)
    sensor = adaptive_map["sensors"][0]
    images = Path(sensor["images_dir"])
    masks = Path(sensor["masks_dir"])
    (images / "IMG_0001.png").write_bytes(b"fake")
    (masks / "IMG_0001_mask.png").write_bytes(b"mask")

    records = exporter.build_adaptive_image_records(
        _adaptive_document(),
        adaptive_map,
        {7: 9},
        pose_convention="metashape_camera_to_world",
        placeholder_poses=False,
    )

    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "adaptive"
    assert record["metashape_camera_id"] == 11
    assert record["camera_id"] == 9
    assert record["image_name"] == "adaptive_sensor_7/IMG_0001.png"
    assert record["image_path"] == str(images / "IMG_0001.png")
    assert record["mask_path"] == str(masks / "IMG_0001_mask.png")
    assert record["qvec"] == (1.0, 0.0, 0.0, 0.0)
    assert record["tvec"] == (-1.0, -2.0, -3.0)


def test_adaptive_asset_packaging_flattens_names(tmp_path):
    adaptive_map = _adaptive_map(tmp_path)
    sensor = adaptive_map["sensors"][0]
    images = Path(sensor["images_dir"])
    masks = Path(sensor["masks_dir"])
    image = images / "IMG_0001.png"
    mask = masks / "IMG_0001_mask.png"
    image.write_bytes(b"fake")
    mask.write_bytes(b"mask")
    records = [{
        "kind": "adaptive",
        "metashape_camera_id": 11,
        "camera_id": 9,
        "image_name": "adaptive_sensor_7/IMG_0001.png",
        "image_path": str(image),
        "mask_path": str(mask),
        "qvec": (1.0, 0.0, 0.0, 0.0),
        "tvec": (0.0, 0.0, 0.0),
        "adaptive_sensor_id": 7,
        "adaptive_stem": "IMG_0001",
    }]

    final, report_lines, image_count, mask_count = exporter._package_adaptive_assets(
        tmp_path / "colmap",
        records,
        package_assets=True,
        used_asset_names=set(),
    )

    assert image_count == 1
    assert mask_count == 1
    assert final[0]["image_name"] == "adaptive_7_IMG_0001.png"
    assert (tmp_path / "colmap" / "images" / "adaptive_7_IMG_0001.png").is_file()
    assert (tmp_path / "colmap" / "masks" / "adaptive_7_IMG_0001.png").is_file()
    assert any(line.startswith("adaptive_image_") for line in report_lines)


def test_passthrough_undistort_can_write_jpg_image_with_png_mask(tmp_path, monkeypatch):
    source_image = tmp_path / "source" / "IMG_0001.jpg"
    source_image.parent.mkdir(parents=True)
    source_image.write_bytes(b"jpg")
    sensor = {
        "id": 3,
        "width": 6000,
        "height": 4000,
        "params": {"f": 2124.0, "cx": 0.0, "cy": 0.0},
    }
    resolution = {
        "camera_id": 11,
        "sensor_id": 3,
        "media_set_slug": "fuji",
        "image_path": str(source_image),
        "image_name": "IMG_0001.jpg",
    }

    def fake_undistort(source, dest, _sensor, *, is_mask, output_format, **_kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(output_format.encode("ascii"))
        assert Path(source) == source_image
        assert is_mask is False
        assert output_format == "jpg"
        return "undistorted"

    def fake_valid_mask(_source, dest, _sensor, **_kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"mask")
        return "generated_valid_mask"

    monkeypatch.setattr(exporter, "_undistort_to_image", fake_undistort)
    monkeypatch.setattr(exporter, "_write_undistort_valid_mask_to_png", fake_valid_mask)

    final, _report_lines, image_count, mask_count, undistorted_count, _reused = (
        exporter._package_passthrough_resolution(
            tmp_path / "colmap",
            resolution,
            sensor,
            package_assets=True,
            undistort=True,
            output_format="jpg",
            require_masks=False,
            used_asset_names=set(),
        )
    )

    assert image_count == 1
    assert mask_count == 1
    assert undistorted_count == 1
    assert final["image_name"] == "fuji_IMG_0001.jpg"
    assert final["mask_name"] == "fuji_IMG_0001.png"
    assert (tmp_path / "colmap" / "images" / "fuji_IMG_0001.jpg").is_file()
    assert (tmp_path / "colmap" / "masks" / "fuji_IMG_0001.png").is_file()


def test_passthrough_image_records_preserve_packaged_mask_name():
    document = {
        "cameras": {
            11: {
                "sensor_id": 3,
                "transform": tuple(float(i % 5 == 0) for i in range(16)),
            },
        },
    }
    passthrough_map = {
        "resolutions": (
            {
                "camera_id": 11,
                "sensor_id": 3,
                "image_name": "fuji_IMG_0001.jpg",
                "image_path": "/fake/colmap/images/fuji_IMG_0001.jpg",
                "mask_name": "fuji_IMG_0001.png",
                "mask_output_path": "/fake/colmap/masks/fuji_IMG_0001.png",
            },
        ),
    }

    records = exporter.build_passthrough_image_records(
        document,
        passthrough_map,
        {3: 5},
        convention=None,
        placeholder_poses=True,
    )

    assert records[0]["image_name"] == "fuji_IMG_0001.jpg"
    assert records[0]["mask_name"] == "fuji_IMG_0001.png"
    assert records[0]["mask_output_path"].endswith("fuji_IMG_0001.png")


def test_packaged_scene_validation_uses_mask_name_when_present(tmp_path):
    output_scene = tmp_path / "colmap"
    (output_scene / "images").mkdir(parents=True)
    (output_scene / "masks").mkdir()
    (output_scene / "images" / "fuji_IMG_0001.jpg").write_bytes(b"jpg")
    (output_scene / "masks" / "fuji_IMG_0001.png").write_bytes(b"mask")

    result = exporter._validate_packaged_scene_assets(
        output_scene,
        [{"image_name": "fuji_IMG_0001.jpg", "mask_name": "fuji_IMG_0001.png"}],
        require_masks=True,
    )

    assert result["missing_images"] == 0
    assert result["missing_masks"] == 0
    assert result["mask_file_count"] == 1


def test_training_scene_writer_accepts_adaptive_map(tmp_path, monkeypatch):
    adaptive_map = _adaptive_map(tmp_path, f_target=901.0, w_out=1802)
    sensor = adaptive_map["sensors"][0]
    (Path(sensor["images_dir"]) / "IMG_0001.png").write_bytes(b"adaptive image")
    (Path(sensor["masks_dir"]) / "IMG_0001_mask.png").write_bytes(b"adaptive mask")

    captured = {}

    def fake_scene_metrics(*_args, **_kwargs):
        return {
            "camera_radius_p95": None,
            "point_radius_p95": None,
            "point_to_camera_radius_ratio": None,
            "combined_bounds_diagonal": None,
        }

    def fake_write_model(
        _metashape_points,
        output_dir,
        camera_records,
        image_records,
        *,
        report_path=None,
        **_kwargs,
    ):
        captured["camera_records"] = tuple(dict(record) for record in camera_records)
        captured["image_records"] = tuple(dict(record) for record in image_records)
        output_dir.mkdir(parents=True, exist_ok=True)
        if report_path is None:
            report_path = output_dir / "conversion_report.txt"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("fake conversion report\n", encoding="utf-8")
        return {
            "output_dir": str(output_dir),
            "cameras_path": str(output_dir / "cameras.txt"),
            "images_path": str(output_dir / "images.txt"),
            "points3D_path": str(output_dir / "points3D.txt"),
            "report_path": str(report_path),
            "camera_count": len(camera_records),
            "image_count": len(image_records),
            "point_count": 0,
        }

    monkeypatch.setattr(exporter, "_scene_scale_metrics", fake_scene_metrics)
    monkeypatch.setattr(exporter, "_write_colmap_text_model", fake_write_model)

    result = exporter.write_colmap_training_scene(
        tmp_path / "points.ply",
        _adaptive_document(),
        _discovery([]),
        tmp_path / "scene",
        adaptive_map=adaptive_map,
        placeholder_poses=True,
        package_assets=True,
    )

    assert result["adaptive_image_count"] == 1
    assert result["adaptive_mask_count"] == 1
    assert result["packaged_image_count"] == 1
    assert result["packaged_mask_count"] == 1
    assert captured["camera_records"] == (
        {
            "camera_id": 1,
            "model": "PINHOLE",
            "width": 1802,
            "height": 1802,
            "params": (901.0, 901.0, 901.0, 901.0),
        },
    )
    assert len(captured["image_records"]) == 1
    assert captured["image_records"][0]["kind"] == "adaptive"
    assert captured["image_records"][0]["image_name"] == "adaptive_7_IMG_0001.png"
    assert captured["image_records"][0]["image_id"] == 1
    assert (tmp_path / "scene" / "images" / "adaptive_7_IMG_0001.png").is_file()
    assert (tmp_path / "scene" / "masks" / "adaptive_7_IMG_0001.png").is_file()


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
