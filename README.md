# Fisheye-to-Cubemap

Convert calibrated fisheye and unstitched 360-camera images into 5 pinhole cube faces, either for direct pinhole SfM experiments or for post-solve COLMAP/pinhole conversion after native fisheye alignment.

> **Status:** Work in progress. Do not use.

## Branch scope: `multi-format`

The `multi-format` branch is an implementation and research branch for removing the hard dependency on Agisoft Metashape calibration XMLs.

The branch goal is to make calibration input provider-based:

- keep the original Metashape XML workflow working;
- add explicit importers for other calibration sources instead of pretending their coefficients are Metashape coefficients;
- normalize each supported provider into the same internal representation: source image geometry plus per-pixel rays;
- support the larger cameras-to-COLMAP goal where SfM happens on native fisheyes first, then solved native cameras are converted into pinhole/cubeface COLMAP-style outputs afterward;
- document and validate each provider before exposing it as a normal user-facing option.

Current practical scope:

- Implemented: Metashape XML, project-native raymap `.npz`, OpenCV fisheye calibration files, and first-wave COLMAP camera files.
- In progress/planned: full post-SfM native-fisheye cameras-to-COLMAP export plumbing.
- Planned but not implemented: RealityScan/RealityCapture XMP and metadata probing.
- RealityScan XMP work is waiting on real fixture data exported from RealityScan on Windows. Local planning/checklist notes may live under ignored `docs/` files during development.

This branch should fail closed for planned providers. A planned provider appearing in the CLI does not mean it is ready to generate rays.

## Why this exists

Standard SfM pipelines tend to assume or favor pinhole data input. Wide-angle fisheye and dual-lens fisheye 360 captures have become popular for their ability to rapidly see more of a scene, but come with the tradeoff of decreased feature quality and fewer/weaker integrations across SfM and 3DGS pipelines. The usual workaround of converting fisheyes to a single equirectangular image leaves you with a projection which pinhole aligners still struggle with. Equirectangular image stitching also unavoidably compromises the accuracy of the scene geometry.

This project takes a different path: read a real lens calibration, convert fisheye pixels to ray directions, and reproject those rays onto 5 faces of a virtual cube.

There are two ways to use that:

1. **Pre-SfM decomposition:** convert fisheye frames into cube faces first, then align those pinhole faces in Metashape, COLMAP, or another pinhole-oriented SfM tool. This is simple and still useful for tests or smaller jobs, but it can create thousands of pinhole images and is not always the best production path.
2. **Post-SfM cameras-to-COLMAP conversion:** align the native fisheye images first in a solver that understands the source cameras, then use the solved native fisheye calibration/poses to generate pinhole cube faces and matching COLMAP-style camera records afterward. This avoids asking SfM to solve thousands of derived pinhole images when the native fisheye alignment is already available.

The second workflow is a major reason for the `multi-format` branch.

## Where this fits in a 3DGS pipeline

Preferred large-dataset path:

```
fisheye / 360 capture
        |
        v
Native fisheye SfM alignment
Metashape today; RealityScan/COLMAP-style sources planned
        |
        v
Export solved calibration + camera poses
        |
        v
THIS PROJECT                             <-- calibration providers + ray remap
        |
        v
5 pinhole cube faces + matching pinhole/COLMAP camera data
        |
        v
3D Gaussian Splatting training
```

Simpler legacy/test path:

```
fisheye / 360 capture
        |
        v
Per-lens calibration
        |
        v
THIS PROJECT
        |
        v
5 pinhole cube faces per fisheye image
        |
        v
SfM alignment on the derived pinhole images
```

The project is also useful outside 3DGS anywhere calibrated fisheye pixels need to be reprojected into pinhole views.

## Calibration inputs

The original release path used Agisoft Metashape calibration XML only. The `multi-format` branch is adding a provider layer so calibration sources can be loaded through explicit model-specific importers.

Currently implemented providers:

| Provider | Status | Notes |
|---|---|---|
| `metashape` | Implemented | Preserves the original Metashape XML path for `equidistant_fisheye` and `equisolid_fisheye`. |
| `raymap` | Implemented | Project-native dense per-pixel ray format (`.npz`) for calibration interchange. |
| `opencv-fisheye` | Implemented, early | Loads explicit JSON and common OpenCV YAML/XML FileStorage keys using OpenCV's fisheye model. |
| `colmap` | Implemented, early | Loads first-wave models from `cameras.txt` or sparse directories. Poses/rigs are not used yet. |
| `realityscan-xmp` | Planned | Fails closed until fixtures and model-specific validation are available. |
| `metadata` | Planned diagnostic | Fails closed as a calibration provider; intended for reporting/probing only. |

