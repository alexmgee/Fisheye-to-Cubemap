# Fisheye-to-Cubemap

Convert calibrated fisheye and unstitched 360-camera imagery into pinhole cubefaces, and optionally package a Metashape alignment as a training-ready COLMAP scene for 3D Gaussian Splatting workflows. Detailed COLMAP export instructions live in [gui/README.md](gui/README.md) and [gui/Instructions.md](gui/Instructions.md).

> **Status:** Active working release. The cubeface converter has been used on real captures, and the COLMAP export workflow is being hardened through field testing. Use at your own risk and validate outputs before production training.

## Why This Exists

Standard SfM pipelines tend to assume or favor pinhole data input. Wide-angle fisheye and dual-lens fisheye 360 captures have become popular for their ability to rapidly see more of a scene, but come with the tradeoff of decreased feature quality and fewer/weaker integrations across SfM and 3DGS pipelines. The usual workaround of converting fisheyes to a single equirectangular image leaves you with a projection which pinhole aligners still struggle with. Equirectangular image stitching also unavoidably compromises the accuracy of the scene geometry.

This script takes a different path: read the lens calibration, convert each fisheye pixel to a ray direction, and reproject those rays onto 5 faces of a virtual cube. Each face is a clean pinhole image that any SfM tool can align without special handling. After alignment, the cube faces and their masks feed directly into 3D Gaussian Splatting training.



The script is also useful outside 3DGS, anywhere a pinhole alignment workflow needs to ingest fisheye data.

## **[Live Demo](https://alexmgee.github.io/Fisheye-to-Cubemap/)**


Drop in your own fisheye image and optional mask to see how the projection
unfolds into the five pinhole cube faces consumed by the SfM pipeline.


## COLMAP Export

The cubeface workflow is useful when you want to create cubefaces first and then align those cubefaces in Metashape, COLMAP, RealityScan, or another SfM tool.

There is another common workflow: align the original fisheye images directly in Metashape first. This can be a better fit for equisolid fisheye cameras because Metashape understands that lens model, while many COLMAP-style training tools expect pinhole images and COLMAP text files.

The COLMAP export feature bridges those two worlds. It reads a Metashape alignment of the original cameras, generates cubefaces internally, assigns each generated cubeface the correct composed pinhole pose, optionally includes additional aligned frame-camera images, and writes a training-ready COLMAP scene.

Detailed COLMAP export instructions live in [gui/README.md](gui/README.md) and [gui/Instructions.md](gui/Instructions.md).



## Workflows

### 1. Generate Cubefaces for Metashape Alignment

Use this when cubefaces are the product you want to import into Metashape or another SfM tool.

```text
fisheye images + masks
  -> cubeface images + masks
  -> Metashape / SfM alignment
  -> training / viewing
```

The converter writes layouts designed for Metashape:

- **Station** layout for Metashape Standard camera-station workflows.
- **Rig** layout for Metashape Pro rig workflows.

### Where This Fits In A 3DGS Pipeline

```text
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

### 2. Generate a COLMAP Scene from a Metashape Alignment

Use this when Metashape has already aligned the original fisheye images and you want a COLMAP-style scene for training tools.


```text
raw fisheye images aligned in Metashape
  -> Metashape cameras.xml + sparse cloud .ply
  -> internal cubeface generation
  -> clean COLMAP scene
  -> 3DGS training / viewing
```

This is useful because Metashape can align equisolid fisheye cameras using its own lens model, while many COLMAP-style trainers expect pinhole images and COLMAP text files.

The exporter composes each aligned fisheye camera pose with the fixed cubeface rotations and writes final pinhole camera entries.

It can also include aligned frame cameras such as:

- drone images,
- DSLR images,
- phone images,
- other normal frame-camera image sets.

The CLI calls these extra non-fisheye sets **passthrough media** because they do not get converted into cubefaces. They still pass through the exporter, where they can be packaged, undistorted, and masked for the final COLMAP scene.

### Where This Fits In A 3DGS Pipeline

```text
fisheye / 360 capture
        |
        v
Metashape alignment of original cameras
        |
        v
cameras.xml + sparse cloud .ply
        |
        v
GUI COLMAP export                       <-- you are here
        |
        v
internal cubeface generation + pose mapping
        |
        v
training-ready COLMAP scene
        |
        v
