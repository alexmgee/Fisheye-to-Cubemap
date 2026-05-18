# Fisheye-to-Cubemap

Convert calibrated fisheye and unstitched 360-camera images into pinhole
images for Structure-from-Motion alignment, and optionally package a Metashape
alignment as a training-ready COLMAP scene for 3D Gaussian Splatting workflows.

> **Status:** Active working release. The original cubeface converter has been
> used on real captures. The newer COLMAP export workflow now supports mixed
> fisheye, frame, and equirectangular Metashape alignments, but should still be
> validated on your own scenes before production training.

## Live Demo

**[Open the browser demo](https://alexmgee.github.io/Fisheye-to-Cubemap/)**

Drop in a fisheye image and optional mask to see how the projection unfolds
into the five pinhole cube faces consumed by an SfM or 3DGS pipeline.

## What This Project Does

This project has two related workflows.

### 1. Cubeface Generation

Use this when you want to convert fisheye images into pinhole cube faces before
alignment.

```text
fisheye images + masks
  -> cubeface images + masks
  -> Metashape / COLMAP / other SfM alignment
  -> training / viewing
```

The converter reads a Metashape-format fisheye calibration, converts each
source pixel to a ray direction, and reprojects those rays onto five virtual
pinhole planes. The result is a set of conventional pinhole images that normal
SfM and 3DGS tools can consume more easily than raw fisheye imagery.

The original script writes layouts designed for Metashape:

- **Station** layout for Metashape Standard camera-station workflows.
- **Rig** layout for Metashape Pro rig workflows.

### 2. COLMAP Export From A Metashape Alignment

Use this when Metashape has already aligned the original images and you want a
COLMAP-style scene for downstream training tools.

```text
raw fisheye / frame / equirectangular images
  -> Metashape alignment
  -> cameras.xml + sparse point cloud .ply
  -> GUI COLMAP export
  -> internal reprojection / splitting / packaging
  -> training-ready COLMAP text scene
```

This is useful because Metashape can align camera models that many
COLMAP-style trainers do not handle directly, such as equisolid or equidistant
fisheye cameras. The exporter keeps Metashape's alignment as the pose source,
then writes an all-pinhole COLMAP output scene.

All final cameras written to `cameras.txt` are `PINHOLE`.

## Current COLMAP Export Workflow

The COLMAP export GUI is now driven by the Metashape `cameras.xml` file.
Instead of hardcoded Lens A / Lens B slots, the GUI discovers the sensors in
the XML and builds a card for each sensor.

Each sensor card owns its own:

- image directories,
- mask directories,
- projection-specific processing options,
- output width behavior,
- routing or split mode,
- filename matching status.

There is no active "Body" layer in the current GUI. The sensor label from
Metashape is shown directly on the sensor card and is treated as the calibration
and configuration boundary.

### Supported Sensor Types

| Sensor type | GUI/exporter behavior | COLMAP output |
| --- | --- | --- |
| Fisheye / equidistant / equisolid | Can be split into multiple pinhole cubefaces or reprojected into one adaptive pinhole image. Supports image dirs, mask dirs, lens-only masks, width auto, and Fourier correction terms when present. | `PINHOLE` images and masks |
| Frame / pinhole | Packaged as aligned frame-camera imagery from the Metashape scene. Useful for drones, DSLR images, phone images, and other normal cameras. | `PINHOLE` images and masks |
| Equirectangular | Can be split into cubemap-style pinhole views, with a width setting for generated faces. | `PINHOLE` images and masks |

### Fisheye Processing Paths

Fisheye sensors have two output paths.

| Path | GUI meaning | Output | Best use |
| --- | --- | --- | --- |
| Multi-pinhole | Checkbox enabled | Multiple cubeface pinhole images per source fisheye image | Very wide fisheye views where one pinhole image would stretch too much |
| Single-pinhole | Checkbox disabled | One adaptive forward pinhole image per source fisheye image | Narrower or cropped fisheye views where a single pinhole image is acceptable |

Multi-pinhole remains the safer default for very wide lenses. Single-pinhole is
intended for cases where a fisheye source covers a more limited useful field of
view and where avoiding a 5x image expansion is valuable.

### Width `0` Means Auto

The GUI and exporter now treat width `0`, empty width, or missing width as
"auto" for generated pinhole outputs.

For fisheye multi-pinhole splitting, auto width is based on the useful central
angular pixel resolution. The intent is to avoid under-sampling the pinhole
faces while also avoiding arbitrary giant output sizes.

The exporter also hardens this path:

- auto widths must resolve to positive dimensions,
- literal zero is not allowed to reach image generation,
- literal zero is not allowed to reach COLMAP intrinsics,
- width changes invalidate existing processed outputs.

### Processing Stamps

Generated outputs are protected by small processing stamp sidecars. These
record only parameters that affect the generated files, such as calibration
digest, face width, output format, mask inputs, and correction state.

On rerun, existing outputs are skipped only when the stamp matches the current
job. If files exist without a matching stamp, the exporter treats them
cautiously and reprocesses instead of silently trusting stale images.

This matters for calibration experiments: a corrected run should not reuse
uncorrected output files just because the filenames happen to match.

## Fourier Additional Corrections

Metashape's **Fit additional corrections** option can export a
`<corrections type="fourier">` block containing 96 coefficients. These model
fine-grained distortion beyond the normal Brown radial/tangential parameters.

This project now supports those correction terms.

When corrections are present:

- the XML parser detects and preserves the correction block,
- the GUI indicates that the sensor has Fourier corrections,
- corrected rays are used during fisheye remapping,
- correction state participates in cache and processing-stamp invalidation,
- corrected and uncorrected runs cannot safely reuse each other's outputs.

Brown parameters and Fourier corrections are co-optimized by Metashape. A
calibration exported with corrections enabled is not the same camera model as a
calibration exported with corrections disabled. If you want to compare Fit vs
NoFit, export both a fresh `cameras.xml` and a matching sparse point cloud from
the corresponding Metashape alignment.

Known validation status:

- Unit and integration tests verify that corrections flow through parsing,
  routing, remapping, stamping, and COLMAP export.
- Real Fit / NoFit comparison runs have produced clean all-pinhole COLMAP
  outputs.
- A pixel-for-pixel validation against Metashape's own corrected projection is
  still a useful future ground-truth check.

## Inputs For COLMAP Export Mode

For COLMAP export mode, prepare:

```text
cameras.xml
pointcloud.ply
source image directories for each discovered sensor
source mask directories for each discovered sensor, when available
optional lens-only masks for fisheye sensors
output folder
```

The `cameras.xml` and `pointcloud.ply` should come from the same Metashape
alignment state. If you re-optimize cameras, change Fit additional corrections,
or otherwise change the alignment, export both files again so camera parameters,
poses, and points stay coherent.

In Metashape:

1. Import and prepare the original source images.
2. Set the correct projection type for each image source.
3. Align, check, and refine the Metashape scene.
4. Export `cameras.xml`.
5. Export sparse point cloud `.ply`.
6. Use the GUI's COLMAP export mode to assign image and mask directories to the
   discovered sensors.

Good camera labels and stable source filenames make matching safer and easier
to validate.

## Calibration Requirement

The cubeface converter expects Metashape-format fisheye calibration XML.

It uses Metashape's lens model and parameter ordering:

```text
f, cx, cy, K1, K2, K3, P1, P2
```

Do not paste OpenCV, COLMAP, or RealityScan calibration coefficients into a
Metashape XML just because field names look similar. Different tools may use
different projection equations and coefficient conventions.

For best results, calibration should eventually be treated as a stable
camera/lens profile:

- one profile per physical lens/camera/settings combination,
- created from a controlled calibration capture,
- reused with fixed intrinsics for normal production datasets,
- validated and versioned.

That calibration-profile workflow is a planned project direction and should be
designed carefully with the cubeface script's original calibration assumptions
in mind.

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

## Running The GUI

From the `gui/` folder:

```bash
python gui.py
```

On Windows you can also double-click:

```text
gui/GUI_v4.vbs
```

The GUI is the recommended way to use the current project because it exposes
both output purposes and the mapping checks.

More detailed GUI instructions live in:

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

For calibrations with Fourier corrections, use the corrected wrapper:

```bash
python AM_ImageAndMask_to_cubemap_v4_corrected.py ^
  --amlenscal lens_calibration_with_corrections.xml ^
  --lenslabel "Osmo360-front" ^
  --directoryfisheyeimages front/images ^
  --directoryfisheyemasks front/masks ^
  --facewidth 2100 ^
  --outputdir output
```

The original `AM_ImageAndMask_to_cubemap_v4.py` script remains available as the
stable unmodified cubeface entry point.

## Cubeface CLI Options

Required:

| Flag | Description |
| --- | --- |
| `--amlenscal` | Metashape-exported fisheye calibration XML. |
| `--lenslabel` | Label used for output folders and filenames. |
| `--directoryfisheyeimages` | Source images for one fisheye lens. |
| `--facewidth` | Cubeface width and height in pixels. |
| `--outputdir` | Output directory. |

Support source options:

| Flag | Description |
| --- | --- |
| `--directoryfisheyemasks` | Per-image masks. Highest priority. May be partial. |
| `--lensonlymask` | One mask for the lens. Used as fallback. |
| `--maxusefulfov` | Manual FOV fallback when no masks exist. |

Output options:

| Flag | Description |
| --- | --- |
| `--rigstructure` | Use Metashape Pro rig-style output. |
| `--outputformat {png,tiff,jpg}` | Color cubeface image format. Masks remain PNG. |
| `--force` | Reprocess even when outputs already exist. |

## Testing

Run the public test suite from the repository root:

```bash
python -m pytest -q
```

The suite covers the main parsing, routing, width-resolution, correction,
stamping, and exporter paths. Real-scene validation is still recommended before
trusting a new camera setup or training pipeline.

## Limitations

- The math is not formally proven for all camera models and capture conditions.
- Equisolid fisheye is the primary exercised path.
- Equidistant support exists but needs more field validation.
- Fourier correction support is implemented and tested, but still deserves a
  ground-truth comparison against Metashape's own corrected projection.
- COLMAP export depends on Metashape alignment quality.
- Scene scale diagnostics and optional normalization are packaging aids, not a
  substitute for sound calibration or survey-scale control.
- The project writes all-pinhole COLMAP scenes. Native fisheye 3DGS training is
  an adjacent research direction, not the current output format.

## Project Status

This project began as a cubeface converter and is expanding into a practical
bridge between Metashape fisheye alignment and COLMAP-style training scenes.

Recent work includes:

- cameras.xml-driven sensor discovery,
- v2 top-level sensor cards with no Body UI,
- multi-directory image and mask assignment per sensor,
- fisheye multi-pinhole and adaptive single-pinhole paths,
- equirectangular splitting,
- frame-camera packaging,
- width `0` auto behavior with exporter-side hardening,
- processing stamps for stale-output protection,
- Fourier correction parsing and corrected-ray remapping,
- mixed-sensor regression coverage.

Near-term priorities:

- continue field-testing Fit vs NoFit correction behavior,
- validate Fourier correction math against a Metashape ground-truth fixture,
- harden documentation around real capture workflows,
- design a robust calibration profile workflow.

## Authors

- Mike Heath (LaunchedPix)
- Alex Gee (Macgregor)

## License

MIT. See [LICENSE](LICENSE).
