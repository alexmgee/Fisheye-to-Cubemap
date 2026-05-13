# Fix: Manifest-Driven COLMAP Scene Export

> **For agentic workers:** Execute inline, no subagents. This is a single-file fix. Read the ENTIRE plan before touching any code.

**Problem:** The `--scene-manifest` code path in `metashape_cameras_to_colmap.py` generates cubefaces but never produces a COLMAP scene. After cubeface generation it writes a run report and returns 0. No `cameras.txt`, `images.txt`, or `points3D.txt` are produced. The feature is broken.

**Root cause:** Lines 5060-5156 of `metashape_cameras_to_colmap.py` call `process_sensor()` for cubeface generation, then exit without ever calling `write_colmap_training_scene()`.

**Fix:** Replace lines 5060-5156 with a complete block that generates cubefaces AND then calls the existing COLMAP pipeline functions.

---

## Expected output

```
output_dir/
  colmap/
    sparse/0/
      cameras.txt       ← PINHOLE cameras
      images.txt        ← posed images
      points3D.txt      ← sparse points
    images/             ← flat cubeface + passthrough PNGs
    masks/              ← flat mask PNGs
  processing/
    body_*/sensor_*/    ← cubeface intermediates
    remap_cache/
    manifests/
  reports/
    conversion_report.txt
    validation_report.txt
```

---

## The fix

**File:** `metashape_cameras_to_colmap.py`

**Action:** Replace the entire `if args.scene_manifest is not None:` block (lines 5060-5156) with the code below. This is a wholesale replacement — do not try to merge or patch incrementally.

The replacement block does everything the current block does (cubeface generation, frame sensor logging) PLUS the missing COLMAP pipeline wiring.

### Complete replacement code

Find this line (approximately line 5060):
```python
        if args.scene_manifest is not None:
```

Replace everything from that line through and including `return 0` (approximately line 5156) with the following single block:

