# Plan: Metashape-free calibration input for Fisheye-to-Cubemap

Date: 2026-05-30

## Branch intention and scope

This document describes the plan behind the `multi-format` branch.

The branch is meant to turn Metashape XML from a hard requirement into one calibration provider among several. It is not meant to make calibration formats interchangeable by name, and it is not meant to accept arbitrary metadata as trusted calibration.

Scope for this branch:

- Preserve the existing Metashape XML workflow and legacy `--amlenscal` flag.
- Add a provider layer that loads different calibration sources through explicit model-specific importers.
- Normalize supported providers into source image geometry plus per-pixel rays.
- Add a project-native `raymap` interchange format.
- Add OpenCV fisheye and COLMAP camera-file support as early free-format providers.
- Plan RealityScan/RealityCapture XMP support, but wait for real exported fixtures before implementing model math.
- Keep generic metadata probing diagnostic-only until a source is proven complete and model-specific.
- Defer broad GUI changes until CLI behavior and provider summaries are stable.

Out of scope for this branch:

- Copying RealityScan, OpenCV, COLMAP, or vendor coefficients into fake Metashape XML files.
- Treating EXIF/MakerNote tags as complete calibration without a documented model.
- Silently using a calibration against resized, cropped, undistorted, stabilized, or wrong-lens images.

## Executive summary

Yes, this project can be made useful to people who do not own Agisoft Metashape. The right way to do it is not to pretend that OpenCV, COLMAP, RealityScan/RealityCapture, DJI, Lensfun, or EXIF coefficients are interchangeable with Metashape coefficients. The right way is to make Metashape only one calibration provider among several, with every provider normalized into the same internal object: a per-pixel camera ray field plus image dimensions and lens support.

The current script already wants that architecture. Its durable center is:

1. read calibration,
2. turn each source pixel into a unit ray,
3. use those rays to resample into five pinhole cube faces,
4. carry masks through the same remap.

Only step 1 is Metashape-specific. The free-format work should focus there.

The metadata claim in the Discord screenshot is only partly viable. We can absolutely run a metadata probing pass that searches EXIF/XMP/MakerNotes/QuickTime/DJI protobuf tracks for intrinsics-ish fields. But we should treat that as a discovery and diagnostics feature, not as a reliable calibration source. Metadata commonly contains focal length and sometimes calibrated optical center; DJI metadata can expose `CalibratedFocalLength`, `CalibratedOpticalCenterX`, `CalibratedOpticalCenterY`, and `DewarpData`, and ExifTool has current support for DJI Osmo 360 protobuf metadata. That still does not mean the data exposes a complete, documented, per-lens fisheye distortion model in the same coordinate system this script needs.

Recommended product path:

1. Add a calibration-provider layer.
2. Keep Metashape XML as provider `metashape`.
3. Add first-class free providers for OpenCV fisheye YAML/JSON, COLMAP `cameras.txt` / `sparse` models, and RealityScan/RealityCapture XMP sidecars.
4. Add a native, explicit `raymap` format as the project-owned interchange format.
5. Add a metadata probe that can prefill guesses and write a report, but requires validation before conversion.
6. Add an OpenCV/ChArUco calibration helper later, so users can create a calibration without Metashape or RealityScan.

## Implementation snapshot

As of 2026-05-31 on branch `multi-format`, the first implementation stages are underway:

| Area | Status | Notes |
|---|---|---|
| Provider interface | Implemented, evolving | `Calibration` and `CalibrationSourceGeometry` dataclasses exist in `calibration/core.py`. A separate model registry is still planned. |
| Metashape provider | Implemented and partially validated | Legacy `--amlenscal` and new `--calibration --calibration-provider metashape` both route through the provider layer. |
| Provider summary artifact | Implemented | Runs write `bonusdata/calibration_provider_summary.json`. |
| Raymap provider | Implemented and partially validated | `.npz` raymap export/import works; real front-lens validation passed at `facewidth 256`. |
| OpenCV fisheye provider | Implemented and partially validated | JSON plus OpenCV FileStorage YAML/XML load paths work. Synthetic project/unproject test with nonzero distortion passed. Real fixture validation is still needed. |
| COLMAP provider | Implemented and partially validated | First wave supports `OPENCV_FISHEYE`, `SIMPLE_FISHEYE`, `FISHEYE`, `SIMPLE_RADIAL_FISHEYE`, `RADIAL_FISHEYE`, `PINHOLE`, and `SIMPLE_PINHOLE`. Real fixture and optional pycolmap validation are still needed. |
| RealityScan XMP provider | Planned | Should wait for real XMP fixtures and export settings. |
| Metadata probe | Planned | Diagnostic-only; should not silently produce calibration. |
| GUI updates | Planned | Wait until CLI provider semantics stabilize. |

Current validation notes are tracked in [`docs/multi-format-progress.md`](multi-format-progress.md).

## Current repo findings

The public README currently states a hard requirement:

- The script is locked to Agisoft Metashape's lens model and parameter ordering.
- It reads `f, cx, cy, K1, K2, K3, P1, P2`.
- It warns users not to copy similarly named coefficients from OpenCV, COLMAP, RealityScan, etc.

That warning is correct and should stay, but it should be reframed once we add provider-specific importers: users must not paste foreign parameters into a Metashape XML, but they can pass a foreign calibration file through the correct provider.

Code paths that are Metashape-specific today:

- `get_metashape_calibration_data()` parses only a Metashape XML shape with `projection`, `width`, `height`, `f`, `cx`, `cy`, `k1`, `k2`, `k3`, `p1`, `p2`, and `date`.
- `main()` hard-wires `--amlenscal` and converts the parsed XML into the tuple `(f, cx, cy, k1, k2, k3, 0.0, p1, p2, 0.0, 0.0)`.
- `main()` accepts only `equidistant_fisheye` and `equisolid_fisheye`.
- `compute_rays()` assumes one shared Metashape-ish parameter tuple and then applies the project-specific projection branch.
- `undistort_points()` currently inverts a Brown-style radial/tangential model before the equidistant/equisolid ray conversion.

