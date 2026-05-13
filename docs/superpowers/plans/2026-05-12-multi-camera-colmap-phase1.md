# Multi-Camera COLMAP Export — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded Lens A/B COLMAP export workflow with cameras.xml-driven sensor discovery supporting any number of 360 cameras and frame sensors.

**Architecture:** Parse Metashape `cameras.xml` to discover sensors, auto-group fisheye sensors into camera bodies, present per-sensor configuration cards in the GUI, and export via a JSON scene manifest. The cubeface converter gets a `process_sensor()` library API so the exporter can call it directly. GUI restructured with purpose toggle at top, 1:1 panel ratio, draggable divider.

**Tech Stack:** Python 3.12, CustomTkinter, xml.etree.ElementTree, JSON manifests, subprocess for exporter invocation.

**Spec:** `docs/superpowers/specs/2026-05-12-multi-camera-colmap-export-design.md`

**Scope:** Phase 1 only — core GUI restructuring + multi-sensor export. Phase 2 (3D scene viewer) is a separate plan.

---

## File Structure

### Files to create
- `gui/sensor_discovery.py` — XML parsing, sensor classification, body grouping logic (moved from gui.py + mapping_resolver.py + new `auto_group_into_bodies`)
- `gui/scene_manifest.py` — data classes for Scene/Body/FisheyeSensor/FrameSensor/ExportOptions + JSON serialization
- `tests/test_sensor_discovery.py` — tests for XML parsing, run detection, body grouping
- `tests/test_scene_manifest.py` — tests for manifest serialization/deserialization
- `tests/test_process_sensor.py` — tests for the cubeface converter library API
- `tests/fixtures/dual_x5_cameras.xml` — synthetic test XML with 4 equisolid + 2 frame sensors

### Files to modify
- `AM_ImageAndMask_to_cubemap_v4.py` — extract `process_sensor()` from `main()`
- `metashape_cameras_to_colmap.py` — add `--scene-manifest` argument, manifest-driven pipeline
- `gui/gui.py` — purpose toggle at top, mode-dependent panels, draggable divider, COLMAP left panel, prefs persistence

---

## Task 1: Test fixture — synthetic multi-camera XML

**Files:**
- Create: `tests/fixtures/dual_x5_cameras.xml`

A minimal but realistic `cameras.xml` with 4 equisolid sensors (simulating 2 X5 cameras × 2 lenses) and 2 frame sensors (drone + phone). Each sensor has a few cameras with transforms and labels following the `cam1_front_0001` naming convention.

