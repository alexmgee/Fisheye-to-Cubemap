# RealityScan / RealityCapture XMP provider plan

Date: 2026-06-01

## Purpose

This document isolates the RealityScan/RealityCapture part of the broader multi-format calibration plan.

Original intent:

> Let users who already align or calibrate fisheye imagery in RealityScan/RealityCapture reuse that calibration in Fisheye-to-Cubemap without requiring Agisoft Metashape.

The goal is not to translate RealityScan coefficients into a fake Metashape XML. The goal is to add a dedicated RealityScan XMP provider that reads RealityScan's own calibration model, converts source pixels into rays, and feeds those rays into the existing cubemap remap pipeline.

## Product position

RealityScan XMP should be treated as a real calibration source, but only when the XMP sidecars describe the same image geometry being processed by this repo.

Safe statement:

> Fisheye-to-Cubemap can use RealityScan/RealityCapture XMP sidecars as a calibration source when those XMP files were exported for the matching original distorted fisheye images and the contained distortion model is supported.

Unsafe statement:

> Any RealityScan XMP can be used as lens metadata.

The unsafe statement is wrong because XMP export settings can describe undistorted exports, resized images, or camera priors rather than the original fisheye pixels.

## Why this provider matters

Some users prefer to align fisheyes in RealityScan/RealityCapture instead of Metashape. If RealityScan has already estimated intrinsics, that calibration may be valuable for this repo's superior cubemap/COLMAP-conversion workflow.

RealityScan XMP support would give those users a path like:

```text
original fisheye images
        |
        v
RealityScan alignment/calibration
        |
        v
RealityScan XMP sidecars
        |
        v
Fisheye-to-Cubemap realityscan-xmp provider
        |
        v
per-pixel rays
        |
        v
5 pinhole cube faces + masks
```

## What RealityScan XMP is, and is not

RealityScan/RealityCapture XMP sidecars are not the same thing as generic EXIF metadata. They are intentional sidecar/export files used by RealityScan/RealityCapture to preserve or reuse alignment/camera information.

That makes them more trustworthy than random MakerNotes, but they are still model-specific and export-setting-specific.

Important distinction:

- Generic metadata probe: diagnostic only.
- RealityScan XMP provider: calibration provider, but only for supported XMP model labels and matching image geometry.

## Relevant documentation sources

