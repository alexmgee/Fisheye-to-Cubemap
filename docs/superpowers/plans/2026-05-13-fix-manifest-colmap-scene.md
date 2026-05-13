# Fix: Manifest-Driven COLMAP Scene Export

> **For agentic workers:** Execute this plan inline (no subagents), task by task. Pause after each task for user review.

**Problem:** The `--scene-manifest` code path generates cubefaces but never produces a COLMAP scene. After cubeface generation it writes a run report and exits. No `cameras.txt`, `images.txt`, `points3D.txt` are produced. No images are packaged. The feature is broken.

**Fix:** Wire the cubeface outputs into the existing `write_colmap_training_scene()` pipeline that already handles all COLMAP scene writing. This function already exists and works — the legacy `--lens-camera-map` path calls it. The manifest path just needs to call it too.

**Scope:** Single file — `metashape_cameras_to_colmap.py`, the manifest dispatch block (lines ~5060-5156).

**Spec:** `docs/superpowers/specs/2026-05-12-multi-camera-colmap-export-design.md`, section "Cubeface generation pipeline in COLMAP mode"

---

## Expected output directory structure

```
output_dir/                         (manifest "output_dir", e.g. D:\Capture\scene\COLMAP)
  colmap/                           (scene root — write_colmap_training_scene target)
    sparse/0/
      cameras.txt                   (PINHOLE cameras)
      images.txt                    (poses)
      points3D.txt                  (sparse points)
    images/                         (flat: cubeface PNGs + passthrough PNGs)
    masks/                          (flat: cubeface masks + passthrough masks)
  processing/                       (cubeface intermediates, remap cache)
    body_X5Camera1/
      sensor_0/images/...           (cubeface output from process_sensor)
      sensor_1/images/...
    remap_cache/                    (written by write_colmap_training_scene)
    manifests/                      (written by write_colmap_training_scene)
  reports/
    conversion_report.txt
    validation_report.txt
```

---

## Key functions (all exist in metashape_cameras_to_colmap.py)

| Function | Line | What it does |
|---|---|---|
| `parse_metashape_cameras_xml(path)` | ~635 | Parse cameras.xml → document dict with `cameras`, `sensors` |
| `discover_cubefaces(root)` | 1023 | Scan a directory tree for cubeface outputs → discovery dict with `lenses`, `stems` |
| `validate_lens_camera_map(document, discovery, mapping)` | 1722 | Match lens labels to XML camera IDs, resolve stems |
| `resolve_passthrough_media_sets(document, media_sets, sensor_ids)` | 1455 | Match frame sensor images to XML camera labels |
| `_with_unique_slugs(media_sets)` | 1238 | Add slug field to media set dicts |
| `write_colmap_training_scene(metashape_points, document, discovery, output_scene, ...)` | 3472 | The big one — composes poses, writes cameras.txt/images.txt/points3D.txt, packages assets |
| `validate_colmap_model(output_dir, ...)` | ~4314 | Validates output file counts match |
| `empty_cubeface_discovery(root)` | 1100 | Returns empty discovery dict (for frame-only scenes) |

---

## Data flow

```
manifest JSON
  │
  ├─► parse_metashape_cameras_xml(manifest["cameras_xml"])
  │     → document dict (cameras with sensor_ids, transforms, labels)
  │
  ├─► process_sensor() for each fisheye sensor (ALREADY WORKS)
  │     → cubeface images in processing/body_X/sensor_N/images/
  │
  ├─► discover_cubefaces(output_dir / "processing")
  │     → discovery dict (lenses found, stems, image paths)
  │     → lens labels like "body_X/sensor_N"
  │
  ├─► build lens_camera_mapping: {lens_label: (camera_id, ...)}
  │     → map each XML camera to its sensor_id
  │     → map each sensor_id to the lens_label from discovery
  │
  ├─► validate_lens_camera_map(document, discovery, mapping)
  │     → resolved lens_map with stem-to-camera matches
  │
  ├─► resolve_passthrough_media_sets(document, media_sets, sensor_ids)
  │     → resolved passthrough_map (if frame sensors exist)
  │
  └─► write_colmap_training_scene(
        metashape_points, document, discovery,
        output_scene = output_dir / "colmap",
        lens_map, passthrough_map, ...)
        → cameras.txt, images.txt, points3D.txt
        → packaged images/ and masks/
```

