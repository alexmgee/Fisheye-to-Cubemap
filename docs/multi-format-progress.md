# Multi-format calibration progress

Branch: `multi-format`

Last updated: 2026-05-31

## Stage status

| Stage | Status | Notes |
|---|---|---|
| Stage 0: preserve existing behavior | Partially validated | Legacy `--amlenscal` remains supported and is normalized through the provider layer. Full example validation still needs to run on real-size data. |
| Stage 1: provider foundation | Partially validated | Added `calibration/` package with core dataclasses, Metashape provider, provider summary writer, and planned-provider fail-closed errors. Tiny smoke run writes provider summary successfully. |
| Stage 2: raymap bridge | Partially validated | Added native raymap load/save helpers and CLI `--export-raymap`; raymap import path is wired through `--calibration-provider raymap`. Tiny smoke run succeeds from exported raymap. |
| Stage 3: OpenCV fisheye | Partially validated | Added JSON and OpenCV FileStorage YAML/XML loader. Uses `cv2.fisheye.undistortPoints` to generate dense rays. Tiny smoke run passes. |
| Stage 4: RealityScan XMP | Not started | Planned after fixtures are available; provider should fail closed on unknown model labels. |
| Stage 5: COLMAP | Partially validated | Added `cameras.txt` / sparse-directory parser for first-wave models. Tiny smoke run and synthetic round-trip pass. Poses/rigs are reporting-only future work. |
| Stage 6: metadata probe | Not started | Diagnostic-only; should redirect RealityScan XMP sidecars to the dedicated provider. |
| Stage 7: GUI updates | Not started | Wait until CLI provider semantics stabilize. |

## Implemented in this pass

- Added `calibration/core.py`.
- Added `Calibration` and `CalibrationSourceGeometry` dataclasses.
- Added `load_calibration()` with implemented providers:
  - `metashape`
  - `raymap`
  - `opencv`
  - `opencv-fisheye`
  - `colmap`
- Added planned-provider fail-closed errors for:
  - `realityscan-xmp`
  - `metadata`
- Added `calibration/colmap.py`.
- Added first-wave COLMAP provider support:
  - `SIMPLE_PINHOLE`
  - `PINHOLE`
  - `SIMPLE_FISHEYE`
  - `FISHEYE`
  - `SIMPLE_RADIAL_FISHEYE`
  - `RADIAL_FISHEYE`
  - `OPENCV_FISHEYE`
- Added `--camera-id` for multi-camera calibration files.
- Added `calibration/opencv.py`.
- Added OpenCV fisheye provider aliases:
  - `opencv`
  - `opencv-fisheye`
- Added `calibration/metashape.py`.
- Added `calibration/raymap.py`.
- Added provider summary JSON output:
  - `bonusdata/calibration_provider_summary.json`
- Added CLI flags:
  - `--calibration`
  - `--calibration-provider`
  - `--export-raymap`
  - `--raymap-compression`
- Kept legacy `--amlenscal` as a Metashape alias.
- Updated `README.md` calibration input docs for the multi-format branch.
- Reworked `docs/realityscan-full-fisheye-alignment-best-practices.md` into a step-by-step Osmo360 RealityScan XMP fixture checklist:
  - tailored to the available 100 synced front/back frame dataset with masks;
  - directs front and back lenses through separate RealityScan projects;
  - specifies original-distorted XMP export as the required provider fixture;
  - keeps undistorted XMP export as an optional reference;
  - includes exact folder layout, README template, RealityScan action phases, file audit, and provider test coverage.

## Validation notes

Validation run in this pass:

1. `python3 -m py_compile AM_ImageAndMask_to_cubemap_v4.py calibration/*.py`
   - Result: passed.
2. Metashape provider parse against `examples/Osmo360_front_adjusted.xml`.
   - Result: passed.
3. `PYTHONPATH=temp/python-deps python3 AM_ImageAndMask_to_cubemap_v4.py --version`
   - Result: passed.
