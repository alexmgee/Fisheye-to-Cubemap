# Multi-Camera COLMAP Export — Design Spec

## Problem

The COLMAP export workflow currently supports one 360 camera (2 fisheye lenses: Lens A + Lens B) plus optional passthrough frame-camera media. A collaborator's rig uses two Insta360 X5 cameras, producing 4 fisheye lenses. The GUI has hardcoded Lens A/B slots and cannot accommodate arbitrary sensor counts. The manual lens-to-camera mapping step is also fragile and confusing.

## Goal

Replace the hardcoded lens configuration with a `cameras.xml`-driven sensor discovery workflow that supports any number of 360 cameras and frame sensors in a single scene, while keeping the Metashape alignment workflow unchanged.

## Design Decisions (from brainstorming)

These decisions were reached through iterative design with visual prototyping:

1. **cameras.xml-driven discovery** — parse sensors from the XML rather than requiring the user to declare lenses upfront. Eliminates the manual lens-to-camera mapping step entirely.
2. **Camera body grouping** — fisheye sensors are grouped into bodies with shared output width. Auto-suggested by filename prefix heuristics, user can override via editable body name and dropdown.
3. **Per-sensor calibration** — each fisheye sensor gets its own calibration XML, image directory, and mask. No assumption that lenses share calibration.
4. **Frame sensor auto-detection** — frame/pinhole sensors from the XML become passthrough cards. User provides an image directory; the GUI validates by matching filenames against camera labels from the XML.
5. **Purpose toggle at top** — the Metashape/COLMAP purpose selector moves to the top of the GUI. Each mode shows a completely different form (no shared fields pulling double duty).
6. **1:1 column ratio** — left and right panels have equal width (changed from the current 1:2).
7. **Draggable divider** — a custom drag-handle approach (two `CTkFrame` panels + grip frame with `<B1-Motion>` binding) replaces the fixed grid layout, allowing the user to widen the right panel while staying within CustomTkinter's theming system.
8. **Tabbed right panel for COLMAP mode** — "Console" and "Scene" tabs. Console shows export progress. Scene tab (phase 2) embeds the VTK point cloud viewer from reconstruction-zone.
9. **Per-source frustum coloring** — the 3D viewer colors camera frustums by source (body or frame sensor) using a rotating palette, with a dynamic legend.

## Architecture

### GUI Layout

```
┌─────────────────────────────────────────────────────┐
│  Purpose: [ Metashape alignment ▎ COLMAP export ]   │
├───────────────────────┬─┬───────────────────────────┤
│                       │▐│                           │
│  Left Panel           │▐│  Right Panel              │
│  (scrollable config)  │▐│  (console / scene viewer) │
│                       │▐│                           │
│                       │▐│  ▐ = draggable divider    │
└───────────────────────┴─┴───────────────────────────┘
```

### Left Panel — Metashape Mode (unchanged)

- Lens A/B tabs with dual-lens checkbox
- Per-lens: calibration XML, images directory, mask, FOV override
- Shared settings: output folder, face width, format (png/tiff/jpg)
- Checkboxes: force reprocess, skip cubeface generation
- Structure: station / rig radio buttons
- Run / Cancel buttons

### Left Panel — COLMAP Mode (new)

```
Purpose toggle
├── COLMAP Export heading
│   ├── cameras.xml file picker
│   ├── Sparse .ply file picker
│   ├── Output directory picker
│   └── Discovery status bar ("4 fisheye, 2 frame sensors detected")
├── Fisheye Sensors section
│   ├── Body 1 (blue-bordered group)
│   │   ├── Header: body name (editable), output width (shared)
│   │   ├── Sensor 0 card: metadata, cal XML, images, mask
│   │   └── Sensor 1 card: metadata, cal XML, images, mask
│   └── Body 2 (same structure)
├── Frame Sensors (Passthrough) section
│   ├── Sensor 4 card: metadata, images dir, match status
│   └── Sensor 5 card: metadata, images dir, match status
├── Options section
│   ├── Pose convention dropdown
│   ├── Require masks, Projected tracks, Force assets
│   ├── Normalize scene scale, Keep processing files
└── Export / Cancel buttons
```

