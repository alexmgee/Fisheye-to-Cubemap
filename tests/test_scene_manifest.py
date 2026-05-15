"""Tests for the v2 scene manifest data model.

v2 changes from v1 (per docs/superpowers/plans/2026-05-14-colmap-export-tab-redesign.md):
- No Body grouping — sensors are top-level on the manifest.
- No calibration_xml — calibration is auto-extracted from cameras.xml.
- Multi-directory image/mask support per sensor.
- EquirectSensor as a separate sensor type with its own split_mode/split_width.
- ExportOptions loses require_masks and projected_tracks.
- FisheyeSensor carries an optional RoutingDecision for the adaptive Path B
  vs cubemap Path A routing (see
  docs/superpowers/plans/Adaptive_Pinhole_Undistort_Plan.md).
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------- FisheyeSensor (multi-dir, no cal_xml) ----------

def test_fisheye_sensor_multi_dir_roundtrip():
    """FisheyeSensor with multiple image and mask directories survives serialization."""
    from gui.scene_manifest import FisheyeSensor
    s = FisheyeSensor(
        sensor_id=7,
        image_dirs=[Path("/a/front"), Path("/a/back")],
        mask_dirs=[Path("/a/front_masks")],
        multi_pinhole=True,
        output_width=2048,
    )
    s2 = FisheyeSensor.from_dict(s.to_dict())
    assert s2.sensor_id == 7
    assert len(s2.image_dirs) == 2
    assert len(s2.mask_dirs) == 1
    assert s2.multi_pinhole is True
    assert s2.output_width == 2048


def test_fisheye_sensor_no_calibration_xml_field():
    """v2 FisheyeSensor must not include calibration_xml — that's auto-extracted."""
    from gui.scene_manifest import FisheyeSensor
    s = FisheyeSensor(sensor_id=7, image_dirs=[Path("/a")], output_width=2048)
    assert "calibration_xml" not in s.to_dict()


def test_fisheye_sensor_lens_only_mask_optional():
    """lens_only_mask is optional and only appears in the dict when set."""
    from gui.scene_manifest import FisheyeSensor
    s = FisheyeSensor(sensor_id=0, image_dirs=[Path("/a")])
    assert "lens_only_mask" not in s.to_dict()
    s.lens_only_mask = Path("/mask.png")
    assert s.to_dict()["lens_only_mask"] == str(Path("/mask.png"))


# ---------- RoutingDecision (adaptive Path B vs cubemap Path A) ----------

def test_fisheye_sensor_routing_decision_roundtrip():
    """A FisheyeSensor with a cached routing decision survives round-trip."""
    from gui.scene_manifest import FisheyeSensor, RoutingDecision
    s = FisheyeSensor(
        sensor_id=0,
        image_dirs=[Path("/a")],
        routing=RoutingDecision(
            processing_mode="single_pinhole",
            f_target=1055.41,
            w_out=2856,
            theta_max_deg=52.3,
            routing_uid="abc123",
        ),
    )
    s2 = FisheyeSensor.from_dict(s.to_dict())
    assert s2.routing is not None
    assert s2.routing.processing_mode == "single_pinhole"
    assert s2.routing.f_target == 1055.41
    assert s2.routing.w_out == 2856
    assert s2.routing.theta_max_deg == 52.3
    assert s2.routing.routing_uid == "abc123"


def test_fisheye_sensor_routing_none_by_default():
    """No routing decision -> field is None and absent from the serialized dict."""
    from gui.scene_manifest import FisheyeSensor
    s = FisheyeSensor(sensor_id=0, image_dirs=[Path("/a")])
    assert s.routing is None
    assert "routing" not in s.to_dict()


def test_routing_decision_multi_pinhole_omits_adaptive_fields():
    """For multi_pinhole routing, f_target and w_out are not populated."""
    from gui.scene_manifest import RoutingDecision
    d = RoutingDecision(processing_mode="multi_pinhole", theta_max_deg=75.0)
    assert d.f_target is None
    assert d.w_out is None
    d2 = RoutingDecision.from_dict(d.to_dict())
    assert d2.processing_mode == "multi_pinhole"
    assert d2.f_target is None


# ---------- FrameSensor (multi-dir, with masks) ----------

def test_frame_sensor_multi_dir_with_masks():
    """FrameSensor gains multi-dir support and mask directories."""
    from gui.scene_manifest import FrameSensor
    s = FrameSensor(
        sensor_id=4,
        image_dirs=[Path("/drone/morning"), Path("/drone/afternoon")],
        mask_dirs=[Path("/drone/masks")],
    )
    s2 = FrameSensor.from_dict(s.to_dict())
    assert s2.sensor_id == 4
    assert len(s2.image_dirs) == 2
    assert len(s2.mask_dirs) == 1


# ---------- EquirectSensor (new) ----------

