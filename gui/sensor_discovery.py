"""Sensor discovery from Metashape cameras.xml.

Parses the XML to classify sensors (equisolid_fisheye, equidistant_fisheye,
frame, equirectangular), extract per-sensor calibration data, detect camera
label prefixes, and count aligned cameras per sensor.

The redesigned COLMAP-export tab uses calibration extracted from the
cameras.xml directly (no per-sensor cal-XML file picker). The calibration
dict returned here is consumed by:

- `gui/solid_angle.py`'s `compute_optimal_width()` for auto-filling the
  cubeface width on each fisheye sensor card,
- `docs/superpowers/plans/Adaptive_Pinhole_Undistort_Groundwork.py`'s
  `extract_lens_characteristics()` for the adaptive routing gateway.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path


# Calibration parameters parsed when present. Missing parameters are absent
# from the returned dict (callers fall back to zero defaults). Order matches
# the Metashape XML element order for readability.
_CALIBRATION_PARAMS = (
    "f", "cx", "cy",
    "k1", "k2", "k3", "k4",
    "p1", "p2",
    "b1", "b2",
)


def _split_label(label: str) -> tuple[str, int | None]:
    """Split a camera label into (prefix, number).

    'cam1_front_0003' -> ('cam1_front_', 3)
    'IMG_4231' -> ('IMG_', 4231)
    'no_number' -> ('no_number', None)
    """
    match = re.match(r"^(?P<prefix>.*?)(?P<number>\d+)$", label)
    if not match:
        return label, None
    return match.group("prefix"), int(match.group("number"))


def classify_sensor_element(sensor_elem) -> str:
    """Classify a Metashape <sensor> element by its projection type.

    Returns one of:
        'equisolid_fisheye'   — fisheye with equisolid projection (e.g. DJI Osmo 360, Insta360 X5)
        'equidistant_fisheye' — fisheye with equidistant projection (Metashape default fisheye)
        'frame'               — pinhole/perspective frame camera
        'equirectangular'     — stitched 360 panorama
        'unknown'             — could not classify (caller decides)

    The classification reads both the <sensor type="..."> attribute and the
    <calibration type="..."> attribute, joined into one string. This handles
    Metashape variants where the sensor type is generic ('fisheye') and the
    specific projection is declared on the calibration block.
    """
    sensor_type = sensor_elem.attrib.get("type", "").lower()
    calibration = sensor_elem.find("calibration")
    cal_type = calibration.attrib.get("type", "").lower() if calibration is not None else ""
    combined = f"{sensor_type} {cal_type}"

    if "equirectangular" in combined:
        return "equirectangular"
    if "equisolid" in combined and "fisheye" in combined:
        return "equisolid_fisheye"
    if "equidistant" in combined:
        return "equidistant_fisheye"
    if "frame" in combined or sensor_type == "frame":
        return "frame"
    return "unknown"


def extract_sensor_calibration(sensor_elem) -> dict | None:
    """Extract the <calibration> block from a sensor element as a dict.

    Returns:
        dict with keys:
            projection: str  — from <calibration type="..."> attribute
            width, height: int — pixel dimensions
            f, cx, cy, k1..k4, p1, p2, b1, b2: float (only when present in XML)

        None when the sensor has no <calibration> block, or when neither the
        calibration nor the sensor element provides a <resolution>.

    Missing optional parameters are simply absent from the dict. The adaptive
    routing module and the cubeface-width calculator both call .get(key, 0.0)
    rather than expecting all eleven parameters to exist.
    """
    calibration = sensor_elem.find("calibration")
    if calibration is None:
        return None

    # Metashape sometimes places <resolution> inside <calibration>, sometimes
    # at sensor scope. Prefer the calibration-scoped one when both exist.
    resolution = calibration.find("resolution")
    if resolution is None:
        resolution = sensor_elem.find("resolution")
    if resolution is None:
        return None

    projection = calibration.attrib.get("type", "unknown")
    params = {}
    for tag in _CALIBRATION_PARAMS:
        elem = calibration.find(tag)
        if elem is not None and elem.text:
            params[tag] = float(elem.text)

    return {
        "projection": projection,
        "width": int(resolution.attrib["width"]),
        "height": int(resolution.attrib["height"]),
        **params,
    }


def _detect_prefix(labels: list[str]) -> str:
    """Find the most common prefix among a list of camera labels.

    For numbered sequences like ['cam1_front_0001', 'cam1_front_0002'],
    returns 'cam1_front_'. For non-numbered labels, returns the most
    common label as-is.
    """
    if not labels:
        return ""
    prefixes = []
    for label in labels:
        prefix, number = _split_label(label)
        if number is not None:
            prefixes.append(prefix)
        else:
            prefixes.append(label)
    if not prefixes:
        return ""
    from collections import Counter
    counts = Counter(prefixes)
    return counts.most_common(1)[0][0]


def discover_sensors(xml_path: Path) -> dict:
    """Discover sensors from a Metashape cameras.xml file.

    Returns:
        dict with four classification keys, each mapping to a list of sensor
        records:
            'equisolid'        — equisolid fisheye sensors
            'equidistant'      — equidistant fisheye sensors
            'frame'            — pinhole frame sensors
            'equirectangular'  — equirectangular sensors

        Each sensor record contains:
            sensor_id   : int
            label       : str (Metashape sensor label)
            camera_count: int (number of aligned cameras for this sensor)
            prefix      : str (auto-detected camera label prefix)
            calibration : dict | None (from extract_sensor_calibration)
            camera_ids  : list[int] (for fisheye/equirect — used by mapping)
            camera_labels: list[str] (for frame/equirect — used by filename matching)

        Or on error:
            {"error": "description"}

    Unknown-classification sensors are dropped (not surfaced as a separate
    category). The exporter and GUI both ignore them.
    """
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        return {"error": f"Failed to parse XML: {e}"}
    except Exception as e:
        return {"error": str(e)}

    root = tree.getroot()

    sensor_info: dict[int, dict] = {}
    for sensor in root.findall(".//sensor"):
        sid = int(sensor.attrib["id"])
        sensor_info[sid] = {
            "sensor_id": sid,
            "label": sensor.attrib.get("label", f"Sensor {sid}"),
            "type": classify_sensor_element(sensor),
            "calibration": extract_sensor_calibration(sensor),
        }

    # Collect aligned cameras per sensor. A camera is "aligned" iff it has a
    # 16-element <transform> block. Unaligned cameras are skipped — they
    # cannot contribute to the COLMAP scene.
    cameras_by_sensor: dict[int, list[dict]] = {}
    for camera in root.findall(".//camera"):
        sid = int(camera.attrib.get("sensor_id", "-1"))
        if sid not in sensor_info:
            continue
        transform = (camera.findtext("transform") or "").split()
        if len(transform) != 16:
            continue
        cameras_by_sensor.setdefault(sid, []).append({
            "id": int(camera.attrib["id"]),
            "label": camera.attrib.get("label", ""),
        })

    categories: dict[str, list[dict]] = {
        "equisolid": [],
        "equidistant": [],
        "frame": [],
        "equirectangular": [],
    }

    for sid, info in sorted(sensor_info.items()):
        cams = cameras_by_sensor.get(sid, [])
        labels = [c["label"] for c in cams]
        prefix = _detect_prefix(labels)

        record = {
            "sensor_id": sid,
            "label": info["label"],
            "camera_count": len(cams),
            "prefix": prefix,
            "calibration": info["calibration"],
        }

        stype = info["type"]
        if stype == "equisolid_fisheye":
            record["camera_ids"] = [c["id"] for c in cams]
            categories["equisolid"].append(record)
        elif stype == "equidistant_fisheye":
            record["camera_ids"] = [c["id"] for c in cams]
            categories["equidistant"].append(record)
        elif stype == "frame":
            record["camera_labels"] = labels
            categories["frame"].append(record)
        elif stype == "equirectangular":
            record["camera_ids"] = [c["id"] for c in cams]
            record["camera_labels"] = labels
            categories["equirectangular"].append(record)
        # 'unknown' sensors are intentionally dropped

    return categories


def auto_group_into_bodies(equisolid_sensors: list[dict]) -> list[dict]:
    """Group equisolid sensors into camera bodies by prefix similarity.

    Algorithm: strip the last _-delimited segment from each sensor's prefix.
    Sensors with identical stripped prefixes are grouped into the same body.

    NOTE: This function exists for the legacy Phase 1 COLMAP-tab GUI. The
    redesigned tab does not use Body grouping (per the
    2026-05-14-colmap-export-tab-redesign.md plan). It is retained here
    until the GUI is fully migrated to v2; remove when no callers remain.
    """
    if not equisolid_sensors:
        return []

    def body_key(prefix: str) -> str:
        stripped = prefix.rstrip("_-")
        parts = re.split(r"[_\-]", stripped)
        if len(parts) > 1:
            return "_".join(parts[:-1])
        return stripped

    groups: dict[str, list[dict]] = {}
    for sensor in equisolid_sensors:
        key = body_key(sensor["prefix"])
        groups.setdefault(key, []).append(sensor)

    bodies = []
    for name, sensors in groups.items():
        bodies.append({
            "name": name,
            "sensor_ids": [s["sensor_id"] for s in sensors],
            "sensors": sensors,
        })
    bodies.sort(key=lambda b: min(b["sensor_ids"]))
    return bodies


def match_frame_sensor_images(image_dir: Path, camera_labels: list[str]) -> dict:
    """Match files in image_dir against expected camera labels.

    Returns dict with matched count, total expected, and list of missing labels.
    Matching is by filename stem (case-sensitive), any image extension accepted.
    """
    stems_in_dir = {f.stem for f in image_dir.iterdir() if f.is_file()}
    matched = []
    missing = []
    for label in camera_labels:
        if label in stems_in_dir:
            matched.append(label)
        else:
            missing.append(label)
    return {
        "matched": len(matched),
        "total": len(camera_labels),
        "missing": missing,
    }


def match_sensor_images_multi(image_dirs: list[Path], camera_labels: list[str]) -> dict:
    """Match files across multiple directories against expected camera labels.

    Generalization of `match_frame_sensor_images` for the v2 multi-directory
    inputs. Union the stems from every directory, then count matches against
    the expected camera labels.

    Used by both fisheye-sensor cards (where one sensor's images may live in
    front/ and back/ subdirectories) and frame-sensor cards (where one frame
    sensor's images may be split across multiple capture sessions).
    """
    stems_in_dirs: set[str] = set()
    for d in image_dirs:
        if d is None:
            continue
        d = Path(d)
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file():
                stems_in_dirs.add(f.stem)

    matched = [label for label in camera_labels if label in stems_in_dirs]
    missing = [label for label in camera_labels if label not in stems_in_dirs]
    return {
        "matched": len(matched),
        "total": len(camera_labels),
        "missing": missing,
    }