- [ ] **Step 1: Create the fixture file**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<document version="2.3.0">
  <chunk label="Chunk 1" enabled="true">
    <sensors next_id="6">
      <!-- X5 Camera 1, front lens -->
      <sensor id="0" label="Insta360 X5 (1)" type="equisolid_fisheye">
        <resolution width="3840" height="3840"/>
        <calibration type="equisolid_fisheye" class="adjusted">
          <resolution width="3840" height="3840"/>
          <f>1055.41</f><cx>-5.32</cx><cy>0.03</cy>
          <k1>0.063</k1><k2>0.065</k2><k3>-0.024</k3>
          <p1>0.00018</p1><p2>-0.00019</p2>
        </calibration>
      </sensor>
      <!-- X5 Camera 1, back lens -->
      <sensor id="1" label="Insta360 X5 (2)" type="equisolid_fisheye">
        <resolution width="3840" height="3840"/>
        <calibration type="equisolid_fisheye" class="adjusted">
          <resolution width="3840" height="3840"/>
          <f>1060.0</f><cx>-3.0</cx><cy>1.0</cy>
          <k1>0.060</k1><k2>0.062</k2><k3>-0.020</k3>
          <p1>0.00015</p1><p2>-0.00016</p2>
        </calibration>
      </sensor>
      <!-- X5 Camera 2, front lens -->
      <sensor id="2" label="Insta360 X5 (3)" type="equisolid_fisheye">
        <resolution width="3840" height="3840"/>
        <calibration type="equisolid_fisheye" class="adjusted">
          <resolution width="3840" height="3840"/>
          <f>1058.0</f><cx>-4.0</cx><cy>0.5</cy>
          <k1>0.061</k1><k2>0.063</k2><k3>-0.022</k3>
          <p1>0.00017</p1><p2>-0.00018</p2>
        </calibration>
      </sensor>
      <!-- X5 Camera 2, back lens -->
      <sensor id="3" label="Insta360 X5 (4)" type="equisolid_fisheye">
        <resolution width="3840" height="3840"/>
        <calibration type="equisolid_fisheye" class="adjusted">
          <resolution width="3840" height="3840"/>
          <f>1062.0</f><cx>-2.0</cx><cy>0.8</cy>
          <k1>0.064</k1><k2>0.066</k2><k3>-0.025</k3>
          <p1>0.00016</p1><p2>-0.00017</p2>
        </calibration>
      </sensor>
      <!-- DJI drone -->
      <sensor id="4" label="DJI Mavic 24mm" type="frame">
        <resolution width="5120" height="2700"/>
        <calibration type="frame" class="adjusted">
          <resolution width="5120" height="2700"/>
          <f>3605.32</f><cx>21.4</cx><cy>-39.25</cy>
          <k1>0.021</k1><k2>-0.074</k2><k3>0.097</k3>
          <p1>0.00013</p1><p2>-0.0011</p2>
        </calibration>
      </sensor>
      <!-- iPhone -->
      <sensor id="5" label="iPhone 15 Pro" type="frame">
        <resolution width="4032" height="3024"/>
        <calibration type="frame" class="adjusted">
          <resolution width="4032" height="3024"/>
          <f>2800.0</f><cx>10.0</cx><cy>-5.0</cy>
          <k1>0.01</k1><k2>-0.03</k2><k3>0.02</k3>
          <p1>0.0001</p1><p2>-0.0002</p2>
        </calibration>
      </sensor>
    </sensors>
    <cameras>
      <!-- Body 1 front (sensor 0): cam1_front_0001..0003 -->
      <camera id="0" sensor_id="0" label="cam1_front_0001">
        <transform>1 0 0 0 0 1 0 0 0 0 1 -5 0 0 0 1</transform>
      </camera>
      <camera id="1" sensor_id="0" label="cam1_front_0002">
        <transform>1 0 0 1 0 1 0 0 0 0 1 -5 0 0 0 1</transform>
      </camera>
      <camera id="2" sensor_id="0" label="cam1_front_0003">
        <transform>1 0 0 2 0 1 0 0 0 0 1 -5 0 0 0 1</transform>
      </camera>
      <!-- Body 1 back (sensor 1): cam1_back_0001..0003 -->
      <camera id="3" sensor_id="1" label="cam1_back_0001">
        <transform>-1 0 0 0 0 1 0 0 0 0 -1 -5 0 0 0 1</transform>
      </camera>
      <camera id="4" sensor_id="1" label="cam1_back_0002">
        <transform>-1 0 0 1 0 1 0 0 0 0 -1 -5 0 0 0 1</transform>
      </camera>
      <camera id="5" sensor_id="1" label="cam1_back_0003">
        <transform>-1 0 0 2 0 1 0 0 0 0 -1 -5 0 0 0 1</transform>
      </camera>
      <!-- Body 2 front (sensor 2): cam2_front_0001..0003 -->
      <camera id="6" sensor_id="2" label="cam2_front_0001">
        <transform>1 0 0 10 0 1 0 0 0 0 1 -5 0 0 0 1</transform>
      </camera>
      <camera id="7" sensor_id="2" label="cam2_front_0002">
        <transform>1 0 0 11 0 1 0 0 0 0 1 -5 0 0 0 1</transform>
      </camera>
      <camera id="8" sensor_id="2" label="cam2_front_0003">
        <transform>1 0 0 12 0 1 0 0 0 0 1 -5 0 0 0 1</transform>
      </camera>
      <!-- Body 2 back (sensor 3): cam2_back_0001..0003 -->
      <camera id="9" sensor_id="3" label="cam2_back_0001">
        <transform>-1 0 0 10 0 1 0 0 0 0 -1 -5 0 0 0 1</transform>
      </camera>
      <camera id="10" sensor_id="3" label="cam2_back_0002">
        <transform>-1 0 0 11 0 1 0 0 0 0 -1 -5 0 0 0 1</transform>
      </camera>
      <camera id="11" sensor_id="3" label="cam2_back_0003">
        <transform>-1 0 0 12 0 1 0 0 0 0 -1 -5 0 0 0 1</transform>
      </camera>
      <!-- DJI drone (sensor 4): DJI_0001..0003 -->
      <camera id="12" sensor_id="4" label="DJI_0001">
        <transform>1 0 0 5 0 1 0 5 0 0 1 20 0 0 0 1</transform>
      </camera>
      <camera id="13" sensor_id="4" label="DJI_0002">
        <transform>1 0 0 6 0 1 0 5 0 0 1 20 0 0 0 1</transform>
      </camera>
      <camera id="14" sensor_id="4" label="DJI_0003">
        <transform>1 0 0 7 0 1 0 5 0 0 1 20 0 0 0 1</transform>
      </camera>
      <!-- iPhone (sensor 5): IMG_4231..4233 -->
      <camera id="15" sensor_id="5" label="IMG_4231">
        <transform>1 0 0 0 0 1 0 2 0 0 1 0 0 0 0 1</transform>
      </camera>
      <camera id="16" sensor_id="5" label="IMG_4232">
        <transform>1 0 0 1 0 1 0 2 0 0 1 0 0 0 0 1</transform>
      </camera>
    </cameras>
  </chunk>
