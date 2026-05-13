# Multi-Camera COLMAP Export — Phase 2: 3D Scene Viewer

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an interactive 3D point cloud + camera frustum viewer to the COLMAP export mode's right panel, with per-source frustum coloring and auto-load after export.

**Architecture:** Copy and adapt the `PointCloudViewer` from `reconstruction-zone`, embed it in a tabbed right panel (Console / Scene tabs) using VTK's `SetParentInfo(hwnd)` embedding. Parse the exported COLMAP text files to build the viewer's data model. Color camera frustums per source using a rotating palette. Gracefully degrade when VTK/pyvista are not installed.

**Tech Stack:** Python 3.12, VTK (via pyvista), CustomTkinter, COLMAP text format parsers (adapted from reconstruction-zone's `colmap_validation.py`).

**Spec:** `docs/superpowers/specs/2026-05-12-multi-camera-colmap-export-design.md` (Phase 2 section)

**Depends on:** Phase 1 complete (sensor discovery, GUI restructure, COLMAP export working)

---

## File Structure

### Files to create
- `gui/pointcloud_viewer.py` — adapted PointCloudViewer from reconstruction-zone, with per-source coloring
- `gui/colmap_text_parser.py` — COLMAP text format parsers (cameras.txt, images.txt, points3D.txt) producing the data model the viewer expects
- `tests/test_colmap_text_parser.py` — tests for text parsing
- `tests/test_pointcloud_viewer.py` — import/availability tests (no display required)
- `tests/fixtures/colmap_scene/sparse/0/cameras.txt` — synthetic COLMAP text fixture
- `tests/fixtures/colmap_scene/sparse/0/images.txt` — synthetic COLMAP text fixture
- `tests/fixtures/colmap_scene/sparse/0/points3D.txt` — synthetic COLMAP text fixture

### Files to modify
- `gui/gui.py` — tabbed right panel for COLMAP mode, auto-load after export, controls bar, legend
- `gui/requirements.txt` — add pyvista as optional dependency

### Source reference (do NOT modify — copy and adapt)
- `D:\Projects\reconstruction-zone\reconstruction_gui\pointcloud_viewer.py` — original viewer (784 lines)
- `D:\Projects\reconstruction-zone\reconstruction_gui\colmap_validation.py` — COLMAP text parsers and data classes (COLMAPCamera, COLMAPImage, COLMAPPoint3D)

---

## Task 1: COLMAP text format test fixtures

**Files:**
- Create: `tests/fixtures/colmap_scene/sparse/0/cameras.txt`
- Create: `tests/fixtures/colmap_scene/sparse/0/images.txt`
- Create: `tests/fixtures/colmap_scene/sparse/0/points3D.txt`

Minimal COLMAP text files representing an exported scene with 3 PINHOLE cameras and a handful of 3D points. These are what the exporter produces in `<output>/sparse/0/`.

- [ ] **Step 1: Create the fixture directory and files**

```bash
mkdir -p tests/fixtures/colmap_scene/sparse/0
```

`cameras.txt` — 3 PINHOLE cameras (cubeface-derived + frame):
```
# Camera list with one line of data per camera:
#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]
# Number of cameras: 3
1 PINHOLE 2048 2048 1024.0 1024.0 1024.0 1024.0
2 PINHOLE 2048 2048 1024.0 1024.0 1024.0 1024.0
3 PINHOLE 5120 2700 3605.32 3605.32 2560.0 1350.0
```

`images.txt` — 5 images (3 cubeface from body 1, 2 frame from drone):
```
# Image list with two lines of data per image:
#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME
#   POINTS2D[] as (X, Y, POINT3D_ID)
# Number of images: 5
1 1.0 0.0 0.0 0.0 0.0 0.0 -5.0 1 cam1_front_0001_dir_plusZ.png
100.0 200.0 1 300.0 400.0 2
2 0.7071 0.0 0.7071 0.0 1.0 0.0 -5.0 1 cam1_front_0001_dir_plusX.png
150.0 250.0 1
3 0.7071 0.0 -0.7071 0.0 -1.0 0.0 -5.0 2 cam1_front_0001_dir_minusX.png
200.0 300.0 2
4 1.0 0.0 0.0 0.0 5.0 5.0 20.0 3 DJI_0001.png
500.0 600.0 1 700.0 800.0 3
5 1.0 0.0 0.0 0.0 6.0 5.0 20.0 3 DJI_0002.png
550.0 650.0 2
```

`points3D.txt` — 3 points:
```
# 3D point list with one line of data per point:
#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)
# Number of points: 3
1 1.0 2.0 3.0 128 64 200 0.5 1 0 2 0 4 0
2 4.0 5.0 6.0 200 100 50 0.8 1 1 3 0
3 7.0 8.0 9.0 50 200 100 0.3 4 1 5 0
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/colmap_scene/
git commit -m "test: add synthetic COLMAP text format fixtures for viewer tests"
```

---

## Task 2: COLMAP text parser module

**Files:**
- Create: `gui/colmap_text_parser.py`
- Create: `tests/test_colmap_text_parser.py`

Standalone parsers for COLMAP text format that produce data objects compatible with the PointCloudViewer's `load_model()` interface. Adapted from `reconstruction-zone/reconstruction_gui/colmap_validation.py` (data classes COLMAPCamera, COLMAPImage, COLMAPPoint3D + parse functions).

- [ ] **Step 1: Write failing tests**

In `tests/test_colmap_text_parser.py`:

```python
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).parent / "fixtures" / "colmap_scene" / "sparse" / "0"

def test_parse_cameras():
    from gui.colmap_text_parser import parse_cameras_txt
    cameras = parse_cameras_txt(FIXTURES / "cameras.txt")
    assert len(cameras) == 3
    assert cameras[1].model == "PINHOLE"
    assert cameras[1].width == 2048
    assert cameras[1].height == 2048
    assert len(cameras[1].params) == 4

def test_parse_images():
    from gui.colmap_text_parser import parse_images_txt
    images = parse_images_txt(FIXTURES / "images.txt")
    assert len(images) == 5
    img1 = images[1]
    assert img1.name == "cam1_front_0001_dir_plusZ.png"
    assert img1.camera_id == 1
    assert len(img1.points2d) == 2
    # Verify camera center computation works
    center = img1.get_camera_center()
    assert isinstance(center, np.ndarray)
    assert center.shape == (3,)

def test_parse_points3d():
    from gui.colmap_text_parser import parse_points3d_txt
    points = parse_points3d_txt(FIXTURES / "points3D.txt")
    assert len(points) == 3
    p1 = points[1]
    assert np.allclose(p1.xyz, [1.0, 2.0, 3.0])
    assert np.array_equal(p1.rgb, [128, 64, 200])
    assert abs(p1.error - 0.5) < 1e-6
    assert len(p1.track) == 3  # 3 (image_id, point2d_idx) pairs

def test_parse_model_dir():
    """parse_colmap_model reads all three files and returns viewer-compatible dict."""
    from gui.colmap_text_parser import parse_colmap_model
    model = parse_colmap_model(FIXTURES.parent.parent)  # colmap_scene/
    assert "cameras" in model
    assert "images" in model
    assert "points3D" in model
    assert len(model["cameras"]) == 3
    assert len(model["images"]) == 5
    assert len(model["points3D"]) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_colmap_text_parser.py -v
```

Expected: ModuleNotFoundError for `gui.colmap_text_parser`

- [ ] **Step 3: Implement `gui/colmap_text_parser.py`**

Adapt the data classes and parsers from `D:\Projects\reconstruction-zone\reconstruction_gui\colmap_validation.py`. Copy only what's needed:

Data classes (lines 33-128 of colmap_validation.py):
- `COLMAPCamera` — dataclass with `camera_id`, `model`, `width`, `height`, `params` (omit `get_distortion()` — not used by the viewer, and all exported cameras are PINHOLE with no distortion)
- `COLMAPImage` — dataclass with `image_id`, `qw-qz`, `tx-tz`, `camera_id`, `name`, `points2d`, plus `get_rotation()`, `get_translation()`, `get_camera_center()` methods
- `COLMAPPoint3D` — dataclass with `point3d_id`, `xyz`, `rgb`, `error`, `track`

Parser functions (lines 245-340 of colmap_validation.py):
- `parse_cameras_txt(path) -> Dict[int, COLMAPCamera]`
- `parse_images_txt(path) -> Dict[int, COLMAPImage]`
- `parse_points3d_txt(path) -> Dict[int, COLMAPPoint3D]`

Plus a convenience function:
```python
def parse_colmap_model(scene_dir: Path) -> Dict[str, Any]:
    """Parse a COLMAP scene directory into the format PointCloudViewer expects."""
    sparse_dir = scene_dir / "sparse" / "0"
    return {
        "cameras": parse_cameras_txt(sparse_dir / "cameras.txt"),
        "images": parse_images_txt(sparse_dir / "images.txt"),
        "points3D": parse_points3d_txt(sparse_dir / "points3D.txt"),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_colmap_text_parser.py -v
```

- [ ] **Step 5: Commit**

```bash
git add gui/colmap_text_parser.py tests/test_colmap_text_parser.py
git commit -m "feat: add COLMAP text format parser for scene viewer data model"
```

---

## Task 3: Adapted PointCloudViewer with per-source coloring

**Files:**
- Create: `gui/pointcloud_viewer.py`
- Create: `tests/test_pointcloud_viewer.py`

Copy and adapt the PointCloudViewer from `D:\Projects\reconstruction-zone\reconstruction_gui\pointcloud_viewer.py` (784 lines). Changes from the original:

1. Background color: `(0.0, 0.0, 0.0)` (pure black, not `(0.04, 0.04, 0.10)`)
2. `_build_cameras()` accepts an optional `color_map: Dict[str, Tuple[float,float,float]]` mapping image name prefixes to RGB colors
3. `_add_perspective_frustum()` takes a `color` parameter instead of using `self._CAMERA_COLOR`
4. Sphere camera method removed (all exported cameras are pinhole)

- [ ] **Step 1: Write availability/import tests**

In `tests/test_pointcloud_viewer.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def test_viewer_module_imports():
    """Module imports without error even if VTK is not installed."""
    import gui.pointcloud_viewer as pv
    assert hasattr(pv, "PointCloudViewer")

def test_available_returns_bool():
    """available() returns a bool regardless of VTK presence."""
    from gui.pointcloud_viewer import PointCloudViewer
    result = PointCloudViewer.available()
    assert isinstance(result, bool)

def test_color_palette_exists():
    """SOURCE_COLOR_PALETTE is exported and has at least 8 colors."""
    from gui.pointcloud_viewer import SOURCE_COLOR_PALETTE
    assert len(SOURCE_COLOR_PALETTE) >= 8
    # Each color is an (r, g, b) tuple with values 0.0-1.0
    for color in SOURCE_COLOR_PALETTE:
        assert len(color) == 3
        assert all(0.0 <= c <= 1.0 for c in color)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_pointcloud_viewer.py -v
```

- [ ] **Step 3: Copy and adapt the viewer**

Copy `D:\Projects\reconstruction-zone\reconstruction_gui\pointcloud_viewer.py` to `gui/pointcloud_viewer.py`. Make these changes:

**a) Change background to pure black:**
```python
_BACKGROUND = (0.0, 0.0, 0.0)
```