### Right Panel — Metashape Mode (unchanged)

- Phase label + progress bar
- Console output (2/3 height)
- Preview area (1/3 height): cubeface thumbnails, mask views, run summary

### Right Panel — COLMAP Mode

**Phase 1 (ship first — no new dependencies):**
- Phase label + progress bar
- Console output (2/3 height)
- Preview area (1/3 height): text diagnostics (directory checks, scale diagnostics, validation/conversion reports), preview dropdown with sensor selector

**Phase 2 (ship second — requires VTK/pyvista):**
- Tabbed: "Console" tab + "Scene" tab
- Console tab: full-height phase label + progress bar + console
- Scene tab: full-height 3D viewer with:
  - Sparse point cloud (color modes: RGB, reproj error, track length, depth, elevation)
  - Pinhole camera frustums colored per source (rotating palette)
  - Controls bar: color mode dropdown, point size slider, cameras toggle, Set Upright, Reset
  - Status bar: camera/point counts, dynamic color legend per source
- Post-export: Scene tab auto-activates, loads exported model
- Graceful fallback: if pyvista not installed, Scene tab hidden or shows install prompt

## Data Model

### Scene (parsed from cameras.xml)

```
Scene
├── xml_path: Path
├── ply_path: Path
├── output_dir: Path
├── bodies: list[Body]
├── frame_sensors: list[FrameSensor]
└── options: ExportOptions

Body
├── name: str (editable, auto-suggested from prefix)
├── output_width: int (shared across sensors in body)
└── sensors: list[FisheyeSensor]

FisheyeSensor
├── sensor_id: int (from XML)
├── label: str (from XML, read-only)
├── camera_count: int (from XML, read-only)
├── prefix: str (auto-detected from camera labels, read-only)
├── calibration_xml: Path (user provides)
├── image_dir: Path (user provides)
└── mask: Path (user provides — directory or single file)

FrameSensor
├── sensor_id: int (from XML)
├── label: str (from XML, read-only)
├── camera_count: int (from XML, read-only)
├── prefix: str (auto-detected, read-only)
├── camera_labels: list[str] (from XML, for validation)
├── image_dir: Path (user provides)
└── match_count: int (computed: how many labels found in image_dir)

ExportOptions
├── pose_convention: str
├── require_masks: bool
├── projected_tracks: bool
├── force_assets: bool
├── normalize_scene: bool
└── keep_processing_files: bool
```

### Body Grouping Heuristics

1. For each equisolid fisheye sensor, extract camera label prefixes (strip trailing digits)
2. Group sensors whose prefixes share a common root (e.g., `cam1_front_` and `cam1_back_` share `cam1_`)
3. Auto-assign body names from the shared root
4. User can override: rename bodies, reassign sensors via dropdown

### Camera Frustum Color Palette

Colors assigned by source order, cycling through:
`#f59e0b` (amber), `#06b6d4` (teal), `#a855f7` (purple), `#f43f5e` (rose), `#4ade80` (green), `#38bdf8` (sky), `#fb923c` (orange), `#f472b6` (pink)

Bodies and frame sensors pull from the same palette in left-panel order.

## Exporter Interface Contract

### Current interface (CLI, invoked via subprocess)

The GUI currently shells out to `metashape_cameras_to_colmap.py` via `subprocess.Popen` (gui.py line ~2270). Key arguments: `--metashape-xml`, `--cubeface-root`, `--lens-camera-map`, `--passthrough-media-manifest`, etc.

### New interface (JSON manifest, still subprocess)

The subprocess pattern stays — it keeps the GUI responsive and isolates crashes. The `--lens-camera-map` string and `--passthrough-media-manifest` JSON are replaced by a single `--scene-manifest` JSON file that encodes the full scene configuration:

```json
{
  "cameras_xml": "D:\\Captures\\dual_x5\\cameras.xml",
  "sparse_ply": "D:\\Captures\\dual_x5\\pointcloud.ply",
  "output_dir": "D:\\Captures\\dual_x5\\colmap_export",
  "bodies": [
    {
      "name": "X5 Camera 1",
      "output_width": 2048,
      "sensors": [
        {
          "sensor_id": 0,
          "calibration_xml": "D:\\Cals\\x5_front_adj.xml",
          "image_dir": "D:\\Captures\\cam1\\front",
          "mask": "D:\\Masks\\front_mask.png"
        },
        {
          "sensor_id": 1,
          "calibration_xml": "D:\\Cals\\x5_back_adj.xml",
          "image_dir": "D:\\Captures\\cam1\\back",
          "mask": "D:\\Masks\\back_mask.png"
        }
      ]
    }
  ],
  "frame_sensors": [
    {
      "sensor_id": 4,
      "image_dir": "D:\\Captures\\drone_shots"
    }
  ],
  "options": {
    "pose_convention": "metashape_camera_to_world",
    "require_masks": false,
    "projected_tracks": true,
    "force_assets": false,
    "normalize_scene": false,
    "keep_processing_files": true
  }
}
```

The exporter reads this manifest, runs cubeface generation internally for each fisheye sensor (using the existing `AM_ImageAndMask_to_cubemap_v4.py` as a library import, not a subprocess), and produces the COLMAP output. The old `--lens-camera-map` and `--passthrough-media-manifest` arguments are deprecated (kept for backward compatibility with any existing CLI scripts, but not used by the new GUI).

### Cubeface generation pipeline in COLMAP mode

The exporter handles cubeface generation internally — the GUI does not queue separate cubeface runs. For each fisheye sensor in the manifest, the exporter:

1. Calls the cubeface converter with the sensor's calibration XML, image directory, mask, and the body's `output_width` (this is the same as "face width" — they are the same parameter, renamed for clarity in the multi-body context)
2. Writes cubefaces to a temp/processing directory
3. Composes cubeface poses from the fisheye poses in the XML
4. Packages into the final COLMAP output

This means COLMAP mode does NOT show face width, format, or "skip cubeface generation" controls — those are Metashape-mode-only settings. The body's `output_width` is the only cubeface dimension control, and output format is always PNG (the training-ready format).

### Cubeface converter library API

`AM_ImageAndMask_to_cubemap_v4.py`'s `main()` is a ~650-line monolith that handles argument parsing, directory setup, progress reporting, and the full processing loop. It cannot be called directly as a library. The exporter needs a callable entry point.

**Approach:** refactor `main()` to extract a `process_sensor()` function with this signature:

```python
def process_sensor(
    calibration_xml: Path,
    image_dir: Path,
    mask: Path | None,        # single file or directory
    output_dir: Path,
    face_width: int,
    output_format: str = "png",
    force: bool = False,
    cache_remapping: bool = True,
    progress_callback: Callable[[str], None] | None = None,
) -> dict:
    """Process all images for one fisheye sensor, producing cubefaces + masks.
    
    Returns dict with keys: processed_count, skipped_count, output_paths.
    """
```

`main()` becomes a thin wrapper that parses CLI args and calls `process_sensor()`. The exporter calls `process_sensor()` directly for each fisheye sensor. The `progress_callback` replaces stdout printing so the GUI can route progress to its console. This refactor is scoped narrowly — the internal functions (`compute_image2cubeface_remapping_cached`, `remap_image`, etc.) are unchanged.

## What the User Still Manually Provides

XML discovery identifies which sensors exist, how many cameras each has, what type they are (equisolid vs frame), and what the camera labels are. The user still provides:

- **Per fisheye sensor:** calibration XML (adjusted lens calibration from Metashape), image directory (raw fisheye images), mask (lens mask file or directory)
- **Per frame sensor:** image directory (the GUI validates filenames against XML camera labels)
- **Per body:** output width (cubeface pixel width, default 2048)
- **Global:** sparse .ply path, output directory, export options