4. `PYTHONPATH=temp/python-deps python3 AM_ImageAndMask_to_cubemap_v4.py --usage`
   - Result: passed.
5. Raymap helper round-trip on a tiny synthetic 8x8 ray field.
   - Result: passed.
6. COLMAP multi-camera fail-closed check without `--camera-id`.
   - Result: passed with expected error requesting `--camera-id`.
7. Tiny 16x16 synthetic end-to-end run from Metashape XML.
   - Result: passed; wrote cube faces, masks, `calibration_provider_summary.json`, and exported `temp/smoke/mini.raymap.npz`.
8. Tiny 16x16 synthetic end-to-end run from exported raymap.
   - Result: passed; generated cube faces and masks from `--calibration-provider raymap`.
9. Tiny 16x16 synthetic end-to-end run using legacy `--amlenscal`.
   - Result: passed; legacy flag routes through the Metashape provider.
10. Real front-lens example at source resolution 3840x3840 with `facewidth 256`, using legacy `--amlenscal`.
    - Result: passed.
11. Real front-lens example at source resolution 3840x3840 with `--calibration --calibration-provider metashape`, exporting `temp/real_validation/front.raymap.npz`.
    - Result: passed.
12. Real front-lens example at source resolution 3840x3840 with `--calibration-provider raymap`.
    - Result: passed.
13. Sampled output comparison between Metashape-provider and raymap-provider outputs.
    - Result: masks matched exactly in sampled outputs; images were visually/statistically equivalent but not bit-exact. Sample image max differences reached 255 on some lateral-face boundary pixels, with low mean differences (`<= 0.345` intensity over sampled images), consistent with float32 raymap precision at remap support boundaries.
14. OpenCV fisheye JSON loader using synthetic identity-distortion calibration.
    - Result: passed.
15. OpenCV FileStorage YAML loader using synthetic identity-distortion calibration.
    - Result: passed.
16. OpenCV fisheye center/unit ray check.
    - Result: passed.
17. Tiny 16x16 synthetic end-to-end run from OpenCV fisheye JSON.
    - Result: passed; generated cube faces, masks, and provider summary.
18. OpenCV fisheye synthetic project/unproject round-trip with nonzero distortion.
    - Result: passed; max sampled pixel reprojection error was `9.17e-07` px.
19. COLMAP `OPENCV_FISHEYE` synthetic project/unproject round-trip with nonzero distortion.
    - Result: passed; max sampled pixel reprojection error was `9.17e-07` px.
20. COLMAP first-wave model load tests.
    - Result: passed for `SIMPLE_PINHOLE`, `PINHOLE`, `SIMPLE_FISHEYE`, `FISHEYE`, `SIMPLE_RADIAL_FISHEYE`, `RADIAL_FISHEYE`, and `OPENCV_FISHEYE`.
21. COLMAP `--camera-id` selection test.
    - Result: passed.
22. Tiny 16x16 synthetic end-to-end run from COLMAP `OPENCV_FISHEYE` `cameras.txt`.
    - Result: passed; generated cube faces, masks, and provider summary.

Validation still needed:

1. Full-resolution example output at `facewidth 2100`.
2. Back-lens real example validation.
3. Wider output comparison between Metashape-input and raymap-input outputs.
4. GUI validation after GUI provider controls are added.

Validation environment:

- Dependencies were installed into `temp/python-deps` inside the repository root per project preference.
- Validation commands used `PYTHONPATH=temp/python-deps`.
- `temp/` is ignored by Git.

## Known gaps

- OpenCV fisheye provider needs real calibration fixture validation.
- RealityScan XMP provider is documented but not implemented.
- COLMAP provider needs real sparse-model fixture validation and optional pycolmap conformance tests.
- Metadata probe is documented but not implemented.
- GUI still references Metashape-specific calibration language.
- Provider registry is described in the plan but not implemented as a separate registry object yet.
- RealityScan fixture documentation is now focused on provider-ready XMP data, but the actual `realityscan-xmp` parser still requires real fixture data before implementation can be trusted.