**b) Add the source color palette as a module-level constant:**
```python
SOURCE_COLOR_PALETTE = [
    (0.96, 0.62, 0.04),  # #f59e0b amber
    (0.02, 0.71, 0.83),  # #06b6d4 teal
    (0.66, 0.33, 0.97),  # #a855f7 purple
    (0.96, 0.25, 0.37),  # #f43f5e rose
    (0.29, 0.87, 0.50),  # #4ade80 green
    (0.22, 0.74, 0.97),  # #38bdf8 sky
    (0.98, 0.57, 0.24),  # #fb923c orange
    (0.96, 0.45, 0.71),  # #f472b6 pink
]
```

**c) Modify `_build_cameras()` to accept a color map:**
```python
def _build_cameras(self, images: dict, cameras: dict, color_map: dict = None):
    """Build camera visualization actors.
    
    color_map: optional dict mapping image name prefix -> (r, g, b) tuple.
    If provided, each frustum is colored by matching the image name against
    prefixes. If no match, uses default _CAMERA_COLOR.
    """
    for actor in self._camera_actors:
        self._renderer.RemoveActor(actor)
    self._camera_actors.clear()

    if not self._show_cameras:
        return

    for img in images.values():
        cam = cameras.get(img.camera_id)
        if cam is None:
            continue

        center = img.get_camera_center()
        R = img.get_rotation()
        
        color = self._CAMERA_COLOR
        if color_map:
            for prefix, c in color_map.items():
                if img.name.startswith(prefix):
                    color = c
                    break
        
        self._add_perspective_frustum(center, R, cam, color=color)
```