def test_equirect_sensor_roundtrip():
    """EquirectSensor carries split_width and split_mode for ERP splitting."""
    from gui.scene_manifest import EquirectSensor
    s = EquirectSensor(
        sensor_id=4,
        image_dirs=[Path("/eq")],
        split_width=2048,
        split_mode="reframe",
    )
    s2 = EquirectSensor.from_dict(s.to_dict())
    assert s2.sensor_id == 4
    assert s2.split_width == 2048
    assert s2.split_mode == "reframe"


def test_equirect_sensor_default_split_mode():
    """Default split_mode is 'reframe' (16-view, recommended for 3DGS)."""
    from gui.scene_manifest import EquirectSensor
    s = EquirectSensor(sensor_id=0, image_dirs=[Path("/eq")])
    assert s.split_mode == "reframe"


# ---------- ExportOptions (require_masks and projected_tracks removed) ----------

def test_export_options_defaults_v2():
    """v2 ExportOptions does not expose require_masks or projected_tracks."""
    from gui.scene_manifest import ExportOptions
    opts = ExportOptions()
    d = opts.to_dict()
    assert "require_masks" not in d
    assert "projected_tracks" not in d
    assert opts.pose_convention == "metashape_camera_to_world"
    assert opts.force_assets is False
    assert opts.normalize_scene is False
    assert opts.keep_processing_files is True


# ---------- SceneManifest (top-level sensors, no bodies) ----------

def test_manifest_v2_top_level_sensors():
    """v2 SceneManifest exposes fisheye_sensors / frame_sensors / equirect_sensors."""
    from gui.scene_manifest import (
        SceneManifest, FisheyeSensor, FrameSensor, EquirectSensor, ExportOptions,
    )
    m = SceneManifest(
        cameras_xml=Path("cameras.xml"),
        sparse_ply=Path("pointcloud.ply"),
        output_dir=Path("output"),
        fisheye_sensors=[FisheyeSensor(sensor_id=0, image_dirs=[Path("/i")], output_width=2048)],
        frame_sensors=[FrameSensor(sensor_id=4, image_dirs=[Path("/d")])],
        equirect_sensors=[EquirectSensor(sensor_id=5, image_dirs=[Path("/e")], split_width=2048)],
        options=ExportOptions(),
    )
    d = m.to_dict()
    assert "fisheye_sensors" in d
    assert "frame_sensors" in d
    assert "equirect_sensors" in d
    assert "bodies" not in d  # v1's grouping is gone


def test_manifest_v2_round_trip_via_json(tmp_path):
    """Full v2 manifest survives save/load via JSON, including routing decision."""
    from gui.scene_manifest import (
        SceneManifest, FisheyeSensor, FrameSensor, EquirectSensor,
        ExportOptions, RoutingDecision,
    )
    m = SceneManifest(
        cameras_xml=Path("cameras.xml"),
        sparse_ply=Path("pointcloud.ply"),
        output_dir=Path("output"),
        fisheye_sensors=[
            FisheyeSensor(
                sensor_id=0,
                image_dirs=[Path("/i/front"), Path("/i/back")],
                mask_dirs=[Path("/i/masks")],
                multi_pinhole=False,
                output_width=2048,
                routing=RoutingDecision(
                    processing_mode="single_pinhole",
                    f_target=1024.0, w_out=2856, theta_max_deg=52.0,
                    routing_uid="uid123",
                ),
            ),
        ],
        frame_sensors=[FrameSensor(sensor_id=4, image_dirs=[Path("/drone")])],
        equirect_sensors=[
            EquirectSensor(sensor_id=5, image_dirs=[Path("/eq")], split_width=2048, split_mode="reframe"),
        ],
        options=ExportOptions(),
    )
    path = tmp_path / "manifest.json"
    m.save(path)
    loaded = SceneManifest.load(path)
    assert loaded.fisheye_sensors[0].sensor_id == 0
    assert loaded.fisheye_sensors[0].multi_pinhole is False
    assert loaded.fisheye_sensors[0].routing.processing_mode == "single_pinhole"
    assert loaded.fisheye_sensors[0].routing.w_out == 2856
    assert loaded.equirect_sensors[0].split_mode == "reframe"


def test_manifest_v2_prefs_round_trip():
    """v2 manifest can be stored in and restored from a prefs dict (GUI persistence)."""
    from gui.scene_manifest import SceneManifest, FisheyeSensor, ExportOptions
    m = SceneManifest(
        cameras_xml=Path("cameras.xml"),
        sparse_ply=Path("pointcloud.ply"),
        output_dir=Path("output"),
        fisheye_sensors=[FisheyeSensor(sensor_id=0, image_dirs=[Path("/i")], output_width=2048)],
        options=ExportOptions(),
    )
    prefs = {"colmap_last_manifest": m.to_dict()}
    restored = SceneManifest.from_dict(json.loads(json.dumps(prefs))["colmap_last_manifest"])
    assert restored.fisheye_sensors[0].sensor_id == 0
    assert restored.fisheye_sensors[0].output_width == 2048