**Do not** copy parameters from another SfM tool's calibration (OpenCV, COLMAP, RealityScan, etc.) into a Metashape-format file even when the parameter names match, as the underlying equations differ. Use the matching `--calibration-provider` instead.

A 360 camera has two fisheye lenses, so plan to run the script twice (once per lens) with each lens's separate calibration and image directory.

## Raymap `.npz` plan

The raymap is the project-owned calibration interchange format. It is intentionally not another coefficient format.

High-level idea:

- Every calibration model this project cares about ultimately answers one geometric question: for this source pixel, what 3D ray left the camera center and hit that pixel?
- Metashape XML, OpenCV fisheye, COLMAP camera models, and future RealityScan XMP all encode that question differently.
- The raymap stores the answer directly as a dense per-pixel ray field.
- Once the ray field is known, the cube-face remap no longer cares which software or equation produced it.

That makes raymap useful for three reasons:

1. It avoids treating incompatible coefficient systems as interchangeable.
2. It gives us a stable cache/interchange artifact for expensive or tool-specific calibration decoding.
3. It lets future provider work focus on one obligation: prove that provider-specific calibration produces the correct source-pixel rays.

### What a raymap contains

A raymap is a NumPy `.npz` file. Version 1 uses:

| Key | Meaning |
|---|---|
| `version` | Format string, currently `fisheye-to-cubemap-raymap-v1`. |
| `width`, `height` | Source image dimensions. |
| `rays` | Dense `float32` array with shape `H x W x 3`. |
| `provider` | Source provider that produced the rays, such as `metashape`, `opencv-fisheye`, or `colmap`. |
| `model` | Provider-specific model name. |
| `source` | Original calibration file path, recorded for traceability. |
| `source_geometry_json` | JSON record describing the image geometry the rays apply to. |
| `params_json` | JSON copy of provider parameters useful for auditing. |
| `solid_angle` | Optional `float32` per-pixel solid angle estimate. |
| `notes` | Free-form notes. |

The `rays[y, x]` vector is a unit ray in the source camera coordinate system for source pixel `(x, y)`. It is not an RGB image, UV map, or distortion coefficient table. It is the camera model sampled over the source image grid.

### How raymaps are generated

Provider generation follows this pattern:

1. Load a provider-specific calibration file.
2. Validate the provider label, model label, source image dimensions, and source image geometry.
3. Sample the calibration on the source image grid.
4. Convert every source pixel in the calibration dimensions to a 3D direction.
5. Normalize every direction to unit length.
6. Separately build the normal useful-pixel mask from masks/FOV/support logic for the current conversion run.
7. Optionally estimate per-pixel solid angle.
8. Save the dense ray field and audit metadata with `--export-raymap`.

Example:

```bash
python AM_ImageAndMask_to_cubemap_v4.py ^
  --calibration lens_calibration.xml ^
  --calibration-provider metashape ^
  --lenslabel "Osmo360-front" ^
  --directoryfisheyeimages front_images ^
  --directoryfisheyemasks front_masks ^
  --facewidth 2100 ^
  --outputdir output ^
  --export-raymap temp\osmo360_front.raymap.npz
```

### How raymaps are loaded

Loading a raymap bypasses provider-specific camera math:

1. Load the `.npz`.
2. Require the expected raymap version.
3. Require `rays` to be `H x W x 3`.
4. Reject non-finite values.
5. Reject zero-length rays.
6. Reject rays whose norm error is too large.
7. Renormalize only tiny floating-point norm drift.
8. Validate that stored source geometry matches the calibration dimensions.
9. Pass the embedded rays directly into the cubemap remapper.

Example:

```bash
python AM_ImageAndMask_to_cubemap_v4.py ^
  --calibration temp\osmo360_front.raymap.npz ^
  --calibration-provider raymap ^
  --lenslabel "Osmo360-front" ^
  --directoryfisheyeimages front_images ^
  --directoryfisheyemasks front_masks ^
  --facewidth 2100 ^
  --outputdir output_from_raymap
```

If the raymap was exported from the same calibration and applied to the same source images/masks, output should be visually and statistically equivalent to running from the original provider. Small float32 boundary differences can appear at remap support edges.

### Why this is valid

The cubemap conversion is fundamentally ray-based. The script does not need a symbolic fisheye equation after the source-pixel ray field exists.

For each cube face, the remapper:

1. Takes the set of valid source pixels and their rays.
2. Intersects/projects those rays against the target cube-face direction.
3. Builds OpenCV remap arrays from cube-face pixels back into source-image coordinates.
4. Applies the same remap to color images and masks.

Therefore the critical correctness claim is:

> If `rays[y, x]` accurately represents the optical ray for source pixel `(x, y)`, then the downstream cubemap projection is independent of whether those rays came from Metashape, OpenCV, COLMAP, RealityScan XMP, or a previously exported raymap.