**d) Modify `_add_perspective_frustum()` to take a color parameter:**
```python
def _add_perspective_frustum(self, center, R, cam, color=None):
    if color is None:
        color = self._CAMERA_COLOR
    # ... rest unchanged, but use `color` instead of `self._CAMERA_COLOR`
    actor.GetProperty().SetColor(*color)
```

**e) Remove `_add_sphere_camera()` method** — all exported cameras are pinhole.

**f) Modify `load_model()` to accept optional `color_map`:**

Do NOT create a separate `load_model_with_colors()` — that duplicates logic. Instead, add `color_map` as an optional parameter to the existing `load_model()`:

```python
def load_model(self, model_data: Dict[str, Any], color_map: dict = None):
    """Load a parsed COLMAP sparse model.
    
    color_map: optional dict mapping image name prefix -> (r, g, b).
    """
    self.clear_model()
    self._model_data = model_data
    self._color_map = color_map  # store for rebuild paths
    # ... rest unchanged, but pass color_map to _build_cameras:
    if images and cameras:
        self._build_cameras(images, cameras, color_map=color_map)
```

**g) Update `toggle_cameras()` to use stored color map:**

The existing `toggle_cameras()` calls `self._build_cameras(images, cameras)` to rebuild frustums. It must pass the stored color map:

