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

### Output directory

**CLI flag:** `--outputdir`
**Required:** Yes

Root directory for all output. The script creates subdirectories under this path:

```
outputdir/
  <lenslabel>/
    images/         — output pinhole color images
    masks/          — output pinhole masks (one folder per lens)
    bonusdata/      — diagnostic files (useful pixel mask, solid angle data, remap cache, run report)
```

The exact layout under `images/` depends on the Station/Rig setting (see below).

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

## Run / Cancel

**Run** validates all fields, saves your settings, then launches the script as a subprocess. In dual-lens mode, Lens A runs first, then Lens B.

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

### Console

Live stdout from the subprocess. Shows the full command line, all log messages, and any errors.

### Preview

After a run completes, use the dropdown to inspect outputs:

- **Cube faces** — thumbnails of the 5 output faces from the first processed image
- **Useful pixel mask** — the computed mask showing which fisheye pixels were used
- **Mask coverage** — heatmap (viridis colormap) showing how many masks contributed to each pixel (only available when support came from a masks directory)
- **Fallback mask** — the generated fallback mask for images without per-image masks (only present if some images were missing masks)
- **Run summary** — support source used, effective angle, image counts, wall clock time, and pinhole camera parameters for Metashape

In dual-lens mode, a second dropdown lets you switch the preview between Lens A and Lens B.

---

## Settings persistence

The GUI saves all field values to `.cubemap_gui_v4_prefs.json` when you click Run. Next time you launch the GUI, all fields are restored automatically.