Code paths that are already format-neutral:

- `compute_metashape_rays_usefulpixmap()` is poorly named for a future multi-provider system, but it mainly consumes `width`, `height`, `params`, `model`, masks, and output paths, then writes the raw ray/solid-angle diagnostic data.
- `compute_image2cubeface_remapping()` consumes an already-built `rays` array and a `useful_pixel_mask`; it does not care whether the rays came from Metashape, OpenCV, COLMAP, RealityScan XMP, or a native raymap file.
- The output image/mask processing and remap cache logic is mostly independent of the source calibration format, although the cache key must include provider/model details.

## Plan review: areas to strengthen

The plan is viable, but several areas need more explicit guardrails before implementation.

### 1. Add an input-image geometry contract

Every calibration provider must declare exactly what pixel geometry its calibration describes:

- original distorted source image;
- vendor-dewarped source image;
- RealityScan/Metashape/COLMAP undistorted export;
- resized image;
- cropped image;
- rotated image;
- stabilized/video-dewarped frame;
- one lens from a dual-fisheye capture.

The current plan mentions this mostly for RealityScan XMP and metadata, but it should be global. Most calibration failures will not be caused by parser bugs; they will come from using valid coefficients against the wrong pixel geometry.

Recommended addition:

```text
Calibration.source_geometry:
  image_width
  image_height
  pixel_center_convention
  source_image_state: original_distorted | undistorted | dewarped | cropped | resized | unknown
  rotation_applied_deg
  crop_rect
  scale_from_calibration
  lens_side
  warnings
```

Provider output should refuse `unknown` geometry unless the user explicitly asks for an experimental run.

### 2. Make provider summaries first-class artifacts

Every run should write a provider summary into `bonusdata/`, for example:

```text
calibration_provider_summary.json
```

This should include:

- provider name and version;
- source file path/hash;
- parsed model name;
- dimensions used;
- raw parameter vector;
- normalized/internal parameter object;
- warnings;
- fields ignored;
- validation scores;
- cache key inputs.

This makes Discord/debug support much easier. If a user says "it seems to look right," the project can ask for the summary instead of guessing.

### 3. Separate "provider import" from "provider trust"

The code should be able to parse a calibration before it is willing to use it. A good provider has at least two operations:

```python
parse() -> CalibrationCandidate
validate(candidate, images) -> ValidationReport
rays(candidate) -> np.ndarray
```

That separation is especially important for RealityScan XMP, metadata, Lensfun, and future AliceVision/Meshroom support.

### 4. Treat raymap size and precision as product concerns

A dense raymap for 3840 x 3840 images is not tiny:

- `rays float32[H,W,3]`: about 177 MB;
- optional `solid_angle float32[H,W]`: about 59 MB;
- plus metadata and compression overhead.

This is still acceptable for a trustworthy interchange format, but the plan should document it. Use compressed `.npz` by default, allow uncompressed output for speed, and consider optional `float16` only after measuring angular error. The default should stay `float32`.

### 5. Add a reference-oracle strategy

For every imported model, use the native tool as an oracle when possible:

- OpenCV provider: cross-check against `cv2.fisheye.undistortPoints` / OpenCV projection.
- COLMAP provider: cross-check against `pycolmap.Camera.cam_from_img()` and `img_from_cam()`.
- RealityScan XMP provider: cross-check against Epic's documented XMP equations and fixtures exported by RealityScan.
- Metashape provider: retain current example outputs and, where possible, compare against Metashape `camera.unproject` on a small set of sampled pixels.

This makes the project less dependent on hand-transcribed equations.

### 6. Explicitly fail closed on unsupported model labels

The provider system should never silently coerce an unknown fisheye model into the nearest supported one. Unknown model labels should produce:

- parsed fields;
- a clear "unsupported model" error;
- the closest planned provider phase;
- a request for sample data if helpful.

This is especially important for RealityScan, AliceVision/Meshroom, Kalibr, mrcal, and vendor metadata.

## Research notes

### Metashape

Agisoft's support documentation defines calibration parameters such as `f`, `cx`, `cy`, `b1`, `b2`, `k1-k4`, and `p1-p2`, and points users to the Metashape manual appendix for the formulas. Agisoft support also confirms that Metashape's frame/fisheye camera models are documented in the manual appendix and can be externally checked against `camera.project` / `camera.unproject`.

Relevant sources:

- [Agisoft support: calibration parameter meanings](https://agisoft.freshdesk.com/support/solutions/articles/31000158119-what-does-camera-calibration-results-mean-in-metashape-)
- [Agisoft forum: exported camera parameters and manual appendix](https://www.agisoft.com/forum/index.php?topic=16207.0)
- [Agisoft forum: projection formula discussion](https://www.agisoft.com/forum/index.php?topic=6437.0)

Implication: Metashape is a real, coherent model. It should remain supported exactly as-is, but we should not make it the interchange format for other tools.

### OpenCV

OpenCV has two relevant calibration families:

- Standard `calibrateCamera`: radial/tangential coefficients in order `(k1, k2, p1, p2, k3)`.
- `cv::fisheye`: fisheye model using `theta_d = theta * (1 + k1 theta^2 + k2 theta^4 + k3 theta^6 + k4 theta^8)`, then maps that distorted angular radius into pixel coordinates.

OpenCV's tutorial explicitly notes that calibration outputs a camera matrix and distortion coefficients, that object patterns include chessboard, ChArUco, symmetric circle, and asymmetric circle grids, and that practical calibration needs many varied snapshots.

Relevant sources:

- [OpenCV camera calibration tutorial](https://docs.opencv.org/4.x/d4/d94/tutorial_camera_calibration.html)
- [OpenCV fisheye camera model](https://docs.opencv.org/4.x/db/d58/group__calib3d__fisheye.html)
- [OpenCV ArUco/ChArUco calibration documentation](https://docs.opencv.org/4.x/da/d13/tutorial_aruco_calibration.html)

Implication: OpenCV fisheye YAML/JSON is a strong first free-format target because the repo already depends on OpenCV. We do not need to translate OpenCV coefficients into Metashape coefficients; we can directly unproject OpenCV pixels into rays using OpenCV's own model equations or `cv2.fisheye.undistortPoints`.

### COLMAP

COLMAP supports multiple camera models. Its docs recommend fisheye-specific camera models for fisheye lenses and point users to `cameras.txt` for exported intrinsic parameters. The output format stores cameras as:

```text
CAMERA_ID MODEL WIDTH HEIGHT PARAMS[]
```

COLMAP's model header defines the exact parameter order. Important models for this repo include:

- `OPENCV_FISHEYE`: `fx, fy, cx, cy, k1, k2, k3, k4`
- `SIMPLE_RADIAL_FISHEYE`: `f, cx, cy, k`
- `RADIAL_FISHEYE`: `f, cx, cy, k1, k2`
- `SIMPLE_FISHEYE`: `f, cx, cy`
- `FISHEYE`: `fx, fy, cx, cy`
- `THIN_PRISM_FISHEYE` and `RAD_TAN_THIN_PRISM_FISHEYE` for more complex cases
- `PINHOLE` / `SIMPLE_PINHOLE` for already-undistorted source imagery

Relevant sources:

- [COLMAP camera model guidance](https://colmap.github.io/cameras.html)
- [COLMAP sparse text output format](https://colmap.github.io/format.html)
- [COLMAP rig and frame output format](https://colmap.github.io/format.html#rigs-txt)
- [pycolmap API documentation](https://colmap.github.io/pycolmap/pycolmap.html)
- [COLMAP camera model parameter definitions](https://raw.githubusercontent.com/colmap/colmap/main/src/colmap/sensor/models.h)

Implication: COLMAP is the strongest "free SfM" input target, especially because LichtFeld users care about COLMAP conversion. The first implementation should support `OPENCV_FISHEYE`, `SIMPLE_FISHEYE`, `FISHEYE`, `SIMPLE_RADIAL_FISHEYE`, and `RADIAL_FISHEYE`. More exotic COLMAP models can come after the interface is stable. pycolmap should be treated as an optional validation oracle, not as a required runtime dependency.

### RealityScan / RealityCapture XMP

RealityScan/RealityCapture XMP is worth treating as a real calibration source, not merely as generic file metadata. RealityScan's help describes XMP sidecars as files that can replace image EXIF information or re-use an alignment configuration. If an XMP file is placed beside an image with the same stem, RealityScan treats them as paired. The documented sample XMP includes fields such as:

- `xcr:DistortionModel`
- `xcr:DistortionCoeficients`
- `xcr:FocalLength35mm`
- `xcr:Skew`
- `xcr:AspectRatio`
- `xcr:PrincipalPointU`
- `xcr:PrincipalPointV`
- `xcr:CalibrationGroup`
- `xcr:DistortionGroup`
- `xcr:Rotation`
- `xcr:Position`

RealityScan's export dialog also has undistortion settings. This matters a lot: an exported XMP may describe original distorted images, or it may describe an undistorted export depending on the export settings. If the XMP corresponds to undistorted images, then using it against the original fisheye pixels would be wrong.

Epic's RealityCapture camera-math article documents the coordinate convention and model families at a useful level:

- image-plane coordinates are normalized in a roughly `[-0.5, 0.5]` coordinate space;
- pixel conversion uses `scale = max(image width, image height)`, then adds half the image width/height;
- focal length is stored as 35 mm equivalent and converted using sensor/image width;
- the calibration matrix includes focal, skew, aspect ratio, and principal point;
- Brown model distortion coefficients are stored in `xcr:DistortionCoeficients` as `k1 k2 k3 k4 t1 t2`;
- Brown3 or non-tangential variants keep the same order and replace unused entries with zero;
- the division model is applied in image coordinates and has different inverse behavior.

RealityScan's distortion-model settings documentation lists these model families:

- `No lens distortion`;
- `Division`;
- `Brown3`;
- `Brown3 with tangential2`;
- `Brown4 with tangential2`;
- `K + Brown3 with tangential2`;
- `K + Brown4 with tangential2`.

That list is a useful implementation boundary. The importer should support only documented labels, and it should preserve the exact label in the provider summary. `K + ...` models should not be implemented by guessing from Brown3/Brown4 alone; they need fixture-based confirmation because the leading `K` term changes the parameter interpretation.

Relevant sources:

- [RealityScan XMP metadata help](https://rshelp.capturingreality.com/en-US/tools/xmpalign.htm)
- [RealityCapture XMP camera math](https://dev.epicgames.com/community/learning/knowledge-base/vzwB/realityscan-realitycapture-xmp-camera-math)
- [RealityScan distortion model settings](https://rshelp.capturingreality.com/en-US/appbasics/settings_distortion_models.htm)
- [Faceform Wrap notes on RealityCapture XMP/CSV/FBX camera import](https://docs.faceform.com/Wrap/ImportCamerasToWrap/ImportCamerasFromRC/ImportCamerasFromRC.html)

Implication: RealityScan XMP is a viable provider, especially for users who already align fisheye images in RealityScan and want to reuse its optimized calibration for cube-face generation. It should be implemented as `realityscan_xmp` or `realitycapture_xmp`, not as a generic metadata parser. The importer must preserve the XMP model semantics, require image dimensions when they are absent, and warn if the XMP appears to have been exported for undistorted images.

Conservative scope for first support:

1. Parse one XMP file or a directory of sidecars.
2. Require paired source images or explicit `--width` / `--height` when dimensions are not present.
3. Support documented `division` and Brown-family models first.
4. Ignore extrinsics for cube-face generation at first, but preserve them in parsed metadata for future rig/COLMAP export work.
5. Require all images in one lens batch to share the same calibration/distortion group unless the user explicitly allows per-image intrinsics.
6. Add diagnostics showing which XMP fields were consumed and which were ignored.

Open questions before implementation:

- Which RealityScan export modes produce XMP describing original distorted fisheye images versus undistorted images?
- Does the XMP exported from fisheye alignments always use `division` / Brown models, or can it contain additional fisheye-specific labels not visible in the sample docs?
- For dual-fisheye cameras, does RealityScan create reliable separate calibration/distortion groups per lens side, and how are those groups identified in user exports?
- How stable are `FocalLength35mm` and principal point values across image downscales and RealityScan export settings?

### Kalibr and other calibration tools

Kalibr supports projection models such as `pinhole`, `omni`, `double sphere`, and `extended unified`, with distortion models including `radtan`, `equidistant`, `fov`, and `none`.

Relevant source:

- [Kalibr supported models](https://github.com/ethz-asl/kalibr/wiki/supported-models)

Implication: Kalibr is valuable for advanced users, but it introduces model families beyond the current script. It is a good second-wave importer, not the first minimum viable free path.

### mrcal, BabelCalib, and other calibration ecosystems

mrcal is a free/open calibration and projection toolkit that documents a broad set of lens models, including OpenCV-style fisheye/equidistant models and splined models intended for high-accuracy calibration. BabelCalib is a research/project ecosystem aimed at converting camera calibration models, including central and non-central models.

Relevant sources:

- [mrcal lens model documentation](https://mrcal.secretsauce.net/lensmodels.html)
- [BabelCalib project](https://github.com/ylochman/babelcalib)

Implication: these are useful knowledge sources and possible future import/export targets, but not MVP targets. They strengthen the core argument for a raymap format: once a tool can produce per-pixel rays, the repo does not need to understand every calibration coefficient family directly.

### AliceVision / Meshroom

AliceVision/Meshroom uses `sfmData` files for structure-from-motion data, including intrinsic camera model information. It supports camera intrinsic models beyond basic pinhole/radial forms. For users already in Meshroom, an `sfmData` importer could eventually play a role similar to the COLMAP importer.

Relevant sources:

- [AliceVision sfmData format](https://alicevision.readthedocs.io/en/latest/md__home_docs_checkouts_readthedocs_8org_user_builds_alicevision_checkouts_latest_src_aliceVision_sfmData_README.html)
- [AliceVision camera models source/documentation entry point](https://github.com/alicevision/AliceVision)

Implication: AliceVision/Meshroom is worth listing as a later-stage provider, but it should not distract from OpenCV/raymap/RealityScan/COLMAP. The safer near-term route is still to support direct raymap export from external tools or scripts.

### Lensfun and Hugin

Lensfun stores lens distortion calibration data and can interpolate distortion models by focal length and crop factor. It is designed for photographic lens correction profiles, not necessarily per-device dual-fisheye factory calibration.

Relevant source:

- [Lensfun lens calibration data structure](https://lensfun.github.io/manual/latest/structlfLens.html)

Implication: Lensfun/Hugin can be a useful source for rectilinear and common lenses, but for unstitched dual-fisheye 360 cameras it is less trustworthy than per-camera calibration. It may be useful as a "best effort / experimental" importer.

### Metadata and ExifTool

ExifTool's tag tables are huge and actively maintained. The tag list documentation explains unknown tags and recommends using `exiftool -s` to see real tag names. DJI tags currently include `CalibratedFocalLength`, `CalibratedOpticalCenterX`, `CalibratedOpticalCenterY`, `DewarpData`, `DewarpFlag`, and many device-specific timed protobuf tags. The current DJI page explicitly mentions Osmo 360 protobuf support under `dvtm_oq101`.

Relevant sources:

- [ExifTool tag-name documentation](https://exiftool.org/TagNames/)
- [ExifTool DJI tags](https://exiftool.org/TagNames/DJI.html)

Implication: a metadata loop is useful, but not sufficient. Metadata can answer "what does this file claim about focal length, center, FOV, dewarp state, camera model, frame index, and lens side?" It usually cannot answer "what exact central projection and distortion model should we use for this lens, at this resolution, after this vendor's dewarp pipeline?"

## Answer to the Discord claim

Could we "run in a loop until you extract the distortion coefficients from the metadata"?

Partly, but that sentence compresses several different problems:

1. Find metadata fields: yes.
2. Extract focal length and optical center: sometimes.
3. Extract distortion-ish blobs: sometimes, especially vendor-specific fields like DJI `DewarpData`.
4. Decode those blobs into a documented fisheye model: rarely guaranteed.
5. Know whether the pixels are raw fisheye, dewarped fisheye, stabilized, cropped, rotated, or stitched: often ambiguous without capture-specific testing.
6. Know whether coefficients are for the current resolution and lens half: not guaranteed.
7. Prove the resulting cube faces are geometrically correct enough for SfM: only by validation.

So the product answer should be:

> We can add a metadata probe that attempts extraction and produces a confidence-ranked calibration candidate. We should not silently run conversion from metadata-only distortion unless the candidate maps cleanly into a known model and passes validation.

This is a good place to be publicly generous but technically firm. The project can offer the workflow, the reports, the validation gates, and the importers, instead of shipping a mystery coefficient blender.

## Proposed architecture

Introduce a small calibration subsystem:

```text
calibration/
  __init__.py
  core.py
  metashape.py
  opencv.py
  colmap.py
  realityscan_xmp.py
  raymap.py
  metadata_probe.py
  validate.py
```

Core data shape:

```python
@dataclass(frozen=True)
class Calibration:
    provider: str
    model: str
    width: int
    height: int
    params: dict[str, float | list[float] | str]
    source_path: Path | None
    warnings: tuple[str, ...] = ()

    def rays(self) -> np.ndarray:
        """Return H x W x 3 float32/float64 unit rays in camera coordinates."""
```

The main script should stop asking "what Metashape tuple do I have?" and start asking:

```python
calibration = load_calibration(args.calibration, provider=args.calibration_provider)
rays = calibration.rays()
```

Then existing mask support, remap, output, and cache code can remain largely intact.

Provider modules should share a small model registry so every supported model has explicit metadata:

```python
@dataclass(frozen=True)
class CameraModelSpec:
    provider: str
    model: str
    parameter_names: tuple[str, ...]
    source_coordinate_system: str
    pixel_center_convention: str
    supports_direct_unprojection: bool
    validation_oracle: str | None
```

The registry should be used for parsing, summaries, cache keys, documentation, and error messages. It should be impossible to add a new model without naming its parameter order and coordinate convention.

Rename later, after compatibility is handled:

- `--amlenscal` remains as an alias.
- New preferred flag: `--calibration`.
- New flag: `--calibration-provider {auto,metashape,opencv,opencv-fisheye,colmap,realityscan-xmp,raymap,metadata}`
- New optional flag: `--camera-id` for COLMAP files containing more than one camera.
- New optional flag: `--xmp-dir` for RealityScan/RealityCapture sidecar directories when calibration is spread across per-image XMP files.
- New optional flag: `--image-width` / `--image-height` for calibration formats that omit resolution.
- New optional flag: `--lens-side {auto,front,back,left,right,lens0,lens1}` for metadata/import workflows.

## Format support matrix

| Format/source | Feasibility | Trust level | Notes |
|---|---:|---:|---|
| Existing Metashape XML | Already works | High when calibrated correctly | Keep exact behavior and warnings. |
| Native raymap `.npz` | Easy | Highest if generated/validated | Project-owned interchange. Stores `rays`, dimensions, provider metadata, optional solid angle. |
| OpenCV fisheye YAML/JSON | Easy-medium | High if calibrated well | Use `cv2.fisheye.undistortPoints` or direct equations. Best first free calibration path. |
| OpenCV standard YAML/JSON | Easy-medium | Medium for non-fisheye / moderate wide-angle | Not ideal for >180 fisheye; useful for rectilinear/wide-angle cameras. |
| COLMAP `cameras.txt` | Medium | Medium-high | Support known models directly. Need careful pixel-center convention tests. |
| COLMAP sparse model directory | Medium | Medium-high | Parse `cameras.txt`; ignore poses unless later rig export needs them. |
| RealityScan/RealityCapture XMP | Medium | Medium-high if exported for the matching distorted images | Strong candidate for users who align fisheyes in RealityScan. Must parse model-specific XMP fields, require image dimensions when absent, and reject mismatched undistorted-export XMP. |
| Kalibr YAML | Medium-high | High for robotics users | More model types; defer until core provider API is stable. |
| mrcal calibration files | Medium-high | High for advanced calibration users | Excellent reference ecosystem; better as future importer/exporter after raymap is stable. |
| AliceVision/Meshroom `sfmData` | Medium-high | Medium-high if intrinsics are stable | Later-stage provider similar to COLMAP; first support raymap/manual export routes. |
| Lensfun profiles | Medium-high | Medium-low for 360 fisheye | Useful experimental profile lookup; not per-device enough for primary path. |
| EXIF/XMP/MakerNotes metadata | Easy to probe, hard to trust | Low alone, higher if model decoded and validated | Use for reports/prefill, not silent conversion. This is separate from RealityScan XMP sidecars, which are intentional camera export files. |
| Manual JSON | Easy | Depends on user | Good for power users and reproducible bug reports. |

## Implementation phases

### Phase 0: tighten current baseline

Goal: make sure existing Metashape behavior is preserved before expanding.

Tasks:

1. Add a small test harness around the example data.
2. Assert that the existing Metashape XML produces the same `+Z` reference outputs or at least visually/pixel statistically equivalent outputs.
3. Extract `get_metashape_calibration_data()`, `compute_rays()`, and projection helpers into a module without changing behavior.
4. Rename internally only where safe: keep CLI and user-facing wording compatible.

Acceptance criteria:

- Existing example scripts still run.
- Existing output layout stays unchanged.
- Existing README warning remains true.

### Phase 1: calibration-provider interface

Goal: remove Metashape as an architectural assumption.

Tasks:

1. Create `calibration/core.py` with the `Calibration` abstraction.
2. Move the current Metashape XML parser into `calibration/metashape.py`.
3. Move ray generation into provider classes/functions.
4. Change `main()` to call `load_calibration()`.
5. Keep `--amlenscal` as an alias for `--calibration-provider metashape --calibration`.
6. Include provider/model/params hash in remap cache keys.

Acceptance criteria:

- No behavior change for existing users.
- A fake/test provider can return identity/pinhole rays and reach remap code.
- Error messages name the provider and model.

### Phase 2: native raymap format

Goal: define a free, project-owned escape hatch that can represent any central camera model without coefficient translation.

Recommended `.npz` schema:

```text
version: "fisheye-to-cubemap-raymap-v1"
width: int
height: int
rays: float32[height,width,3]
solid_angle: optional float32[height,width]
provider: string
model: string
source: string
source_geometry_json: string
params_json: string
notes: string
```

Tasks:

1. Add `--export-raymap <path>` to write rays from any provider.
2. Add provider `raymap` to load rays directly.
3. Add validation: shape, finite values, unit length tolerance, center ray sanity, no all-zero rays.
4. Document ray coordinate convention.
5. Default to compressed `.npz`, but allow `--raymap-compression {compressed,stored}` for users who prefer faster local reads/writes.
6. Store raymaps as `float32` by default. Treat `float16` as experimental until angular error is measured.

Acceptance criteria:

- Metashape XML can be converted to raymap.
- Raymap input produces the same cube faces as original Metashape input.
- Users can share a small `.npz` calibration without sharing Metashape or source images.
- The raymap loader rejects mismatched image dimensions unless the user explicitly provides a compatible image-geometry transform.

Why this matters:

- It avoids coefficient-name confusion entirely.
- It lets users calibrate however they want, as long as they can generate rays.
- It gives the repo a stable free interchange format that belongs to the project.
- It creates a practical bridge for future providers such as mrcal, BabelCalib, AliceVision/Meshroom, vendor-specific metadata decoders, or one-off community scripts.

### Phase 3: OpenCV fisheye importer

Goal: first real Metashape-free workflow.

Supported input:

- OpenCV YAML/XML/JSON with `camera_matrix` / `K` and `distortion_coefficients` / `D`.
- Explicit JSON:

```json
{
  "provider": "opencv_fisheye",
  "width": 3840,
  "height": 3840,
  "K": [[fx, 0, cx], [0, fy, cy], [0, 0, 1]],
  "D": [k1, k2, k3, k4]
}
```

Ray strategy:

1. Generate pixel coordinates for the full image.
2. Use `cv2.fisheye.undistortPoints(points, K, D, P=None)` to get normalized rays, or implement the inverse directly and cross-check against OpenCV.
3. Convert normalized points `(x, y)` to unit rays `[x, y, 1] / norm`.
4. Apply orientation conventions to match current cube-face expectations.

Potential pitfall:

OpenCV's fisheye model is an equidistant angular-distortion model. It is not the same as the current Metashape equisolid branch. That is fine because the importer generates rays directly; it never tries to become a Metashape XML.

Acceptance criteria:

- Known synthetic OpenCV fisheye calibration round-trips within a small angular tolerance.
- A calibration board dataset can produce sane cube faces.
- The report warns when `fx` and `fy` differ materially, but supports them.

### Phase 4: COLMAP importer

Goal: let users who already have COLMAP camera models feed this project directly.

Supported first wave:

- `OPENCV_FISHEYE`
- `SIMPLE_FISHEYE`
- `FISHEYE`
- `SIMPLE_RADIAL_FISHEYE`
- `RADIAL_FISHEYE`
- `PINHOLE` / `SIMPLE_PINHOLE`

Input:

- `--calibration-provider colmap --calibration path/to/cameras.txt --camera-id 1`
- If a sparse model directory is passed, find `cameras.txt`.

Ray strategy:

- For OpenCV-like fisheye models, reuse the OpenCV fisheye unprojection path where parameter semantics match.
- For `SIMPLE_FISHEYE` / `FISHEYE`, implement equidistant inverse.
- For radial fisheye variants, follow COLMAP model definitions and verify against pycolmap if available.

Acceptance criteria:

- Parse multiple cameras and require `--camera-id` when ambiguous.
- Unit tests for every supported model's parameter order.
- Synthetic projection/unprojection checks against COLMAP formulas.
- Optional pycolmap conformance tests pass when pycolmap is installed.
- COLMAP rig/frame files are parsed only for reporting at first; the importer does not imply rig-aware cube-face export until a separate downstream export phase exists.

### Phase 5: RealityScan / RealityCapture XMP importer

Goal: let users who align fisheye imagery in RealityScan/RealityCapture reuse the exported XMP calibration as a ray source without passing through Metashape.

This is distinct from the generic metadata probe. RealityScan XMP is an intentional sidecar format with documented calibration fields, not a random EXIF scrape. The importer still needs to be conservative because XMP export settings can describe undistorted outputs rather than original distorted source images.

Supported input:

- One `.xmp` file paired with a representative source image.
- A directory of `.xmp` sidecars paired with the fisheye image directory by matching stems.
- Optional `_common.xmp` if the user has a shared calibration sidecar.
- Optional explicit dimensions via `--image-width` / `--image-height` when paired image dimensions cannot be read.

First-wave XMP fields:

- `xcr:DistortionModel`
- `xcr:DistortionCoeficients`
- `xcr:FocalLength35mm`
- `xcr:Skew`
- `xcr:AspectRatio`
- `xcr:PrincipalPointU`
- `xcr:PrincipalPointV`
- `xcr:CalibrationGroup`
- `xcr:DistortionGroup`
- `xcr:Version`

Fields to parse and preserve for future use, but not use for the first ray-generation pass:

- `xcr:Rotation`
- `xcr:Position`
- `xcr:PosePrior`
- `xcr:Coordinates`
- `xcr:Rig`
- `xcr:RigInstance`
- `xcr:RigPoseIndex`

Supported model scope:

- `division`: implement only after confirming the inverse mapping against Epic's XMP camera math and at least one RealityScan-exported fixture.
- Brown-family models: support the documented coefficient order `k1 k2 k3 k4 t1 t2`, with unused entries accepted as zero.
- Unknown labels: parse and report, but refuse ray generation until a model implementation exists.

Ray strategy:

1. Parse XMP with `xml.etree.ElementTree` and namespace-aware access to `xcr:*` attributes/elements.
2. Determine image dimensions from the paired image, explicit CLI dimensions, or a project manifest; refuse if unknown.
3. Convert XMP normalized image coordinates and focal data into a provider-local camera model.
4. For every source pixel, convert pixel coordinates back to RealityScan's normalized coordinate convention.
5. Invert the relevant distortion model from distorted image coordinates to an undistorted central ray coordinate.
6. Convert undistorted central coordinates to unit rays.
7. Validate ray field sanity and write diagnostics.

Important validation checks:

- Confirm dimensions used by the importer match actual source image dimensions.
- Confirm all XMP files in a run share compatible `CalibrationGroup`, `DistortionGroup`, dimensions, focal/aspect/skew/principal point, and distortion coefficients unless per-image intrinsics are explicitly enabled.
- Warn if XMP export settings or attributes suggest undistorted images.
- Generate a compact XMP calibration summary in `bonusdata/` showing consumed fields, ignored extrinsics, model name, derived dimensions, and warnings.
- Compare a small set of projected/unprojected sample points against RealityScan's documented equations.
- Put `K + Brown*` labels behind explicit fixtures; do not infer them from non-`K` Brown behavior.

Acceptance criteria:

- A sample RealityScan XMP file parses into a structured calibration object with a clear summary.
- Unsupported `xcr:DistortionModel` values produce a helpful error, not silent fallbacks.
- Supported Brown-family XMP fixtures pass synthetic ray round-trip tests.
- Division-model fixtures pass a separate inverse-mapping test before division support is marked production-ready.
- If the same RealityScan calibration is exported per image, the importer deduplicates identical intrinsics and reports any outliers.
- The importer refuses to run when image dimensions are unknown or inconsistent.

Product positioning:

RealityScan XMP support should be documented as "for users who already aligned/calibrated in RealityScan and exported XMP for the matching image geometry." It should not be described as a universal free calibration method, and it should not be mixed with the generic metadata probe.

### Phase 6: metadata probe

Goal: make the "run in a loop" idea useful without making it unsafe.

CLI:

```bash
python AM_ImageAndMask_to_cubemap_v4.py \
  --probe-metadata images_or_video_path \
  --metadata-report output/metadata_calibration_probe.json
```

Probe strategy:

1. Prefer `exiftool -json -G1 -s -n -ee -u`.
2. Also support a no-ExifTool fallback using Python libraries for basic EXIF, but clearly mark it as limited.
3. Search known keys and key substrings:
   - `Make`, `Model`, `LensModel`, `SerialNumber`, `ImageWidth`, `ImageHeight`
   - `FocalLength`, `FocalLengthIn35mmFormat`
   - `CalibratedFocalLength`
   - `CalibratedOpticalCenterX`, `CalibratedOpticalCenterY`
   - `DewarpData`, `DewarpFlag`
   - camera/lens side indicators such as `CamReverse`, stream names, file naming patterns
   - unknown tags containing `calib`, `dist`, `dewarp`, `intrinsic`, `lens`, `focal`, `center`, `principal`, `k1`, `k2`, `k3`, `k4`
4. Group repeated tags across frames to see whether values are stable.
5. Produce confidence-ranked candidates:
   - `metadata_intrinsics_only`
   - `metadata_vendor_dewarp_blob`
   - `metadata_known_model_candidate`
   - `insufficient`

Rules:

- Do not auto-run conversion from `DewarpData` unless a decoder for that exact vendor/model/file type is implemented and tested.
- If only focal length and center are found, offer a no-distortion/equidistant guess only as a diagnostic preview, not as production output.
- Always include the raw extracted tags in the report so advanced users can help reverse engineer model-specific fields.

Acceptance criteria:

- Produces useful reports for DJI Osmo 360 files.
- Warns clearly when coefficients are absent or unknown.
- Does not create a fake Metashape XML.
- Identifies RealityScan/RealityCapture XMP sidecars and redirects users to the `realityscan-xmp` provider instead of treating them as generic metadata.

### Phase 7: calibration helper

Goal: give non-Metashape users a full first-party workflow.

Options:

1. OpenCV ChArUco/fisheye calibration helper.
2. Import from a folder of calibration target images.
3. Export OpenCV JSON and native raymap.
4. Write a calibration quality report with corner coverage heatmaps and rejected images.

Possible command:

```bash
python tools/calibrate_opencv_fisheye.py \
  --images calibration_frames/lens0 \
  --board charuco_7x10_40mm \
  --width 3840 --height 3840 \
  --output calibration_lens0.opencv_fisheye.json \
  --export-raymap calibration_lens0.raymap.npz
```

Acceptance criteria:

- Reports RMS reprojection error.
- Saves debug images with detected corners.
- Refuses calibrations with too few views, badly distributed views, or implausible intrinsics.
- Documents best capture practice: fill the frame, vary distance/tilt, include edges and corners, keep lens/resolution/dewarp/stabilization mode fixed, and capture each physical fisheye lens separately.
- Requires enough observations near the outer useful FOV, not only a good RMS near the center.

### Phase 8: GUI support

Goal: make the free path approachable.

GUI changes:

- Replace "Metashape calibration XML" with "Calibration file".
- Add provider dropdown: Auto, Metashape XML, OpenCV fisheye, COLMAP, RealityScan XMP, Raymap, Metadata probe.
- Show provider-specific summary:
  - model
  - dimensions
  - focal lengths
  - principal point
  - distortion count
  - RealityScan calibration/distortion group when applicable
  - warnings
- Add a "Probe metadata" button that generates and opens the report.
- Add "Export raymap" option.

Acceptance criteria:

- Existing Metashape users are not confused.
- Free-path users can discover what to do next from the GUI state, not from a Discord explanation.

## Validation strategy

This feature is geometry-sensitive. Visual "seems right" is not enough.

Use four tiers:

1. Parser tests: ensure parameter order and dimensions are correct.
2. Synthetic model tests: project known rays to pixels, unproject back, measure angular error.
3. Cross-tool tests: compare provider rays against OpenCV/COLMAP/pycolmap/RealityScan-documented formulas where possible.
4. Real capture tests: run SfM alignment and compare feature quality / reconstruction behavior.

Add one more practical tier:

5. Geometry-contract tests: intentionally run valid calibrations against resized/cropped/rotated/undistorted image geometry and confirm the provider refuses or warns loudly.

Suggested metrics:

- Mean and max ray angular error in degrees for synthetic tests.
- Unit ray norm tolerance, e.g. `abs(norm - 1) < 1e-6`.
- Center ray direction sanity.
- Monotonic radial angle from center for central fisheye models.
- Cube-face image coverage/mask coverage compared to expected FOV.
- COLMAP alignment success and mean reprojection error on generated cube faces.
- RealityScan XMP import sanity: same XMP plus same dimensions produces stable rays; changed dimensions or undistorted-export settings cause warnings/errors.
- Calibration target coverage score for the helper workflow: number of views, corner distribution, radius coverage, and rejected-frame count.

## Risk register

| Risk | Severity | Mitigation |
|---|---:|---|
| Users paste random coefficients into wrong model | High | Provider-specific file formats, loud warnings, no "generic k1/k2" importer. |
| Metadata fields are incomplete or vendor-proprietary | High | Probe/report only; require known decoder plus validation for auto use. |
| Pixel-center convention differences cause subtle ray offsets | Medium-high | Synthetic projection/unprojection tests and source-specific convention docs. |
| Valid calibration used against resized/cropped/rotated images | High | Add source-geometry metadata, paired-image dimension checks, and geometry-contract tests. |
| Raymap files are large enough to annoy users | Medium | Default to compressed `.npz`, document expected sizes, and keep `float32` default until precision tradeoffs are measured. |
| OpenCV fisheye cannot model some 360 lenses well | Medium | Native raymap and future Kalibr/double-sphere support. |
| COLMAP intrinsics estimated from already-problematic fisheye images are poor | Medium | Encourage calibration board or shared intrinsics; expose validation metrics. |
| RealityScan XMP may describe undistorted exports rather than original fisheye pixels | High | Require paired image dimensions, surface XMP/export warnings, document exact export requirements, and refuse ambiguous cases where possible. |
| RealityScan XMP may omit image resolution | Medium-high | Read dimensions from paired images or require explicit `--image-width` / `--image-height`; include dimensions in cache key and diagnostics. |
| RealityScan distortion model names or semantics may vary by version/export mode | Medium-high | Start with documented division and Brown-family models only; fail closed on unknown labels; keep fixtures by RealityScan version. |
| GUI becomes too complex | Medium | Progressive disclosure: default Metashape path, advanced provider dropdown. |
| Existing users' scripts break | High | Keep `--amlenscal` alias and old behavior. |

## Recommended MVP

The smallest release that honestly solves the Discord need:

1. Provider interface.
2. Existing Metashape provider unchanged.
3. OpenCV fisheye JSON/YAML provider.
4. Native raymap import/export.
5. Metadata probe report.
6. README update with "free calibration path" and "metadata reality check".

Do not make COLMAP the first MVP unless users already have reliable fisheye intrinsics in COLMAP. COLMAP is hugely important downstream, but OpenCV fisheye is the faster route to a free, reproducible calibration source because this repo already depends on OpenCV and the equations are directly documented.

Add two items to the MVP definition before public release:

1. provider summary JSON in `bonusdata/`;
2. source-geometry checks for image dimensions and obvious crop/resize mismatch.

RealityScan XMP is a strong near-MVP candidate if the community has sample exports available. It is more implementation-specific than OpenCV fisheye, but it is more trustworthy than generic metadata because it is an intentional calibration export. If sample XMP fixtures arrive early, implement `realityscan-xmp` immediately after raymap/OpenCV and before deeper COLMAP model coverage. If no fixtures are available, document the target and keep it as the next provider rather than guessing against screenshots or incomplete sample snippets.

RealityScan XMP acceptance for MVP inclusion:

1. At least one real XMP sidecar exported from RealityScan/RealityCapture for original distorted fisheye images.
2. The matching source image dimensions.
3. The RealityScan export settings used.
4. Confirmation of the `xcr:DistortionModel` values present.
5. A visual and synthetic validation pass showing the generated ray field is plausible.

## Suggested public wording

> Fisheye-to-Cubemap no longer needs Metashape specifically; it needs a trustworthy per-lens calibration. Metashape XML remains supported, but free workflows can use OpenCV fisheye calibration, RealityScan/RealityCapture XMP exports, or the project-native raymap format. Metadata probing is available to inspect camera files for focal length, optical center, and vendor dewarp data, but metadata-only conversion is treated as experimental unless the vendor model is decoded and validated.

## Concrete next code tasks

1. Add `calibration/core.py`.
2. Add model registry and provider summary objects.
3. Move current parser to `calibration/metashape.py`.
4. Add `load_calibration(path, provider="auto")`.
5. Change `main()` to accept `--calibration`, `--calibration-provider`, and legacy `--amlenscal`.
6. Write `bonusdata/calibration_provider_summary.json` for every run.
7. Add source-geometry validation for dimensions and declared image state.
8. Add `calibration/raymap.py`.
9. Add `--export-raymap`.
10. Add OpenCV fisheye provider.
11. Add RealityScan/RealityCapture XMP provider scaffold that parses and reports XMP fields, initially failing closed for unsupported models.
12. Add documented Brown-family RealityScan XMP ray generation once fixtures exist.
13. Add COLMAP provider for first-wave fisheye models.
14. Add tests for Metashape parity, OpenCV synthetic unprojection, RealityScan XMP parsing/model fixtures, COLMAP parameter ordering, and geometry-contract failures.
15. Add metadata probe using ExifTool if available.
16. Update README and GUI labels.

## Bottom line

Make the repo Metashape-free by making the internal contract rays, not coefficients. That honors the existing warning instead of weakening it. The free path is very doable. OpenCV fisheye and native raymap are the safest first steps; RealityScan XMP is a viable and useful provider for users who already align/calibrate there; generic metadata extraction should remain a probe and validation aid until a specific camera's metadata format is decoded well enough to trust.