</document>
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/dual_x5_cameras.xml
git commit -m "test: add synthetic multi-camera XML fixture for sensor discovery tests"
```

---

## Task 2: Sensor discovery module — XML parsing + classification

**Files:**
- Create: `gui/sensor_discovery.py`
- Create: `tests/test_sensor_discovery.py`
- Reference: `gui/gui.py:185-310` (existing run detection to move), `gui/mapping_resolver.py:383` (existing `_split_label`)

Extract XML parsing and sensor classification into a standalone module. Move `_metashape_equisolid_camera_runs()` and `_merge_fragmented_runs()` from `gui.py`, adapt for general sensor discovery (not just equisolid).

- [ ] **Step 0: Create `gui/__init__.py`**

The `gui/` directory is not a Python package. Create an empty `gui/__init__.py` so imports like `from gui.sensor_discovery import ...` work.

```bash
touch gui/__init__.py
```

- [ ] **Step 1: Write failing tests for sensor discovery**

In `tests/test_sensor_discovery.py`:

```python
import sys
from pathlib import Path
import pytest

# Add project root to path so gui package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).parent / "fixtures"

def test_discover_sensors_counts():
    """XML with 4 equisolid + 2 frame sensors should return correct counts."""
    from gui.sensor_discovery import discover_sensors
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    assert len(result["equisolid"]) == 4
    assert len(result["frame"]) == 2

def test_discover_sensors_metadata():
    """Each discovered sensor has sensor_id, label, camera_count, prefix."""
    from gui.sensor_discovery import discover_sensors
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    s0 = result["equisolid"][0]
    assert s0["sensor_id"] == 0
    assert s0["label"] == "Insta360 X5 (1)"
    assert s0["camera_count"] == 3
    assert s0["prefix"] == "cam1_front_"

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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd d:\Projects\Fisheye-to-Cubemap
python -m pytest tests/test_sensor_discovery.py -v
```

Expected: ModuleNotFoundError for `gui.sensor_discovery`

- [ ] **Step 3: Implement `gui/sensor_discovery.py`**

Create the module with `discover_sensors(xml_path) -> dict`. Move `_metashape_equisolid_camera_runs()` and `_merge_fragmented_runs()` from `gui/gui.py` (lines 185-310). Extend to also discover frame sensors. Key functions:

- `discover_sensors(xml_path: Path) -> dict` — main entry point. Returns `{"equisolid": [...], "frame": [...]}` or `{"error": "..."}`.
- `_classify_sensor(sensor_element) -> str` — returns "equisolid_fisheye", "frame", or "unknown"
- `_detect_camera_runs(root, sensor_ids) -> list[dict]` — adapted from existing `_metashape_equisolid_camera_runs`
- `_merge_fragmented_runs(runs) -> list[dict]` — moved from gui.py
- `_split_label(label) -> tuple[str, int|None]` — moved from mapping_resolver.py (duplicate exists in gui.py too)

Each equisolid sensor entry: `{"sensor_id", "label", "camera_count", "prefix", "camera_ids"}`.
Each frame sensor entry: `{"sensor_id", "label", "camera_count", "prefix", "camera_labels"}`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_sensor_discovery.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add gui/__init__.py gui/sensor_discovery.py tests/test_sensor_discovery.py
git commit -m "feat: add sensor discovery module — XML parsing and sensor classification"
```

