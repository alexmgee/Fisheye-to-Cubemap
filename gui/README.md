# Fisheye-to-Cubemap GUI v4

Standalone GUI for the fisheye cubeface converter and the Metashape-to-COLMAP export workflow.

## What It Does

The GUI supports two output purposes:

1. **Metashape alignment**
   - Generates cubeface images and masks for alignment in Metashape.
   - Shows the Station/Rig layout choice.
   - Preserves the original cubeface converter's output structure.

2. **COLMAP export**
   - Uses a Metashape alignment of the original fisheye images to create a training-ready COLMAP scene.
   - Treats cubefaces as internal intermediate files.
   - Hides Station/Rig layout complexity.
   - Writes a clean export root:

```text
output/
  colmap/
    images/
    masks/
    sparse/0/
      cameras.txt
      images.txt
      points3D.txt
  processing/
    remap_cache/
    manifests/
    logs/
  reports/
    conversion_report.txt
    validation_report.txt
    run_summary.txt
```

In COLMAP export mode, final training images are flat under `output/colmap/images/`, masks are flat under `output/colmap/masks/`, and reports/support files stay outside the final COLMAP scene.

## COLMAP Export Notes

The COLMAP export path is intended for Metashape projects where the original raw cameras have already been aligned. It can combine:

- equisolid fisheye images converted to cubeface pinhole cameras,
- additional aligned frame cameras, such as drone, DSLR, phone, or other normal photos,
- undistorted copies of those frame-camera images when their Metashape sensor has distortion,
- generated valid-pixel masks for those undistorted frame-camera images.

All final cameras written to `cameras.txt` are `PINHOLE`.

In the code and CLI, these additional non-fisheye image sets are called **passthrough media** because they do not go through the fisheye-to-cubeface conversion. They are still processed enough to be packaged into the final COLMAP scene; if needed, they are undistorted and given valid-pixel masks.

The original cubeface script remains unchanged. In COLMAP export mode, the GUI runs it into an internal temporary folder, the exporter packages the final scene, then temporary cubeface files are removed after success.

Processing files such as remap caches, manifests, and logs are kept by default because they are useful for review, debugging, and faster reruns. Uncheck **Keep processing files after successful export** when you want the exporter to leave only `output/colmap/` and `output/reports/`.

## Mapping Safety

In COLMAP export mode, every generated cubeface image needs the correct camera pose from Metashape. The mapping tells the exporter which original Metashape fisheye cameras belong to each GUI lens label, such as `Osmo360-front` or `Osmo360-back`.

This matters because a dual-fisheye capture often has matching frame numbers for both lenses. For example, `front/000001.jpg` and `back/000001.jpg` are different physical cameras, even though their filenames share the same stem. If the front and back camera runs are swapped, the exported COLMAP scene will have valid-looking files with the wrong poses.

Use **Check Mapping** before COLMAP export. The GUI uses a structured mapping resolver that checks:

- active lens labels,
- Metashape camera runs,
- group labels,
- sensor IDs,
- source filename stems,
- ID gaps,
- ambiguous or heuristic assignments,
- stale generated mapping text.

If the resolver can prove the mapping, export can proceed automatically. If the resolver can only propose a heuristic mapping, use **Use Proposed Map** to copy it into the manual field after review.

## Setup

Install the project requirements first, then install the GUI requirements:

```bash
pip install -r requirements.txt
pip install -r gui/requirements.txt
```

Python 3.10+ is recommended.

## Run

Double-click:

```text
GUI_v4.vbs
```

Or from a terminal:

```bash
cd gui
python gui.py
```

## Files

| File | Purpose |
|---|---|
| `gui.py` | GUI implementation |
| `mapping_resolver.py` | Structured lens-to-Metashape-camera mapping resolver |
| `GUI_v4.vbs` | Windows double-click launcher |
| `requirements.txt` | GUI dependencies |
| `Instructions.md` | Detailed field-by-field workflow guide |

## Detailed Instructions

See [Instructions.md](Instructions.md) for field-level explanations, output layouts, mapping checks, additional frame-camera media setup, and preview behavior.
