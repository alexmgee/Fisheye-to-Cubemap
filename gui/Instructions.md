# Fisheye-to-Cubemap GUI v4 — Instructions

This document walks through every input field and option in the GUI, explains what it controls, and describes how the underlying script uses it.

## What the script does

`AM_ImageAndMask_to_cubemap_v4.py` takes wide-angle fisheye images (equisolid or equidistant) and splits each one into **5 pinhole cube-face images** (+Z, +X, -X, +Y, -Y). The output pinhole images can then be aligned in Agisoft Metashape as standard frame cameras.

The script must be run once per lens. For 360 cameras with two fisheye lenses, the GUI handles both lenses in sequence with a single Run click using **Dual-lens Mode**.

---

## Lens Configuration

The top of the left column of the GUI is where you configure per-lens inputs. In single-lens mode only "Lens A" is active. Enable "Dual-lens Mode" to configure and process the second lens.

### Lens A / Lens B tabs

Switch between lens configurations. In dual-lens mode, both lenses are processed sequentially when you click Run.

### Dual-lens Mode checkbox

Enables the Lens B tab and processes both lenses in one run. Each lens runs as a separate subprocess with its own calibration, images, and masks.

---

### Calibration XML

**CLI flag:** `--amlenscal`
**Required:** Yes

Path to an Agisoft Metashape lens calibration XML file. This file contains the camera model type (e.g. equisolid, equidistant), image dimensions, focal length (`f`), principal point (`cx`, `cy`), and distortion coefficients (`k1`-`k3`, `p1`-`p2`).

**How to export from Metashape:** Tools > Camera Calibration > select the lens > click the "Adjusted" tab > click the floppy disk icon to save. Export one XML per fisheye lens.

When you select a calibration file, the GUI auto-fills the **Lens label** with the filename stem and displays a summary of the calibration data (projection type, resolution, focal length).

---

### Lens label

**CLI flag:** `--lenslabel`
**Required:** Yes

A short name for this lens (e.g. `Cam1-Lens0`). This becomes the name of the output subdirectory under the output directory, and is used in filenames and the run report.

Auto-filled from the calibration XML filename, but you can change it to anything.

---

### Images directory

**CLI flag:** `--directoryfisheyeimages`
**Required:** Yes

Directory containing the fisheye images from **one lens only**. Supported formats: `.jpg`, `.jpeg`, `.png`, `.tif`, `.tiff`.

For a 360 camera, you need to separate images by lens into different directories before processing. Each lens gets its own run.

---

### Masks directory (optional)

**CLI flag:** `--directoryfisheyemasks`
**Required:** No (strongly recommended)

Directory containing per-image binary masks. Each mask should be a PNG where **0 = exclude** and **255 = include** the corresponding pixel. Mask filenames are matched to image filenames by stem.

The mask directory does not need to contain a mask for every image. If some images are missing masks, the script uses a fallback (see support-source priority below).

This is the **highest-priority** support source. Use the **X** button to clear this field if you want to use a lower-priority source instead.

---

### Lens-only mask (optional)

**CLI flag:** `--lensonlymask`
**Required:** No

A single mask image that applies to the lens itself (not a specific image). Used as a fallback for any image that does not have a matching mask in the masks directory. If no masks directory is provided at all, this mask is used for every image.

This is the **second-priority** support source. Use the **X** button to clear.

---

### Max useful FOV checkbox + entry

**CLI flag:** `--maxusefulfov`
**Required:** No

The full field-of-view angle (in degrees) of the usable area of the lens. The script divides this by 2 to get the maximum half-angle for computing the useful pixel region.

This is the **lowest-priority** support source. It creates a radial mask from the lens model rather than using an actual mask image. Use this when you have no masks at all, but be conservative with the value — setting it too large can cause artifacts where the lens model breaks down.

Check the checkbox to enable, then enter the FOV value.

---

### Support-source priority

The script needs to know which pixels in the fisheye image contain real lens content. It determines this from whichever source you provide, following this strict priority:

| Priority | Source | What it does |
|---|---|---|
| 1 | **Masks directory** (>= 1 mask) | Unions all masks to compute the useful pixel region. Per-image masks are used when available; missing images get a fallback mask. |
| 2 | **Lens-only mask** | Uses this single mask as the useful pixel region. Applied to every image. |
| 3 | **Manual FOV** | Creates a circular mask from the FOV angle and lens model. No actual mask image needed. |