```python
        if args.scene_manifest is not None:
            manifest = load_scene_manifest(args.scene_manifest)
            print(f"Loaded scene manifest from {args.scene_manifest}", file=sys.stderr)
            print(f"Manifest: {len(manifest['bodies'])} bodies, "
                  f"{len(manifest['frame_sensors'])} frame sensors", file=sys.stderr)

            from AM_ImageAndMask_to_cubemap_v4 import process_sensor as _process_sensor
            import time as _time

            output_dir = Path(manifest["output_dir"])
            output_dir.mkdir(parents=True, exist_ok=True)
            opts = manifest.get("options", {})

            run_start = _time.perf_counter()
            sensor_results = []

            # ── Phase 1: Generate cubefaces for each fisheye sensor ──
            for body in manifest["bodies"]:
                body_name = body["name"]
                body_width = body.get("output_width", 2048)
                for sensor in body.get("sensors", []):
                    sensor_id = sensor["sensor_id"]
                    cal_xml = sensor.get("calibration_xml")
                    image_dir = sensor.get("image_dir")
                    mask = sensor.get("mask")
                    if not cal_xml or not image_dir:
                        print(f"  Skipping sensor {sensor_id} in body '{body_name}': "
                              f"missing calibration_xml or image_dir", file=sys.stderr)
                        sensor_results.append({
                            "type": "fisheye", "sensor_id": sensor_id,
                            "body": body_name, "status": "skipped",
                            "reason": "missing calibration_xml or image_dir",
                        })
                        continue
                    sensor_output = (output_dir / "processing"
                                     / f"body_{body_name}" / f"sensor_{sensor_id}")
                    print(f"  Processing sensor {sensor_id} (body '{body_name}') "
                          f"-> {sensor_output}", file=sys.stderr)
                    t0 = _time.perf_counter()
                    result = _process_sensor(
                        calibration_xml=Path(cal_xml),
                        image_dir=Path(image_dir),
                        mask=Path(mask) if mask else None,
                        output_dir=sensor_output,
                        face_width=body_width,
                        output_format="png",
                        force=opts.get("force_assets", False),
                        cache_remapping=True,
                        progress_callback=lambda msg: print(f"    {msg}", file=sys.stderr),
                    )
                    elapsed = _time.perf_counter() - t0
                    sensor_results.append({
                        "type": "fisheye", "sensor_id": sensor_id,
                        "body": body_name, "status": "ok",
                        "face_width": body_width,
                        "output_dir": str(sensor_output),
                        "processed": result.get("processed_count", 0),
                        "skipped": result.get("skipped_count", 0),
                        "elapsed_s": round(elapsed, 1),
                    })

            # ── Phase 2: Parse XML and discover generated cubefaces ──
            cameras_xml_path = Path(manifest["cameras_xml"])
            sparse_ply_path = (Path(manifest["sparse_ply"])
                               if manifest.get("sparse_ply") else None)
            document = parse_metashape_cameras_xml(cameras_xml_path)

            cubeface_root = output_dir / "processing"
            if cubeface_root.is_dir():
                discovery = discover_cubefaces(cubeface_root)
            else:
                discovery = empty_cubeface_discovery(cubeface_root)
            print(f"  Discovered {discovery['image_count']} cubeface images "
                  f"across {discovery['lens_count']} lenses", file=sys.stderr)

            # ── Phase 3: Build lens-camera mapping ──
            # Map sensor_id → lens_label (relative path under cubeface_root)
            sensor_id_to_lens_label = {}
            for sr in sensor_results:
                if sr["type"] == "fisheye" and sr["status"] == "ok":
                    sr_output = Path(sr["output_dir"])
                    try:
                        rel = sr_output.relative_to(cubeface_root).as_posix()
                    except ValueError:
                        rel = sr_output.name
                    sensor_id_to_lens_label[sr["sensor_id"]] = rel

            # Map lens_label → tuple of camera_ids (from XML cameras)
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

            # ── Phase 4: Build passthrough map for frame sensors ──
            passthrough_map = None
            frame_ok = [sr for sr in sensor_results
                        if sr["type"] == "frame" and sr["status"] == "ok"]
            # Also handle frame sensors that haven't been through sensor_results
            # (they were only logged, not processed in Phase 1)
            for fs in manifest.get("frame_sensors", []):
                fs_id = fs["sensor_id"]
                fs_image_dir = fs.get("image_dir")
                if not fs_image_dir:
                    continue
                fs_path = Path(fs_image_dir)
                if not fs_path.is_dir():
                    print(f"  Frame sensor {fs_id}: image_dir not found: {fs_image_dir}",
                          file=sys.stderr)
                    continue
                # Check if already in frame_ok
                already = any(sr["sensor_id"] == fs_id for sr in frame_ok)
                if not already:
                    frame_ok.append({
                        "type": "frame", "sensor_id": fs_id,
                        "status": "ok", "image_dir": str(fs_image_dir),
                    })

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
                print(f"  Passthrough: {passthrough_map['resolved_count']} "
                      f"frame images resolved", file=sys.stderr)

            # ── Phase 5: Write COLMAP training scene ──
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

            print(f"  COLMAP scene: {colmap_result.get('camera_count', '?')} cameras, "
                  f"{colmap_result.get('image_count', '?')} images, "
                  f"{colmap_result.get('point_count', '?')} points", file=sys.stderr)

            # ── Phase 6: Validate output files exist ──
            validate_colmap_model(
                scene_output / "sparse" / "0",
                expected_cameras=colmap_result["camera_count"],
                expected_images=colmap_result["image_count"],
                expected_points=colmap_result["point_count"],
            )
            print(f"  Validation passed", file=sys.stderr)

            # ── Write run report and exit ──
            sensor_results.append({
                "type": "colmap_scene", "status": "ok",
                "output_dir": str(scene_output),
                "camera_count": colmap_result.get("camera_count", 0),
                "image_count": colmap_result.get("image_count", 0),
                "point_count": colmap_result.get("point_count", 0),
            })
            total_elapsed = _time.perf_counter() - run_start
            _write_manifest_run_report(output_dir, manifest, sensor_results, total_elapsed)

            return 0
```

**That's it. One block replaces one block. No other changes to the file.**

The line immediately after `return 0` should be the existing `summary = inspect_inputs(` line (the start of the legacy pipeline). Do not touch that or anything below it.

---

## Verification

After applying the fix, run a COLMAP export from the GUI. Then check:

```bash
# These files MUST exist — if any are missing, the fix is wrong
ls output_dir/colmap/sparse/0/cameras.txt
ls output_dir/colmap/sparse/0/images.txt
ls output_dir/colmap/sparse/0/points3D.txt

# images/ must contain cubeface PNGs (not empty)
ls output_dir/colmap/images/ | head -5

# masks/ must contain mask PNGs
ls output_dir/colmap/masks/ | head -5

# cameras.txt must contain only PINHOLE cameras
grep -v "^#" output_dir/colmap/sparse/0/cameras.txt | head -3
# Every line should contain "PINHOLE"
```

If `cameras.txt` does not exist, the fix was not applied correctly.

---

## Execution prompt

```
Fix the --scene-manifest code path in metashape_cameras_to_colmap.py. The current code generates cubefaces but never produces a COLMAP scene. The fix plan is at docs/superpowers/plans/2026-05-13-fix-manifest-colmap-scene.md. It contains a single complete replacement block — find the `if args.scene_manifest is not None:` block (lines ~5060-5156), replace the entire thing with the code in the plan. Do not modify anything else in the file. Read the plan fully before starting.
```
