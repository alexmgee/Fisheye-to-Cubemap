import sys
import xml.etree.ElementTree as ET
from pathlib import Path
import pytest

# Add project root to path so gui package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).parent / "fixtures"


def _make_sensor_elem(sensor_type, cal_type, include_resolution=True, **params):
    """Build a minimal <sensor> Element with given type/cal_type for classifier tests."""
    res_xml = '<resolution width="2048" height="2048"/>' if include_resolution else ""
    param_xml = "".join(f"<{k}>{v}</{k}>" for k, v in params.items())
    cal_block = (
        f'<calibration type="{cal_type}">{res_xml}{param_xml}</calibration>'
        if cal_type else ""
    )
    xml = f'<sensor id="0" label="test" type="{sensor_type}">{cal_block}</sensor>'
    return ET.fromstring(xml)


# ---------- Task 1: classify_sensor_element (4 sensor types) ----------

def test_classify_equisolid_fisheye():
    from gui.sensor_discovery import classify_sensor_element
    elem = _make_sensor_elem("equisolid_fisheye", "equisolid_fisheye")
    assert classify_sensor_element(elem) == "equisolid_fisheye"

def test_classify_equidistant_fisheye():
    from gui.sensor_discovery import classify_sensor_element
    elem = _make_sensor_elem("fisheye", "equidistant")
    assert classify_sensor_element(elem) == "equidistant_fisheye"

def test_classify_frame():
    from gui.sensor_discovery import classify_sensor_element
    elem = _make_sensor_elem("frame", "frame")
    assert classify_sensor_element(elem) == "frame"

def test_classify_equirectangular():
    from gui.sensor_discovery import classify_sensor_element
    elem = _make_sensor_elem("equirectangular", "equirectangular")
    assert classify_sensor_element(elem) == "equirectangular"

def test_classify_spherical_as_equirectangular():
    """Metashape exports stitched 360 panoramas with type='spherical' — the
    same export path as equirectangular sensors."""
    from gui.sensor_discovery import classify_sensor_element
    elem = _make_sensor_elem("spherical", "spherical")
    assert classify_sensor_element(elem) == "equirectangular"

def test_classify_unknown_no_calibration():
    """Sensor with no calibration block and an unrecognized type -> 'unknown'."""
    from gui.sensor_discovery import classify_sensor_element
    elem = ET.fromstring('<sensor id="0" label="x" type="weirdtype"/>')
    assert classify_sensor_element(elem) == "unknown"


# ---------- Task 1: extract_sensor_calibration ----------

def test_extract_calibration_full():
    from gui.sensor_discovery import extract_sensor_calibration
    elem = _make_sensor_elem(
        "equisolid_fisheye", "equisolid_fisheye",
        f="1055.41", cx="-5.32", cy="0.03",
        k1="0.063", k2="0.065", k3="-0.024",
        p1="0.00018", p2="-0.00019",
    )
    cal = extract_sensor_calibration(elem)
    assert cal is not None
    assert cal["projection"] == "equisolid_fisheye"
    assert cal["width"] == 2048
    assert cal["height"] == 2048
    assert cal["f"] == 1055.41
    assert cal["cx"] == -5.32
    assert cal["k1"] == 0.063
    assert cal["p2"] == -0.00019

def test_extract_calibration_missing_block_returns_none():
    from gui.sensor_discovery import extract_sensor_calibration
    elem = ET.fromstring('<sensor id="0" label="x" type="frame"/>')
    assert extract_sensor_calibration(elem) is None


def test_extract_spherical_resolution_without_calibration():
    from gui.sensor_discovery import extract_sensor_calibration
    elem = ET.fromstring(
        '<sensor id="0" label="erp" type="spherical">'
        '<resolution width="7680" height="3840"/>'
        '</sensor>'
    )
    cal = extract_sensor_calibration(elem)
    assert cal == {
        "projection": "equirectangular",
        "width": 7680,
        "height": 3840,
    }

def test_extract_calibration_optional_params_absent():
    """Missing optional params (k4, b1, b2) should simply be absent from dict."""
    from gui.sensor_discovery import extract_sensor_calibration
    elem = _make_sensor_elem(
        "equisolid_fisheye", "equisolid_fisheye",
        f="1000", cx="0", cy="0",
    )
    cal = extract_sensor_calibration(elem)
    assert "f" in cal
    assert "k4" not in cal
    assert "b1" not in cal
    assert "b2" not in cal


# ---------- Fourier corrections integration ----------

def _make_sensor_elem_with_corrections(coeffs_count=96):
    """Build a sensor element with a <corrections type="fourier"> block."""
    coeffs = " ".join("0.01" for _ in range(coeffs_count))
    xml = (
        '<sensor id="0" label="test" type="equisolid_fisheye">'
        '<calibration type="equisolid_fisheye">'
        '<resolution width="3840" height="3840"/>'
        '<f>963.5</f><cx>-7.7</cx><cy>0.3</cy>'
        '<k1>0.18</k1><k2>0.028</k2><k3>-0.02</k3>'
        '<p1>-0.00017</p1><p2>0.00013</p2>'
        f'<corrections type="fourier">'
        f'<coeffs>{coeffs}</coeffs>'
        f'<extent><min>0 0</min><max>3840 3840</max></extent>'
        f'</corrections>'
        '</calibration>'
        '</sensor>'
    )
    return ET.fromstring(xml)