---

## Task 1: Replace the manifest dispatch exit with COLMAP pipeline wiring

**File:** `metashape_cameras_to_colmap.py`

The current manifest block runs from ~line 5060 to `return 0` at line 5156. The cubeface generation loop (5078-5122) and frame sensor loop (5124-5149) stay. Everything between the frame sensor loop and `return 0` gets replaced.

- [ ] **Step 1: Read the current manifest dispatch block**

Read lines 5058-5156 to understand the current structure. Identify:
- Where `sensor_results` is built (cubeface loop and frame loop)
- Where `_write_manifest_run_report` is called
- Where `return 0` is

- [ ] **Step 2: After the frame sensor loop, add XML parsing**

Insert after line ~5149 (end of frame sensor loop), before `_write_manifest_run_report`:

```python
            # ── COLMAP scene export ─────────────────────────────
            cameras_xml_path = Path(manifest["cameras_xml"])
            sparse_ply_path = Path(manifest["sparse_ply"]) if manifest.get("sparse_ply") else None
            document = parse_metashape_cameras_xml(cameras_xml_path)
            opts = manifest.get("options", {})
```

- [ ] **Step 3: Discover cubefaces from the processing directory**

```python
            cubeface_root = output_dir / "processing"
            if cubeface_root.is_dir():
                discovery = discover_cubefaces(cubeface_root)
            else:
                discovery = empty_cubeface_discovery(cubeface_root)
```

- [ ] **Step 4: Build the lens-camera mapping**

This is the critical wiring. Each fisheye sensor's cubeface output directory becomes a "lens" in discovery. The lens label is the relative path under `cubeface_root`, e.g., `body_X5Camera1/sensor_0`. We need to map each lens label to the XML camera IDs that belong to that sensor.

```python
            # Map sensor_id → lens_label (from sensor_results)
            sensor_id_to_lens_label = {}
            for sr in sensor_results:
                if sr["type"] == "fisheye" and sr["status"] == "ok":
                    sr_output = Path(sr["output_dir"])
                    try:
                        rel = sr_output.relative_to(cubeface_root).as_posix()
                    except ValueError:
                        rel = sr_output.name
                    sensor_id_to_lens_label[sr["sensor_id"]] = rel

            # Map lens_label → tuple of camera_ids (from XML)
            lens_camera_mapping = {}
            for camera_id, camera in document["cameras"].items():
                sid = int(camera["sensor_id"])
                if sid in sensor_id_to_lens_label:
                    label = sensor_id_to_lens_label[sid]
                    lens_camera_mapping.setdefault(label, []).append(camera_id)
            for label in lens_camera_mapping:
                lens_camera_mapping[label] = tuple(sorted(lens_camera_mapping[label]))

            lens_map = validate_lens_camera_map(document, discovery, lens_camera_mapping)
            print(f"  Lens map: {len(lens_map['resolutions'])} cubeface stems resolved",
                  file=sys.stderr)
```

- [ ] **Step 5: Build passthrough map for frame sensors**

```python
            passthrough_map = None
            frame_ok = [sr for sr in sensor_results
                        if sr["type"] == "frame" and sr["status"] == "ok"]
            if frame_ok:
                media_sets = []
                frame_sensor_ids = []
                for sr in frame_ok:
                    media_sets.append({
                        "name": f"sensor_{sr['sensor_id']}",
                        "image_root": Path(sr["image_dir"]),
                        "mask_root": None,
                    })
                    frame_sensor_ids.append(sr["sensor_id"])
                media_sets = _with_unique_slugs(media_sets)
                passthrough_map = resolve_passthrough_media_sets(
                    document, media_sets, frame_sensor_ids,
                    require_masks=opts.get("require_masks", False),
                )
                print(f"  Passthrough: {passthrough_map['resolved_count']} frame images resolved",
                      file=sys.stderr)
```

- [ ] **Step 6: Call write_colmap_training_scene**

