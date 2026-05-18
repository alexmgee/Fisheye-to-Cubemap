import importlib.util
import json
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "metashape_cameras_to_colmap.py"

sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location("exporter", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _write_rgb(path: Path, width: int, height: int, color=(96, 128, 192)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :] = color
    assert cv2.imwrite(str(path), image)


def _write_mask(path: Path, width: int, height: int, value: int = 255) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mask = np.full((height, width), value, dtype=np.uint8)
    assert cv2.imwrite(str(path), mask)


def _write_mixed_cameras_xml(path: Path) -> None:
    transform = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<document version="2.3.0">
  <chunk label="Mixed">
    <sensors next_id="4">
      <sensor id="1" label="Tiny Fisheye" type="equidistant_fisheye">
        <resolution width="64" height="64"/>
        <calibration type="equidistant_fisheye" class="adjusted">
          <resolution width="64" height="64"/>
          <f>32</f><cx>0</cx><cy>0</cy>
          <k1>0</k1><k2>0</k2><k3>0</k3><p1>0</p1><p2>0</p2>
        </calibration>
      </sensor>
      <sensor id="2" label="Tiny Frame" type="frame">
        <resolution width="64" height="48"/>
        <calibration type="frame" class="adjusted">
          <resolution width="64" height="48"/>
          <f>48</f><cx>0</cx><cy>0</cy>
          <k1>0</k1><k2>0</k2><k3>0</k3><p1>0</p1><p2>0</p2>
        </calibration>
      </sensor>
      <sensor id="3" label="Tiny ERP" type="spherical">
        <resolution width="128" height="64"/>
        <calibration type="equirectangular" class="adjusted">
          <resolution width="128" height="64"/>
        </calibration>
      </sensor>
    </sensors>
    <cameras>
      <camera id="1" sensor_id="1" label="FISH_0001">
        <transform>{transform}</transform>
      </camera>
      <camera id="2" sensor_id="2" label="FRAME_0001">
        <transform>{transform}</transform>
      </camera>
      <camera id="3" sensor_id="3" label="ERP_0001">
        <transform>{transform}</transform>
      </camera>
    </cameras>
  </chunk>
</document>
""",
        encoding="utf-8",
    )


def test_scene_manifest_export_mixes_fisheye_frame_and_equirect(tmp_path):
    xml_path = tmp_path / "cameras.xml"
    _write_mixed_cameras_xml(xml_path)

    fisheye_images = tmp_path / "fisheye" / "images"
    fisheye_masks = tmp_path / "fisheye" / "masks"
    frame_images = tmp_path / "frame" / "images"
    frame_masks = tmp_path / "frame" / "masks"
    erp_images = tmp_path / "erp" / "images"
    erp_masks = tmp_path / "erp" / "masks"

    _write_rgb(fisheye_images / "FISH_0001.png", 64, 64, color=(32, 96, 160))
    _write_mask(fisheye_masks / "FISH_0001.png", 64, 64)
    _write_rgb(frame_images / "FRAME_0001.png", 64, 48, color=(160, 96, 32))
    _write_mask(frame_masks / "FRAME_0001.png", 64, 48)
    _write_rgb(erp_images / "ERP_0001.png", 128, 64, color=(80, 160, 96))
    _write_mask(erp_masks / "ERP_0001.png", 128, 64)

    output_dir = tmp_path / "out"
    manifest = {
        "cameras_xml": str(xml_path),
        "sparse_ply": None,
        "output_dir": str(output_dir),
        "fisheye_sensors": [
            {
                "sensor_id": 1,
                "image_dirs": [str(fisheye_images)],
                "mask_dirs": [str(fisheye_masks)],
                "multi_pinhole": False,
                "output_width": 0,
                "routing": {
                    "processing_mode": "single_pinhole",
                    "f_target": 32.0,
                    "w_out": 64,
                    "recommended_output_width": 64,
                    "theta_max_deg": 45.0,
                },
            }
        ],
        "frame_sensors": [
            {
                "sensor_id": 2,
                "image_dirs": [str(frame_images)],
                "mask_dirs": [str(frame_masks)],
            }
        ],
        "equirect_sensors": [
            {
                "sensor_id": 3,
                "image_dirs": [str(erp_images)],
                "mask_dirs": [str(erp_masks)],
                "split_mode": "cubemap",
                "split_width": 0,
            }
        ],
        "options": {
            "pose_convention": "metashape_camera_to_world",
            "force_assets": True,
            "normalize_scene": False,
            "keep_processing_files": True,
        },
    }
    manifest_path = tmp_path / "scene_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = mod.main([f"--scene-manifest={manifest_path}"])

    assert result == 0
    sparse_dir = output_dir / "colmap" / "sparse" / "0"
    mod.validate_colmap_model(
        sparse_dir,
        expected_cameras=3,
        expected_images=8,
        expected_points=0,
    )
    cameras = mod.parse_colmap_cameras(sparse_dir / "cameras.txt")
    images = mod.parse_colmap_images(sparse_dir / "images.txt")
    assert {camera["model"] for camera in cameras.values()} == {"PINHOLE"}

    image_names = {image["image_name"] for image in images.values()}
    assert len(image_names) == 8
    assert any(name.startswith("adaptive_1_FISH_0001") for name in image_names)
    assert any("FRAME_0001" in name for name in image_names)
    assert sum(1 for name in image_names if name.startswith("erp_3_")) == 6

    for name in image_names:
        assert (output_dir / "colmap" / "images" / name).is_file()

    run_report = output_dir / "run_report.txt"
    report = run_report.read_text(encoding="utf-8")
    assert "Width source: recommended_equirect_width" in report