The improvement over the current workflow: the user no longer needs to manually construct a lens-to-camera mapping string or manually add passthrough media sets. Sensor existence, type, and camera assignments come from the XML.

## Frame Sensor Filename Matching

When the user sets an image directory for a frame sensor, the GUI scans the directory and compares filenames against the camera labels from `cameras.xml`:

- **Match criteria:** filename stem (without extension) equals camera label stem. Case-sensitive. Extensions stripped from both sides.
- **Full match:** match_count == camera_count → green "45/45 matched"
- **Partial match:** match_count < camera_count → amber "38/45 matched — 7 missing" (warning, not a blocker — missing cameras are excluded from export)
- **No directory set:** amber "No image directory set" (sensor is skipped entirely)
- **Extra files:** files in the directory that don't match any label are silently ignored

## Error and Empty States

- **XML has zero equisolid sensors:** Fisheye Sensors section shows "No fisheye sensors found in cameras.xml." Body grouping UI is hidden. Frame sensors still shown if present.
- **XML has sensors with zero aligned cameras:** sensor card shows "0 cameras (none aligned)" in amber. Sensor is skippable.
- **XML is malformed / not Metashape export:** error message in the discovery status bar: "Failed to parse cameras.xml: [error details]". No sensors shown, Export button stays disabled.
- **All sensors unconfigured:** Export button stays disabled until at least one fisheye sensor has all three fields (cal XML, images, mask) filled.

## Preference Persistence

The current prefs system stores flat key-value pairs. With dynamic sensor counts, the COLMAP prefs key shifts to storing the manifest JSON directly:

```json
{
  "colmap_last_manifest": {
    "cameras_xml": "...",
    "bodies": [...],
    "frame_sensors": [...],
    ...
  }
}
```

On GUI load: if `colmap_last_manifest` exists and the `cameras_xml` path still points to a valid file, restore the configuration. If the XML has changed (detected by comparing the set of `sensor_id` values from the stored manifest against the freshly parsed XML), show the new sensors but clear the per-sensor fields. Old Lens A/B prefs are preserved for Metashape mode — no migration needed, the two modes use independent prefs keys.

## Draggable Divider Implementation

`tk.PanedWindow` is a raw tkinter widget and does not respect CustomTkinter theming. Two options:

1. **Wrap in themed frame:** place the `PanedWindow` inside a `CTkFrame` and style the sash area manually with a narrow `CTkFrame` grip handle (matching `COLOR_BG`). The sash itself is functional but visually overridden.
2. **Custom drag handler:** use two `CTkFrame` panels with a thin `CTkFrame` grip between them. Bind `<B1-Motion>` on the grip to resize the panels manually. More code but fully themed.

Option 2 is recommended — it keeps the entire GUI within CustomTkinter's styling system and avoids the visual mismatch. The grip frame shows a subtle dot pattern (as shown in the V3 Final mockup).

## Body Grouping Algorithm

Note: the run detection functions (`_metashape_equisolid_camera_runs`, `_merge_fragmented_runs`) currently live in `gui.py` (not `mapping_resolver.py`). They need to be moved to `mapping_resolver.py` as part of this work, alongside the existing `_split_label()` function.

Algorithm:

1. Call `_metashape_equisolid_camera_runs()` to identify runs (consecutive camera groups per sensor, with detected prefix and group label)
2. Call `_merge_fragmented_runs()` to combine split runs
3. **New step — `auto_group_into_bodies()`:** for each pair of runs, extract the prefix string from `_split_label()` (which returns `(prefix, number)`). Compute the longest common prefix of the prefix strings. Two runs whose prefixes share a common prefix up to the last `_` or `-` delimiter (e.g., `cam1_front` and `cam1_back` share `cam1_`) are proposed as the same body.
4. Concretely: strip the last `_`-delimited segment from each prefix. Runs with identical stripped prefixes are grouped. `cam1_front_` and `cam1_back_` both strip to `cam1` → same body. `cam2_front_` strips to `cam2` → different body.
5. Runs with no common prefix with any other run become single-sensor bodies.
6. The body name is derived from the shared stripped prefix (e.g., `cam1` → "cam1").
7. Body grouping is purely a UI convenience + shared output width. It does not affect the exporter's behavior — the exporter processes each sensor independently regardless of body assignment.