This is also why raymap is a good test oracle for provider work. A provider can be validated by generating a raymap once, replaying from that raymap, and comparing outputs. If provider output and raymap replay diverge, the issue is likely in provider loading, geometry validation, or ray generation. If they match, the remap stage is isolated from the provider.

### What raymap does not solve

Raymap is an intrinsics/ray-field artifact. It does not by itself contain:

- per-image camera poses;
- rig constraints;
- COLMAP `images.txt` entries;
- source image pixels;
- masks;
- a proof that the source calibration was correct.

For the post-SfM cameras-to-COLMAP workflow, raymap covers the per-lens projection geometry. It still has to be combined with solved native fisheye poses from Metashape, RealityScan, COLMAP, or another solver to emit complete pinhole/cubeface COLMAP camera records.

### Current implementation status

Implemented now:

- Save/load `.npz` raymaps.
- Store dense `H x W x 3` float32 rays.
- Store provider/model/source metadata.
- Store source-geometry metadata.
- Validate version, shape, finite values, and unit length on load.
- Export a raymap from any provider path that reaches ray generation.
- Re-run the cubemap conversion from `--calibration-provider raymap`.

Planned improvements:

- Stronger image-dimension checks against actual input image folders.
- More explicit pixel-center convention metadata.
- Real fixture comparisons for OpenCV, COLMAP, and RealityScan-derived rays.
- Optional tooling to inspect angular differences between two raymaps.
- Integration with the later post-SfM cameras-to-COLMAP export path.

## Install

Python 3.9+ recommended.

```bash
git clone <repo-url>
cd Fisheye-to-Cubemap
python -m venv .venv
.venv\Scripts\activate         # Windows
# source .venv/bin/activate    # macOS/Linux
pip install -r requirements.txt
```

To use the GUI, additionally:

```bash
pip install -r gui/requirements.txt
```

## Quick start

A worked example dataset is **coming soon** to [`examples/`](examples/).

The minimal command line is:

```bash
python AM_ImageAndMask_to_cubemap_v4.py ^
  --calibration lens_calibration.xml ^
  --calibration-provider metashape ^
  --lenslabel "Cam1-Lens0" ^
  --directoryfisheyeimages images/ ^
  --directoryfisheyemasks masks/ ^
  --facewidth 2100 ^
  --outputdir output/
```

Replace the `^` line continuation with `\` on macOS/Linux.

## CLI reference

Required:

| Flag | Description |
|---|---|
| `--calibration` | Path to a calibration file. Currently implemented: Metashape XML, raymap `.npz`, OpenCV fisheye JSON/YAML/XML, COLMAP `cameras.txt` or sparse directory. |
| `--calibration-provider {auto,metashape,raymap,opencv-fisheye,opencv,colmap,realityscan-xmp,metadata}` | Select calibration loader. `auto` detects Metashape XML and raymap `.npz`. Planned providers fail closed until implemented. |
| `--camera-id` | Camera ID for multi-camera calibration files such as COLMAP `cameras.txt`. Required when a COLMAP file contains more than one camera. |
| `--amlenscal` | Legacy alias for a Metashape-exported lens calibration XML file. Still supported. |
| `--lenslabel` | Free-form label used in output filenames (e.g. `Cam1-Lens0`). |
| `--directoryfisheyeimages` | Directory of fisheye images for one lens. Recognized: `.jpg .jpeg .png .tif .tiff`. |
| `--facewidth` | Output cube face width in pixels. Default `2100`. |
| `--outputdir` | Output directory. Will be created if missing. |

Mask source — pick one (priority high to low):

| Flag | Description |
|---|---|
| `--directoryfisheyemasks` | Directory of per-image PNG masks. Filenames must match the source images (with `.png` extension). 0 = ignore pixel, 255 = use pixel. May be partial; missing entries fall back to the next option. |
| `--lensonlymask` | A single PNG mask reused for every image that lacks a per-image mask. |
| `--maxusefulfov` | Generates a circular mask from the lens field of view, in degrees. **Risky** if set too wide — the lens model breaks down beyond the calibrated FOV. |

Output options:

| Flag | Description |
|---|---|
| `--rigstructure` | Reorganize outputs for Metashape rig alignment (one folder per cube face, all images per face together). Default layout groups by source image, suitable for "camera stations" alignment. |
| `--outputformat {png,tiff,jpg}` | Color face image format. Default `png`. Masks always PNG. |
| `--export-raymap` | Write the loaded/generated per-pixel ray calibration to a raymap `.npz`. |
| `--raymap-compression {compressed,stored}` | Compression mode for `--export-raymap`. Default `compressed`. |
| `--force` | Reprocess images whose outputs already exist. |
| `--version` | Print version. |
| `--h` / `--usage` | Print extended help. |

Computing the per-face remap takes up to ~2 minutes per face (5 faces). After that, applying the remap to each input image is fast (~4–5 seconds per fisheye image).

## Output layout

### Default (camera-stations-friendly)

```
outputdir/
└── <lenslabel>/
    ├── images/
    │   └── <image_name>/
    │       ├── <image_name>_dir_plusZ.png      # forward
    │       ├── <image_name>_dir_plusX.png      # right
    │       ├── <image_name>_dir_minusX.png     # left
    │       ├── <image_name>_dir_plusY.png      # up
    │       └── <image_name>_dir_minusY.png     # down
    ├── masks/
    │   ├── <image_name>_dir_plusZ.png
    │   └── ...
    └── bonusdata/
        ├── useful_pixel_mask.png
        ├── SolidAngleRayDirQuaternionwxyz_BandSequential_FLOAT_<W>x<H>x5.raw
        └── <visualizations>