---

## Task 3: Body grouping algorithm

**Files:**
- Modify: `gui/sensor_discovery.py`
- Modify: `tests/test_sensor_discovery.py`

Add `auto_group_into_bodies()` that groups equisolid sensors by prefix similarity.

- [ ] **Step 1: Write failing tests for body grouping**

Append to `tests/test_sensor_discovery.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify new tests fail**

```bash
python -m pytest tests/test_sensor_discovery.py::test_auto_group_two_bodies -v
```

Expected: ImportError for `auto_group_into_bodies`

- [ ] **Step 3: Implement `auto_group_into_bodies()`**

Add to `gui/sensor_discovery.py`:

```python
def auto_group_into_bodies(equisolid_sensors: list[dict]) -> list[dict]:
    """Group equisolid sensors into camera bodies by prefix similarity.
    
    Algorithm: strip the last _-delimited segment from each sensor's prefix.
    Sensors with identical stripped prefixes are grouped into the same body.
    """
    if not equisolid_sensors:
        return []
    
    def body_key(prefix: str) -> str:
        # "cam1_front_" -> strip trailing _, split by _, drop last -> "cam1"
        stripped = prefix.rstrip("_-")
        parts = re.split(r"[_\-]", stripped)
        if len(parts) > 1:
            return "_".join(parts[:-1])
        return stripped
    
    groups = {}
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
```

- [ ] **Step 4: Run all sensor discovery tests**

```bash
python -m pytest tests/test_sensor_discovery.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add gui/sensor_discovery.py tests/test_sensor_discovery.py
git commit -m "feat: add auto body grouping by prefix similarity"
```

---

## Task 4: Scene manifest data model + serialization

**Files:**
- Create: `gui/scene_manifest.py`
- Create: `tests/test_scene_manifest.py`

Data classes for the scene configuration and JSON serialization.

- [ ] **Step 1: Write failing tests**

In `tests/test_scene_manifest.py`:

```python
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def test_manifest_round_trip(tmp_path):
    """Manifest serializes to JSON and deserializes back identically."""
    from gui.scene_manifest import SceneManifest, Body, FisheyeSensor, FrameSensor, ExportOptions
    manifest = SceneManifest(
        cameras_xml=Path("cameras.xml"),
        sparse_ply=Path("pointcloud.ply"),
        output_dir=Path("output"),
        bodies=[Body(
            name="X5 Camera 1",
            output_width=2048,
            sensors=[FisheyeSensor(
                sensor_id=0,
                calibration_xml=Path("cal.xml"),
                image_dir=Path("images"),
                mask=Path("mask.png"),
            )],
        )],
        frame_sensors=[FrameSensor(sensor_id=4, image_dir=Path("drone"))],
        options=ExportOptions(),
    )
    path = tmp_path / "manifest.json"
    manifest.save(path)
    loaded = SceneManifest.load(path)
    assert loaded.bodies[0].name == "X5 Camera 1"
    assert loaded.bodies[0].output_width == 2048
    assert loaded.bodies[0].sensors[0].sensor_id == 0
    assert loaded.frame_sensors[0].sensor_id == 4
    assert loaded.options.projected_tracks is True  # default

def test_manifest_defaults():
    """ExportOptions has correct defaults."""
    from gui.scene_manifest import ExportOptions
    opts = ExportOptions()
    assert opts.pose_convention == "metashape_camera_to_world"
    assert opts.require_masks is False
    assert opts.projected_tracks is True
    assert opts.force_assets is False
    assert opts.normalize_scene is False
    assert opts.keep_processing_files is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_scene_manifest.py -v
```

- [ ] **Step 3: Implement `gui/scene_manifest.py`**

Dataclasses with `save(path)` and `load(path)` class methods. JSON serialization converts Path to string.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_scene_manifest.py -v
```

- [ ] **Step 5: Commit**

```bash
git add gui/scene_manifest.py tests/test_scene_manifest.py
git commit -m "feat: add scene manifest data model with JSON serialization"
```

---

## Task 5: Frame sensor filename matching

**Files:**
- Modify: `gui/sensor_discovery.py`
- Modify: `tests/test_sensor_discovery.py`

