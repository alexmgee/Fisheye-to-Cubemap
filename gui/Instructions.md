# Fisheye-to-Cubemap GUI v4 Instructions

This guide explains the current GUI fields and the two supported output purposes.

## Core Idea

The GUI has one important mode switch in **Shared Settings**:

```text
Output purpose:
  Metashape alignment
  COLMAP export
```

Choose **Metashape alignment** when cubefaces are the final product you want to import into Metashape.

Choose **COLMAP export** when Metashape has already aligned the original fisheye images and you want the GUI to create a clean COLMAP scene for training/viewer tools.

## Lens Configuration

The top of the left column configures the fisheye lens inputs. In single-lens mode only Lens A is active. Enable **Dual-lens Mode** for a 360 camera with separate front/back fisheye lenses.

### Calibration XML

**CLI flag:** `--amlenscal`

Path to a Metashape-exported lens calibration XML file for one fisheye lens.

The converter expects Metashape's fisheye model and parameter ordering. Do not paste OpenCV, COLMAP, or RealityScan coefficients into a Metashape-style XML unless they have been explicitly converted.

Export from Metashape:

```text
Tools > Camera Calibration > select lens > Adjusted tab > save/export
```

### Lens Label

**CLI flag:** `--lenslabel`

Name used for this lens in output folders and COLMAP image names. Examples:

```text
Osmo360-front
Osmo360-back
Lens-front
Lens-back
```

In COLMAP export mode, the lens label becomes part of the flat final filenames, for example:

```text
Osmo360_front_000001_dir_plusZ.png
```

### Images Directory

**CLI flag:** `--directoryfisheyeimages`

Directory containing fisheye images for one lens only.

Supported extensions:

```text
.jpg .jpeg .png .tif .tiff
```

For dual-lens 360 captures, split front/back images into separate folders before running.

### Masks Directory

**CLI flag:** `--directoryfisheyemasks`

Optional but strongly recommended. Contains binary PNG masks matched to image stems:

```text
0   = exclude pixel
255 = use pixel
```

The mask directory may be partial. If an image lacks a matching mask, the converter uses the next available support source.

### Lens-Only Mask

**CLI flag:** `--lensonlymask`

A single mask for the lens. Used when there is no mask directory, or as fallback for images missing per-image masks.

### Max Useful FOV

**CLI flag:** `--maxusefulfov`

Fallback support source when no masks are available. The converter creates a radial support mask from the lens model.

Use this carefully. If the value is too wide, the lens model can be pushed beyond its reliable region.

### Support Priority

The converter chooses useful fisheye pixels in this order:

| Priority | Source | Behavior |
|---|---|---|
| 1 | Masks directory | Uses all available masks to derive support; missing per-image masks get a fallback. |
| 2 | Lens-only mask | Uses one mask for the lens. |
| 3 | Max useful FOV | Generates radial support from the lens model. |

At least one support source is required.

## Shared Settings

### Output Purpose

Controls the workflow and which options are visible.

#### Metashape Alignment

Use when the output cubefaces will be aligned in Metashape.

Visible options:

- Station/Rig
- Cubeface output folder

Output layout is the original converter layout.

#### COLMAP Export

Use when Metashape has already aligned the raw fisheye cameras and you want a training-ready COLMAP scene.

Visible behavior:

- Station/Rig is hidden.
- The output folder becomes a single export root.
- Cubefaces are generated internally under `processing/tmp/cubefaces`.
- The final scene is written to `colmap/`.

### Output Folder

The meaning depends on output purpose.

#### In Metashape Alignment Mode

This is the cubeface output folder:

```text
output/
  <lenslabel>/
    images/
    masks/
    bonusdata/
```

#### In COLMAP Export Mode

This is the export root:

```text
output/
  colmap/
  processing/
  reports/
```

Training software should point at:

```text
output/colmap/
```

### Face Width

**CLI flag:** `--facewidth`

Width and height of each generated cubeface. Default is `2100`.

Higher values preserve more image detail but increase runtime and file size.

### Format

**CLI flag:** `--outputformat`

Color cubeface format:

```text
png
tiff
jpg
```

Masks are always PNG.

### Force Reprocess

**CLI flag:** `--force`

Reprocess cubefaces even when outputs already exist.

### Skip Cubeface Generation

Skips Lens A / Lens B conversion and runs only the COLMAP export step.

Use this when the internal or external cubefaces already exist and you only need to rerun export, mapping, additional frame-camera media, or scene packaging.

In COLMAP export mode, skipped cubefaces are read from the output root's expected processing location.

### Station / Rig

Visible only in **Metashape alignment** mode.

#### Station

Default. Groups the five cubefaces from each source image together:

```text
images/
  000001/
    000001_dir_plusZ.png
    000001_dir_plusX.png
    000001_dir_minusX.png
    000001_dir_plusY.png
    000001_dir_minusY.png
```

Use this for Metashape Standard camera-station workflows.

#### Rig

Groups by face direction:

```text
images/
  dir_plusZ/
    000001_dir_plusZ.png
    000002_dir_plusZ.png
  dir_plusX/
    ...
```

Use this for Metashape Pro rig workflows.

## COLMAP Export Mode

COLMAP export mode creates a training-ready pinhole scene from a Metashape alignment.

Typical workflow:

1. Align raw fisheye images in Metashape.
2. Optionally align drone, DSLR, phone, or other frame cameras in the same chunk.
3. Export Metashape `cameras.xml`.
4. Export the aligned sparse cloud as `.ply`.
5. Select **Output purpose > COLMAP export** in the GUI.
6. Run the export.

### COLMAP Output Structure

The selected output folder becomes:

```text
output/
  colmap/
    images/
      Osmo360_front_000001_dir_plusZ.png
      Osmo360_front_000001_dir_plusX.png
      Osmo360_back_000001_dir_plusZ.png
      fuji_DSCF1710.png
      iphone_IMG_4674.png
    masks/
      Osmo360_front_000001_dir_plusZ.png
      Osmo360_back_000001_dir_plusZ.png
      fuji_DSCF1710.png
      iphone_IMG_4674.png
    sparse/
      0/
        cameras.txt
        images.txt
        points3D.txt

  processing/
    remap_cache/
    manifests/
    logs/

  reports/
    conversion_report.txt
    validation_report.txt
    run_summary.txt
```

`processing/tmp/` is removed after a successful export.

### Metashape Cameras XML

Aligned camera export from Metashape. It contains camera poses, sensor calibration, camera labels, and chunk transform information.

The exporter keeps cameras and sparse points in the same coordinate frame. Local chunk transforms and WGS84 geographic exports are handled automatically and recorded in the reports.

### Metashape Sparse Cloud PLY

Aligned sparse point cloud exported from Metashape. This becomes `points3D.txt`.

When **Projected tracks** is enabled, the exporter projects the sparse PLY points into the generated COLMAP cameras and writes synthetic 2D observations. These are not the original Metashape tie-point tracks.

### Check Mapping

In COLMAP export mode, each generated cubeface image needs the pose of the original Metashape fisheye camera it came from. Mapping connects each GUI lens label to the correct Metashape camera IDs or camera run.

This is especially important for dual-fisheye cameras because both lenses may use the same source stems. `front/000001.jpg` and `back/000001.jpg` can both become cubefaces named from `000001`, but they must receive different original Metashape poses.

Use **Check Mapping** before COLMAP export.

The resolver examines:

- active GUI lens labels,
- source image stems,
- Metashape camera IDs,
- camera group labels,
- sensor IDs,
- exact and subset filename stem matches,
- ID gaps,
- ambiguous or heuristic assignments.

If mapping is proven, the GUI reports that export is ready.

If mapping is plausible but heuristic, the GUI enables **Use Proposed Map**. Click it only after reviewing the proposal. The copied generated map is tracked; if inputs change later, the GUI treats the stale map as an export blocker.

Manual map example:

```text
Osmo360-front=16-34,Osmo360-back=35-53
```

### Additional Frame-Camera Media Sets

Use **Add Frame Camera Media Set** for aligned non-fisheye image families.

Examples:

```text
iphone
fuji
drone
dslr
```

Each row has:

- name,
- image folder,
- optional mask folder.

These are called **passthrough media** in the CLI because they do not get converted into cubefaces. They still pass through the COLMAP exporter, where they may be copied, hardlinked, undistorted, renamed, and masked so they fit the final `output/colmap/` scene.

If one of these frame-camera sensors has distortion, the exporter undistorts its images into final `PINHOLE` images and generates valid-pixel masks when no mask folder is provided.

### Export Options

#### Pose

Metashape transform convention. The default is:

```text
metashape_camera_to_world
```

Use the other convention only if you are deliberately testing transform interpretation.

#### Require Masks

Fails the export if any final image lacks a mask.

Cubeface masks, user masks for additional frame-camera media, and generated undistortion valid-pixel masks count as masks.

#### Projected Tracks

Writes synthetic projected observations into the COLMAP sparse model. Recommended for training tools that expect non-empty `points3D.txt` tracks.

#### Normalize Scene Scale

Default: unchecked.

When enabled, the exporter recenters the COLMAP scene around the camera path and uniformly scales both camera positions and sparse points to a viewer-friendly size. Camera rotations and image projections are preserved.

Use this when the exported scene feels unusually tiny, huge, or slow to navigate in training/viewer tools. It is a packaging transform, not a calibration or metric-survey correction.

The exporter writes:

```text
processing/manifests/scene_scale_diagnostics.json
processing/manifests/scene_normalization_transform.json
```

The transform manifest is only written when normalization is applied.

#### Force Assets

Regenerates or relinks packaged scene assets.

#### Keep Processing Files After Successful Export

Default: checked.

When checked, keeps:

```text
processing/remap_cache/
processing/manifests/
processing/logs/
```

When unchecked, the exporter removes those folders after a successful export and keeps only:

```text
output/colmap/
output/reports/
```

This can make final folders tidier, but reruns may be slower and debugging information is reduced.

## Output Validation

A healthy COLMAP export should show:

```text
all_cameras_pinhole: True
missing_images: 0
missing_masks: 0
```

Human-readable reports are written to:

```text
output/reports/
```

Detailed logs and machine-readable support files are written to:

```text
output/processing/
```

Scene scale diagnostics are summarized in `conversion_report.txt`. When processing files are kept, the full diagnostics manifest is written to:

```text
output/processing/manifests/scene_scale_diagnostics.json
```

## Run / Cancel

**Run** validates fields, saves settings, and starts the queued subprocesses.

In Metashape alignment mode:

```text
Lens A
Lens B, if enabled
```

In COLMAP export mode:

```text
Lens A
Lens B, if enabled
COLMAP Scene
```

If **Skip cubeface generation** is checked, the lens steps are omitted.

**Cancel** terminates the running subprocess and clears the remaining queue.

## Preview Panel

After a run, the preview dropdown can show:

- Cube faces
- Useful pixel mask
- Mask coverage
- Fallback mask
- Run summary
- COLMAP scene

The COLMAP scene preview includes scene scale diagnostics when the manifest is available.

In dual-lens mode, a second dropdown selects Lens A or Lens B for cubeface previews.

## Settings Persistence

The GUI saves settings to:

```text
gui/.cubemap_gui_v4_prefs.json
```

The file is ignored by git and restored automatically on next launch.
