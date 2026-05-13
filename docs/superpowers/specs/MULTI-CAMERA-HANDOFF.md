# Multi-Camera COLMAP Export — Handoff Document

## What This Is

A complete guide for implementing the multi-camera COLMAP export feature across two phases. This document ties together the design spec, implementation plans, and execution instructions so a new session can pick up the work with no prior context.

## Documents

| Document | Path | Status |
|---|---|---|
| Design spec | `docs/superpowers/specs/2026-05-12-multi-camera-colmap-export-design.md` | Complete, reviewed, committed |
| Phase 1 plan | `docs/superpowers/plans/2026-05-12-multi-camera-colmap-phase1.md` | Complete, reviewed, verified, committed |
| Phase 2 plan | `docs/superpowers/plans/2026-05-12-multi-camera-colmap-phase2.md` | Complete, reviewed, verified, committed |
| Visual mockups | `plans/planning.pen` | Complete (open with Pencil desktop app) |

## Current State

- **Branch:** `colmap-conversion`
- **Phase 1:** Executed. Sensor discovery, body grouping, scene manifest, GUI restructure with purpose toggle, draggable divider, COLMAP left panel, prefs persistence — all implemented.
- **Phase 2:** Not yet executed. 3D scene viewer integration is next.

## What the Feature Does

Replaces the hardcoded Lens A/B COLMAP export workflow with a `cameras.xml`-driven sensor discovery system:

1. User loads a Metashape `cameras.xml` → GUI discovers all sensors automatically
2. Equisolid fisheye sensors are auto-grouped into camera bodies by filename prefix heuristics
3. Each sensor gets its own configuration card (calibration XML, images, mask)
4. Frame/pinhole sensors become passthrough cards (image directory + filename validation)
5. A JSON scene manifest drives the exporter (replaces the old manual lens-to-camera mapping)
6. (Phase 2) Post-export, a 3D point cloud + camera frustum viewer loads the scene with per-source coloring

## Phase 1 Summary (12 tasks — DONE)

| Task | What | Key files |
|---|---|---|
| 1 | Synthetic multi-camera XML fixture | `tests/fixtures/dual_x5_cameras.xml` |
| 2 | Sensor discovery module | `gui/sensor_discovery.py`, `gui/__init__.py` |
| 3 | Body grouping algorithm | `gui/sensor_discovery.py` (`auto_group_into_bodies`) |
| 4 | Scene manifest data model | `gui/scene_manifest.py` |
| 5 | Frame sensor filename matching | `gui/sensor_discovery.py` (`match_frame_sensor_images`) |
| 6 | Cubeface `process_sensor()` API | `AM_ImageAndMask_to_cubemap_v4.py` |
| 7 | Exporter `--scene-manifest` | `metashape_cameras_to_colmap.py` |
| 8 | GUI restructure (purpose toggle, divider) | `gui/gui.py` |
| 9 | COLMAP left panel (sensor cards) | `gui/gui.py` |
| 10 | Preference persistence | `gui/gui.py` |
| 11 | Old UI cleanup | `gui/gui.py` |
| 12 | Integration testing | `tests/test_sensor_discovery.py` |

## Phase 2 Summary (7 tasks — TODO)

| Task | What | Key files |
|---|---|---|
| 1 | COLMAP text format fixtures | `tests/fixtures/colmap_scene/sparse/0/*.txt` |
| 2 | COLMAP text parser | `gui/colmap_text_parser.py` |
| 3 | Adapted PointCloudViewer | `gui/pointcloud_viewer.py` |
| 4 | Tabbed right panel (Console/Scene) | `gui/gui.py` |
| 5 | Auto-load after export + color map | `gui/gui.py` |
| 6 | Requirements + graceful fallback | `gui/requirements.txt` |
| 7 | Integration verification | manual testing |

## Phase 2 Execution Instructions

### Prompt for new session

```
Execute the implementation plan at docs/superpowers/plans/2026-05-12-multi-camera-colmap-phase2.md, task by task, inline (no subagents). The design spec is at docs/superpowers/specs/2026-05-12-multi-camera-colmap-export-design.md. The 3D viewer source to adapt is at D:\Projects\reconstruction-zone\reconstruction_gui\pointcloud_viewer.py and the COLMAP text parsers to adapt are at D:\Projects\reconstruction-zone\reconstruction_gui\colmap_validation.py. Start at Task 1. Read the plan and both source files before writing any code. Pause after each task for my review.
```

### Prerequisites

Phase 1 must be complete. Verify:

```bash
cd d:\Projects\Fisheye-to-Cubemap
git branch  # should be on colmap-conversion
python -m pytest tests/ -v  # all tests pass
python -c "from gui.sensor_discovery import discover_sensors, auto_group_into_bodies; print('OK')"
python -c "from gui.scene_manifest import SceneManifest; print('OK')"
```

### Dependencies for Phase 2

The 3D viewer requires VTK via pyvista. Install before starting Phase 2:

```bash
pip install pyvista
```

Verify:

```bash
python -c "import pyvista; print(pyvista.__version__)"
```

