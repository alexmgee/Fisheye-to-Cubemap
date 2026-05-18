"""Cubeface Path A orchestration without modifying the v4 script.

This module keeps ``AM_ImageAndMask_to_cubemap_v4.py`` as a stable dependency:
we reuse its public calibration, mask, ray, remap, and apply functions while
owning the exporter-facing batch API here.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import cv2
import numpy as np

from AM_ImageAndMask_to_cubemap_v4 import (
    ImageMaskWorkItem,
    compute_image2cubeface_remapping_cached,
    compute_metashape_rays_usefulpixmap,
    remap_image,
    remap_mask,
    sum_thresholded_masks,
)
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


SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
OUTPUT_FORMAT_EXTS = {"png": ".png", "tiff": ".tif", "jpg": ".jpg"}
FACE_TAGS = ("+Z", "-X", "+X", "-Y", "+Y")
FACE_FILENAME_SUFFIX = {
    "+Z": "_dir_plusZ",
    "-X": "_dir_minusY",
    "+X": "_dir_plusY",
    "-Y": "_dir_minusX",
    "+Y": "_dir_plusX",
}
SUPPORT_PADDING_PX = 1
FALLBACK_MASK_EROSION_PX = 0


def _emit(progress_callback: Callable[[str], None] | None, message: str) -> None:
    if progress_callback is not None:
        progress_callback(message)


def _emit_progress(
    progress_callback: Callable[[str], None] | None,
    phase: str,
    current: int,
    total: int,
    message: str,
) -> None:
    _emit(progress_callback, f"[PROGRESS] {phase} {current}/{total}: {message}")


def _filtered_image_files(directory: Path, *, allow_empty: bool = False) -> list[Path]:
    directory = Path(directory)
    if not directory.is_dir():
        raise SystemExit(f"Error: image directory does not exist: {directory}")
    images = sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTS
    )
    if not images and not allow_empty:
        raise SystemExit(
            f"Error: no supported image files found in {directory}. "
            f"Supported extensions: {SUPPORTED_IMAGE_EXTS}."
        )
    return images


def _split_mask_string(input_str: str) -> tuple[str, str]:
    if input_str.endswith("_mask"):
        return input_str[:-5], "_mask"
    return input_str, ""


def _mask_lookup(mask_dir: Path | None) -> tuple[dict[str, Path], list[Path]]:
    masks_by_stem: dict[str, Path] = {}
    all_masks: list[Path] = []
    if mask_dir is None:
        return masks_by_stem, all_masks
    mask_dir = Path(mask_dir)
    if not mask_dir.is_dir():
        raise SystemExit(f"Error: mask directory does not exist: {mask_dir}")
    for mask_path in _filtered_image_files(mask_dir, allow_empty=True):
        mask_stem_base, _suffix = _split_mask_string(mask_path.stem)
        if mask_stem_base in masks_by_stem:
            raise SystemExit(
                "Error: duplicate mask stem after stripping '_mask'. "
                f"Stem '{mask_stem_base}' appears in both "
                f"{masks_by_stem[mask_stem_base]} and {mask_path}."
            )
        masks_by_stem[mask_stem_base] = mask_path
        all_masks.append(mask_path)
    return masks_by_stem, all_masks


def _stem_override_for_path(
    image_path: Path,
    stem_overrides: Mapping[str, str] | None,
) -> str:
    if not stem_overrides:
        return image_path.stem
    candidates = (
        str(image_path),
        image_path.as_posix(),
        str(image_path.resolve()),
        image_path.resolve().as_posix(),
    )
    for key in candidates:
        if key in stem_overrides:
            value = str(stem_overrides[key]).strip()
            if value:
                return value
    return image_path.stem


def _collect_image_mask_inputs(
    image_dirs: Sequence[Path],
    mask_dirs: Sequence[Path] | None,
    stem_overrides: Mapping[str, str] | None = None,
) -> tuple[list[tuple[ImageMaskWorkItem, str]], list[Path]]:
    mask_dirs = list(mask_dirs or ())
    shared_mask_lookup = None
    if len(mask_dirs) == 1:
        shared_mask_lookup = _mask_lookup(mask_dirs[0])

    plans: list[tuple[ImageMaskWorkItem, str]] = []
    mask_paths: list[Path] = list(shared_mask_lookup[1]) if shared_mask_lookup is not None else []
    output_stems: dict[str, Path] = {}

    for dir_index, image_dir in enumerate(image_dirs):
        image_paths = _filtered_image_files(Path(image_dir))
        if shared_mask_lookup is not None:
            masks_by_stem, _dir_mask_paths = shared_mask_lookup
        else:
            mask_dir = mask_dirs[dir_index] if dir_index < len(mask_dirs) else None
            masks_by_stem, dir_mask_paths = _mask_lookup(mask_dir)
            mask_paths.extend(dir_mask_paths)

        for image_path in image_paths:
            output_stem = _stem_override_for_path(image_path, stem_overrides)
            if output_stem in output_stems:
                raise SystemExit(
                    "Error: duplicate image stems across fisheye image directories "
                    "after output naming: "
                    f"{output_stem}. Cubeface exporter output requires unique "
                    "stems per sensor."
                )
            output_stems[output_stem] = image_path

            mask_path = masks_by_stem.get(image_path.stem)
            mask_stem_base = output_stem
            mask_suffix = "_mask"

            if mask_path is None:
                plans.append(
                    (
                        ImageMaskWorkItem(
                            image_path=image_path,
                            mask_path=None,
                            mask_stem_base=mask_stem_base,
                            mask_suffix=mask_suffix,
                            mask_source="fallback-pending",
                        ),
                        output_stem,
                    )
                )
                continue
            plans.append(
                (
                    ImageMaskWorkItem(
                        image_path=image_path,
                        mask_path=mask_path,
                        mask_stem_base=mask_stem_base,
                        mask_suffix=mask_suffix,
                        mask_source="per-image-mask",
                    ),
                    output_stem,
                )
            )
    return sorted(plans, key=lambda plan: plan[1]), mask_paths


def _resolve_support_inputs(
    mask_paths: Sequence[Path],
    lens_only_mask: Path | None,
) -> tuple[str, list[Path], None]:
    if mask_paths:
        return "mask-directory", list(mask_paths), None
    if lens_only_mask is not None:
        lens_only_mask = Path(lens_only_mask)
        if not lens_only_mask.is_file():
            raise SystemExit(f"Error: lens-only mask file does not exist: {lens_only_mask}")
        if lens_only_mask.suffix.lower() not in SUPPORTED_IMAGE_EXTS:
            raise SystemExit(
                f"Error: lens-only mask file has unsupported extension: {lens_only_mask}. "
                f"Supported extensions: {SUPPORTED_IMAGE_EXTS}."
            )
        return "lens-only-mask", [lens_only_mask], None
    return "geometric-calibration", [], None


def _write_fallback_mask_from_useful_pixel_mask(
    useful_pixel_mask,
    output_path: Path,
    erosion_px: int = FALLBACK_MASK_EROSION_PX,
) -> Path:
    fallback = np.asarray(useful_pixel_mask, dtype=np.uint8)
    if erosion_px > 0:
        kernel = np.ones((3, 3), np.uint8)
        fallback = cv2.erode(fallback, kernel, iterations=erosion_px)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), fallback)
    return output_path


def _apply_fallback_mask_to_missing_items(
    items: Iterable[ImageMaskWorkItem],
    fallback_mask_path: Path,
    *,
    source: str,
) -> list[ImageMaskWorkItem]:
    resolved: list[ImageMaskWorkItem] = []
    for item in items:
        if item.mask_path is not None:
            resolved.append(item)
            continue
        resolved.append(
            ImageMaskWorkItem(
                image_path=item.image_path,
                mask_path=Path(fallback_mask_path),
                mask_stem_base=item.mask_stem_base,
                mask_suffix=item.mask_suffix,
                mask_source=source,
            )
        )
    return resolved


def _expected_output_paths(
    image_stem: str,
    mask_stem_base: str,
    mask_suffix: str,
    output_images_dir: Path,
    output_masks_dir: Path,
    image_ext: str,
) -> list[Path]:
    paths: list[Path] = []
    image_dir = output_images_dir / image_stem
    for face in FACE_TAGS:
        face_suffix = FACE_FILENAME_SUFFIX[face]
        paths.append(image_dir / f"{image_stem}{face_suffix}{image_ext}")
        paths.append(output_masks_dir / f"{mask_stem_base}{face_suffix}{mask_suffix}.png")
    return paths


def _all_paths_exist(paths: Iterable[Path]) -> bool:
    return all(Path(path).is_file() for path in paths)


def _pairs_requiring_processing(
    plans: Sequence[tuple[ImageMaskWorkItem, str]],
    output_images_dir: Path,
    output_masks_dir: Path,
    image_ext: str,
    force: bool,
) -> list[tuple[ImageMaskWorkItem, str]]:
    if force:
        return list(plans)
    pending: list[tuple[ImageMaskWorkItem, str]] = []
    for item, output_stem in plans:
        expected = _expected_output_paths(
            output_stem,
            item.mask_stem_base,
            item.mask_suffix,
            output_images_dir,
            output_masks_dir,
            image_ext,
        )
        if not _all_paths_exist(expected):
            pending.append((item, output_stem))
    return pending


def _validate_source_dimensions(
    plans: Sequence[tuple[ImageMaskWorkItem, str]],
    expected_shape: tuple[int, int],
) -> None:
    for item, _output_stem in plans:
        image = cv2.imread(str(item.image_path), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise SystemExit(f"Error: cv2.imread returned None for {item.image_path}")
        if image.shape[:2] != expected_shape:
            raise SystemExit(
                f"Error: image {item.image_path} shape {image.shape[:2]} does "
                f"not match calibration shape {expected_shape}."
            )
        if item.mask_path is not None:
            mask = cv2.imread(str(item.mask_path), cv2.IMREAD_UNCHANGED)
            if mask is None:
                raise SystemExit(f"Error: cv2.imread returned None for {item.mask_path}")
            if mask.shape[:2] != expected_shape:
                raise SystemExit(
                    f"Error: mask {item.mask_path} shape {mask.shape[:2]} does "
                    f"not match calibration shape {expected_shape}."
                )


def _repeated_mask_paths(plans: Sequence[tuple[ImageMaskWorkItem, str]]) -> set[str]:
    counts: dict[str, int] = {}
    for item, _output_stem in plans:
        if item.mask_path is None:
            continue
        key = str(Path(item.mask_path))
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def _model_from_projection(projection: str) -> str:
    if projection == "equidistant_fisheye":
        return "equidistant"
    if projection == "equisolid_fisheye":
        return "equisolid"
    raise SystemExit(f'Projection "{projection}" is not supported.')


def process_cubeface_sensor(
    calibration_xml: Path,
    image_dirs: Sequence[Path],
    output_dir: Path,
    face_width: int = 2100,
    *,
    mask_dirs: Sequence[Path] | None = None,
    lens_only_mask: Path | None = None,
    output_format: str = "png",
    force: bool = False,
    cache_remapping: bool = True,
    stem_overrides: Mapping[str, str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict:
    """Process one fisheye sensor through the cubeface Path A pipeline."""
    if output_format not in OUTPUT_FORMAT_EXTS:
        raise ValueError(f"Unsupported output_format: {output_format}")
    if not image_dirs:
        raise SystemExit("Error: process_cubeface_sensor requires at least one image directory")

    calibration_xml = Path(calibration_xml)
    output_dir = Path(output_dir)
    output_images_dir = output_dir / "images"
    output_masks_dir = output_dir / "masks"
    output_bonus_dir = output_dir / "bonusdata"
    for directory in (output_images_dir, output_masks_dir, output_bonus_dir):
        directory.mkdir(parents=True, exist_ok=True)

    result = get_calibration_with_corrections(str(calibration_xml))
    if not result:
        raise SystemExit(
            f"Error: failed to load Metashape calibration from {calibration_xml}."
        )
    projection, width, height, f, cx, cy, k1, k2, k3, k4, p1, p2, b1, b2, _date, corrections = result
    model = _model_from_projection(projection)
    params = (f, cx, cy, k1, k2, k3, k4, p1, p2, b1, b2)
    image_ext = OUTPUT_FORMAT_EXTS[output_format]

    if corrections is not None:
        _emit(progress_callback, f"Fourier corrections: detected ({len(corrections.coeffs)} coefficients)")

    work_plans, mask_paths = _collect_image_mask_inputs(
        image_dirs,
        mask_dirs,
        stem_overrides=stem_overrides,
    )
    support_origin, support_mask_paths, maxangle_initial = _resolve_support_inputs(
        mask_paths,
        Path(lens_only_mask) if lens_only_mask is not None else None,
    )
    _emit(progress_callback, f"useful_pixel_mask source: {support_origin}")

    # Build the processing stamp for this run's recipe
    corr_hash = corrections_cache_hash(corrections)
    cal_digest = compute_calibration_digest(projection, width, height, params, corr_hash)
    mask_digest = compute_mask_input_digest(
        support_origin, support_mask_paths,
        Path(lens_only_mask) if lens_only_mask is not None else None,
    )
    current_stamp = build_stamp(
        calibration_digest=cal_digest,
        face_width=face_width,
        output_format=output_format,
        mask_input_digest=mask_digest,
        rig_structure=False,
    )

    # Check whether the existing outputs were produced by the same recipe
    existing_stamp = read_stamp(output_dir)
    recipe_changed = not stamp_matches(existing_stamp, current_stamp)
    if recipe_changed and existing_stamp is not None:
        _emit(progress_callback, "Processing recipe changed — reprocessing all outputs")
    elif recipe_changed:
        _emit(progress_callback, "No processing stamp found — outputs will be verified or regenerated")

    # force reprocessing when recipe changed (stamp mismatch or missing)
    effective_force = force or recipe_changed

    preflight_pending = _pairs_requiring_processing(
        work_plans,
        output_images_dir,
        output_masks_dir,
        image_ext,
        effective_force,
    )
    if not preflight_pending and not effective_force:
        _emit_progress(
            progress_callback,
            "DONE",
            len(work_plans),
            len(work_plans),
            f"processed=0 skipped={len(work_plans)}",
        )
        return {
            "processed_count": 0,
            "skipped_count": len(work_plans),
            "output_dir": str(output_dir),
            "face_width": int(face_width),
            "support_origin": support_origin,
            "maxangle_deg": None,
        }

    maskpixelcount_for_derivation = None
    image_derived_support = None
    if support_mask_paths:
        maskpixelcount_for_derivation = sum_thresholded_masks(
            support_mask_paths,
            (height, width),
        )
        diagnostic_path = output_bonus_dir / "validpixelcountimage_frommasks_16bit.tif"
        cv2.imwrite(str(diagnostic_path), maskpixelcount_for_derivation.astype(np.uint16))
        maxangle_initial = None
    elif support_origin == "geometric-calibration":
        image_derived_support = np.full((height, width), 255, dtype=np.uint8)
        maxangle_initial = None

    if corrections is not None:
        _emit_progress(progress_callback, "RAYS", 0, 1, f"building corrected ray field for {projection}")
        rays, _ = compute_rays_with_corrections(width, height, params, model, corrections=corrections)
        useful_pixel_mask, _omega, maxangle = derive_useful_pixel_mask(
            rays,
            maskpixelcount=maskpixelcount_for_derivation,
            image_derived_support=image_derived_support,
            maxangle=maxangle_initial,
        )
        cv2.imwrite(str(output_bonus_dir / "useful_pixel_mask.png"), useful_pixel_mask)
    else:
        _emit_progress(progress_callback, "RAYS", 0, 1, f"building ray field for {projection}")
        rays, useful_pixel_mask, maxangle = compute_metashape_rays_usefulpixmap(
            width,
            height,
            params,
            maxangle_initial,
            str(output_bonus_dir),
            model=model,
            maskpixelcount=maskpixelcount_for_derivation,
            image_derived_support=image_derived_support,
        )
    _emit_progress(progress_callback, "RAYS", 1, 1, "ray field and useful-pixel mask complete")

    missing_mask_count = sum(1 for item, _output_stem in work_plans if item.mask_path is None)
    if missing_mask_count:
        if lens_only_mask is not None:
            resolved_items = _apply_fallback_mask_to_missing_items(
                (item for item, _output_stem in work_plans),
                Path(lens_only_mask),
                source="fallback-lens-only-mask",
            )
        else:
            fallback_mask_path = output_bonus_dir / "fallback_mask_from_useful_pixel_mask.png"
            _write_fallback_mask_from_useful_pixel_mask(useful_pixel_mask, fallback_mask_path)
            resolved_items = _apply_fallback_mask_to_missing_items(
                (item for item, _output_stem in work_plans),
                fallback_mask_path,
                source="fallback-useful-pixel-mask",
            )
        work_plans = [
            (resolved_item, output_stem)
            for resolved_item, (_old_item, output_stem)
            in zip(resolved_items, work_plans)
        ]

    pending_items = _pairs_requiring_processing(
        work_plans,
        output_images_dir,
        output_masks_dir,
        image_ext,
        force,
    )
    if pending_items:
        _validate_source_dimensions(pending_items, (height, width))

    # Include corrections hash in cache key to prevent cross-contamination
    effective_support_origin = support_origin
    if corrections is not None:
        corr_hash = corrections_cache_hash(corrections)
        effective_support_origin = f"{support_origin}+fourier_{corr_hash}"

    remaps = {}
    n_faces = len(FACE_TAGS)
    for index, face in enumerate(FACE_TAGS):
        _emit_progress(progress_callback, "REMAP_PRECOMPUTE", index, n_faces, f"starting face {face}")
        remaps[face] = compute_image2cubeface_remapping_cached(
            width,
            height,
            rays,
            useful_pixel_mask,
            face_width,
            face,
            model,
            params,
            maxangle,
            output_bonus_dir,
            cache_remapping,
            effective_support_origin,
            SUPPORT_PADDING_PX,
        )
    _emit_progress(progress_callback, "REMAP_PRECOMPUTE", n_faces, n_faces, "all five faces ready")

    processed = 0
    skipped = 0
    total_items = len(work_plans)
    repeated_masks = _repeated_mask_paths(work_plans)
    remapped_mask_cache: dict[tuple[str, str], Path] = {}
    for item_index, (item, output_stem) in enumerate(work_plans, start=1):
        image_stem = output_stem
        expected_outputs = _expected_output_paths(
            image_stem,
            item.mask_stem_base,
            item.mask_suffix,
            output_images_dir,
            output_masks_dir,
            image_ext,
        )
        if not effective_force and _all_paths_exist(expected_outputs):
            skipped += 1
            _emit_progress(
                progress_callback,
                "REMAP_APPLY",
                item_index,
                total_items,
                f"skip {image_stem} (all {len(expected_outputs)} outputs present; pass force=True to reprocess)",
            )
            continue

        source_image_dir = output_images_dir / image_stem
        source_image_dir.mkdir(parents=True, exist_ok=True)
        _emit_progress(progress_callback, "REMAP_APPLY", item_index, total_items, f"processing {image_stem}")
        for face in FACE_TAGS:
            face_suffix = FACE_FILENAME_SUFFIX[face]
            sourceimage_x, sourceimage_y, indices, pixel_weights = remaps[face]
            remap_image(
                sourceimage_x,
                sourceimage_y,
                indices,
                pixel_weights,
                face_width,
                item.image_path,
                source_image_dir / f"{image_stem}{face_suffix}{image_ext}",
                expected_shape=(height, width),
            )
            mask_destination = output_masks_dir / f"{item.mask_stem_base}{face_suffix}{item.mask_suffix}.png"
            mask_key = str(Path(item.mask_path))
            cache_key = (mask_key, face)
            if mask_key in repeated_masks:
                cached_mask = remapped_mask_cache.get(cache_key)
                if cached_mask is None:
                    cached_mask = output_bonus_dir / f"shared_mask{face_suffix}{item.mask_suffix}.png"
                    remap_mask(
                        sourceimage_x,
                        sourceimage_y,
                        indices,
                        pixel_weights,
                        face_width,
                        item.mask_path,
                        cached_mask,
                        expected_shape=(height, width),
                    )
                    remapped_mask_cache[cache_key] = cached_mask
                shutil.copyfile(cached_mask, mask_destination)
            else:
                remap_mask(
                    sourceimage_x,
                    sourceimage_y,
                    indices,
                    pixel_weights,
                    face_width,
                    item.mask_path,
                    mask_destination,
                    expected_shape=(height, width),
                )
        processed += 1

    _emit_progress(progress_callback, "DONE", total_items, total_items, f"processed={processed} skipped={skipped}")

    # Write the processing stamp so future runs can validate these outputs
    write_stamp(output_dir, current_stamp)

    return {
        "processed_count": processed,
        "skipped_count": skipped,
        "output_dir": str(output_dir),
        "face_width": int(face_width),
        "support_origin": support_origin,
        "maxangle_deg": float(maxangle),
    }


__all__ = [
    "FACE_FILENAME_SUFFIX",
    "FACE_TAGS",
    "OUTPUT_FORMAT_EXTS",
    "SUPPORTED_IMAGE_EXTS",
    "process_cubeface_sensor",
]
