# Next work options while waiting for RealityScan XMP fixtures

Date: 2026-06-01

This document expands the remaining work into practical options. The goal is to make each item understandable enough to choose, schedule, or defer.

## Recommended order

If we cannot run full real validation yet, the best order is:

1. Add tests for the providers already implemented.
2. Improve source-geometry validation and provider summaries.
3. Add provider registry / cleanup.
4. Add synthetic/example calibration fixtures.
5. Write provider docs.
6. Draft the GUI change list.
7. Add metadata probe.
8. Rename/refactor misleading internals.

This order builds confidence before adding more surface area.

## 1. Full real validation

Status: deferred for now.

Why it matters:

The provider layer changes how calibration enters the script. The safest proof is to run the same real examples through old and new paths and compare outputs.

What it would involve:

- Run front lens with legacy `--amlenscal`.
- Run front lens with `--calibration --calibration-provider metashape`.
- Export raymap from front lens.
- Run front lens from raymap.
- Repeat for back lens.
- Run at `facewidth 2100` for final acceptance.
- Compare representative outputs against expected examples.

Current blocker:

- User cannot do full validation yet.

Useful partial substitute:

- Keep using synthetic tests and smaller `facewidth` real-image smoke tests.

## 2. Provider test suite

Status: good next task.

What this means:

Right now validation is mostly command-driven. A test suite would make regressions obvious and repeatable. This does not need to run full image conversion. Most tests can be small and fast.

Suggested structure:

```text
tests/
  test_metashape_provider.py
  test_raymap_provider.py
  test_opencv_provider.py
  test_colmap_provider.py
  test_provider_failures.py
  fixtures/
    metashape_equisolid.xml
    opencv_fisheye.json
    opencv_fisheye.yml
    colmap_cameras.txt
```

Checklist:

- Add a lightweight test runner.
  - Option A: use Python `unittest`, no new dependency.
  - Option B: add `pytest`, nicer but new dependency.
- Add Metashape parser tests.
  - Parses example XML.
  - Produces expected width/height/model.
  - Includes warning about Metashape semantics.
  - Fails on unsupported projection.
- Add raymap tests.
  - Saves a tiny 8x8 raymap.
  - Loads it back.
  - Rejects bad version.
  - Rejects wrong shape.
  - Rejects zero-length rays.
  - Renormalizes slightly imperfect rays.
- Add OpenCV fisheye tests.
  - Loads JSON.
  - Loads YAML/FileStorage.
  - Center pixel ray points forward when principal point is centered.
  - Nonzero distortion project/unproject round-trip stays below tolerance.
- Add COLMAP tests.
  - Parses one-camera `cameras.txt`.
  - Requires `camera_id` for multi-camera files.
  - Selects requested `camera_id`.
  - Loads all first-wave models.
  - Rejects unsupported model.
  - Rejects wrong parameter count.
- Add failure behavior tests.
  - `realityscan-xmp` fails closed for now.
  - `metadata` fails closed for now.
  - unknown provider errors clearly.
- Add CLI smoke tests if practical.
  - `--version`
  - `--usage`
  - tiny synthetic full run

Expected benefit:

- Safer refactoring.
- Easier to accept future provider contributions.
- Better confidence before RealityScan XMP work.

## 3. Provider registry / cleanup

Status: useful, but optional before tests.

What this means:

Right now each provider is implemented as a module, and supported models are hard-coded inside those modules. A registry would centralize provider/model metadata.

Plain-English version:

The registry is a table that says:

```text
Provider: colmap
Model: OPENCV_FISHEYE
Params: fx, fy, cx, cy, k1, k2, k3, k4
Status: implemented
Validation oracle: OpenCV fisheye projectPoints/undistortPoints
```

Why it matters:

- Prevents mystery parameter ordering.
- Makes documentation easier to generate.
- Makes GUI provider dropdown easier.
- Makes error messages clearer.
- Makes unsupported models fail in a consistent way.

Checklist:

- Add `calibration/registry.py`.
- Define `CameraModelSpec`.
- Register implemented models:
  - Metashape `equidistant`
  - Metashape `equisolid`
  - Raymap `dense-raymap`
  - OpenCV `opencv_fisheye`
  - COLMAP first-wave models
- Register planned models:
  - RealityScan `Division`
  - RealityScan `Brown3`
  - RealityScan `Brown3 with tangential2`
  - RealityScan `Brown4 with tangential2`
  - metadata probe
- Use registry in provider summary JSON.
- Use registry in unsupported model errors.
- Optionally generate part of docs from registry later.

Expected benefit:

- Cleaner architecture.
- Less repeated model knowledge.
- Easier GUI integration.

## 4. Source-geometry validation

Status: high-value hardening.

What this means:

Calibration only works if it describes the exact pixel geometry being processed. If a calibration was made for a 3840x3840 fisheye image, it should not be silently used on a 1920x1920 resized image or an undistorted export.

Current protection:

- The script checks image dimensions against calibration width/height.

What should be improved:

- Better shared validation object.
- Better error messages.
- More provider-specific warnings.
- Record geometry validation in provider summary.

Checklist:

- Expand `CalibrationSourceGeometry`.
  - width
  - height
  - source image state
  - crop rect
  - scale
  - rotation
  - lens side
  - warnings
- Add `validate_against_image_set()`.
  - Reads dimensions from first image.
  - Confirms all images match calibration.
  - Detects mixed image sizes.
  - Records validation result.
- Make error messages provider-aware.
  - "COLMAP camera 2 expects 3840x3840, but image is 1920x1920."
  - "This may mean you are using resized, cropped, or undistorted images."
- Add tests for mismatch failures.
- Add summary JSON fields:
  - `image_geometry_validation`
  - `expected_shape`
  - `observed_shapes`
  - `passed`

Expected benefit:

- Prevents the most dangerous silent failure mode.
- Helps users debug wrong inputs quickly.

## 5. Provider summary improvements

Status: useful and easy to grow incrementally.

What this means:

Every run already writes:

```text
bonusdata/calibration_provider_summary.json
```

We can make that file much more useful.

Current summary includes:

- provider
- model
- dimensions
- source path/hash
- params
- source geometry
- warnings
- ignored fields
- raw parsed data
- cache fingerprint
- whether rays are embedded

Potential additions:

- script version
- run timestamp
- CLI arguments
- output face width
- output format
- mask/support source
- image count
- image geometry validation
- ray statistics
- raymap precision info
- remap cache key/fingerprint
- provider implementation version
- validation warnings

Checklist:

- Add a `run_context` object.
- Include CLI args safely.
- Include support source after it is resolved.
- Include image count and pending/skipped counts.
- Include ray stats:
  - min/max finite check
  - max norm error
  - center ray
  - approximate FOV from useful mask
- Include raymap details when applicable:
  - float dtype
  - compression mode
  - max normalization error
- Update tests to assert summary keys exist.

Expected benefit:

- Better bug reports.
- Easier Discord support.
- Makes "it seems right" less hand-wavy.

## 6. Metadata probe

Status: useful, but keep diagnostic-only.

What this means:

Metadata probe scans images/videos for calibration-looking tags and writes a report. It does not produce calibration unless a future known decoder exists.

Plain-English version:

It answers:

> "What useful camera/lens/dewarp/calibration information is hiding in these files?"

It does not answer:

> "Here are trustworthy distortion coefficients, go convert."

Possible command:

```bash
python AM_ImageAndMask_to_cubemap_v4.py \
  --probe-metadata images_or_video_path \
  --metadata-report temp/metadata_probe.json
```

Checklist:

- Decide whether this lives in main script or `tools/probe_metadata.py`.
- Prefer external `exiftool` if installed.
- Add fallback for basic image metadata using Python/OpenCV/Pillow if no ExifTool.
- Scan for known tags:
  - Make
  - Model
  - LensModel
  - FocalLength
  - ImageWidth/ImageHeight
  - CalibratedFocalLength
  - CalibratedOpticalCenterX/Y
  - DewarpData
  - DewarpFlag
- Scan unknown tags containing:
  - `calib`
  - `dist`
  - `dewarp`
  - `intrinsic`
  - `lens`
  - `focal`
  - `center`
  - `principal`
  - `k1`, `k2`, `k3`, `k4`
- Group repeated values across files.
- Detect RealityScan XMP sidecars and say:
  - "Use the RealityScan XMP provider when implemented."
- Output JSON report.
- Optional human-readable Markdown report.

Expected benefit:

- Helps discover whether DJI/Insta360/etc. files contain useful metadata.
- Helps reverse-engineer future camera-specific providers.
- Gives non-technical users a report to share.

Risks:

- Users may think metadata equals calibration.
- Mitigation: report must say confidence level and "diagnostic only."

## 7. GUI change list

Status: should be designed before implementation, and user wants hands-on involvement.

Goal:

Make the GUI reflect multi-format calibration without making it confusing.

Current likely GUI problem:

The GUI labels and assumptions are Metashape-specific.

New concepts the GUI needs:

- Calibration provider.
- Provider-specific file types.
- Provider warnings.
- Provider summary.
- Raymap export.
- COLMAP camera ID.
- Metadata probe.

Checklist by GUI area:

### Calibration input row

- Rename label:
  - from "Metashape calibration XML"
  - to "Calibration file"
- Add provider dropdown:
  - Auto
  - Metashape XML
  - Raymap
  - OpenCV fisheye
  - COLMAP
  - RealityScan XMP
  - Metadata probe
- Update file picker filters by provider:
  - Metashape: `.xml`
  - Raymap: `.npz`
  - OpenCV: `.json`, `.yml`, `.yaml`, `.xml`
  - COLMAP: `cameras.txt` or folder
  - RealityScan XMP: `.xmp` or folder