def test_extract_calibration_with_corrections_includes_corrections_key():
    """P1-C: Sensor with <corrections> block -> dict has 'corrections' key."""
    from gui.sensor_discovery import extract_sensor_calibration
    elem = _make_sensor_elem_with_corrections()
    cal = extract_sensor_calibration(elem)
    assert cal is not None
    assert "corrections" in cal
    assert len(cal["corrections"].coeffs) == 96
    assert cal["corrections"].extent_max == (3840.0, 3840.0)


def test_extract_calibration_without_corrections_has_no_corrections_key():
    """P1-D: Sensor without <corrections> -> dict has no 'corrections' key."""
    from gui.sensor_discovery import extract_sensor_calibration
    elem = _make_sensor_elem(
        "equisolid_fisheye", "equisolid_fisheye",
        f="1000", cx="0", cy="0",
        k1="0", k2="0", k3="0", p1="0", p2="0",
    )
    cal = extract_sensor_calibration(elem)
    assert cal is not None
    assert "corrections" not in cal


# ---------- Task 1: discover_sensors new return shape ----------

def test_discover_sensors_returns_four_categories():
    """discover_sensors returns four classification keys, not two."""
    from gui.sensor_discovery import discover_sensors
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    assert set(result.keys()) >= {"equisolid", "equidistant", "frame", "equirectangular"}
    assert len(result["equisolid"]) == 4
    assert len(result["equidistant"]) == 0
    assert len(result["frame"]) == 2
    assert len(result["equirectangular"]) == 0

def test_discover_sensors_embeds_calibration():
    """Each discovered sensor includes a 'calibration' dict from the XML."""
    from gui.sensor_discovery import discover_sensors
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    s0 = result["equisolid"][0]
    assert "calibration" in s0
    cal = s0["calibration"]
    assert cal is not None
    assert cal["projection"] == "equisolid_fisheye"
    assert cal["width"] == 3840
    assert cal["height"] == 3840
    assert cal["f"] == 1055.41

def test_discover_sensors_frame_has_calibration():
    """Frame sensors also carry calibration (drone/DSLR intrinsics matter for exporter)."""
    from gui.sensor_discovery import discover_sensors
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    dji = result["frame"][0]
    assert dji["calibration"]["projection"] == "frame"
    assert dji["calibration"]["f"] == 3605.32

def test_discover_sensors_counts():
    """XML with 4 equisolid + 2 frame sensors should return correct counts."""
    from gui.sensor_discovery import discover_sensors
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    assert len(result["equisolid"]) == 4
    assert len(result["frame"]) == 2

def test_discover_sensors_metadata():
    """Each discovered fisheye sensor keeps ids and labels for matching."""
    from gui.sensor_discovery import discover_sensors
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    s0 = result["equisolid"][0]
    assert s0["sensor_id"] == 0
    assert s0["label"] == "Insta360 X5 (1)"
    assert s0["camera_count"] == 3
    assert s0["prefix"] == "cam1_front_"
    assert s0["camera_ids"] == [0, 1, 2]
    assert s0["camera_labels"] == ["cam1_front_0001", "cam1_front_0002", "cam1_front_0003"]

def test_discover_sensors_frame_labels():
    """Frame sensors include camera_labels list for filename matching."""
    from gui.sensor_discovery import discover_sensors
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    dji = result["frame"][0]
    assert dji["sensor_id"] == 4
    assert dji["label"] == "DJI Mavic 24mm"
    assert set(dji["camera_labels"]) == {"DJI_0001", "DJI_0002", "DJI_0003"}

def test_discover_sensors_malformed_xml(tmp_path):
    """Malformed XML returns error dict instead of crashing."""
    bad = tmp_path / "bad.xml"
    bad.write_text("not xml at all")
    from gui.sensor_discovery import discover_sensors
    result = discover_sensors(bad)
    assert "error" in result

def test_discover_sensors_frame_only(tmp_path):
    """XML with only frame sensors returns empty equisolid list."""
    from gui.sensor_discovery import discover_sensors
    xml_content = '''<?xml version="1.0" encoding="UTF-8"?>
    <document version="2.3.0"><chunk><sensors>
      <sensor id="0" label="DJI" type="frame">
        <calibration type="frame"><f>3000</f></calibration>
      </sensor>
    </sensors><cameras>
      <camera id="0" sensor_id="0" label="DJI_0001">
        <transform>1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</transform>
      </camera>
    </cameras></chunk></document>'''
    xml_path = tmp_path / "frame_only.xml"
    xml_path.write_text(xml_content)
    result = discover_sensors(xml_path)
    assert len(result["equisolid"]) == 0
    assert len(result["frame"]) == 1


