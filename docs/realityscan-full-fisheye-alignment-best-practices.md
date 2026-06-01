# RealityScan XMP Fixture Checklist

Date: 2026-06-01

Purpose: create RealityScan/RealityCapture XMP fixture data for the planned `realityscan-xmp` provider.

Your current test case:

- DJI Osmo360 source.
- 100 synced front/back fisheye frames.
- Existing masks.
- Already aligned in Metashape and already useful for validating this repo's cubemap/COLMAP work.

Follow this like a lab protocol. The output we need is a clean, documented set of RealityScan XMP sidecars that match the exact original fisheye images.

## Target Output

Create this folder package:

```text
temp/realityscan_osmo360_xmp_fixture/
  README.md
  front/
    original_images/
    masks/
    xmp_original_distorted/
    xmp_undistorted_reference/
    export_notes/
  back/
    original_images/
    masks/
    xmp_original_distorted/
    xmp_undistorted_reference/
    export_notes/
  comparison/
    cameras_Fit.xml
    cameras_noFit.xml
    Osmo360_front.xml
    Osmo360_front_Fit.xml
    Osmo360_back.xml
    Osmo360_back_Fit.xml
    notes.md
```

Use `temp/` because local fixture data should not be committed unless we intentionally promote a tiny sanitized fixture later.

## Success Criteria

The fixture is useful if all of these are true:

- Front and back lenses are kept separate.
- Each lens has original distorted fisheye images.
- Each lens has matching RealityScan XMP sidecars for original distorted images.
- Export settings are documented with screenshots or notes.
- The RealityScan version is recorded.
- The XMP files were not manually edited.
- The images were not silently resized, cropped, stabilized, dewarped, renamed, or replaced after export.

The fixture is still useful if RealityScan alignment is imperfect, as long as some images register and export valid XMPs. Document exactly what happened.

## Phase 1: Prepare The Fixture Folder

Do this before opening RealityScan.

- [ ] Create:

```text
temp/realityscan_osmo360_xmp_fixture/
```

- [ ] Inside it, create:

```text
front/original_images/
front/masks/
front/xmp_original_distorted/
front/xmp_undistorted_reference/
front/export_notes/
back/original_images/
back/masks/
back/xmp_original_distorted/
back/xmp_undistorted_reference/
back/export_notes/
comparison/
```

- [ ] Copy the 100 front-lens original fisheye frames into:

```text
front/original_images/
```

- [ ] Copy the 100 back-lens original fisheye frames into:

```text
back/original_images/
```

- [ ] Copy the front masks into:

```text
front/masks/
```

- [ ] Copy the back masks into:

```text
back/masks/
```

- [ ] Copy the Metashape XML files available from the previous validation into:

```text
comparison/
```

- [ ] Include the combined camera XMLs if available:

```text
comparison/cameras_Fit.xml
comparison/cameras_noFit.xml
```

- [ ] Include the per-lens XMLs if available:

```text
comparison/Osmo360_front.xml
comparison/Osmo360_front_Fit.xml
comparison/Osmo360_back.xml
comparison/Osmo360_back_Fit.xml
```

- [ ] If only one combined Metashape XML is available, that is still acceptable. Put it in `comparison/` and name it clearly.

- [ ] In `comparison/notes.md`, record what each XML represents:

```text
cameras_Fit.xml: combined Metashape camera export after Optimize Cameras with Apply Additional Corrections enabled.
cameras_noFit.xml: combined Metashape camera export after Optimize Cameras with Apply Additional Corrections disabled.
Osmo360_front.xml: front-lens calibration export without the additional-corrections fit variant.
Osmo360_front_Fit.xml: front-lens calibration export with the additional-corrections fit variant.
Osmo360_back.xml: back-lens calibration export without the additional-corrections fit variant.
Osmo360_back_Fit.xml: back-lens calibration export with the additional-corrections fit variant.
```

- [ ] If any of these descriptions are wrong for your actual files, correct `comparison/notes.md`. The notes matter more than the exact filenames.

- [ ] Do not rename the images unless necessary.

- [ ] If you must rename anything, create a rename map:

```text
comparison/rename_map.csv
```

with columns:

```text
original_name,new_name,lens
```

## Phase 2: Create The Fixture README

Create:

```text
temp/realityscan_osmo360_xmp_fixture/README.md
```

Paste this template and fill it in as you work:

```markdown
# Osmo360 RealityScan XMP Fixture

## Software

- RealityScan or RealityCapture version:
- Operating system:
- Date created:

## Dataset

- Camera: DJI Osmo360
- Lens sets included: front, back
- Frames per lens: 100
- Are frames synced front/back pairs:
- Original image dimensions:
- Image file format:
- Masks included:
- Were images renamed for RealityScan:
- Rename map path, if any:

## Image Geometry

- Are front images original distorted fisheye images:
- Are back images original distorted fisheye images:
- Were images resized before RealityScan import:
- Were images cropped before RealityScan import:
- Were images stabilized/dewarped/undistorted before RealityScan import:
- Were images modified after RealityScan export:

## Front Lens RealityScan Attempt

- Project filename:
- Images imported:
- Masks imported:
- Images aligned:
- Component count:
- Distortion model:
- Lens distortion prior:
- Calibration group:
- Distortion group:
- Notes:

## Front Lens XMP Export

- Export command/menu used:
- Exported original-distorted XMPs:
- Exported undistorted-reference XMPs:
- Export settings screenshot or notes path:
- Any warnings:

## Back Lens RealityScan Attempt

- Project filename:
- Images imported:
- Masks imported:
- Images aligned:
- Component count:
- Distortion model:
- Lens distortion prior:
- Calibration group:
- Distortion group:
- Notes:

## Back Lens XMP Export

- Export command/menu used:
- Exported original-distorted XMPs:
- Exported undistorted-reference XMPs:
- Export settings screenshot or notes path:
- Any warnings:

## Known Concerns

- Missing files:
- Failed alignment areas:
- Ambiguous export settings:
- Anything that might make XMPs not match images:
```

## Phase 3: RealityScan Front-Lens Project

Do front and back as separate RealityScan projects first. This is simpler for provider work because each physical lens can have its own intrinsics.

- [ ] Open RealityScan.

- [ ] Create a new scene/project.

- [ ] Import only:

```text
temp/realityscan_osmo360_xmp_fixture/front/original_images/
```

- [ ] If importing masks is easy with your existing mask naming, import the front masks too.

- [ ] If RealityScan does not immediately recognize the masks, do not spend much time fixing masks on the first pass. Note that masks were not used and continue. The XMP fixture is about calibration metadata first.

- [ ] Select all front images.

- [ ] Set pose priors to `Unknown` unless you intentionally want RealityScan to use existing trusted priors.

- [ ] Use one calibration/lens grouping for the front lens.

- [ ] Set the lens distortion model to `Division` for the first attempt.

- [ ] Set lens distortion prior to `Unknown` unless you are intentionally importing a known RealityScan-compatible prior.

- [ ] Run alignment.

- [ ] Record in the README:

```text
images imported
images aligned
component count
distortion model
lens distortion prior
calibration group
distortion group
```

- [ ] Save the project as:

```text
temp/realityscan_osmo360_xmp_fixture/front/export_notes/front_lens.rsproj
```

If alignment fails completely:

- [ ] Save the project anyway.
- [ ] Record the failure in the README.
- [ ] Try one recovery pass with higher feature settings only if you want to.
- [ ] Do not start changing image geometry to make alignment work unless you document every change.

## Phase 4: Export Front Original-Distorted XMPs

This is the most important export.

- [ ] In RealityScan, use the registration/XMP export path.

- [ ] Choose a RealityScan XMP export format.

- [ ] Export XMPs that correspond to the original distorted images.

In RealityScan XMP export wording, this means:

- [ ] If there is an `Undistort images` option, set it to `No` for this export.

- [ ] If there is a newer `XMPs with Image List` export, choose the option corresponding to original distorted images.

- [ ] Export into:

```text
temp/realityscan_osmo360_xmp_fixture/front/xmp_original_distorted/
```

- [ ] Take a screenshot of the export dialog/settings and save it into:

```text
temp/realityscan_osmo360_xmp_fixture/front/export_notes/
```

- [ ] Confirm that XMP filenames correspond clearly to the original image filenames.

- [ ] Open one XMP in a text editor and confirm it contains RealityScan/RealityCapture fields such as `xcr:DistortionModel` or similar `xcr:*` entries.

- [ ] Do not edit the XMP.

## Phase 5: Export Front Undistorted-Reference XMPs

This is optional but very useful. It helps us detect how RealityScan changes sidecars when the export targets undistorted images.

- [ ] Repeat the XMP export.

- [ ] This time export XMPs corresponding to undistorted images.

In RealityScan XMP export wording, this means:

- [ ] If there is an `Undistort images` option, set it to `Yes` for this export.

- [ ] Export into:

```text
temp/realityscan_osmo360_xmp_fixture/front/xmp_undistorted_reference/
```

- [ ] Save a screenshot or notes for this export into:

```text
temp/realityscan_osmo360_xmp_fixture/front/export_notes/
```

- [ ] If RealityScan also exports undistorted image files, keep them in a separate folder named:

```text
front/undistorted_reference_images/
```

Do not mix these with `front/original_images/`.

## Phase 6: RealityScan Back-Lens Project

Repeat the same process for the back lens.

- [ ] Create a new scene/project.

- [ ] Import only:

```text
temp/realityscan_osmo360_xmp_fixture/back/original_images/
```

- [ ] Import back masks only if RealityScan recognizes them cleanly.

- [ ] Select all back images.

- [ ] Set pose priors to `Unknown` unless intentionally using trusted priors.

- [ ] Use one calibration/lens grouping for the back lens.