```

Grouping per source image lets Metashape Standard Edition lock the 5 faces to a shared nodal point via "camera stations".

### `--rigstructure` (Metashape Pro rig)

```
outputdir/
└── <lenslabel>/
    ├── images/
    │   ├── dir_plusZ/
    │   │   ├── <image_001>_dir_plusZ.png
    │   │   └── <image_002>_dir_plusZ.png
    │   ├── dir_plusX/
    │   ├── dir_minusX/
    │   ├── dir_plusY/
    │   └── dir_minusY/
    ├── masks/
    │   ├── <image_001>_dir_plusZ.png
    │   └── ...
    └── bonusdata/
```

Grouping by face is the layout Metashape Pro expects for rig-constrained alignment.

### Dual-lens (360 capture)

When the GUI runs both lenses against the same `outputdir`, two `<lenslabel>/` siblings are produced — one per lens — each with its own `images/`, `masks/`, and `bonusdata/`. Mask filenames within each lens still share stems with the matching colour images, so Metashape's "Mask From Folder" rule works unchanged at the per-lens level.

> **Note:** the cube intentionally omits the `-Z` (rear) face. In a 360 camera, the scene content in this direction would be imaged with the opposing lens.

## The bonusdata RAW file

`SolidAngleRayDirQuaternionwxyz_BandSequential_FLOAT_<W>x<H>x5.raw` is a headerless float32, little-endian, band-sequential (BSQ) raster. It carries the same per-pixel geometry the script computed from the lens calibration:

| Band | Contents |
|------|----------|
| 1 | Solid angle (steradians) |
| 2 | Quaternion `w` (ray direction relative to +Z) |
| 3 | Quaternion `x` |
| 4 | Quaternion `y` |
| 5 | Quaternion `z` |

Read in NumPy:

```python
import numpy as np
W, H = 3840, 3840  # input fisheye dimensions
arr = np.fromfile("SolidAngleRayDir...3840x3840x5.raw", dtype=np.float32).reshape(5, H, W)
solid_angle, qw, qx, qy, qz = arr
```

Or in [ImageJ](https://imagej.net/): **File → Import → Raw**, set Width / Height, Images=5, 32-bit Real, Little-endian.

Quaternion convention: `(w, x, y, z)` rotates `+Z` onto the per-pixel ray direction. The antipodal direction (south pole) is handled with a special-case quaternion to avoid numerical degeneracy.

## GUI

A standalone CustomTkinter wrapper with file pickers, live console, progress bar, and output preview lives in [`gui/`](gui/). See [`gui/README.md`](gui/README.md) for usage.

## Limitations and known gaps

- **Equidistant fisheye support is implemented but not validated.** Equisolid (the default for most consumer 360 cameras) has been exercised on real captures.
- **Math is not formally verified.** The reprojection has been compared visually and used productively, but there is no analytic ground-truth check.
- **No extrinsics support.** Each lens is processed in isolation; pose-aware multi-camera workflow happens downstream in your SfM tool.
- **Multi-format calibration is staged.** Metashape XML, raymap `.npz`, OpenCV fisheye, and first-wave COLMAP camera models are implemented on the `multi-format` branch. RealityScan XMP and metadata probing are planned and fail closed until validated.
- **`-Z` cube face is intentionally not generated** (see note above).
- **Compute cost.** Building the per-face remap can be multi-minute run time.

## Contributing

Issues and PRs welcome — there's no formal process. The script is shipped working but not finished; feedback from real captures is the most useful kind.


## Authors

- **Mike Heath** ([@LaunchedPix](https://github.com/LaunchedPix))
- **Alex Gee** ([@Macgregor](https://github.com/Macgregor))

## License

MIT — see [LICENSE](LICENSE).