```python
def toggle_cameras(self, show: bool):
    self._show_cameras = show
    if self._model_data is not None:
        images = self._model_data.get("images", {})
        cameras = self._model_data.get("cameras", {})
        self._build_cameras(images, cameras, color_map=self._color_map)
        self._reapply_upright()
    self.render()
```

Without this fix, toggling cameras off and back on would lose per-source colors.

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_pointcloud_viewer.py -v
```

- [ ] **Step 5: Commit**

```bash
git add gui/pointcloud_viewer.py tests/test_pointcloud_viewer.py
git commit -m "feat: add PointCloudViewer with per-source frustum coloring"
```

---

## Task 4: Tabbed right panel for COLMAP mode

**Files:**
- Modify: `gui/gui.py`

Replace the right panel with a `CTkTabview` when in COLMAP mode. Two tabs: "Console" (full-height console + progress) and "Scene" (3D viewer or fallback message).

- [ ] **Step 1: Restructure `_build_right()` for mode-dependent content**

Currently `_build_right(parent)` builds a single fixed layout (gui.py:1639-1695). Modify it to check the purpose mode:

- **Metashape mode:** unchanged — phase label, progress bar, console (2/3), preview (1/3)
- **COLMAP mode:** `CTkTabview` with "Console" and "Scene" tabs
  - Console tab: phase label, progress bar, full-height console
  - Scene tab: if `PointCloudViewer.available()`, embed the viewer frame. Otherwise, show "Install pyvista for 3D scene preview" message with install instructions.

The right panel needs to rebuild when the purpose toggle changes. Add a `_rebuild_right_panel()` method called from `_on_purpose_changed()`.

- [ ] **Step 2: Build the Console tab content**

The Console tab contains the same widgets as the current right panel's top section:
- Phase label (`self._phase_label`)
- Progress bar (`self._progress`)
- Console textbox (`self._console`)

These widget references must stay valid regardless of which tab is active, because the subprocess output writes to them during export.

- [ ] **Step 3: Build the Scene tab content**

The Scene tab contains:
- A `tk.Frame` container for the VTK viewer (needed because VTK embeds via HWND on a raw tkinter Frame, not a CTkFrame)
- Controls bar below the viewer:
  - Color mode dropdown: `CTkOptionMenu` with values from `PointCloudViewer.COLOR_MODES` ("rgb", "reproj_error", "track_length", "depth", "elevation")
  - Point size slider: `CTkSlider` range 1-10, default 3
  - Cameras toggle: `CTkCheckBox` "Cameras", default checked
  - Set Upright button: `CTkButton`
  - Reset button: `CTkButton`
- Status bar at the bottom:
  - Camera/point count label (e.g., "475 cameras · 12,341 points")
  - Dynamic color legend: colored dots + source names, built from the manifest's body names and frame sensor labels

Wire up each control to the viewer's public methods:
- Color dropdown → `viewer.set_color_mode(mode)`
- Point size slider → `viewer.set_point_size(value)`
- Cameras checkbox → `viewer.toggle_cameras(show)`
- Set Upright → `viewer.set_upright()`
- Reset → `viewer.reset_camera()`

- [ ] **Step 4: Handle viewer lifecycle**

The viewer must be created lazily (only when Scene tab is first shown) and destroyed when the GUI closes:
- `_create_viewer()` — called when Scene tab first activated and VTK is available
- `viewer.pause_pump()` — called when switching to Console tab (saves CPU)
- `viewer.resume_pump()` — called when switching back to Scene tab
- `viewer.destroy()` — called from the GUI's window close handler

Add tab change callback via `CTkTabview`'s `configure(command=...)` to manage pump start/stop.

- [ ] **Step 5: Add GUI smoke test update**

Update `tests/test_gui_smoke.py` to verify the updated gui.py still parses:

```bash
python -m pytest tests/test_gui_smoke.py -v
```

- [ ] **Step 6: Manual verification**

Launch GUI, switch to COLMAP mode:
- Verify Console/Scene tabs appear in right panel
- If pyvista installed: Scene tab shows black viewer area with controls
- If pyvista not installed: Scene tab shows install prompt
- Console tab shows normal console output
- Switch between tabs — no crashes

- [ ] **Step 7: Commit**

```bash
git add gui/gui.py
git commit -m "feat: add tabbed right panel with Console/Scene tabs for COLMAP mode"
```

---

## Task 5: Auto-load scene after export + color map

**Files:**
- Modify: `gui/gui.py`

After a COLMAP export completes, automatically parse the exported scene and load it into the viewer with per-source frustum colors.

- [ ] **Step 1: Build the source color map from the manifest**

When the export finishes, the GUI knows the manifest (bodies + frame sensors). Build a `color_map` dict that maps image name prefixes to colors:

```python
def _build_frustum_color_map(self) -> dict:
    """Map image name prefixes to colors based on configured bodies/sensors."""
    color_map = {}
    palette_idx = 0
    
    # Bodies: cubeface images are named like cam1_front_0001_dir_plusZ.png
    # The prefix is the sensor's camera label prefix
    for body in self._colmap_bodies:
        for sensor in body["sensors"]:
            prefix = sensor.get("prefix", "")
            if prefix and palette_idx < len(SOURCE_COLOR_PALETTE):
                color_map[prefix] = SOURCE_COLOR_PALETTE[palette_idx]
        palette_idx += 1  # one color per body, not per sensor
    
    # Frame sensors: images keep their original names (DJI_0001.png, IMG_4231.png)
    for frame_sensor in self._colmap_frame_sensors:
        prefix = frame_sensor.get("prefix", "")
        if prefix and palette_idx < len(SOURCE_COLOR_PALETTE):
            color_map[prefix] = SOURCE_COLOR_PALETTE[palette_idx]
        palette_idx += 1
    
    return color_map