```python
            scene_output = output_dir / "colmap"
            support_dir = output_dir / "processing"
            reports_dir = output_dir / "reports"

            print(f"  Writing COLMAP scene to {scene_output}", file=sys.stderr)
            colmap_result = write_colmap_training_scene(
                sparse_ply_path,
                document,
                discovery,
                scene_output,
                lens_map=lens_map,
                passthrough_map=passthrough_map,
                pose_convention=opts.get("pose_convention", "metashape_camera_to_world"),
                package_assets=True,
                force_assets=opts.get("force_assets", False),
                support_output_dir=support_dir,
                reports_output_dir=reports_dir,
                keep_processing_files=opts.get("keep_processing_files", True),
                progress=getattr(args, "progress", False),
                progress_interval=getattr(args, "progress_interval", 250),
                require_masks=opts.get("require_masks", False),
                normalize_scene=opts.get("normalize_scene", False),
                projected_tracks=opts.get("projected_tracks", True),
                strict_pinhole=True,
                undistort_passthrough="auto",
                passthrough_output_format="png",
            )
            print(f"  COLMAP scene complete: {colmap_result.get('camera_count', '?')} cameras, "
                  f"{colmap_result.get('image_count', '?')} images, "
                  f"{colmap_result.get('point_count', '?')} points",
                  file=sys.stderr)
```

- [ ] **Step 7: Validate the output**

```python
            validate_colmap_model(
                scene_output / "sparse" / "0",
                expected_cameras=colmap_result.get("camera_count"),
                expected_images=colmap_result.get("image_count"),
            )
            print(f"  Validation passed", file=sys.stderr)
```

- [ ] **Step 8: Update the run report and keep `return 0`**

Move `_write_manifest_run_report` AFTER the COLMAP pipeline, and add the COLMAP stats to it:

```python
            # Add COLMAP stats to sensor_results for the run report
            sensor_results.append({
                "type": "colmap_scene",
                "status": "ok",
                "output_dir": str(scene_output),
                "camera_count": colmap_result.get("camera_count", 0),
                "image_count": colmap_result.get("image_count", 0),
                "point_count": colmap_result.get("point_count", 0),
            })

            total_elapsed = _time.perf_counter() - run_start
            _write_manifest_run_report(output_dir, manifest, sensor_results, total_elapsed)
            return 0
```

Remove the old `total_elapsed` and `_write_manifest_run_report` and `return 0` that currently sit right after the frame sensor loop.

- [ ] **Step 9: Commit**

```bash
git add metashape_cameras_to_colmap.py
git commit -m "fix: wire manifest cubeface outputs into write_colmap_training_scene pipeline"
```

---

## Task 2: Verify

- [ ] **Step 1: Run all tests**

```bash
cd d:\Projects\Fisheye-to-Cubemap
python -m pytest tests/ -v
```

Expected: all pass (no regression)

- [ ] **Step 2: Verify output structure exists**

After running a COLMAP export from the GUI with `--scene-manifest`, check that ALL of these exist:

```bash
# Replace with actual output_dir from your test run
ls output_dir/colmap/sparse/0/cameras.txt
ls output_dir/colmap/sparse/0/images.txt
ls output_dir/colmap/sparse/0/points3D.txt
ls output_dir/colmap/images/
ls output_dir/colmap/masks/
ls output_dir/reports/
```

Every camera in `cameras.txt` must be `PINHOLE`. The `images/` directory must contain flat PNGs (cubeface images with names like `cam1_front_0001_dir_plusZ.png`). The `masks/` directory must contain matching mask PNGs.

- [ ] **Step 3: Verify camera/image counts are sane**

```bash
# Count lines in cameras.txt (minus comments)
grep -v "^#" output_dir/colmap/sparse/0/cameras.txt | wc -l

# Count image pairs in images.txt (2 lines per image, minus comments)  
grep -v "^#" output_dir/colmap/sparse/0/images.txt | wc -l
# Divide by 2 = number of images

# Count files in images/
ls output_dir/colmap/images/ | wc -l
```

The image count from `images.txt` should match the file count in `images/`. Each fisheye frame produces 5 cubeface images, so 100 frames × 4 sensors × 5 faces = 2000 images for the test case with 4 sensors of 100 frames each.

- [ ] **Step 4: Commit any fixes**

```bash
git add -u
git commit -m "fix: corrections from COLMAP scene export verification"
```

---

## What NOT to change

- The cubeface generation loop (lines 5078-5122) — it works correctly
- The `process_sensor()` function — it works correctly
- The `load_scene_manifest()` function — it works correctly
- The legacy `--lens-camera-map` / `--cubeface-root` code path — don't touch it
- `write_colmap_training_scene()` internals — call it, don't modify it
- `gui/gui.py` — no GUI changes needed for this fix