If pyvista is NOT installed, Phase 2 still works — the Scene tab shows a fallback "install pyvista" message instead of the 3D viewer. All other functionality (Console tab, export, etc.) is unaffected.

### Source files to adapt (DO NOT MODIFY originals)

These live in a sibling project and are copied/adapted, not imported:

- **Viewer:** `D:\Projects\reconstruction-zone\reconstruction_gui\pointcloud_viewer.py` (784 lines)
  - VTK/pyvista 3D viewer embedded in tkinter via HWND
  - Key public API: `load_model()`, `set_color_mode()`, `set_point_size()`, `toggle_cameras()`, `set_upright()`, `reset_camera()`, `pause_pump()`, `resume_pump()`, `destroy()`
  - `COLOR_MODES = ["rgb", "reproj_error", "track_length", "depth", "elevation"]`

- **Parsers:** `D:\Projects\reconstruction-zone\reconstruction_gui\colmap_validation.py`
  - Data classes: `COLMAPCamera`, `COLMAPImage`, `COLMAPPoint3D`
  - Parser functions: `parse_cameras_txt()`, `parse_images_txt()`, `parse_points3d_txt()`
  - Copy only data classes + parsers. Omit `get_distortion()`, `GeometricValidator`, and anything using cv2.

### Key implementation details from verification

These were discovered during the verification pass and are noted in the plan, but worth highlighting:

1. **`self._color_map = None` must be initialized in viewer `__init__`** — otherwise `toggle_cameras()` throws AttributeError before first `load_model()` call.

2. **`self._colmap_frame_sensors_data` does not exist after Phase 1** — Task 5 Step 0 adds it. The discovery result's `result["frame"]` list is used to build cards but not stored on self. Must be stored for the color map builder.

3. **`toggle_cameras()` must pass `self._color_map`** — the original viewer rebuilds frustums with a fixed color. The adapted version must pass the stored color map on rebuild, or toggling cameras off/on loses per-source colors.

4. **`load_model()` gets an optional `color_map` parameter** — do NOT create a separate `load_model_with_colors()`. One method, optional parameter.

5. **VTK embeds via `SetParentInfo(hwnd)` on a raw `tk.Frame`** — this must be a plain tkinter Frame, not a CTkFrame. The Scene tab should contain a `tk.Frame` child for the viewer.

6. **`CTkTabview` accepts `command=` for tab change callbacks** — use this to pause/resume the VTK pump.

### GUI structure after Phase 2

```
┌─────────────────────────────────────────────────────────┐
│  Purpose: [ Metashape alignment ▎ COLMAP export ]       │
├────────────────────────┬──┬─────────────────────────────┤
│ Left Panel (scrollable)│▐▐│ Right Panel                 │
│                        │▐▐│                             │
│ COLMAP Export          │▐▐│ ┌─Console─┬──Scene──┐       │
│  cameras.xml  [...]    │▐▐│ │                   │       │
│  sparse.ply   [...]    │▐▐│ │  (active tab      │       │
│  output dir   [...]    │▐▐│ │   content here)    │       │
│  ✓ 4 fisheye, 2 frame  │▐▐│ │                   │       │
│                        │▐▐│ │                   │       │
│ Fisheye Sensors        │▐▐│ └───────────────────┘       │
│ ┌─ Body 1 ──────────┐ │▐▐│ ┌─controls bar──────┐       │
│ │ S0 │ S1           │ │▐▐│ │Color▾ PtSize Cams │       │
│ └────────────────────┘ │▐▐│ └───────────────────┘       │
│ Frame Sensors          │▐▐│ ┌─status bar────────┐       │
│ ┌─ S4: DJI ──────────┐│▐▐│ │475 cams · 12k pts │       │
│ └────────────────────┘ │▐▐│ │● Body1 ● Body2 ● F│      │
│ Options / Export       │▐▐│ └───────────────────┘       │
└────────────────────────┴──┴─────────────────────────────┘
```

### Color palette (per-source, cycling)

| Index | Color | Hex | Use |
|---|---|---|---|
| 0 | Amber | `#f59e0b` | Body 1 |
| 1 | Teal | `#06b6d4` | Body 2 |
| 2 | Purple | `#a855f7` | Body 3 |
| 3 | Rose | `#f43f5e` | Body 4 |
| 4 | Green | `#4ade80` | Frame sensor 1 |
| 5 | Sky | `#38bdf8` | Frame sensor 2 |
| 6 | Orange | `#fb923c` | Frame sensor 3 |
| 7 | Pink | `#f472b6` | Frame sensor 4 |

Bodies and frame sensors pull from the same palette in left-panel order. Cycles if >8 sources.

### Test commands

```bash
# Run all tests (Phase 1 + Phase 2)
python -m pytest tests/ -v

# Run Phase 2 tests only
python -m pytest tests/test_colmap_text_parser.py tests/test_pointcloud_viewer.py -v

# Verify pyvista available
python -c "from gui.pointcloud_viewer import PointCloudViewer; print(PointCloudViewer.available())"

# Launch GUI for manual testing
cd gui && python gui.py
```
