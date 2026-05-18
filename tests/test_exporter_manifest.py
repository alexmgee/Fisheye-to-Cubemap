import sys
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "metashape_cameras_to_colmap.py"

sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location("exporter", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_png(width: int = 16, height: int = 16) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00" * 8
        + int(width).to_bytes(4, "big")
        + int(height).to_bytes(4, "big")
        + b"\x00" * 8
    )


def test_load_scene_manifest_v2(tmp_path):
    """Exporter can load and parse a v2 scene manifest JSON."""
    manifest = {
        "cameras_xml": str(FIXTURES / "dual_x5_cameras.xml"),
        "sparse_ply": "pointcloud.ply",
        "output_dir": str(tmp_path / "output"),
        "fisheye_sensors": [{
            "sensor_id": 0,
            "image_dirs": ["images/front", "images/back"],
            "mask_dirs": ["masks/front"],
            "multi_pinhole": True,
            "output_width": 2048,
            "output_format": "jpg",
        }],
        "frame_sensors": [{
            "sensor_id": 4,
            "image_dirs": ["drone"],
            "mask_dirs": [],
        }],
        "equirect_sensors": [],
        "options": {"pose_convention": "metashape_camera_to_world"},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    loaded = mod.load_scene_manifest(manifest_path)
    assert len(loaded["fisheye_sensors"]) == 1
    assert loaded["fisheye_sensors"][0]["multi_pinhole"] is True
    assert loaded["fisheye_sensors"][0]["image_dirs"] == ["images/front", "images/back"]
    assert loaded["fisheye_sensors"][0]["output_format"] == "jpg"
    assert loaded["frame_sensors"][0]["sensor_id"] == 4
    assert loaded["equirect_sensors"] == []
    assert loaded["options"]["pose_convention"] == "metashape_camera_to_world"


def test_load_scene_manifest_rejects_v1_bodies(tmp_path):
    """v1 manifests with 'bodies' must be rejected with a clear error."""
    manifest = {
        "cameras_xml": "cameras.xml",
        "output_dir": str(tmp_path / "output"),
        "bodies": [{"name": "old", "sensors": []}],
    }
    manifest_path = tmp_path / "v1_manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="v1 'bodies' manifest is no longer supported"):
        mod.load_scene_manifest(manifest_path)


def test_effective_manifest_mode_uses_recompute_when_routing_missing():
    routing = {"processing_mode": "single_pinhole", "f_target": 900.0, "w_out": 1800}

    mode = mod._effective_manifest_fisheye_mode(
        {"sensor_id": 7, "multi_pinhole": True},
        routing,
    )

    assert mode == "single_pinhole"


def test_effective_manifest_mode_honors_cached_user_override():
    routing = {"processing_mode": "single_pinhole", "f_target": 900.0, "w_out": 1800}

    mode = mod._effective_manifest_fisheye_mode(
        {"sensor_id": 7, "multi_pinhole": True, "routing": routing},
        routing,
    )

    assert mode == "multi_pinhole"


def test_adaptive_intrinsics_reject_corrupt_single_pinhole_routing():
    with pytest.raises(mod.ValidationError, match="routing.f_target or routing.w_out is missing"):
        mod._adaptive_intrinsics_from_routing(
            7,
            {"processing_mode": "single_pinhole", "theta_max_deg": 42.0},
        )


def test_adaptive_intrinsics_reject_nonpositive_single_pinhole_width():
    with pytest.raises(mod.ValidationError, match="routing.w_out must be positive"):
        mod._adaptive_intrinsics_from_routing(
            7,
            {"processing_mode": "single_pinhole", "f_target": 900.0, "w_out": 0},
        )


def test_manifest_auto_int_treats_zero_and_empty_as_auto():
    assert mod._manifest_auto_int(None, field_name="output_width", sensor_id=7) is None
    assert mod._manifest_auto_int("", field_name="output_width", sensor_id=7) is None
    assert mod._manifest_auto_int("0", field_name="output_width", sensor_id=7) is None
    assert mod._manifest_auto_int(0, field_name="output_width", sensor_id=7) is None
    assert mod._manifest_auto_int("2048", field_name="output_width", sensor_id=7) == 2048


@pytest.mark.parametrize("value", [-1, "-4", "abc", "12.5", True])
def test_manifest_auto_int_rejects_invalid_widths(value):
    with pytest.raises(mod.ValidationError, match="sensor 7 output_width"):
        mod._manifest_auto_int(value, field_name="output_width", sensor_id=7)


def test_manifest_fisheye_output_width_zero_uses_routing_recommendation():
    sensor = ET.fromstring(
        '<sensor id="7" label="Fish" type="equidistant_fisheye">'
        '<resolution width="64" height="64"/>'
        '<calibration type="equidistant_fisheye">'
        '<f>32</f><cx>0</cx><cy>0</cy>'
        '</calibration>'
        '</sensor>'
    )

    resolved, source = mod._resolve_manifest_fisheye_output_width(
        {"sensor_id": 7, "output_width": 0},
        sensor,
        {"processing_mode": "multi_pinhole", "recommended_output_width": 1984},
        None,
    )

    assert resolved == 1984
    assert source == "routing.recommended_output_width"


def test_manifest_fisheye_output_width_zero_uses_solid_angle_without_recommendation(monkeypatch):
    sensor = ET.fromstring(
        '<sensor id="7" label="Fish" type="equidistant_fisheye">'
        '<resolution width="64" height="64"/>'
        '<calibration type="equidistant_fisheye">'
        '<f>32</f><cx>0</cx><cy>0</cy>'
        '</calibration>'
        '</sensor>'
    )

    monkeypatch.setattr(mod, "_compute_manifest_optimal_width", lambda *_args, **_kwargs: 1536)

    resolved, source = mod._resolve_manifest_fisheye_output_width(
        {"sensor_id": 7, "output_width": "0"},
        sensor,
        {"processing_mode": "multi_pinhole"},
        None,
    )

    assert resolved == 1536
    assert source == "compute_optimal_width"


def test_manifest_fisheye_output_width_negative_raises():
    sensor = ET.fromstring(
        '<sensor id="7" label="Fish" type="equidistant_fisheye">'
        '<resolution width="64" height="64"/>'
        '<calibration type="equidistant_fisheye"><f>32</f></calibration>'
        '</sensor>'
    )

    with pytest.raises(mod.ValidationError, match="sensor 7 output_width"):
        mod._resolve_manifest_fisheye_output_width(
            {"sensor_id": 7, "output_width": -1},
            sensor,
            {"processing_mode": "multi_pinhole"},
            None,
        )


def test_manifest_equirect_split_width_zero_uses_recommended_width():
    sensor = ET.fromstring(
        '<sensor id="9" label="ERP" type="spherical">'
        '<resolution width="7680" height="3840"/>'
        '</sensor>'
    )

    resolved, source = mod._resolve_manifest_equirect_split_width(
        {"sensor_id": 9, "split_mode": "cubemap", "split_width": 0},
        sensor,
    )

    assert resolved == 1920
    assert source == "recommended_equirect_width"


def test_manifest_equirect_split_width_negative_raises():
    sensor = ET.fromstring(
        '<sensor id="9" label="ERP" type="spherical">'
        '<resolution width="7680" height="3840"/>'
        '</sensor>'
    )

    with pytest.raises(mod.ValidationError, match="sensor 9 split_width"):
        mod._resolve_manifest_equirect_split_width(
            {"sensor_id": 9, "split_width": -1},
            sensor,
        )


def test_discover_cubefaces_ignores_adaptive_output_tree(tmp_path):
    root = tmp_path / "processing"
    cubeface_images = root / "sensor_1" / "images" / "station_0001"
    cubeface_images.mkdir(parents=True)
    for suffix in mod.KNOWN_SUFFIXES:
        (cubeface_images / f"IMG_0001{suffix}.png").write_bytes(_fake_png())

    adaptive_images = root / "adaptive_sensor_7" / "images"
    adaptive_images.mkdir(parents=True)
    (adaptive_images / "IMG_0001.png").write_bytes(_fake_png())

    discovery = mod.discover_cubefaces(root)

    assert discovery["lens_count"] == 1
    assert discovery["lenses"][0]["lens_label"] == "sensor_1"
    assert discovery["image_count"] == len(mod.KNOWN_SUFFIXES)


def test_fisheye_stem_plan_prefixes_duplicate_dir_stems(tmp_path):
    front_dir = tmp_path / "front" / "frames"
    back_dir = tmp_path / "back" / "frames"
    front_dir.mkdir(parents=True)
    back_dir.mkdir(parents=True)
    (front_dir / "000001.jpg").write_bytes(b"front")
    (back_dir / "000001.jpg").write_bytes(b"back")
    document = {
        "cameras": {
            24: {"id": 24, "sensor_id": 1, "label": "000001", "transform": tuple(range(16))},
            47: {"id": 47, "sensor_id": 1, "label": "000001", "transform": tuple(range(16))},
        }
    }

    overrides, entries = mod._build_fisheye_stem_plan(
        document,
        1,
        [front_dir, back_dir],
    )

    assert overrides[str(front_dir / "000001.jpg")] == "front_000001"
    assert overrides[str(back_dir / "000001.jpg")] == "back_000001"
    assert [entry["camera_id"] for entry in entries] == [24, 47]


def test_source_image_map_resolves_duplicate_xml_camera_labels(tmp_path):
    root = tmp_path / "processing"
    sensor_root = root / "sensor_1"
    for stem in ("front_000001", "back_000001"):
        cubeface_images = sensor_root / "images" / stem
        cubeface_images.mkdir(parents=True, exist_ok=True)
        for suffix in mod.KNOWN_SUFFIXES:
            (cubeface_images / f"{stem}{suffix}.png").write_bytes(_fake_png())

    (sensor_root / mod.SOURCE_IMAGE_MAP_FILENAME).write_text(json.dumps({
        "version": 1,
        "stems": [
            {
                "output_stem": "front_000001",
                "source_stem": "000001",
                "camera_id": 24,
                "camera_label": "000001",
            },
            {
                "output_stem": "back_000001",
                "source_stem": "000001",
                "camera_id": 47,
                "camera_label": "000001",
            },
        ],
    }))

    discovery = mod.discover_cubefaces(root)
    document = {
        "cameras": {
            24: {"id": 24, "sensor_id": 1, "label": "000001", "transform": tuple(range(16))},
            47: {"id": 47, "sensor_id": 1, "label": "000001", "transform": tuple(range(16))},
        }
    }
    lens_map = mod.validate_lens_camera_map(
        document,
        discovery,
        {"sensor_1": (24, 47)},
    )

    resolved = {
        item["stem"]: item["camera_id"]
        for item in lens_map["resolutions"]
    }
    assert resolved == {"back_000001": 47, "front_000001": 24}


def test_write_sensor_calibration_xml_flattens_metashape_resolution(tmp_path):
    import xml.etree.ElementTree as ET

    sensor = ET.fromstring(
        '<sensor id="0" label="Fuji" type="equidistant_fisheye">'
        '<resolution width="6000" height="4000"/>'
        '<calibration type="equidistant_fisheye">'
        '<f>2122.455</f><cx>-8.57</cx><cy>35.92</cy>'
        '<k1>0.04</k1><k2>0.004</k2><k3>0.0006</k3>'
        '<p1>-0.0001</p1><p2>0.0002</p2>'
        '</calibration>'
        '</sensor>'
    )
    path = tmp_path / "calibration.xml"

    mod._write_sensor_calibration_xml(sensor, path)
    root = ET.parse(path).getroot()

    assert root.findtext("projection") == "equidistant_fisheye"
    assert root.findtext("width") == "6000"
    assert root.findtext("height") == "4000"
    assert root.find("resolution") is None
