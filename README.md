# Fisheye-to-Cubemap

Convert calibrated fisheye and unstitched 360-camera images into 5 pinhole cube faces for Structure-from-Motion alignment, en route to 3D Gaussian Splatting reconstruction.

> **Status:** Working release. The math has been exercised on real captures but is not formally verified. **Use at your own risk.** 

## Why this exists

Standard SfM pipelines tend to assume or favor pinhole data input. Wide-angle fisheye and dual-lens fisheye 360 captures have become popular for their ability to rapidly see more of a scene, but come with the tradeoff of decreased feature quality and fewer/weaker integrations across SfM and 3DGS pipelines. The usual workaround of converting fisheyes to a single equirectangular image leaves you with a projection which pinhole aligners still struggle with. Equirectangular image stitching also unavoidably compromises the accuracy of the scene geometry.

This script takes a different path: read the lens calibration, convert each fisheye pixel to a ray direction, and reproject those rays onto 5 faces of a virtual cube. Each face is a clean pinhole image that any SfM tool can align without special handling. After alignment, the cube faces (and their masks) feed directly into 3D Gaussian Splatting training.

## Where this fits in a 3DGS pipeline

```
fisheye / 360 capture
        |
        v
Agisoft Metashape lens calibration       (one-time per lens)
        |
        v
THIS SCRIPT                              <-- you are here
        |
        v
5 pinhole cube faces (per fisheye image)
        |
        v
SfM alignment (Metashape, COLMAP, etc.)
        |
        v
3D Gaussian Splatting training
```

The script is also useful outside 3DGS, anywhere a pinhole alignment workflow needs to ingest fisheye data.

## Hard requirement: Metashape-format calibration

The math in this script is locked to Agisoft Metashape's lens model and parameter ordering (Appendix D of the Metashape manual). It reads a Metashape calibration XML and uses `f, cx, cy, K1, K2, K3, P1, P2`.

**Do not** copy parameters from another SfM tool's calibration (OpenCV, COLMAP, RealityScan, etc.) into a Metashape-format file even when the parameter names match, as the underlying equations differ. Calibrate in Metashape, or translate the calibration explicitly.

A 360 camera has two fisheye lenses, so plan to run the script twice (once per lens) with each lens's separate calibration and image directory.

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
  --amlenscal lens_calibration.xml ^
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
| `--amlenscal` | Path to a Metashape-exported lens calibration XML file. |
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
- **Metashape-format calibration is the only supported input.** Translating other SfM tools' calibrations is left to the user.
- **`-Z` cube face is intentionally not generated** (see note above).
- **Compute cost.** Building the per-face remap can be multi-minute run time.

## Contributing

Issues and PRs welcome — there's no formal process. The script is shipped working but not finished; feedback from real captures is the most useful kind.


## Authors

- **Mike Heath** ([@LaunchedPix](https://github.com/LaunchedPix))
- **Alex Gee** ([@Macgregor](https://github.com/Macgregor))

## License

MIT — see [LICENSE](LICENSE).