- [RealityScan XMP metadata help](https://rshelp.capturingreality.com/en-US/tools/xmpalign.htm)
- [RealityCapture XMP camera math](https://dev.epicgames.com/community/learning/knowledge-base/vzwB/realityscan-realitycapture-xmp-camera-math)
- [RealityScan distortion model settings](https://rshelp.capturingreality.com/en-US/appbasics/settings_distortion_models.htm)
- [Faceform Wrap notes on RealityCapture XMP/CSV/FBX camera import](https://docs.faceform.com/Wrap/ImportCamerasToWrap/ImportCamerasFromRC/ImportCamerasFromRC.html)

## Fixture data needed from RealityScan

To implement this correctly, we need real exported fixtures. Screenshots or partial copied coefficients are not enough.

### Minimum fixture

```text
realityscan_fixture/
  README.md
  images/
    000020.jpg
    000034.jpg
  xmp/
    000020.xmp
    000034.xmp
```

Minimum metadata in the fixture README:

- RealityScan/RealityCapture version.
- Camera model.
- Lens side, if dual-fisheye.
- Whether images are original distorted fisheye frames.
- Whether images were resized, cropped, stabilized, dewarped, or undistorted before import.
- How XMP was exported.
- RealityScan export settings, especially undistortion-related settings.
- Whether XMP is per-image or shared/common.

### Ideal fixture

```text
realityscan_fixture/
  README.md
  original_images/
    lens0_000020.jpg
    lens0_000034.jpg
    lens1_000020.jpg
    lens1_000034.jpg
  xmp_original_distorted/
    lens0_000020.xmp
    lens0_000034.xmp
    lens1_000020.xmp
    lens1_000034.xmp
  exported_undistorted_images/
    ...
  xmp_undistorted_export/
    ...
  screenshots_or_notes/
    export_settings.png
    realityscan_version.txt
```

Why ideal fixture includes undistorted exports:

We need to learn how RealityScan marks, omits, or changes XMP fields when export settings describe undistorted images. That lets the provider warn or refuse ambiguous cases.

## XMP fields to parse

First-wave fields used for calibration:

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

Fields to parse and preserve, but not initially use for ray generation:

- `xcr:Rotation`
- `xcr:Position`
- `xcr:PosePrior`
- `xcr:Coordinates`
- `xcr:Rig`
- `xcr:RigInstance`
- `xcr:RigPoseIndex`

Why preserve extrinsics:

The cubemap conversion only needs intrinsics/rays at first. But preserving rotation/position/rig fields in the provider summary keeps the door open for future rig-aware exports or COLMAP conversion helpers.

## Distortion models to support

RealityScan's documented distortion model settings include:

- `No lens distortion`
- `Division`
- `Brown3`
- `Brown3 with tangential2`
- `Brown4 with tangential2`
- `K + Brown3 with tangential2`
- `K + Brown4 with tangential2`

Conservative support order:

1. Parse and report all labels.
2. Implement `No lens distortion`.
3. Implement Brown-family models after fixture confirmation.
4. Implement `Division` after fixture confirmation.
5. Hold `K + Brown*` behind explicit fixtures and tests.
6. Fail closed on unknown labels.

Fail closed means:

- Parse the XMP.
- Write a useful summary.
- Refuse ray generation.
- Explain the unsupported model label.
- Ask for fixture data if useful.

## Why image geometry is the main risk

The provider can parse coefficients correctly and still be wrong if the XMP applies to a different image geometry.

Examples of bad pairings:

- XMP describes undistorted exported images, but user passes original fisheye images.
- XMP describes original images, but user passes resized images.
- XMP describes lens0, but user passes lens1 images.
- XMP was exported after cropping or downscaling.
- Video frames were stabilized/dewarped before import.

The importer must therefore validate:

- XMP dimensions, if available.
- Paired image dimensions.
- Calibration/distortion groups.
- Distortion model label.
- Principal point range.
- Focal value plausibility.
- Whether all sidecars in one run share compatible intrinsics.

## Provider CLI shape

Planned command examples:

Single representative XMP:

```bash
python AM_ImageAndMask_to_cubemap_v4.py \
  --calibration path/to/000020.xmp \
  --calibration-provider realityscan-xmp \
  --lenslabel "RealityScan-Lens0" \
  --directoryfisheyeimages images/lens0 \
  --directoryfisheyemasks masks/lens0 \
  --facewidth 2100 \
  --outputdir output
```

Directory of sidecars:

```bash
python AM_ImageAndMask_to_cubemap_v4.py \
  --calibration path/to/xmp_dir \
  --calibration-provider realityscan-xmp \
  --lenslabel "RealityScan-Lens0" \
  --directoryfisheyeimages images/lens0 \
  --directoryfisheyemasks masks/lens0 \
  --facewidth 2100 \
  --outputdir output
```

Possible future flags:

- `--xmp-dir`
- `--image-width`
- `--image-height`
- `--lens-side`
- `--allow-per-image-intrinsics`
- `--allow-unknown-source-geometry`

Default behavior should be strict. Experimental bypass flags should be loud.

## Implementation plan

### Stage 1: parser and report only

Goal:

Parse RealityScan XMP sidecars and produce a useful structured summary, but do not generate rays yet.

Checklist:

- Add `calibration/realityscan_xmp.py`.
- Add provider routing for `realityscan-xmp`.
- Parse XMP namespaces with `xml.etree.ElementTree`.
- Support one XMP file.
- Support directory of XMP sidecars.
- Extract known `xcr:*` fields.
- Preserve unknown `xcr:*` fields in summary.
- Read paired image dimensions.
- Group sidecars by:
  - `CalibrationGroup`
  - `DistortionGroup`
  - model label
  - focal/principal/distortion values
- Write `bonusdata/calibration_provider_summary.json`.
- Fail closed before ray generation with clear "parser-only" status if model support is not enabled.

Acceptance criteria:

- Real fixture XMP parses.
- Summary includes all consumed fields.
- Unknown fields are visible.
- Multi-XMP directory reports whether intrinsics are identical or varied.
- Unsupported model labels produce clear errors.

### Stage 2: source-geometry validation

Goal:

Make the provider refuse likely-wrong image pairings.

Checklist:

- Confirm paired image exists for each XMP where possible.
- Confirm image dimensions are known.
- Detect missing dimensions in XMP and use paired image dimensions.
- Detect mixed dimensions in image directory.
- Warn on suspicious principal point or focal values.
- Add summary fields:
  - `source_geometry_validation`
  - `paired_images_found`
  - `observed_image_shapes`
  - `xmp_groups`
  - `warnings`
- Add tests for:
  - missing paired image;
  - mixed dimensions;
  - XMP directory with inconsistent groups;
  - missing model label.

Acceptance criteria:

- Provider refuses unknown dimensions.
- Provider refuses mixed image sizes.
- Provider warns or refuses inconsistent intrinsics in one lens batch.

### Stage 3: `No lens distortion`

Goal:

Implement the simplest possible RealityScan model.

Ray strategy:

- Convert pixel coordinates to RealityScan normalized image coordinates.
- Apply focal/aspect/skew/principal point transform.
- Generate unit rays.

Why this matters:

This validates the coordinate conversion without adding distortion inversion complexity.

Acceptance criteria:

- Synthetic no-distortion round-trip passes.
- Real no-distortion fixture, if available, produces plausible rays.

### Stage 4: Brown-family models

Goal:

Support documented Brown models after fixture confirmation.

Candidate labels:

- `Brown3`
- `Brown3 with tangential2`
- `Brown4 with tangential2`

Known coefficient order from documentation:

```text
k1 k2 k3 k4 t1 t2
```

Implementation tasks:

- Implement forward distortion for documented Brown model.
- Implement iterative inverse from distorted normalized coordinates to undistorted normalized coordinates.
- Convert undistorted normalized coordinates to rays.
- Add synthetic project/unproject tests.
- Add fixture-based tests.

Acceptance criteria:

- Synthetic round-trip angular/pixel error is below tolerance.
- Fixture summary confirms expected model label and coefficient count.
- Unknown or unsupported coefficient counts fail closed.

### Stage 5: Division model

Goal:

Support RealityScan `Division` model only after confirming inverse behavior with fixture data and documented math.

Implementation tasks:

- Implement documented forward model.
- Implement inverse or iterative solve.
- Add synthetic tests.
- Add fixture tests.

Acceptance criteria:

- Synthetic round-trip passes.
- Real fixture output is plausible.
- Model behavior is documented in provider summary.

### Stage 6: `K + Brown*` models

Goal:

Support `K + Brown3 with tangential2` and `K + Brown4 with tangential2` only with real fixture confirmation.

Reason for caution:

The `K +` prefix changes interpretation enough that guessing from non-`K` Brown models is unsafe.

Acceptance criteria:

- Real fixture with `K +` model exists.
- Implemented math matches documented equations.
- Synthetic and fixture tests pass.

### Stage 7: end-to-end conversion

Goal:

Run the provider through the full cubemap pipeline.

Checklist:

- Run with one lens fixture.
- Generate cube faces and masks.
- Write provider summary.
- Export raymap from RealityScan XMP rays.
- Re-run from raymap.
- Compare RealityScan-XMP output vs raymap output.

Acceptance criteria:

- XMP and raymap outputs match visually/statistically.
- Masks are stable.
- Provider summary contains enough debug information to reproduce the run.

## Provider summary requirements

RealityScan provider summaries should include:

- provider: `realityscan-xmp`
- RealityScan/RealityCapture version if present
- source XMP path(s)
- paired image path(s)
- image dimensions
- `xcr:DistortionModel`
- `xcr:DistortionCoeficients`
- `xcr:FocalLength35mm`
- `xcr:Skew`
- `xcr:AspectRatio`
- `xcr:PrincipalPointU`
- `xcr:PrincipalPointV`
- `xcr:CalibrationGroup`
- `xcr:DistortionGroup`
- ignored extrinsics
- unknown XMP fields
- source-geometry validation result
- warnings
- whether ray generation was enabled
- cache fingerprint

## Tests to add

Parser tests:

- Parses one XMP.
- Parses directory of XMP sidecars.
- Extracts known fields.
- Preserves unknown fields.
- Handles namespaces correctly.
- Fails on missing `DistortionModel`.

Grouping tests:

- Identical intrinsics across sidecars pass.
- Different `CalibrationGroup` in one lens batch warns or fails.
- Different `DistortionGroup` in one lens batch warns or fails.
- Per-image intrinsics require explicit opt-in.

Geometry tests:

- Missing paired image fails or warns depending mode.
- Mixed image dimensions fail.
- Explicit dimensions work when paired images are unavailable.

Math tests:

- No-distortion synthetic round-trip.
- Brown-family synthetic round-trip.
- Division synthetic round-trip.
- Fixture-based sample point checks.

End-to-end tests:

- Tiny synthetic XMP full pipeline.
- Real fixture full pipeline.
- XMP output vs exported raymap output comparison.

## GUI implications

RealityScan provider adds these GUI needs:

- Provider dropdown option:
  - `RealityScan XMP`
- File/folder picker:
  - one `.xmp`
  - directory of `.xmp`
- Warning text:
  - "XMP must describe the same original distorted fisheye images being processed."
- Summary fields:
  - distortion model
  - calibration group
  - distortion group
  - focal 35mm
  - principal point
  - sidecar count
  - warnings
- Optional future controls:
  - allow per-image intrinsics
  - explicit image width/height
  - lens side

GUI should not expose RealityScan as a normal ready provider until parser and at least one model implementation are validated.

## Open questions

- Which RealityScan export mode produces XMP for original distorted images?
- Which export mode produces XMP for undistorted images?
- Does RealityScan always include enough information to infer image dimensions?
- Are dual-fisheye lens sides reliably separated by `CalibrationGroup` or `DistortionGroup`?
- What model labels appear in real fisheye alignments?
- Do consumer 360 camera fisheyes commonly export as `Division`, Brown-family, or `K + Brown*`?
- Are `PrincipalPointU/V` normalized around image center consistently across versions?
- Does `FocalLength35mm` require sensor assumptions not present in XMP?

## What the user can do in RealityScan now

To prepare fixtures:

1. Create a small RealityScan project with original distorted fisheye images.
2. Keep one lens side separate if possible.
3. Align/calibrate the images.
4. Export XMP sidecars for the original images.
5. Record the exact export settings.
6. Note whether any undistortion/export-image step was used.
7. Save RealityScan version.
8. Keep the source images and XMP sidecars together.

Desired folder:

```text
fixtures/realityscan/
  README.md
  images/
  xmp/
  export-settings-notes/
```

## First implementation target

The first useful implementation should not attempt all math.

Recommended first PR:

1. Add `calibration/realityscan_xmp.py`.
2. Parse one XMP or directory.
3. Extract and summarize fields.
4. Validate paired image dimensions.
5. Fail closed before ray generation unless model is explicitly supported.
6. Add tests from real fixture.

This lets us inspect real RealityScan output safely before writing distortion inversion code.

## Final acceptance for RealityScan provider

RealityScan XMP support should be considered usable only when:

- At least one real fixture parses.
- At least one supported distortion model generates rays.
- Synthetic round-trip tests pass.
- XMP input and exported raymap input produce equivalent cubemap outputs.
- Provider summary explains all consumed and ignored fields.
- Unknown models fail closed with a clear error.
- Documentation explains original-vs-undistorted image geometry risk.