3D Gaussian Splatting training / viewers
```

All final cameras written to `cameras.txt` are `PINHOLE`. 


## Calibration Requirement

The cubeface converter expects Metashape-format fisheye calibration XML.

It uses Metashape's lens model and parameter ordering:

```text
f, cx, cy, K1, K2, K3, P1, P2
```

Do not paste OpenCV, COLMAP, or RealityScan calibration coefficients into a Metashape XML just because field names look similar. Different tools may use different projection equations and coefficient conventions.

For best results, calibration should eventually be treated as a stable camera/lens profile:

- one profile per physical lens/camera/settings combination,
- created from a controlled calibration capture,
- reused with fixed intrinsics for normal production datasets,
- validated and versioned.

That calibration-profile workflow is a planned project direction and should be designed carefully with the cubeface script's original calibration assumptions in mind.


## Installation

Python 3.10+ is recommended.

```bash
git clone <repo-url>
cd Fisheye-to-Cubemap
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r gui/requirements.txt
```

On macOS/Linux:

```bash
source .venv/bin/activate
```

## Running the GUI

From the `gui/` folder:

```bash
python gui.py
```

On Windows you can also double-click:

```text
gui/GUI_v4.vbs
```

The GUI is the recommended way to use the current project because it exposes both output purposes and the mapping checks.

Detailed GUI instructions:

```text
gui/Instructions.md
```

## Cubeface CLI Quick Start

The original cubeface converter can still be run directly:

```bash
python AM_ImageAndMask_to_cubemap_v4.py ^
  --amlenscal lens_calibration.xml ^
  --lenslabel "Osmo360-front" ^
  --directoryfisheyeimages front/images ^
  --directoryfisheyemasks front/masks ^
  --facewidth 2100 ^
  --outputdir output
```

Use `\` instead of `^` for shell line continuation on macOS/Linux.

## Cubeface CLI Options

Required:

| Flag | Description |
|---|---|
| `--amlenscal` | Metashape-exported fisheye calibration XML. |
| `--lenslabel` | Label used for output folders and filenames. |
| `--directoryfisheyeimages` | Source images for one fisheye lens. |
| `--facewidth` | Cubeface width and height in pixels. |
| `--outputdir` | Output directory. |

Support source options:

| Flag | Description |
|---|---|
| `--directoryfisheyemasks` | Per-image masks. Highest priority. May be partial. |
| `--lensonlymask` | One mask for the lens. Used as fallback. |
| `--maxusefulfov` | Manual FOV fallback when no masks exist. |

Output options:

| Flag | Description |
|---|---|
| `--rigstructure` | Use Metashape Pro rig-style output. |
| `--outputformat {png,tiff,jpg}` | Color cubeface image format. Masks remain PNG. |
| `--force` | Reprocess even when outputs already exist. |

## Metashape Export Inputs for COLMAP Mode

For COLMAP export mode, prepare:

```text
cameras.xml
pointcloud.ply
fisheye lens calibration XMLs
source fisheye images/masks by lens
optional additional frame-camera image sets
```

In the GUI, these are presented as additional frame-camera media sets.

The GUI docs cover the full COLMAP export setup, including mapping, output layout, and processing/report folders: [gui/README.md](gui/README.md) and [gui/Instructions.md](gui/Instructions.md).

In Metashape:

1. Align the original cameras.
2. Export camera XML.
3. Export sparse cloud PLY.
4. Keep camera labels aligned with source filenames where possible.

Good camera labels make mapping safer and easier to validate.

## Limitations

- The math is not formally proven for all camera models and capture conditions.
- Equisolid fisheye is the primary exercised path.
- Equidistant support exists but needs more validation.
- The original cubeface converter does not estimate camera extrinsics.
- COLMAP export depends on Metashape alignment quality.
- Calibration stability is a known area for future profile-based workflow design.
- Scene scale normalization is planned but not yet implemented.

## Project Status

This project began as a cubeface converter and is expanding into a practical bridge between Metashape fisheye alignment and COLMAP-style training scenes.

Near-term priorities:

- harden mapping checks,
- keep COLMAP output clean and navigable,
- improve documentation,
- add scene scale diagnostics and optional normalization,
- design a robust calibration profile workflow.

## Authors

- Mike Heath (LaunchedPix)
- Alex Gee (Macgregor)

## License

MIT. See [LICENSE](LICENSE).
