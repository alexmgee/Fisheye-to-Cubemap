import sys
import importlib.util
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "metashape_cameras_to_colmap.py"

sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location("exporter", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FIXTURES = Path(__file__).parent / "fixtures"

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
    import pytest
    with pytest.raises(ValueError, match="v1 'bodies' manifest is no longer supported"):
        mod.load_scene_manifest(manifest_path)