### Provider summary panel

- Show:
  - provider
  - model
  - width x height
  - focal values
  - principal point
  - distortion coefficients count
  - warnings
- Add "Open summary JSON" button after a run.

### COLMAP controls

- Add optional `Camera ID` field.
- Enable only when provider is COLMAP.
- If empty and file has multiple cameras, CLI will error; GUI can pre-warn later.

### Raymap controls

- Add checkbox:
  - "Export raymap"
- Add path picker for raymap output.
- Add compression selector:
  - compressed
  - stored

### Metadata probe controls

- Add button:
  - "Probe metadata"
- Output report path.
- Do not route metadata probe as normal conversion.

### RealityScan controls

- Leave visible but marked planned until implemented, or hide until ready.
- Once implemented:
  - XMP file/folder picker.
  - paired image requirement warning.
  - calibration/distortion group summary.

### Run command builder

- Add CLI flags:
  - `--calibration`
  - `--calibration-provider`
  - `--camera-id`
  - `--export-raymap`
  - `--raymap-compression`
- Keep `--amlenscal` support only for backward compatibility, not new GUI output.

Expected benefit:

- Users can actually discover the new providers.
- Fewer Discord explanations needed.
- Better guardrails for non-Metashape users.

## 8. Example calibration fixtures

Status: useful and low-risk.

What this means:

Add tiny or synthetic calibration files to the repo so tests/docs have stable examples.

Possible files:

```text
examples/calibrations/
  opencv_fisheye_identity.json
  opencv_fisheye_identity.yml
  colmap_opencv_fisheye_cameras.txt
  colmap_multi_camera_cameras.txt
```

Maybe not commit raymap binary unless small:

```text
examples/calibrations/mini_raymap.npz
```

Checklist:

- Create small OpenCV JSON fixture.
- Create small OpenCV YAML fixture.
- Create small COLMAP one-camera fixture.
- Create small COLMAP multi-camera fixture.
- Decide whether to commit a tiny raymap.
- Add README explaining fixtures are synthetic and for tests/docs only.
- Use these fixtures in tests.

Expected benefit:

- Makes docs concrete.
- Gives tests stable inputs.
- Helps users understand expected formats.

## 9. User documentation

Status: important.

Needed docs:

```text
docs/calibration-providers.md
docs/raymap-format.md
docs/opencv-fisheye-workflow.md
docs/colmap-import.md
docs/realityscan-xmp-notes.md
docs/metadata-probe.md
```

Checklist:

### `calibration-providers.md`

- Provider table.
- Implemented/planned status.
- File types.
- What each provider is for.
- Warning against coefficient copying.

### `raymap-format.md`

- `.npz` schema.
- Ray coordinate convention.
- Size/precision expectations.
- How to export.
- How to import.

### `opencv-fisheye-workflow.md`

- Expected JSON format.
- Expected YAML/FileStorage keys.
- How to calibrate with OpenCV/ChArUco later.
- Known limitations.

### `colmap-import.md`

- Supported camera models.
- `cameras.txt` format.
- `--camera-id`.
- What poses/rigs are not used yet.

### `realityscan-xmp-notes.md`

- What fixtures are needed.
- Known fields.
- Known distortion models.
- Why original-vs-undistorted geometry matters.

### `metadata-probe.md`

- Diagnostic purpose.
- What it scans.
- Why it does not equal calibration.

Expected benefit:

- The project becomes usable by people outside the original Discord thread.
- Future contributors have fewer ways to misunderstand the math.

## 10. Internal naming/refactor

Status: useful, but do carefully after tests exist.

What this means:

Some function names still say "Metashape" even though they now handle raymap/OpenCV/COLMAP rays too.

Candidate rename:

- Current:
  - `compute_metashape_rays_usefulpixmap`
- Better:
  - `compute_rays_and_useful_pixel_mask`
  - or `write_ray_diagnostics_and_useful_mask`

Other cleanup candidates:

- Move ray math into `calibration/` or `geometry/`.
- Move remap code into `remap.py`.
- Move CLI parsing into a smaller function.
- Replace five repeated face precompute blocks with a loop.
- Make support/mask derivation a separate module.

Checklist:

- Add tests first.
- Rename function with minimal behavior change.
- Keep a compatibility wrapper temporarily if needed.
- Update call sites.
- Update comments.
- Run smoke tests.
- Avoid mixing refactor with new provider math.

Expected benefit:

- Code becomes easier to maintain.
- New contributors will not assume everything is Metashape-only.

Risk:

- Refactors can break working behavior.
- Mitigation: do after test suite exists.

## Suggested immediate next slice

Best next PR-sized chunk:

1. Add tests using `unittest`.
2. Add synthetic fixtures under `examples/calibrations/`.
3. Improve provider summary JSON with image geometry validation results.
4. Write `docs/calibration-providers.md` and `docs/raymap-format.md`.

This builds the floor before RealityScan XMP work.