## Changes to Existing Code

### gui.py

- Move purpose toggle to top of `_build_left()`
- Metashape mode: current `_build_left()` content (lens config, shared settings) wrapped in a conditional frame, shown only when purpose == Metashape
- COLMAP mode: new `_build_colmap_left()` method builds the XML-driven form. No face width, format, or station/rig controls — those are Metashape-only
- Replace fixed `grid` layout with custom drag-handle approach (two `CTkFrame` panels + grip `CTkFrame` with `<B1-Motion>` binding) for draggable divider
- Change default column proportions from 1:2 to 1:1
- COLMAP right panel: add `CTkTabview` with "Console" and "Scene" tabs (phase 2)
- Remove from COLMAP mode: manual lens-to-camera mapping field, "Check Mapping" / "Use Proposed Map" buttons, "Add Frame Camera Media Set" button
- Preview lens selector becomes sensor/source selector in COLMAP mode
- Prefs persistence: store `colmap_last_manifest` JSON blob alongside existing flat keys

### metashape_cameras_to_colmap.py

- New `--scene-manifest` argument: path to JSON manifest file (replaces `--lens-camera-map` and `--passthrough-media-manifest`)
- Parse manifest to get N fisheye sensor configs with per-sensor calibration XML, image dir, mask, and per-body output width
- Internal cubeface generation: import cubeface converter as library, run per sensor (no subprocess)
- Frame sensor handling: read image directories from manifest, validate against XML camera labels, package into COLMAP output
- Old `--lens-camera-map` and `--passthrough-media-manifest` arguments deprecated (kept for backward compatibility but not used by new GUI)

### mapping_resolver.py

- Body grouping heuristics extracted from existing `_metashape_equisolid_camera_runs()` and `_merge_fragmented_runs()`
- New function: `auto_group_into_bodies(runs) -> list[Body]` using `_split_label()` tokenizer for prefix similarity
- Existing run detection and mapping validation logic reused internally (user no longer interacts with it directly)

### New: pointcloud_viewer.py integration (phase 2)

- Copy/adapt `PointCloudViewer` from reconstruction-zone
- Wire into Scene tab: auto-load after export, parse COLMAP text model
- Add per-source frustum coloring (extend `_build_cameras()` to accept color map)
- Viewer background: pure black `(0.0, 0.0, 0.0)`

## Implementation Phases

### Phase 1: Core GUI restructuring + multi-sensor export

- Purpose toggle at top, mode-dependent left panel
- XML sensor discovery and display
- Body grouping with shared output width
- Frame sensor cards with image directory + label matching
- Wire up to exporter with N-sensor support
- Right panel: console + text preview (no viewer)
- 1:1 column ratio, draggable divider

### Phase 2: 3D scene viewer

- Integrate PointCloudViewer into Scene tab
- Per-source frustum coloring with palette
- Dynamic legend
- Auto-load post-export
- Graceful fallback when VTK not available

## What Does NOT Change

- `AM_ImageAndMask_to_cubemap_v4.py` — cubeface conversion math is untouched
- Metashape alignment workflow — entirely unchanged
- Right panel console/progress during export — same behavior
- COLMAP output format — same `cameras.txt`, `images.txt`, `points3D.txt` structure
- Cubeface face basis rotations, RBF interpolation, mask fallback chain

## Visual Reference

All mockup iterations are in `plans/planning.pen` (Pencil design file). Key screens:
- Screen A/B: Metashape mode (single/dual lens)
- Screen C: COLMAP empty state
- Screen D: COLMAP configured state
- V3a: Full layout without 3D viewer
- V3b/V3 Final: Full layout with tabbed 3D viewer, draggable divider, per-source frustum colors