You must provide **at least one** of these. If you provide multiple, the highest-priority source wins and the GUI shows a badge indicating which source is active and which are ignored.

---

### Mode badge

The status line at the bottom of each lens panel shows which support source is currently active:

- **Support: mask directory** (green) — masks directory has images and will be used
- **Support: lens-only mask** (blue) — no masks directory, using lens-only mask
- **Support: manual FOV** (blue) — no masks at all, using FOV value
- **Incomplete: [reason]** (red) — missing required fields, cannot run

---

## Shared Settings

These settings apply to all lenses in the run.

### Cubeface working output folder

**CLI flag:** `--outputdir`
**Required:** Yes

Root directory for the original fisheye-to-cubeface script output. This is a working folder, not the final packaged COLMAP scene. The script creates subdirectories under this path:

```
cubefaces/
  <lenslabel>/
    images/         — output pinhole color images
    masks/          — output pinhole masks (one folder per lens)
    bonusdata/      — diagnostic files (useful pixel mask, solid angle data, remap cache, run report)
```

The exact layout under `images/` depends on the Station/Rig setting (see below). When the COLMAP export section is used, this folder should be separate from the final COLMAP scene folder. For example:

```
D:\Capture\testing_fisheye\cubefaces
D:\Capture\testing_fisheye\colmap
```

---

### Face width

**CLI flag:** `--facewidth`
**Required:** Yes
**Default:** 2100

Width (and height) in pixels of each output cube-face image. All five faces are square at this resolution.

Larger values produce higher-resolution output but increase processing time and output file size. 2100 is the script's default. Common values range from 1024 to 3840.

---

### Format (png / tiff / jpg)

**CLI flag:** `--outputformat`
**Default:** png

Output format for the **color** cube-face images. Masks are always written as PNG regardless of this setting.

- **png** — lossless, larger files
- **tiff** — lossless, larger files
- **jpg** — lossy, smaller files (not recommended if feeding into further reconstruction)

---

### Force reprocess

**CLI flag:** `--force`
**Default:** Off

By default, the script checks whether an image's full output set (5 color faces + 5 mask faces) already exists and skips it. This makes it safe to re-run after an interruption — it picks up where it left off.

Enable this to reprocess images even if their output already exists. Useful when you've changed calibration or mask inputs but the output filenames haven't changed.

---

### Skip cubeface generation

**Default:** Off

When checked, the GUI skips the cubeface conversion step entirely (Lens A and Lens B) and runs only the COLMAP scene export. Use this when you have already generated the cubeface images in a previous run and only need to re-run the COLMAP export — for example, after changing passthrough media sets, pose convention, or export options.

The cubeface output folder must still contain the previously generated cubeface images. The COLMAP exporter reads them from there.

---

### Station / Rig

**CLI flag:** `--rigstructure` (when Rig is selected)
**Default:** Station

Controls how output images are organized on disk. Both produce the same images — only the directory layout differs.

**Station** (default) — groups the 5 cube faces from each source image into a subdirectory named after the source image. Designed for Metashape Standard or Pro using "camera stations" to constrain alignment so all 5 pinhole views from the same fisheye frame share a position.

```
images/
  image_001/
    image_001_dir_plusZ.png
    image_001_dir_plusX.png
    image_001_dir_minusX.png
    image_001_dir_plusY.png
    image_001_dir_minusY.png
  image_002/
    ...
```

**Rig** — groups all images of the same cube face into a shared directory. Designed for Metashape Pro's "rig" feature where each face direction is treated as a separate camera in a calibrated rig.

```
images/
  dir_plusZ/
    image_001_dir_plusZ.png
    image_002_dir_plusZ.png
  dir_plusX/
    image_001_dir_plusX.png
    image_002_dir_plusX.png
  ...
```

---

## Metashape COLMAP Export

This section is collapsed when the GUI opens. After filling the lens configuration and shared settings, click the caret/title row for **Metashape COLMAP Export**; the left pane scrolls down to bring the export controls into view.

Use this section when Metashape has already aligned the original cameras and you want to reuse that alignment for a training-ready COLMAP scene. A typical workflow is:

1. Align the raw fisheye images in Metashape, optionally alongside drone, DSLR, phone, or other frame images.
2. Export the aligned Metashape camera XML and aligned sparse cloud PLY.
3. Run this GUI. The original fisheye-to-cubeface conversion still runs first and keeps its original cubeface logic unchanged.
4. The exporter composes each aligned fisheye pose with the fixed cubeface rotations, undistorts aligned frame/pinhole passthrough images when needed, and writes the final COLMAP scene.

When both **Metashape cameras.xml** and **Metashape sparse cloud .ply** are provided, the GUI automatically appends a final exporter step after Lens A and optional Lens B complete. The exporter writes a training-ready COLMAP scene with:

```
final_scene/
  images/
  masks/
  sparse/
    0/
      cameras.txt
      images.txt
      points3D.txt
```

Every camera written to `cameras.txt` is `PINHOLE`. The final scene is intended for training software that should not need to run a separate undistort step.

### Metashape cameras.xml

The aligned camera export from Metashape. It contains the aligned camera transforms, sensor calibration, camera labels, and chunk transform information.

If Metashape exports a local chunk/display transform, the exporter applies the same transform to the camera poses so the cameras and PLY points share the same coordinate frame. If the XML/PLY are geographic WGS84 exports, the exporter converts points back into Metashape local space instead. This is handled automatically and is recorded in `sparse/0/conversion_report.txt`.

### Metashape sparse cloud .ply

The aligned sparse point cloud export from Metashape. This becomes `points3D.txt`. When **Projected tracks** is enabled, the exporter also projects these sparse points into the generated COLMAP images and writes synthetic 2D observations.

The projected tracks are useful for making a real, parser-valid sparse model, but they are still projected from the PLY and Metashape poses. They are not the original Metashape tie-point tracks.

### Final COLMAP scene folder

Destination for the packaged COLMAP scene. It must be different from the cubeface output folder and from any passthrough media folder.

The final scene folder is intended to contain only the training-ready dataset. Images and masks are organized into subdirectories that mirror the `image_name` paths written in `images.txt`:

```
colmap/
  sparse/0/
    cameras.txt                                       — one PINHOLE entry per sensor
    images.txt                                        — image_name paths reference into images/
    points3D.txt
    conversion_report.txt
  images/
    <cubeface_folder_name>/                           — cubeface images (e.g. "cubefaces/")
      <lens_label>/images/<face_dir>/<stem>_<face>.png
      ...
    undistorted_passthrough/                           — undistorted passthrough images (if sensor has distortion)
      <media_set_slug>/                                — one subdir per media set (e.g. "iphone/", "fuji/")
        <filename>.png
    passthrough/                                       — raw passthrough images (if sensor has no distortion)
      <media_set_slug>/
        <filename>.<original_ext>
  masks/
    <cubeface_folder_name>/                           — cubeface masks (mirrors images/ layout)
      ...
    undistorted_passthrough/                           — auto-generated valid-pixel masks or undistorted user masks
      <media_set_slug>/
        <filename>.png
```

The subdirectory structure under `images/` is required — each entry in `images.txt` references a relative path like `cubefaces/Osmo360-back/images/dir_plusZ/000001_dir_plusZ.png` or `undistorted_passthrough/iphone/IMG_4670.png`. COLMAP-compatible trainers resolve these paths relative to the `images/` root.

On the same drive, the exporter first tries to hardlink cubeface assets, so the packaged COLMAP image entries usually do not consume a second full copy of the same files. `bonusdata/` stays in the separate cubeface working folder.

Exporter reports, undistort cache metadata, and the generated passthrough media manifest are kept out of the final scene entirely. The GUI stores them under `<cubeface working output folder>/colmap_export/`. The final scene root should contain only `images/`, `masks/`, and `sparse/`.

### Advanced manual lens-to-camera map

This optional field is normally left empty. When empty, the GUI automatically maps fisheye poses for the normal Lens A / Lens B workflow by reading the Metashape XML, comparing equisolid fisheye camera labels to the raw fisheye filenames selected in each Lens tab, and building the exporter map internally.

If automatic mapping cannot safely resolve the scene, enter a manual map here. The map connects cubeface lens output folders to Metashape fisheye camera ids. Example:

```
Osmo360_back=0-36,Osmo360_front=37-73
```

This usually only happens when the XML camera labels do not match the source image filenames, or when multiple possible camera runs have the same count and cannot be distinguished safely.

Use **Check Mapping** before a long export if you want to verify this stage. It reports either a ready mapping, for example `Osmo360_back=37-73`, or a review message explaining why automatic mapping could not be resolved.