- [ ] Set the lens distortion model to `Division` for the first attempt.

- [ ] Set lens distortion prior to `Unknown` unless intentionally using a known prior.

- [ ] Run alignment.

- [ ] Record the same alignment details in the README.

- [ ] Save the project as:

```text
temp/realityscan_osmo360_xmp_fixture/back/export_notes/back_lens.rsproj
```

## Phase 7: Export Back XMPs

Repeat both XMP exports for the back lens.

- [ ] Export original-distorted XMPs into:

```text
temp/realityscan_osmo360_xmp_fixture/back/xmp_original_distorted/
```

- [ ] Save export settings screenshot/notes into:

```text
temp/realityscan_osmo360_xmp_fixture/back/export_notes/
```

- [ ] Optional but useful: export undistorted-reference XMPs into:

```text
temp/realityscan_osmo360_xmp_fixture/back/xmp_undistorted_reference/
```

- [ ] If RealityScan exports undistorted back images, keep them in:

```text
back/undistorted_reference_images/
```

Do not mix these with `back/original_images/`.

## Phase 8: Quick File Audit

After exporting, check the folder package.

- [ ] `front/original_images/` contains only original front fisheye images.

- [ ] `back/original_images/` contains only original back fisheye images.

- [ ] `front/xmp_original_distorted/` contains RealityScan XMPs for the front original distorted images.

- [ ] `back/xmp_original_distorted/` contains RealityScan XMPs for the back original distorted images.

- [ ] Optional `xmp_undistorted_reference/` folders are separate from original-distorted XMP folders.

- [ ] Every XMP has an obvious matching image, or the mismatch is documented.

- [ ] Front XMPs and back XMPs are not mixed.

- [ ] Export screenshots/notes exist for each XMP export.

- [ ] README is filled in.

## Phase 9: What To Send Back To Implementation

The most useful files are:

- `README.md`
- `front/original_images/`
- `front/xmp_original_distorted/`
- `front/export_notes/`
- `back/original_images/`
- `back/xmp_original_distorted/`
- `back/export_notes/`
- optional `front/xmp_undistorted_reference/`
- optional `back/xmp_undistorted_reference/`
- optional undistorted reference images, if RealityScan exported them
- `comparison/cameras_Fit.xml`
- `comparison/cameras_noFit.xml`
- `comparison/Osmo360_front.xml`
- `comparison/Osmo360_front_Fit.xml`
- `comparison/Osmo360_back.xml`
- `comparison/Osmo360_back_Fit.xml`
- `comparison/notes.md`

If the package is too large, start with:

- README
- 2-5 front original images
- matching front original-distorted XMPs
- 2-5 back original images
- matching back original-distorted XMPs
- export settings screenshots
- `comparison/cameras_Fit.xml`
- `comparison/cameras_noFit.xml`
- `comparison/notes.md`

## Phase 10: What The Provider Will Test With This

Once the fixture exists, implementation can test:

- Parse one RealityScan XMP.
- Parse a directory of RealityScan XMPs.
- Extract `xcr:DistortionModel`.
- Extract `xcr:DistortionCoeficients`.
- Extract focal, skew, aspect, and principal point fields.
- Extract calibration and distortion groups.
- Compare front and back groups.
- Confirm image dimensions match the source files.
- Reject mixed front/back inputs unless explicitly allowed.
- Reject undistorted-reference XMPs when paired with original distorted images.
- Fail closed on unsupported model labels.
- Generate rays only after model math is implemented and validated.
- Export an XMP-derived raymap.
- Re-run from that raymap and compare cubemap output.

## Fields We Hope To See In The XMPs

These are the important first-wave fields:

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

These may appear too. Preserve them, but they are not the first ray-generation target:

- `xcr:Rotation`
- `xcr:Position`
- `xcr:PosePrior`
- `xcr:Coordinates`
- `xcr:Rig`
- `xcr:RigInstance`
- `xcr:RigPoseIndex`

Unknown `xcr:*` fields are useful. Do not remove them.

## Important Do-Nots

- Do not mix front and back lenses in the same XMP fixture folder.
- Do not pair undistorted XMPs with original distorted images.
- Do not pair original-distorted XMPs with undistorted images.
- Do not manually edit XMP coefficients.
- Do not resize or crop images after XMP export.
- Do not overwrite the original images with RealityScan exports.
- Do not rely on copied coefficients without the actual XMP sidecars.
- Do not spend time polishing RealityScan output beyond what is needed to export valid XMPs.

## Sources

- [RealityScan XMP metadata help](https://rshelp.capturingreality.com/en-US/tools/xmpalign.htm)
- [RealityScan Image Layers help](https://rshelp.capturingreality.com/en-US/tools/imglayers.htm)
- [RealityScan Mask Images help](https://rshelp.capturingreality.com/en-US/tools/mask.htm)
- [RealityScan 2.1.1 release notes](https://dev.epicgames.com/documentation/realityscan/realityscan-2-1-1?lang=en-US)