def test_auto_group_two_bodies():
    """4 sensors with cam1_front/cam1_back/cam2_front/cam2_back -> 2 bodies."""
    from gui.sensor_discovery import discover_sensors, auto_group_into_bodies
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    bodies = auto_group_into_bodies(result["equisolid"])
    assert len(bodies) == 2
    assert bodies[0]["name"] == "cam1"
    assert len(bodies[0]["sensor_ids"]) == 2
    assert set(bodies[0]["sensor_ids"]) == {0, 1}
    assert bodies[1]["name"] == "cam2"
    assert set(bodies[1]["sensor_ids"]) == {2, 3}

def test_auto_group_single_sensor_body():
    """A sensor with no prefix match to others becomes a single-sensor body, while paired sensors group."""
    from gui.sensor_discovery import auto_group_into_bodies
    sensors = [
        {"sensor_id": 0, "prefix": "cam1_front_", "label": "X5 (1)", "camera_count": 3},
        {"sensor_id": 1, "prefix": "cam1_back_", "label": "X5 (2)", "camera_count": 3},
        {"sensor_id": 2, "prefix": "solo_lens_", "label": "Solo", "camera_count": 5},
    ]
    bodies = auto_group_into_bodies(sensors)
    assert len(bodies) == 2
    paired_body = [b for b in bodies if 0 in b["sensor_ids"]][0]
    assert set(paired_body["sensor_ids"]) == {0, 1}
    solo_body = [b for b in bodies if 2 in b["sensor_ids"]][0]
    assert len(solo_body["sensor_ids"]) == 1

def test_auto_group_empty():
    """Empty sensor list returns empty body list."""
    from gui.sensor_discovery import auto_group_into_bodies
    assert auto_group_into_bodies([]) == []


def test_match_frame_images_full(tmp_path):
    """All labels found -> full match."""
    from gui.sensor_discovery import match_frame_sensor_images
    labels = ["DJI_0001", "DJI_0002", "DJI_0003"]
    for label in labels:
        (tmp_path / f"{label}.jpg").touch()
    result = match_frame_sensor_images(tmp_path, labels)
    assert result["matched"] == 3
    assert result["total"] == 3
    assert result["missing"] == []

def test_match_frame_images_partial(tmp_path):
    """Some labels missing -> partial match with missing list."""
    from gui.sensor_discovery import match_frame_sensor_images
    labels = ["DJI_0001", "DJI_0002", "DJI_0003"]
    (tmp_path / "DJI_0001.jpg").touch()
    (tmp_path / "DJI_0003.png").touch()
    result = match_frame_sensor_images(tmp_path, labels)
    assert result["matched"] == 2
    assert result["missing"] == ["DJI_0002"]

def test_match_frame_images_extra_ignored(tmp_path):
    """Extra files in directory don't affect match count."""
    from gui.sensor_discovery import match_frame_sensor_images
    labels = ["DJI_0001"]
    (tmp_path / "DJI_0001.jpg").touch()
    (tmp_path / "unrelated.jpg").touch()
    result = match_frame_sensor_images(tmp_path, labels)
    assert result["matched"] == 1


def test_recommended_equirect_widths_from_xml_resolution():
    from gui.sensor_discovery import recommended_equirect_width
    cal = {"projection": "equirectangular", "width": 7680, "height": 3840}
    assert recommended_equirect_width(cal, "cubemap") == 1920
    assert recommended_equirect_width(cal, "reframe") == 2444


def test_full_pipeline_xml_to_manifest_v2(tmp_path):
    """End-to-end (v2): discover sensors -> build top-level manifest -> serialize.

    The v2 manifest has no Body grouping; every fisheye sensor is a top-level
    entry on `fisheye_sensors`. Calibration is not stored in the manifest
    (the exporter reads it from cameras.xml via sensor_discovery).
    """
    from gui.sensor_discovery import discover_sensors
    from gui.scene_manifest import (
        SceneManifest, FisheyeSensor, FrameSensor, ExportOptions,
    )

    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")

    fisheye_sensors = [
        FisheyeSensor(
            sensor_id=s["sensor_id"],
            image_dirs=[Path("images")],
            mask_dirs=[Path("masks")],
            output_width=2048,
        )
        for s in result["equisolid"]
    ]
    frame_sensors = [
        FrameSensor(sensor_id=fs["sensor_id"], image_dirs=[Path("images")])
        for fs in result["frame"]
    ]

    manifest = SceneManifest(
        cameras_xml=FIXTURES / "dual_x5_cameras.xml",
        sparse_ply=Path("pointcloud.ply"),
        output_dir=tmp_path / "output",
        fisheye_sensors=fisheye_sensors,
        frame_sensors=frame_sensors,
        options=ExportOptions(),
    )

    path = tmp_path / "manifest.json"
    manifest.save(path)

    loaded = SceneManifest.load(path)
    assert len(loaded.fisheye_sensors) == 4  # 4 equisolid sensors in the fixture
    assert len(loaded.frame_sensors) == 2
    # No bodies key in v2
    raw = path.read_text()
    assert '"fisheye_sensors"' in raw
    assert '"bodies"' not in raw