Add `match_frame_sensor_images()` that validates an image directory against XML camera labels.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_sensor_discovery.py`:

```python
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
```

- [ ] **Step 2: Run to verify fail, implement, run to verify pass**

- [ ] **Step 3: Commit**

```bash
git add gui/sensor_discovery.py tests/test_sensor_discovery.py
git commit -m "feat: add frame sensor filename matching against XML labels"
```

---

## Task 6: Cubeface converter `process_sensor()` API

**Files:**
- Modify: `AM_ImageAndMask_to_cubemap_v4.py:1670-2330`
- Create: `tests/test_process_sensor.py`

Extract the callable `process_sensor()` function from `main()`. This is a careful refactor — `main()` becomes a thin CLI wrapper.

- [ ] **Step 1: Write a smoke test for `process_sensor()`**

In `tests/test_process_sensor.py`:

```python
from pathlib import Path
import importlib.util

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "AM_ImageAndMask_to_cubemap_v4.py"

spec = importlib.util.spec_from_file_location("cubemap_v4", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def test_process_sensor_exists():
    """process_sensor is importable and callable."""
    assert hasattr(mod, "process_sensor")
    assert callable(mod.process_sensor)

def test_process_sensor_signature():
    """process_sensor accepts the expected parameters."""
    import inspect
    sig = inspect.signature(mod.process_sensor)
    param_names = list(sig.parameters.keys())
    assert "calibration_xml" in param_names
    assert "image_dir" in param_names
    assert "mask" in param_names
    assert "output_dir" in param_names
    assert "face_width" in param_names
    assert "progress_callback" in param_names
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m pytest tests/test_process_sensor.py -v
```

- [ ] **Step 3: Refactor `main()` to extract `process_sensor()`**

**Boundary guide for the split:**

Stays in `main()` (~lines 1670-1840):
- `codeversion` string (line 1687)
- `logging.basicConfig()` setup (line 1695)
- `argparse.ArgumentParser` and all `parser.add_argument()` calls (lines 1702-1736)
- `args = parser.parse_args()` (line 1736)
- `--version`, `--h`, `--usage` early returns (lines 1741-1840)
- Map parsed args to `process_sensor()` parameters and call it

Moves into `process_sensor()` (~lines 1843-2320):
- Input validation (required args checks, path existence)
- Mask/support resolution (`_resolve_support_inputs` equivalent)
- Remapping computation and caching
- Image processing loop (iterate images, remap each, write cubefaces + masks)
- Progress reporting (replace `print`/`logging` with `progress_callback`)
- Final summary/results dict

Replace `print()` and `logging.info()` calls inside the moved code with `if progress_callback: progress_callback(message)` so the GUI can route output to its console.

Key constraint: **do not change the behavior of the CLI interface.** Running `python AM_ImageAndMask_to_cubemap_v4.py --amlenscal=... --directoryfisheyeimages=... --facewidth=2100 --outputdir=...` must produce identical output before and after this refactor. The `main()` wrapper sets up logging and calls `process_sensor()` with a callback that writes to the logger, preserving the original output.

- [ ] **Step 4: Run existing test + new tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass (including existing `test_scene_scale.py`)

- [ ] **Step 5: Manual verification — run CLI to confirm identical behavior**

```bash
python AM_ImageAndMask_to_cubemap_v4.py --version
```

Expected: prints version string, exits cleanly.

- [ ] **Step 6: Commit**

```bash
git add AM_ImageAndMask_to_cubemap_v4.py tests/test_process_sensor.py
git commit -m "refactor: extract process_sensor() API from cubeface converter main()"
```

---

## Task 7: Exporter `--scene-manifest` support

**Files:**
- Modify: `metashape_cameras_to_colmap.py`
- Create: `tests/test_exporter_manifest.py`

Add `--scene-manifest` argument. When provided, the exporter reads the JSON manifest, runs cubeface generation per sensor via `process_sensor()`, and processes frame sensors from manifest image directories. The old arguments continue to work for backward compatibility.

### Step 7a: Argparser + manifest loading

- [ ] **Step 1: Write test for manifest loading**

In `tests/test_exporter_manifest.py`:

```python
import importlib.util
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "metashape_cameras_to_colmap.py"
spec = importlib.util.spec_from_file_location("exporter", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FIXTURES = Path(__file__).parent / "fixtures"

def test_load_scene_manifest(tmp_path):
    """Exporter can load and parse a scene manifest JSON."""
    manifest = {
        "cameras_xml": str(FIXTURES / "dual_x5_cameras.xml"),
        "sparse_ply": "pointcloud.ply",
        "output_dir": str(tmp_path / "output"),
        "bodies": [{
            "name": "X5 Camera 1",
            "output_width": 2048,
            "sensors": [{"sensor_id": 0, "calibration_xml": "cal.xml", "image_dir": "images", "mask": "mask.png"}],
        }],
        "frame_sensors": [{"sensor_id": 4, "image_dir": "drone"}],
        "options": {"pose_convention": "metashape_camera_to_world"},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    loaded = mod.load_scene_manifest(manifest_path)
    assert loaded["bodies"][0]["name"] == "X5 Camera 1"
    assert len(loaded["bodies"][0]["sensors"]) == 1
    assert loaded["options"]["pose_convention"] == "metashape_camera_to_world"
```

- [ ] **Step 2: Add `--scene-manifest` argument and `load_scene_manifest()` function**

Add the argparse argument (~line 4728) and a `load_scene_manifest(path) -> dict` function that reads and validates the JSON.

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests/test_exporter_manifest.py -v
```

- [ ] **Step 4: Commit**

```bash
git add metashape_cameras_to_colmap.py tests/test_exporter_manifest.py
git commit -m "feat: add --scene-manifest argument and manifest loading to exporter"
```

### Step 7b: Manifest-driven cubeface generation

- [ ] **Step 5: Add manifest dispatch branch**

In the exporter's main flow, add a branch: if `args.scene_manifest` is provided:
1. Load manifest via `load_scene_manifest()`
2. For each body → for each sensor: call `process_sensor()` (imported from cubeface converter) with the sensor's calibration XML, image dir, mask, and body's output_width
3. Collect cubeface output paths for pose composition

This bypasses the old `--cubeface-root` / `--lens-camera-map` code path entirely. The existing code path stays for backward compatibility.

- [ ] **Step 6: Add manifest dispatch branch for frame sensors**

For each frame sensor in the manifest:
1. Validate image directory against XML camera labels
2. Package images into the COLMAP output (same logic as existing passthrough pipeline, but driven by manifest instead of `--passthrough-media-manifest`)

- [ ] **Step 7: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass including existing `test_scene_scale.py`

- [ ] **Step 8: Commit**

```bash
git add metashape_cameras_to_colmap.py
git commit -m "feat: implement manifest-driven cubeface generation and frame sensor passthrough"
```

---

## Task 8: GUI restructure — purpose toggle at top + draggable divider

**Files:**
- Modify: `gui/gui.py`

Restructure the GUI shell: purpose toggle moves to the very top, panels use 1:1 ratio with a draggable divider grip between them. The left panel content switches based on mode. This task handles the shell only — the COLMAP left panel content comes in the next task.

- [ ] **Step 1: Move purpose toggle to top of `_build_ui()`**

Currently the purpose toggle is buried inside `_build_left()` at the "Shared Settings" section (gui.py ~974-988). Move it to `_build_ui()` so it sits above both panels. The toggle controls which content frame is visible in the left panel.

- [ ] **Step 2: Replace grid layout with drag-handle approach**

Replace the `grid_columnconfigure` setup (gui.py ~896-900) with three horizontal children: left `CTkFrame`, grip `CTkFrame` (8px wide, subtle dot pattern), right `CTkFrame`. Bind `<B1-Motion>` on the grip to resize left/right frames. Set minimum widths (left: 380px, right: 300px).

- [ ] **Step 3: Wrap existing left panel content in Metashape conditional frame**

The existing `_build_left()` content (lens config, shared settings, colmap section) stays but is wrapped in a frame that's only visible when purpose == Metashape. A new empty frame is shown when purpose == COLMAP (populated in next task).

- [ ] **Step 4: Add GUI smoke test**

In `tests/test_gui_smoke.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def test_gui_imports_without_error():
    """gui.py can be imported without crashing (no display required)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gui", Path(__file__).resolve().parents[1] / "gui" / "gui.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Don't exec — just verify the module parses without syntax errors
    # Full exec would require a display
    import ast
    source = (Path(__file__).resolve().parents[1] / "gui" / "gui.py").read_text()
    ast.parse(source)  # raises SyntaxError if broken
```

- [ ] **Step 5: Verify Metashape mode still works (manual)**

Launch the GUI, verify:
- Purpose toggle visible at top
- Metashape mode shows lens A/B, shared settings, run/cancel
- Draggable divider works
- All existing functionality preserved

- [ ] **Step 6: Commit**

```bash
git add gui/gui.py tests/test_gui_smoke.py
git commit -m "refactor: move purpose toggle to top, add draggable divider, 1:1 panel ratio"
```

---

## Task 9: GUI — COLMAP left panel (sensor cards + configuration)

**Files:**
- Modify: `gui/gui.py`
- Reference: `gui/sensor_discovery.py`, `gui/scene_manifest.py`

Build the COLMAP-mode left panel: XML file pickers, sensor discovery, body-grouped fisheye sensor cards, frame sensor cards, options, export button.

- [ ] **Step 1: Build the COLMAP left panel structure**

New method `_build_colmap_left(parent)` that creates:
- cameras.xml, sparse .ply, output dir file pickers
- Discovery status bar (hidden until XML loaded)
- Fisheye Sensors section (populated dynamically after XML parse)
- Frame Sensors section (populated dynamically)
- Options section (pose dropdown, checkboxes)
- Export / Cancel buttons

- [ ] **Step 2: Wire up XML loading → sensor discovery**

When the user browses to a cameras.xml:
1. Call `discover_sensors()` from `sensor_discovery.py`
2. If error: show error in discovery status bar, disable Export, stop
3. If zero equisolid sensors: show "No fisheye sensors found" message, hide body section
4. Call `auto_group_into_bodies()` on the equisolid results
5. Dynamically create body frames with sensor cards (show "0 cameras (none aligned)" in amber for sensors with zero aligned cameras)
6. Dynamically create frame sensor cards
7. Update discovery status bar with counts
8. Export button stays disabled until at least one fisheye sensor has all three fields filled

- [ ] **Step 3: Build sensor card widgets**

Each fisheye sensor card: header (sensor ID, badge, metadata line), cal XML browse, images browse, mask browse.
Each frame sensor card: header (sensor ID, badge, metadata), images browse, match status label.
Body header: editable name entry, shared output width entry.

- [ ] **Step 4: Wire up frame sensor image matching**

When the user sets an image directory for a frame sensor, call `match_frame_sensor_images()` and update the match status label.

- [ ] **Step 5: Wire up Export button**

Export button calls `_build_cmd_for_colmap_export()` which:
1. Builds a `SceneManifest` from the GUI state
2. Saves it to a temp file
3. Constructs the subprocess command with `--scene-manifest`
4. Queues it for execution

- [ ] **Step 6: Verify COLMAP mode works end-to-end visually**

Launch GUI, switch to COLMAP mode, browse to `dev/equisolid_and_pinhole_dataset/cameras.xml`, verify sensors are discovered and cards appear.

- [ ] **Step 7: Commit**

```bash
git add gui/gui.py
git commit -m "feat: add COLMAP left panel with sensor discovery and body grouping"
```

---

## Task 10: Preference persistence for COLMAP mode

**Files:**
- Modify: `gui/gui.py`

Store and restore COLMAP sensor configuration in prefs.

- [ ] **Step 0: Write prefs round-trip test**

In `tests/test_scene_manifest.py`, append:

```python
def test_colmap_prefs_round_trip(tmp_path):
    """COLMAP manifest can be stored in and restored from prefs dict."""
    from gui.scene_manifest import SceneManifest, Body, FisheyeSensor, FrameSensor, ExportOptions
    import json

    manifest = SceneManifest(
        cameras_xml=Path("cameras.xml"),
        sparse_ply=Path("pointcloud.ply"),
        output_dir=Path("output"),
        bodies=[Body(name="Test", output_width=2048, sensors=[
            FisheyeSensor(sensor_id=0, calibration_xml=Path("c.xml"), image_dir=Path("i"), mask=Path("m.png")),
        ])],
        frame_sensors=[FrameSensor(sensor_id=4, image_dir=Path("drone"))],
        options=ExportOptions(),
    )
    
    # Simulate prefs storage
    prefs = {"colmap_last_manifest": manifest.to_dict()}
    prefs_json = json.dumps(prefs)
    restored_prefs = json.loads(prefs_json)
    restored = SceneManifest.from_dict(restored_prefs["colmap_last_manifest"])
    
    assert restored.bodies[0].name == "Test"
    assert restored.bodies[0].sensors[0].sensor_id == 0
    assert restored.frame_sensors[0].sensor_id == 4
```

This test requires adding `to_dict()` and `from_dict()` methods to `SceneManifest` (in addition to the existing `save()`/`load()` which wrap these). Add these to `gui/scene_manifest.py` if not already present.

- [ ] **Step 1: Extend `_save_current_prefs()` to include COLMAP manifest**

When saving prefs, if COLMAP mode has been configured, serialize the current GUI state as a `colmap_last_manifest` JSON blob within the existing prefs dict.

- [ ] **Step 2: Extend `_load_prefs()` restore path**

On GUI load, if `colmap_last_manifest` exists in prefs and the `cameras_xml` path is still valid:
1. Re-parse the XML
2. Compare sensor IDs against stored manifest
3. If matching: restore per-sensor fields (cal XML, image dir, mask, body names, output widths)
4. If changed: show new sensors with cleared fields

- [ ] **Step 3: Verify prefs round-trip**

Configure COLMAP mode, close GUI, reopen, verify settings are restored.

- [ ] **Step 4: Commit**

```bash
git add gui/gui.py
git commit -m "feat: persist and restore COLMAP sensor configuration in prefs"
```

---

## Task 11: Clean up old COLMAP UI code

**Files:**
- Modify: `gui/gui.py`

Remove the old COLMAP-specific UI elements that are replaced by the new sensor discovery workflow. These are only hidden in COLMAP mode but their code is still present.

- [ ] **Step 1: Remove from COLMAP mode path only**

Remove or gate behind Metashape mode:
- Manual lens-to-camera mapping text field and associated variables (`_lens_camera_map`, `_last_proposed_spec`, etc.)
- "Check Mapping" and "Use Proposed Map" buttons
- "Add Frame Camera Media Set" button and `_media_sets_frame`
- The old `_build_colmap_export_section()` method (replaced by `_build_colmap_left()`)

Keep backward compatibility: the old code can remain as dead code if removing it is risky. Mark with comments for future removal.

- [ ] **Step 2: Update gui.py imports**

Add imports for `sensor_discovery` and `scene_manifest` modules if not already present.

- [ ] **Step 3: Run all tests**

```bash
python -m pytest tests/ -v
```

- [ ] **Step 4: Commit**

```bash
git add gui/gui.py
git commit -m "refactor: remove old COLMAP mapping UI, gate behind Metashape mode"
```

---

## Task 12: Integration testing

**Files:**
- Modify: `tests/test_sensor_discovery.py`

Verify the full pipeline: XML → discovery → body grouping → manifest → exporter invocation.

- [ ] **Step 1: Write integration test**

```python
def test_full_pipeline_xml_to_manifest(tmp_path):
    """End-to-end: discover sensors, group bodies, build manifest, serialize."""
    from gui.sensor_discovery import discover_sensors, auto_group_into_bodies
    from gui.scene_manifest import SceneManifest, Body, FisheyeSensor, FrameSensor, ExportOptions
    
    result = discover_sensors(FIXTURES / "dual_x5_cameras.xml")
    bodies_raw = auto_group_into_bodies(result["equisolid"])
    
    bodies = []
    for b in bodies_raw:
        sensors = [
            FisheyeSensor(
                sensor_id=s["sensor_id"],
                calibration_xml=Path("cal.xml"),
                image_dir=Path("images"),
                mask=Path("mask.png"),
            )
            for s in b["sensors"]
        ]
        bodies.append(Body(name=b["name"], output_width=2048, sensors=sensors))
    
    frame_sensors = [
        FrameSensor(sensor_id=fs["sensor_id"], image_dir=Path("images"))
        for fs in result["frame"]
    ]
    
    manifest = SceneManifest(
        cameras_xml=FIXTURES / "dual_x5_cameras.xml",
        sparse_ply=Path("pointcloud.ply"),
        output_dir=tmp_path / "output",
        bodies=bodies,
        frame_sensors=frame_sensors,
        options=ExportOptions(),
    )
    
    path = tmp_path / "manifest.json"
    manifest.save(path)
    
    loaded = SceneManifest.load(path)
    assert len(loaded.bodies) == 2
    assert len(loaded.bodies[0].sensors) == 2
    assert len(loaded.frame_sensors) == 2
```

- [ ] **Step 2: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_sensor_discovery.py
git commit -m "test: add integration test for discovery-to-manifest pipeline"
```