```

- [ ] **Step 2: Auto-load after export completion**

In the export completion handler (where `_phase_label` is set to "Done"), add:

```python
if self._is_colmap_purpose() and hasattr(self, '_viewer') and self._viewer is not None:
    scene_dir = self._colmap_scene_output_dir()
    if scene_dir and (scene_dir / "sparse" / "0" / "cameras.txt").is_file():
        from gui.colmap_text_parser import parse_colmap_model
        model = parse_colmap_model(scene_dir)
        color_map = self._build_frustum_color_map()
        self._viewer.load_model(model, color_map=color_map)
        self._update_scene_status(model)
        # Auto-switch to Scene tab
        self._right_tabview.set("Scene")
```

- [ ] **Step 3: Update the status bar and legend**

`_update_scene_status(model)` updates the status bar with:
- Camera count: `len(model["images"])`
- Point count: `len(model["points3D"])`
- Dynamic legend: one colored dot + label per source (body name or frame sensor label)

The legend is built by iterating the manifest bodies and frame sensors in order, paired with the same palette colors used for the color map.

- [ ] **Step 4: Manual verification**

1. Configure a COLMAP export with `dev/equisolid_and_pinhole_dataset/cameras.xml`
2. Run the export
3. Verify: Scene tab auto-activates, point cloud and frustums appear, colors match sources, legend is correct, controls work (orbit, color mode, point size, cameras toggle)

- [ ] **Step 5: Commit**

```bash
git add gui/gui.py
git commit -m "feat: auto-load exported scene into 3D viewer with per-source frustum colors"
```

---

## Task 6: Update requirements + graceful fallback

**Files:**
- Modify: `gui/requirements.txt`

- [ ] **Step 1: Add pyvista as optional dependency**

In `gui/requirements.txt`, add:

```
# Optional: 3D scene viewer (Phase 2)
# pyvista
```

Note: commented out by default. The viewer is optional — the GUI works without it. Users who want the 3D viewer run `pip install pyvista` separately.

- [ ] **Step 2: Verify graceful fallback without pyvista**

If pyvista is not installed:
1. `PointCloudViewer.available()` returns `False`
2. Scene tab shows a message: "3D scene preview requires pyvista. Install with: pip install pyvista"
3. No import errors, no crashes
4. Console tab works normally
5. Export works normally

This should already work from Task 3 and 4's implementation, but verify explicitly.

- [ ] **Step 3: Commit**

```bash
git add gui/requirements.txt
git commit -m "docs: add pyvista as optional dependency for 3D scene viewer"
```

---

## Task 7: Integration verification

**Files:**
- No new files

Full end-to-end verification of the Phase 2 feature.

- [ ] **Step 1: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all PASS (Phase 1 tests + Phase 2 tests)

- [ ] **Step 2: Manual integration test**

Full workflow verification:
1. Launch GUI
2. Switch to COLMAP mode
3. Load a cameras.xml (e.g., `dev/equisolid_and_pinhole_dataset/cameras.xml`)
4. Configure at least one fisheye sensor with calibration XML, images, mask
5. Set output directory
6. Click Export
7. Watch Console tab during export — progress output appears
8. After export completes:
   - Scene tab auto-activates
   - Point cloud visible against black background
   - Camera frustums visible with distinct colors per source
   - Color legend in status bar matches source names
   - Camera/point counts shown
9. Test controls:
   - Orbit (mouse drag)
   - Zoom (scroll)
   - Color mode dropdown (switch to "reproj_error", verify color change)
   - Point size slider (drag, verify size change)
   - Cameras checkbox (uncheck, frustums disappear; re-check, they reappear)
   - Set Upright button (click, verify gizmo axes align)
   - Reset button (click, verify view resets)
10. Switch to Console tab — console log still there
11. Switch back to Scene tab — viewer still shows scene (pump resumed)
12. Switch purpose to Metashape — right panel reverts to old layout (no tabs)
13. Switch back to COLMAP — tabs reappear, viewer shows previous scene

- [ ] **Step 3: Verify Metashape mode unaffected**

1. Switch to Metashape mode
2. Configure lens, run cubeface generation
3. Preview shows cubeface thumbnails as before
4. No regressions

- [ ] **Step 4: Commit any fixes found during testing**

```bash
git add -u
git commit -m "fix: integration fixes from Phase 2 end-to-end testing"
```
