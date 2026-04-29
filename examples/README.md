# Worked example (COMING SOON)

A small dataset and matching script output will land here so a fresh clone can verify a successful first run end-to-end.

## Planned contents (in-repo)

| File | Purpose |
|------|---------|
| `lens_calibration.xml` | Agisoft Metashape lens calibration export(s) for the example camera. |
| `images/` | 1–2 dual-fisheye source frames. |
| `masks/` | Matching per-image PNG masks (0 = ignore, 255 = use). |
| `expected_output/` | One or two full-resolution cube faces from a successful run, for visual comparison. |
| `run_example.bat` / `run_example.sh` | Exact command line to reproduce the run. |

## Expanded future contents (GitHub Release)

A larger multi-frame dataset with full output (cube faces, masks, `bonusdata/`) will be attached as a downloadable `.zip` to a tagged release, for users who want to see the full pipeline including downstream alignment.

## Notes

- Source frames will have any identifying content (faces, etc.) blurred **before** processing. Any blur visible in cube face output is therefore from the source frames, not from the reprojection.
- Sample data redistribution is being confirmed by the authors.