### Passthrough Media Sets

Use **Add Passthrough Media Set** for each aligned non-fisheye image family, such as a drone, DSLR, or phone set. Each row writes one manifest entry with:

- name
- image folder
- mask folder, optional

Leave the mask folder empty when you do not have a real content/subject mask for that media set. For distorted passthrough sensors, the exporter undistorts the images into `images/undistorted_passthrough/...` and automatically generates matching valid-pixel masks in `masks/undistorted_passthrough/...`. These generated masks exclude the black invalid border created by undistortion; they are not semantic object masks.

During undistortion, the exporter re-centers the principal point to the exact image center (`cx=width/2`, `cy=height/2`). The COLMAP `cameras.txt` records the same centered values. This ensures compatibility with 3DGS trainers that assume a centered principal point, including the original Inria reference implementation and LichtFeld Studio.

If a passthrough media set already has masks, those masks are undistorted with the image. If a passthrough sensor has no distortion, and no mask folder is provided, the image is packaged without a generated mask unless **Require masks** is enabled.

### Export Options

- **Pose** tells the exporter how to interpret camera transforms in the Metashape XML. The default, `auto`, tests the supported transform conventions by projecting sparse PLY points through cubeface camera poses and counting how many land within image bounds. The convention with more in-bounds projections wins. A wrong convention produces cameras pointing away from the scene, so the correct one always scores higher. Export only fails if both conventions score zero in-bounds (meaning the PLY and camera poses don't overlap at all). You can also select `metashape_camera_to_world` or `metashape_world_to_camera` explicitly if you know which convention your Metashape version uses.
- **Require masks** is off by default. Enable it only when every final image should have a mask. Cubeface masks and generated undistorted-passthrough valid-pixel masks count as final masks. Export fails if any remaining image cannot provide one.
- **Projected tracks** writes projected sparse-cloud observations into `images.txt` / `points3D.txt`. This is recommended for training software that expects a non-empty COLMAP sparse model.
- **Force assets** regenerates or relinks packaged scene assets.

### Output validation

After export, the support folder contains `validation_report.txt`. A healthy training scene should report:

```
all_cameras_pinhole: True
missing_images: 0
missing_masks: 0
```

The sparse report at `final_scene/sparse/0/conversion_report.txt` records the selected pose convention, whether a camera-world or point transform was applied, how many passthrough images were undistorted or reused, and how many projected track observations were written.

---

## Run / Cancel

**Run** validates all fields, saves your settings, then launches the script as a subprocess. In dual-lens mode, Lens A runs first, then Lens B. If both Metashape XML and PLY are present, the COLMAP scene step runs last. If **Skip cubeface generation** is checked, the lens steps are omitted entirely and only the COLMAP scene step runs.

**Cancel** terminates the running subprocess and clears any remaining queue.

---

## Right panel

### Progress bar and phase label

Shows real-time progress parsed from the script's `[PROGRESS]` output lines. Phases you'll see:

1. **Analyzing masks** — reading and combining masks to determine the useful pixel region
2. **Computing rays** — building the ray direction field from the lens calibration (indeterminate progress bar)
3. **Precomputing remap** — computing the remap tables for each cube face (this is the slow part, ~1-2 minutes per face, but cached for reuse)
4. **Processing image N/M** — applying the remap to each image and writing output faces
5. **Lens complete** — done
6. **Building COLMAP scene** — final scene packaging/export if enabled

### Console

Live stdout from the subprocess. Shows the full command line, all log messages, and any errors.

### Preview

After a run completes, use the dropdown to inspect outputs:

- **Cube faces** — thumbnails of the 5 output faces from the first processed image
- **Useful pixel mask** — the computed mask showing which fisheye pixels were used
- **Mask coverage** — heatmap (viridis colormap) showing how many masks contributed to each pixel (only available when support came from a masks directory)
- **Fallback mask** — the generated fallback mask for images without per-image masks (only present if some images were missing masks)
- **Run summary** — support source used, effective angle, image counts, wall clock time, and pinhole camera parameters for Metashape
- **COLMAP scene** — packaged scene paths and report excerpts

In dual-lens mode, a second dropdown lets you switch the preview between Lens A and Lens B.

---

## Settings persistence

The GUI saves all field values to `.cubemap_gui_v4_prefs.json` when you click Run. Next time you launch the GUI, all fields are restored automatically.
