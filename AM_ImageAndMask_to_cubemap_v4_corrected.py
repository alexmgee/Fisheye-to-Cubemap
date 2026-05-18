"""Cubeface conversion with Fourier corrections support.

Standalone CLI script for the Cubeface tab's subprocess path when
'Apply additional corrections' is enabled. Same CLI interface as
AM_ImageAndMask_to_cubemap_v4.py but uses the corrected ray computation.

When the calibration XML contains no <corrections> block, falls through
to the standard v4 pipeline by calling v4.main() directly.

This script owns its own orchestration flow — it does NOT monkey-patch
v4's module-level functions. It imports v4's individual worker functions
(remap, mask, apply) and calls them explicitly.

Reference:
  docs/plans/2026-05-18-fourier-corrections-plan.md (Phase 7)
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import cv2
import numpy as np

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from gui.fourier_corrections import corrections_cache_hash, get_calibration_with_corrections
from gui.corrected_rays import compute_rays_with_corrections, derive_useful_pixel_mask
from gui.processing_stamp import (
    build_stamp,
    compute_calibration_digest,
    compute_mask_input_digest,
    read_stamp,
    stamp_matches,
    write_stamp,
)

from AM_ImageAndMask_to_cubemap_v4 import (
    _all_paths_exist,
    _apply_fallback_mask_to_missing_items,
    _collect_image_mask_inputs,
    _expected_output_paths,
    _FACE_FILENAME_SUFFIX,
    _FACE_TAGS,
    _OUTPUT_FORMAT_EXTS,
    _resolve_support_inputs,
    _validate_source_dimensions,
    _write_fallback_mask_from_useful_pixel_mask,
    compute_image2cubeface_remapping_cached,
    remap_image,
    remap_mask,
    report_progress,
    sum_thresholded_masks,
    SUPPORT_PADDING_PX,
)

logger = logging.getLogger("am_cubemap_v4_corrected")

_SUFFIX_TO_FACE = {v: k for k, v in _FACE_FILENAME_SUFFIX.items()}


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    # Match v4's full argument set so the fallthrough path works for every
    # flag combination, including --version / --h / --usage.
    parser = argparse.ArgumentParser(
        description="Process fisheye images into cubeface pinholes with Fourier corrections."
    )
    parser.add_argument("--outputdir", type=str)
    parser.add_argument("--amlenscal", type=str)
    parser.add_argument("--lenslabel", type=str)
    parser.add_argument("--directoryfisheyeimages", type=str)
    parser.add_argument("--directoryfisheyemasks", type=str, default=None)
    parser.add_argument("--lensonlymask", type=str, default=None)
    parser.add_argument("--maxusefulfov", type=int, default=None)
    parser.add_argument("--facewidth", type=int, default=2100)
    parser.add_argument("--rigstructure", action="store_true")
    parser.add_argument(
        "--outputformat",
        type=str,
        choices=tuple(_OUTPUT_FORMAT_EXTS.keys()),
        default="png",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--h", action="store_true")
    parser.add_argument("--usage", action="store_true")
    args = parser.parse_args()

    # ── Informational commands: delegate to v4 ──────────────────────
    if args.version or args.h or args.usage:
        import AM_ImageAndMask_to_cubemap_v4 as v4
        v4.main()
        return

    # ── Validate required processing args ───────────────────────────
    required = {
        "--outputdir": args.outputdir,
        "--amlenscal": args.amlenscal,
        "--lenslabel": args.lenslabel,
        "--directoryfisheyeimages": args.directoryfisheyeimages,
    }
    missing = [flag for flag, value in required.items() if value is None]
    if missing:
        parser.error(f"the following arguments are required for processing: {', '.join(missing)}")

    # ── Calibration read (single pass, includes corrections) ────────
    result = get_calibration_with_corrections(args.amlenscal)
    if not result:
        raise SystemExit(f"Error: failed to load calibration from {args.amlenscal}.")

    projection, width, height, f, cx, cy, k1, k2, k3, k4, p1, p2, b1, b2, date, corrections = result

    if corrections is None:
        logger.info(
            "No Fourier corrections in calibration XML. Running standard v4 pipeline."
        )
        import AM_ImageAndMask_to_cubemap_v4 as v4
        v4.main()
        return

    logger.info("Fourier corrections: %d coefficients", len(corrections.coeffs))
    logger.info(
        "Calibration: projection=%s %dx%d f=%.6g cx=%.6g cy=%.6g "
        "k1=%.6g k2=%.6g k3=%.6g k4=%.6g p1=%.6g p2=%.6g b1=%.6g b2=%.6g",
        projection, width, height, f, cx, cy, k1, k2, k3, k4, p1, p2, b1, b2,
    )

    if projection not in ("equidistant_fisheye", "equisolid_fisheye"):
        raise SystemExit(f'Projection "{projection}" is not supported.')
    model = "equidistant" if projection == "equidistant_fisheye" else "equisolid"
    params = (f, cx, cy, k1, k2, k3, k4, p1, p2, b1, b2)
    facewidth = args.facewidth
    image_ext = _OUTPUT_FORMAT_EXTS[args.outputformat]

    # ── Directory setup ─────────────────────────────────────────────
    outputdir = Path(args.outputdir)
    lensdirectory = outputdir / args.lenslabel
    outputimagesdirectory = lensdirectory / "images"
    outputbonusdirectory = lensdirectory / "bonusdata"
    outputmasksdirectory = lensdirectory / "masks"
    for d in (lensdirectory, outputimagesdirectory, outputbonusdirectory, outputmasksdirectory):
        d.mkdir(parents=True, exist_ok=True)

    rig_face_dirs = {
        face: outputimagesdirectory / _FACE_FILENAME_SUFFIX[face].lstrip("_")
        for face in _FACE_TAGS
    }
    if args.rigstructure:
        for d in rig_face_dirs.values():
            d.mkdir(parents=True, exist_ok=True)

    # ── Image/mask collection ───────────────────────────────────────
    images_dir = Path(args.directoryfisheyeimages)
    masks_dir = Path(args.directoryfisheyemasks) if args.directoryfisheyemasks else None
    lensonlymask_path = Path(args.lensonlymask) if args.lensonlymask else None

    work_items, mask_dir_paths = _collect_image_mask_inputs(images_dir, masks_dir)
    n_per_image_masks = sum(1 for item in work_items if item.mask_path is not None)
    logger.info(
        "Validated %d image files; %d matching per-image masks; "
        "%d total masks available in mask directory.",
        len(work_items), n_per_image_masks, len(mask_dir_paths),
    )

    # ── Support resolution ──────────────────────────────────────────
    support_origin, support_mask_paths, maxangle_initial = _resolve_support_inputs(
        mask_dir_paths, lensonlymask_path, args.maxusefulfov,
    )
    logger.info("useful_pixel_mask source: %s", support_origin)

    maskpixelcount_for_derivation = None
    if support_mask_paths:
        maskpixelcount_for_derivation = sum_thresholded_masks(
            support_mask_paths, (height, width)
        )
        diagnostic_path = outputbonusdirectory / "validpixelcountimage_frommasks_16bit.tif"
        logger.info("Writing %s...", diagnostic_path)
        cv2.imwrite(str(diagnostic_path), maskpixelcount_for_derivation.astype(np.uint16))
        maxangle_initial = None

    # ── Processing stamp ───────────────────────────────────────────
    corr_hash = corrections_cache_hash(corrections)
    cal_digest = compute_calibration_digest(projection, width, height, params, corr_hash)
    mask_digest = compute_mask_input_digest(
        support_origin, support_mask_paths, lensonlymask_path,
    )
    current_stamp = build_stamp(
        calibration_digest=cal_digest,
        face_width=facewidth,
        output_format=args.outputformat,
        mask_input_digest=mask_digest,
        rig_structure=args.rigstructure,
    )
    existing_stamp = read_stamp(lensdirectory)
    recipe_changed = not stamp_matches(existing_stamp, current_stamp)
    if recipe_changed and existing_stamp is not None:
        logger.info("Processing recipe changed — reprocessing all outputs")
    elif recipe_changed:
        logger.info("No processing stamp found — outputs will be verified or regenerated")
    effective_force = args.force or recipe_changed

    # ── Preflight ───────────────────────────────────────────────────
    pending_pairs = _pairs_requiring_processing(
        work_items, args.rigstructure, effective_force,
        outputimagesdirectory, outputmasksdirectory, rig_face_dirs, image_ext,
    )
    if pending_pairs:
        logger.info("Preflight: validating source dimensions for %d image(s).", len(pending_pairs))
        _validate_source_dimensions(pending_pairs, (height, width))

    # ── Corrected ray computation ───────────────────────────────────
    report_progress("RAYS", 0, 1, f"building corrected ray field for {projection}")
    rays, _ = compute_rays_with_corrections(width, height, params, model, corrections=corrections)
    useful_pixel_mask, _omega, maxangle = derive_useful_pixel_mask(
        rays,
        maskpixelcount=maskpixelcount_for_derivation,
        maxangle=maxangle_initial,
    )
    cv2.imwrite(str(outputbonusdirectory / "useful_pixel_mask.png"), useful_pixel_mask)
    report_progress("RAYS", 1, 1, "ray field and useful-pixel mask complete")

    # ── Fallback masks ──────────────────────────────────────────────
    missing_mask_count = sum(1 for item in work_items if item.mask_path is None)
    if missing_mask_count:
        if lensonlymask_path is None:
            fallback_mask_path = outputbonusdirectory / "fallback_mask_from_useful_pixel_mask.png"
            _write_fallback_mask_from_useful_pixel_mask(useful_pixel_mask, fallback_mask_path)
            work_items = _apply_fallback_mask_to_missing_items(work_items, fallback_mask_path)
            logger.info("Resolved %d missing mask(s) using %s.", missing_mask_count, fallback_mask_path)
        else:
            work_items = _apply_fallback_mask_to_missing_items(work_items, lensonlymask_path)
            logger.info("Resolved %d missing mask(s) using %s.", missing_mask_count, lensonlymask_path)

    # ── Cache key with corrections hash ─────────────────────────────
    corr_hash = corrections_cache_hash(corrections)
    effective_support_origin = f"{support_origin}+fourier_{corr_hash}"

    # ── Remap precomputation ────────────────────────────────────────
    n_faces = len(_FACE_TAGS)
    remaps = {}
    for index, face in enumerate(_FACE_TAGS):
        report_progress("REMAP_PRECOMPUTE", index, n_faces, f"starting face {face}")
        remaps[face] = compute_image2cubeface_remapping_cached(
            width, height, rays, useful_pixel_mask, facewidth, face,
            model, params, maxangle, outputbonusdirectory, False,
            effective_support_origin, SUPPORT_PADDING_PX,
        )
    report_progress("REMAP_PRECOMPUTE", n_faces, n_faces, "all five faces ready")

    # ── Per-image application ───────────────────────────────────────
    n_images = len(work_items)
    skipped = 0
    processed = 0

    for indx, item in enumerate(work_items, start=1):
        image_path = item.image_path
        mask_path = item.mask_path
        image_stem = image_path.stem

        expected_outputs = _expected_output_paths(
            image_stem, item.mask_stem_base, item.mask_suffix,
            outputimagesdirectory, outputmasksdirectory,
            args.rigstructure, rig_face_dirs, image_ext,
        )

        if not effective_force and _all_paths_exist(expected_outputs):
            skipped += 1
            report_progress(
                "REMAP_APPLY", indx, n_images,
                f"skip {image_stem} (all {len(expected_outputs)} outputs present; pass --force to reprocess)",
            )
            continue

        report_progress("REMAP_APPLY", indx, n_images, f"processing {image_stem}")
        start_time = time.perf_counter()

        if not args.rigstructure:
            thisimagedirectory = outputimagesdirectory / image_stem
            thisimagedirectory.mkdir(parents=True, exist_ok=True)

        for face in _FACE_TAGS:
            face_suffix = _FACE_FILENAME_SUFFIX[face]
            sourceimage_x, sourceimage_y, indices, pixel_weights = remaps[face]

            if args.rigstructure:
                image_out = rig_face_dirs[face] / f"{image_stem}{face_suffix}{image_ext}"
            else:
                image_out = thisimagedirectory / f"{image_stem}{face_suffix}{image_ext}"
            mask_out = outputmasksdirectory / f"{item.mask_stem_base}{face_suffix}{item.mask_suffix}.png"

            remap_image(
                sourceimage_x, sourceimage_y, indices, pixel_weights,
                facewidth, image_path, image_out, expected_shape=(height, width),
            )
            remap_mask(
                sourceimage_x, sourceimage_y, indices, pixel_weights,
                facewidth, mask_path, mask_out, expected_shape=(height, width),
            )

        elapsed = time.perf_counter() - start_time
        logger.info("Finished %s in %.3f s", image_stem, elapsed)
        processed += 1

    # Write the processing stamp
    write_stamp(lensdirectory, current_stamp)

    report_progress("DONE", n_images, n_images, f"processed={processed} skipped={skipped}")


def _pairs_requiring_processing(
    work_items, rigstructure, force,
    outputimagesdirectory, outputmasksdirectory, rig_face_dirs, image_ext,
):
    """Return work items whose outputs are not already complete."""
    if force:
        return list(work_items)
    pending = []
    for item in work_items:
        expected = _expected_output_paths(
            item.image_path.stem, item.mask_stem_base, item.mask_suffix,
            outputimagesdirectory, outputmasksdirectory,
            rigstructure, rig_face_dirs, image_ext,
        )
        if not _all_paths_exist(expected):
            pending.append(item)
    return pending


if __name__ == "__main__":
    main()
