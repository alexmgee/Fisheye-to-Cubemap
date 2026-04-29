##############################################################################################
# Program: AM_ImageAndMask_to_cubemap_v4.py
# Authors: Mike Heath (@LaunchedPix) and Alex Gee (@Macgregor)
# (and AI (chatgpt & google AI & Anthropic Claude Code & OpenAI Codex) - Hey give credit where its due.)
# Date: 4/28/2026
#
# Disclaimer: We really have NO assurance that this program is correct.  !USE AT YOUR OWN RISK!
#
# Purpose:
#
# This software performs a pinhole decomposition of wide angle fisheye lens/camera images.
#
# At this point it is specifically written to process fisheye images that have lens
# parameters defined in Agisoft Metashape. In fact it processes images by reading in a
# camera calibration file that was exported from Agisoft Metashape. A word of caution is due.
# Do not just look at an Agisoft Metashape lens calibration file, study it and see the names
# of the lens parameters and find similar parameter names used by another structure from motion
# software derived lens calibration and substitute those in for the Agisoft Metashape parameters
# with the same name. This will not work! The mathematical equation implemented in other sfm software
# are not the same as those used in Agisoft Metashape.
#
# This command line summarizes what the program needs to run it. NOTE: The amlenscal is
# the lens calibration for one fisheye lens. The directories of images and (optionally) masks
# need to each have the images and masks from just one lens. Thus, to process imagery
# from one 360 camera, you will need to run this code twice.
#
# usage: AM_ImageAndMask_to_cubemap_v4_nocache.py [-h] [--outputdir OUTPUTDIR] [--amlenscal AMLENSCAL] [--lenslabel LENSLABEL] [--directoryfisheyeimages DIRECTORYFISHEYEIMAGES] [--directoryfisheyemasks DIRECTORYFISHEYEMASKS]
#                                        [--lensonlymask LENSONLYMASK] [--maxusefulfov MAXUSEFULFOV] [--facewidth FACEWIDTH] [--rigstructure] [--prerelease] [--version] [--h] [--usage] [--outputformat {png,tiff,jpg}] [--force]
#
# You can run the code in several ways with different combinations of inputs.
#
# You will alsoways need to supply --amlenscal
#
# You will always need to supply --directoryfisheyeimages
#
# You will always need to supply --lenslabel
#
# You will always need to supply --facewidth
#
# You will always need to supply --outputdir
#
# You can optionally supplu different combinations of --directoryfisheyemasks , --lensonlymask and --maxusefulfov.
#
# I'd say the beseline use of the code is to supply the --directoryfisheyemasks which is a directory containing able
# mask for every fisheye image (.png format where 0 idicates not to use the corresponding fisheye image pixel and 255
# indicates to use it.
#
# A "fallback" option is to provide --lensonlymask which is a single .png mask. This will be used for every image
# that does not have a mask. So if you supply a --directoryfisheyemasks and some fisheye image is missing a mask, then
# the --lensonlymask will be used for that fisheye image. If you didn't supply a --directoryfisheyemasks then the
# --lensonlymask will be used for every image.
#
# A second "fallback" option is to use --maxusefulfov in place of --lensonlymask . This option essentially
# creates a mask inplace of the --lensonlymask by creating it from the lens field of view. This is a little more risky
# option for the user because if the --maxusefulfov value is chosen too large, there can be issues doing the pinhole
# splits because the mathmatics of the lens model falls apart beyond a certain field of view.
#
# The parameter --rigstructure is optional and simply organizes the output directories in a different structure
# that makes it easier to use a rig in Metashape Pro. Without this option being specified, the default behavior
# of the program structures the ouput directories in a format that makes it easy to use "camera stations" in Metashhape
# standard edition to put a constraint on alignment so each pinhole image split from the same fisheye image
# shares the same aligned position (nodal point).
#
# It takes a fairly sizable abount of time to compute the remapping for each cube face for each lens.
# Once those five remapping functions are computed (maybe up to two minutes each), the
# application of remapping all of the fisheye images and masks though each of the five
# remapping functions is pretty quick per image (~4.5 sec for me), though with a lot
# images, it can still take a while.
#
# Note: I specifically use Agisoft Metashape format (and mathematics behind it) lens
# calibration data in this code. I convert the data to ray directions from the nodal point
# as a preprocessing to remove the lens distortions and the projection. Then I reproject these
# rays to faces of a cube.
#
# My goal here was to set up a universal (ray directions) framework to work within that is "convenient"
# regarding cameras and camera calibrations. In my understanding, there are many different
# conventions for storing camera calibrations that may be inconsistent with eachother (Open_CV,
# Reality Scan etc). It is inconvenient to work with a camera model with distortion parameters
# because the process of determining the effective "ray direction" for each pixel is
# dependent on the mathematics frameworks used by each different SFM software. My desire to transform
# to ray directions was to isolate theis step up front. One day someone might write this
# ray direction conversion to work for Reality Scan or other SFM software packages.
#
# Here I've tried, and I hope it works, to compute the solid angle of each pixel in an image
# and the ray direction (as a quaternion). This is just another format to store and represent
# the image intrinsics.
#
# The parameterization I've used here is based on my (and heavily on AI's) understanding of
# the conventions used in Agisoft Metashape and how to transform a calibration to this new
# format.
#
# It will be POSSIBLE to convert lens calibrations in other conventions to this format too
# such as those from Reality Scan though I have not done that yet.
#
# It seems to me that working with quaternions SHOULD be convenient for transforming these
# camera ray directions using the extrinsics for an "aligned" set of images.
#
# My first use of this program will be to look at conversions from equisolid "dual-fisheye"
# 360 camera images into pinhole images for subsequent alignment in Metashape.
#
# I have also implemented (but not tested) converting wide field of view equidistant
# fisheye images into pinhole splits. Those can be run with the current code. The results
# have not been valided though.
#
# When you run the code, a bonusdata directory will be output with some images that convey
# information to the user. One item in this directory is a raw file that contains the
# solid angle and ray direction image
# (such as SolidAngleRayDirQuaternionwxyz_BandSequential_FLOAT_3840x3840x5.raw).
#
# This is a headerless float32 image that contains:
#
#   First: Solid angle in steradians
#   Next: the Quaternions (four images (w,x,y,z)).
#
# One can use whatever program one desires to view/inspect the output images.
# 
# One option is ImageJ (which can be found on-line and downloaded for free).
#
# If you use this you can File->Import->RAW  (imput Width, Height, Images=5, little endian on a windows PC)
#
# You then need to scale the image to view it (its float32). You can do anlysis such as line
# profiles etc.
#
# Other files in the bonusdata directory include visualizations of the ray directions and masks.
#
# Good luck!
#
# And remember that:              !USE AT YOUR OWN RISK!
#
##############################################################################################

import argparse
import hashlib
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial import KDTree


# =============================================================================
# Module-level constants and helpers introduced in v2.
# =============================================================================

# Bumped whenever the remap algorithm or its invariants change. Older caches
# carrying a different version tag are never loaded.
# Phase 7 bump: mask-derived support, support_origin / support_padding_px in
# the cache key, theta_deg threshold input, and 1-pixel support dilation.
REMAP_VERSION = "v2.1-mask-support"

# Phase 7: default 1-pixel safety pad on the support boundary so the downstream
# RBF interpolation has a valid neighborhood to sample from at the edge.
SUPPORT_PADDING_PX = 1

# Mike mask-model update: fallback per-image masks are derived from the
# run-level useful_pixel_mask and eroded by one pixel so they are slightly
# conservative relative to the remap support pad.
FALLBACK_MASK_EROSION_PX = 1

# Chunk size for the RBF weight solve (Phase 5B). A 2100x2100 face full-batch
# would allocate ~2.8 GB; 100k keeps the working set well under 1 GB.
RBF_CHUNK_SIZE = 100_000

# Experimental image-derived support: number of input images sampled when
# deriving the support radius from image content alone. This helper is not part
# of the production useful_pixel_mask fallback chain after the Mike mask-model
# update.
# 8 is a balance between robustness (more samples -> smoother bright_anywhere
# mask) and runtime (each sample is a full grayscale read).
IMAGE_DERIVED_SAMPLE_SIZE = 8

# Image-derived support: pixel-intensity threshold above which a uint8
# grayscale pixel is treated as "real lens content". 5 is conservative -- even
# very dark lens content tends to clear it, while sensor-frame artifacts and
# pure black borders stay below. Initial constant; verification matrix is the
# acceptance gate.
IMAGE_DERIVED_BRIGHTNESS_THRESHOLD = 5

# Image-derived support: side length (in pixels) of the central seed window
# used to identify which connected components belong to the lens circle.
# Larger than 1x1 so a dark pixel exactly at the image centre (night scenes,
# dark interiors, lens-cap edges, dark vehicle interior) does not break
# detection. 21 covers a 21x21 region around the image centre.
IMAGE_DERIVED_SEED_WINDOW = 21

# Extensions recognized for image-directory and mask-directory filtering (Phase 2B).
_SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")

# Output format options for color face images (Phase 3C).
_OUTPUT_FORMAT_EXTS = {"png": ".png", "tiff": ".tif", "jpg": ".jpg"}

# Face tags in the script-wide canonical order. -Z is intentionally excluded
# (adding it is a Phase 7 product decision).
_FACE_TAGS = ("+Z", "-X", "+X", "-Y", "+Y")

_FACE_FILENAME_SUFFIX = {
    "+Z": "_dir_plusZ",
    # Archive and sourceV2 comparisons show the lateral face content follows the
    # expected internal axes, but the historical output labels map X faces to Y
    # filenames and vice versa. Keep the geometry untouched and align the output
    # names to that established convention.
    "-X": "_dir_minusY",
    "+X": "_dir_plusY",
    "-Y": "_dir_minusX",
    "+Y": "_dir_plusX",
}

# Module logger. `logging.basicConfig` is configured from `main()` so that the
# CLI invocation sets the level; library use would override if needed.
logger = logging.getLogger("am_cubemap_v4")


@dataclass(frozen=True)
class ImageMaskWorkItem:
    image_path: Path
    mask_path: Path | None
    mask_stem_base: str
    mask_suffix: str
    mask_source: str


def report_progress(phase, current, total, message):
    """Emit a stable parseable progress line alongside human-readable logs (Phase 4C).

    Format: `[PROGRESS] PHASE current/total: message`

    Phases used by this script: RAYS, REMAP_PRECOMPUTE, REMAP_APPLY, DONE.
    """
    line = f"[PROGRESS] {phase} {current}/{total}: {message}"
    # Print directly so wrappers get the line even if logging is muted.
    print(line, flush=True)


def _build_remap_cache_key(
    width,
    height,
    model,
    params,
    maxangle,
    facewidth,
    face,
    n_neighbors,
    epsilon,
    pad,
    support_origin,
    support_padding_px,
):
    """Phase 4A / Phase 7: deterministic cache key covering every input that affects the remap.

    A mismatch on any input — including the REMAP_VERSION tag — forces a full
    recomputation, so stale caches cannot outlive a code fix.

    Phase 7 added two fields:
      - support_origin: e.g. 'mask-directory' / 'lens-only-mask' / 'manual-fov'.
        Distinguishes support sources that happen to land on the same numerical maxangle.
      - support_padding_px: the 1-pixel support-edge dilation applied in
        filter_center_component. Changes here change the support set and so
        change the precomputed remap.
    """
    params_str = "_".join(f"{float(p):.12g}" for p in params)
    # Format maxangle deterministically so float vs int differences with the
    # same value share the same cache key.
    maxangle_str = f"{float(maxangle):.12g}"
    key_parts = (
        f"{width}x{height}"
        f"|model={model}"
        f"|params={params_str}"
        f"|maxangle={maxangle_str}"
        f"|facewidth={facewidth}"
        f"|face={face}"
        f"|n={n_neighbors}"
        f"|eps={epsilon}"
        f"|pad={pad}"
        f"|sup_origin={support_origin}"
        f"|sup_pad={support_padding_px}"
        f"|ver={REMAP_VERSION}"
    )
    return hashlib.md5(key_parts.encode("utf-8")).hexdigest()


def _remap_cache_path(outputbonusdirectory, face, cache_key):
    # Sanitize the face tag for the filename (no `+` / `-` literals in paths).
    face_tag = _FACE_FILENAME_SUFFIX[face].lstrip("_")
    return Path(outputbonusdirectory) / f"remap_cache_{face_tag}_{cache_key}.npz"


def _load_remap_cache(cache_path):
    """Return `(sourceimage_x, sourceimage_y, indices, pixel_weights)` or None."""
    if not cache_path.is_file():
        return None
    try:
        with np.load(str(cache_path)) as data:
            return (
                data["sourceimage_x"],
                data["sourceimage_y"],
                data["indices"],
                data["pixel_weights"],
            )
    except (OSError, KeyError, ValueError) as e:
        logger.warning("Failed to load remap cache %s: %s", cache_path, e)
        return None


def _save_remap_cache(cache_path, sourceimage_x, sourceimage_y, indices, pixel_weights):
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            str(cache_path),
            sourceimage_x=np.asarray(sourceimage_x),
            sourceimage_y=np.asarray(sourceimage_y),
            indices=np.asarray(indices),
            pixel_weights=np.asarray(pixel_weights),
        )
    except OSError as e:
        logger.warning("Failed to save remap cache %s: %s", cache_path, e)


def _filtered_image_files(d):
    """List supported image files in a directory, sorted, filtered by extension."""
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in _SUPPORTED_IMAGE_EXTS
    )


def _collect_image_mask_inputs(images_dir, masks_dir):
    """Return work items plus all masks found in an optional mask directory.

    Mike mask-model update: a mask directory may be partial. Masks that match a
    colour image by stem are used as that image's source mask; missing per-image
    masks are resolved later to a fallback mask derived from the run-level
    useful_pixel_mask. All masks found in the directory remain available for
    run-level support derivation.
    """
    images_dir = Path(images_dir)
    if not images_dir.is_dir():
        raise SystemExit(f"Error: image directory does not exist: {images_dir}")

    images = _filtered_image_files(images_dir)
    if not images:
        raise SystemExit(
            f"Error: no supported image files found in {images_dir}. "
            f"Supported extensions: {_SUPPORTED_IMAGE_EXTS}."
        )

    if masks_dir is None:
        return [
            ImageMaskWorkItem(
                image_path=img_path,
                mask_path=None,
                mask_stem_base=img_path.stem,
                mask_suffix="_mask",
                mask_source="fallback-pending",
            )
            for img_path in images
        ], []

    masks_dir = Path(masks_dir)
    if not masks_dir.is_dir():
        raise SystemExit(f"Error: mask directory does not exist: {masks_dir}")

    masks = _filtered_image_files(masks_dir)
    masks_by_stem = {}
    for mask_path in masks:
        mask_stem = mask_path.stem
        mask_stem_base, _ = split_mask_string(mask_stem)
        if mask_stem_base in masks_by_stem:
            raise SystemExit(
                "Error: duplicate mask stem after stripping '_mask'. "
                f"Stem '{mask_stem_base}' appears in both "
                f"{masks_by_stem[mask_stem_base].name} and {mask_path.name}."
            )
        masks_by_stem[mask_stem_base] = mask_path

    items = []
    for img_path in images:
        mask_path = masks_by_stem.get(img_path.stem)
        if mask_path is None:
            items.append(
                ImageMaskWorkItem(
                    image_path=img_path,
                    mask_path=None,
                    mask_stem_base=img_path.stem,
                    mask_suffix="_mask",
                    mask_source="fallback-pending",
                )
            )
        else:
            mask_stem_base, mask_suffix = split_mask_string(mask_path.stem)
            items.append(
                ImageMaskWorkItem(
                    image_path=img_path,
                    mask_path=mask_path,
                    mask_stem_base=mask_stem_base,
                    mask_suffix=mask_suffix,
                    mask_source="per-image-mask",
                )
            )

    return items, masks


def _resolve_support_inputs(mask_paths, lensonlymask_path, maxusefulfov):
    """Choose the source for the one run-level useful_pixel_mask."""
    if mask_paths:
        return "mask-directory", list(mask_paths), None
    if lensonlymask_path is not None:
        lensonlymask_path = Path(lensonlymask_path)
        if not lensonlymask_path.is_file():
            raise SystemExit(f"Error: lens-only mask file does not exist: {lensonlymask_path}")
        if lensonlymask_path.suffix.lower() not in _SUPPORTED_IMAGE_EXTS:
            raise SystemExit(
                f"Error: lens-only mask file has unsupported extension: {lensonlymask_path}. "
                f"Supported extensions: {_SUPPORTED_IMAGE_EXTS}."
            )
        return "lens-only-mask", [lensonlymask_path], None
    if maxusefulfov is not None:
        return "manual-fov", [], maxusefulfov / 2
    raise SystemExit(
        "Error: cannot compute useful_pixel_mask. Provide a mask directory "
        "containing at least one mask image, --lensonlymask=<file>, or "
        "--maxusefulfov=<angle>."
    )


def _write_fallback_mask_from_useful_pixel_mask(
    useful_pixel_mask,
    output_path,
    erosion_px=FALLBACK_MASK_EROSION_PX,
):
    """Write the per-image fallback mask derived from the run-level support."""
    fallback = np.asarray(useful_pixel_mask, dtype=np.uint8)
    if erosion_px > 0:
        kernel = np.ones((3, 3), np.uint8)
        fallback = cv2.erode(fallback, kernel, iterations=erosion_px)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), fallback)
    return output_path


def _apply_fallback_mask_to_missing_items(items, fallback_mask_path):
    """Resolve missing per-image masks to the generated fallback mask."""
    resolved = []
    for item in items:
        if item.mask_path is not None:
            resolved.append(item)
        else:
            resolved.append(
                ImageMaskWorkItem(
                    image_path=item.image_path,
                    mask_path=Path(fallback_mask_path),
                    mask_stem_base=item.mask_stem_base,
                    mask_suffix=item.mask_suffix,
                    mask_source="fallback-useful-pixel-mask",
                )
            )
    return resolved


def _pairs_requiring_processing(
    items,
    outputimagesdirectory,
    outputmasksdirectory,
    rigstructure,
    rig_face_dirs,
    image_ext,
    force_reprocess,
):
    """Return the image/mask pairs whose outputs are not already complete.

    This is intentionally a quiet preflight helper. The main processing loop
    still owns user-facing skip/progress logging, but dimension validation can
    use this list to avoid reading source files for already-complete resume
    entries.
    """
    if force_reprocess:
        return list(items)

    pending = []
    for item in items:
        image_stem = item.image_path.stem
        expected_outputs = _expected_output_paths(
            image_stem,
            item.mask_stem_base,
            item.mask_suffix,
            outputimagesdirectory,
            outputmasksdirectory,
            rigstructure,
            rig_face_dirs,
            image_ext,
        )
        if not _all_paths_exist(expected_outputs):
            pending.append(item)
    return pending


def _validate_source_dimensions(items, expected_shape):
    """Fail before ray/remap precompute if pending source files are wrong-sized."""
    expected_shape = tuple(expected_shape)
    for item in items:
        image_path = item.image_path
        mask_path = item.mask_path
        img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise SystemExit(f"Error: cv2.imread returned None for {image_path}")
        if img.shape[:2] != expected_shape:
            raise SystemExit(
                f"Error: image {image_path} shape {img.shape[:2]} does "
                f"not match calibration shape {expected_shape}. Either re-export "
                f"the calibration XML for the actual image dimensions, or fix the "
                f"image set."
            )

        if mask_path is not None:
            mask = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED)
            if mask is None:
                raise SystemExit(f"Error: cv2.imread returned None for {mask_path}")
            if mask.shape[:2] != expected_shape:
                raise SystemExit(
                    f"Error: mask {mask_path} shape {mask.shape[:2]} does "
                    f"not match calibration shape {expected_shape}. Either re-export "
                    f"the calibration XML for the actual image dimensions, or fix the "
                    f"mask set."
                )


def _expected_output_paths(
    image_stem,
    mask_stem_base,
    mask_suffix,
    outputimagesdirectory,
    outputmasksdirectory,
    rigstructure,
    rig_face_dirs,
    image_ext,
):
    """Return the output paths expected for one source image.

    Mike mask-model update: every processed image has a mask source, either a
    per-image mask or a fallback mask derived from the run-level support. That
    means every expected output set has 10 paths (5 colour + 5 mask).

    `rig_face_dirs` must be a dict `{face_tag: Path}` when `rigstructure` is True;
    it is ignored otherwise.
    """
    outputimagesdirectory = Path(outputimagesdirectory)
    outputmasksdirectory = Path(outputmasksdirectory)

    paths = []
    for face in _FACE_TAGS:
        face_suffix = _FACE_FILENAME_SUFFIX[face]
        if rigstructure:
            img_dir = rig_face_dirs[face]
        else:
            img_dir = outputimagesdirectory / image_stem
        paths.append(img_dir / f"{image_stem}{face_suffix}{image_ext}")
        paths.append(outputmasksdirectory / f"{mask_stem_base}{face_suffix}{mask_suffix}.png")
    return paths


def _all_paths_exist(paths):
    return all(Path(p).is_file() for p in paths)

# =============================================================================
# Original module code continues below (with Phase 1-6 edits applied in place).
# =============================================================================

##############################################################################################
# Function: sum_thresholded_masks
# Purpose: Phase 7 / mask-derived support. Read each user mask, threshold at 127, and
# accumulate a per-pixel count image: how many masks approve this pixel? The result is used
# downstream to derive the maximum useful polar angle for the support region.
#
# Hardening over Mike's sourceV2 implementation:
#   - Hard-fails on any unreadable mask (instead of silently skipping it).
#   - Hard-fails when any mask's shape disagrees with the calibration `expected_shape`.
#   - Hard-fails on an empty input list.
#   - Emits `[PROGRESS] MASK_SUM i/N` lines so wrappers can show progress.
##############################################################################################
def sum_thresholded_masks(mask_paths, expected_shape):
    """Return an int32 (h, w) per-pixel count of masks that approve each pixel.

    `expected_shape` is `(height, width)` — typically the calibration XML
    dimensions. Every mask must match it exactly.
    """
    if not mask_paths:
        raise SystemExit("Error: sum_thresholded_masks called with no masks.")
    h, w = expected_shape
    sum_img = np.zeros((h, w), dtype=np.int32)
    n = len(mask_paths)
    for i, mask_path in enumerate(mask_paths, start=1):
        report_progress("MASK_SUM", i, n, str(mask_path))
        img = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise SystemExit(f"Error: failed to read mask {mask_path}.")
        if img.shape != (h, w):
            raise SystemExit(
                f"Error: mask {mask_path} shape {img.shape} does not match "
                f"calibration shape {(h, w)}."
            )
        # Threshold at 127, output 0/1 so the int32 accumulator stays meaningful.
        _, binary = cv2.threshold(img, 127, 1, cv2.THRESH_BINARY)
        sum_img += binary.astype(np.int32)
    return sum_img


##############################################################################################
# Function: _central_bright_component
# Purpose: Experimental image-derived support helper. Returns a uint8 (h, w) mask of every
# connected component of `binary_mask` that overlaps a small window around the image centre.
#
# `binary_mask` is treated as truthy/falsy: any non-zero value is foreground. Output pixels are
# 0 (excluded) or 255 (included). If no foreground component reaches the seed window, the
# output is all zeros.
#
# Seed-window size is `IMAGE_DERIVED_SEED_WINDOW`. Using a window rather than a single centre
# pixel makes the helper robust to dark central pixels (night scenes, lens-cap edges, dark
# vehicle interiors, etc.). This is intentionally NOT a refactor of `filter_center_component`
# because that function's single-pixel-seed contract is correct for `theta_deg` input
# (theta_deg[centre] = 0 is always inside the support) but would be wrong for image-brightness
# input.
##############################################################################################
def _central_bright_component(binary_mask):
    binary = (binary_mask != 0).astype(np.uint8)
    _num_labels, labels = cv2.connectedComponents(binary, connectivity=8)
    rows, cols = binary.shape

    half = IMAGE_DERIVED_SEED_WINDOW // 2
    cy, cx = rows // 2, cols // 2
    y0, y1 = max(0, cy - half), min(rows, cy + half + 1)
    x0, x1 = max(0, cx - half), min(cols, cx + half + 1)
    window = labels[y0:y1, x0:x1]

    seed_labels = set(np.unique(window).tolist())
    seed_labels.discard(0)  # background label is always 0

    output = np.zeros(binary.shape, dtype=np.uint8)
    if not seed_labels:
        return output
    output[np.isin(labels, list(seed_labels))] = 255
    return output


##############################################################################################
# Function: _build_image_derived_support
# Purpose: Experimental image-derived support. Per-pixel "bright in any sample" mask used
# to derive the support angle from image content alone.
#
# Samples up to `IMAGE_DERIVED_SAMPLE_SIZE` images evenly spaced through the input list,
# thresholds each at `IMAGE_DERIVED_BRIGHTNESS_THRESHOLD`, OR-accumulates into a per-pixel
# "ever bright" mask, and returns the central bright component(s) of that mask reaching the
# seed window (as a uint8 0/255 image).
#
# Hard-fails on:
#   - empty image list
#   - any sample image that fails to read
#   - any sample shape disagreeing with `expected_shape`
#   - no bright pixels found anywhere
#   - no foreground component reaching the central seed window
##############################################################################################
def _build_image_derived_support(image_paths, expected_shape):
    if not image_paths:
        raise SystemExit(
            "Error: _build_image_derived_support called with no images."
        )

    h, w = expected_shape
    n_samples = min(IMAGE_DERIVED_SAMPLE_SIZE, len(image_paths))
    sample_idxs = np.linspace(
        0, len(image_paths) - 1, n_samples, dtype=int
    )
    sample_paths = [image_paths[i] for i in sample_idxs]

    bright_anywhere = np.zeros((h, w), dtype=bool)
    for i, sample_path in enumerate(sample_paths, start=1):
        report_progress("IMAGE_SAMPLE", i, n_samples, str(sample_path))
        img = cv2.imread(str(sample_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise SystemExit(
                f"Error: failed to read sample image {sample_path}."
            )
        if img.shape != (h, w):
            raise SystemExit(
                f"Error: sample image {sample_path} shape {img.shape} does "
                f"not match calibration shape {(h, w)}."
            )
        bright_anywhere |= img > IMAGE_DERIVED_BRIGHTNESS_THRESHOLD

    if not np.any(bright_anywhere):
        raise SystemExit(
            "Error: image-derived support found no bright pixels in the "
            "sampled images. Either pass --maxusefulfov=<angle> to force "
            "the manual support path, or check whether the images are "
            f"very dark (intensity threshold is "
            f"{IMAGE_DERIVED_BRIGHTNESS_THRESHOLD})."
        )

    component = _central_bright_component(bright_anywhere)
    if not np.any(component):
        raise SystemExit(
            "Error: image-derived support found bright pixels but none "
            "reached the seed window around the image centre (window size "
            f"{IMAGE_DERIVED_SEED_WINDOW}px). Pass --maxusefulfov=<angle> "
            "to force the manual support path."
        )
    return component


##############################################################################################
# Function: filter_center_component
# Purpose: This function thresholds an image and returns an output image that has the values
# zero and 255. If the input image has values <= thesh_value AND can be connected to the
# center pixel of the image with pixels than are also <= threshold then the output for that
# pixel will get the value 255. Otherwise that pixel will be set to zero. We will use
# this function to determine which pixels in the image are useful for potential reprojection
# into a cube face. This will be used in reprojecting the images and the masks.
#
# Phase 7: a 1-pixel dilation is applied after the connected-component selection so the
# downstream RBF interpolation has a small valid neighborhood to sample from at the edge of
# the support region. The dilation amount is configurable and is captured in the cache key.
#
# Usage Example:
# img = cv2.imread('your_image.png', cv2.IMREAD_GRAYSCALE)
# processed_img = filter_center_component(img, 100)
##############################################################################################
def filter_center_component(image, thresh_val=100, dilation_px=1):
    # 1. Thresholding: pixels <= 100 become 255, others 0
    # Note: Using np.where is direct for the "less than or equal to" logic
    binary = np.where(image <= thresh_val, 255, 0).astype(np.uint8)

    # 2. Run 8-neighbor connected components
    # labels is an image where each connected region has a unique ID
    num_labels, labels = cv2.connectedComponents(binary, connectivity=8)

    # 3. Get the label at the center point (rows/2, cols/2)
    rows, cols = image.shape
    mid_row, mid_col = rows // 2, cols // 2
    target_label = labels[mid_row, mid_col]

    # 4. Create output: only keep pixels belonging to the target_label
    # We check target_label > 0 to ensure the center isn't background.
    # Force uint8 so the result is a proper binary mask even when the input is
    # a float `theta_deg` array (Phase 7).
    output = np.zeros(image.shape, dtype=np.uint8)
    if target_label > 0:
        output[labels == target_label] = 255

    # 5. Phase 7: dilate by `dilation_px` pixels so the support boundary has a
    # safe neighborhood for the downstream RBF interpolation. iterations=N grows
    # the boundary by N pixels with a 3x3 structuring element.
    if dilation_px > 0:
        kernel = np.ones((3, 3), np.uint8)
        output = cv2.dilate(output, kernel, iterations=dilation_px)

    return output

##############################################################################################
# Function: get_metashape_calibration_data
# Purpose: This function reads in the lens calibration data from an Agisoft Metashape lens
# calibration file. It specifically looks for, and requires the fields listed in the schema
# shown in the code. If other calibrations parameters like k4, b1 or b2 are present, they are
# ignored and a print statement tells the user this is happening. This file can be created
# in Agisoft Metashape by Tools->CameraCalibration, clicking on a lens, clicking on the
# "Adjusted" tab and clicking on the little "disk icon" to save the adjusted lens parameters.
# This can be done once an alignment of the images has been done. It needs to be done for each
# lens.
# Name: Mike Heath
# Date: 4/18/2026
##############################################################################################
def get_metashape_calibration_data(file_path):
    # Define expected fields and their target types
    schema = {
        "projection": str, "date": str,
        "width": int, "height": int,
        "f": float, "cx": float, "cy": float,
        "k1": float, "k2": float, "k3": float,
        "p1": float, "p2": float
    }

    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
    except Exception as e:
        print(f"Error reading file: {e}")
        return None

    # Check if the root tag is 'calibration' or if it's a child
    if root.tag == 'calibration':
        calibration_block = root
    else:
        calibration_block = root.find('calibration')

    if calibration_block is None:
        print("Validation Error: No <calibration> block found in the XML.")
        return None

    data = {}
    # Iterate through all items in the calibration block
    for element in calibration_block:
        label = element.tag
        if label in schema:
            try:
                # Python floats are double-precision by default
                data[label] = schema[label](element.text)
            except (ValueError, TypeError):
                print(f"Error: Could not convert {label} value '{element.text}' to {schema[label].__name__}")
        else:
            # Warning for unexpected items
            print(f"Warning: Label '{label}' found in calibration block. This will be ignored.")

    # Validation: Ensure all 12 required variables are present
    missing_fields = [field for field in schema if field not in data]
    if missing_fields:
        print(f"Validation Error: The following required labels were missing: {', '.join(missing_fields)}")
        return None

    # Return as separate variables in the specific order requested
    return (
        data["projection"], data["width"], data["height"],
        data["f"], data["cx"], data["cy"],
        data["k1"], data["k2"], data["k3"],
        data["p1"], data["p2"], data["date"]
    )


# =========================================
# Distortion inversion (shared)
# =========================================
def undistort_points(xp, yp, K1, K2, K3, K4, P1, P2, iterations=8):
    x = xp.copy()
    y = yp.copy()

    for _ in range(iterations):
        r2 = x*x + y*y
        r4 = r2*r2
        r6 = r4*r2
        r8 = r4*r4

        D = 1 + K1*r2 + K2*r4 + K3*r6 + K4*r8

        dx = P1*(r2 + 2*x*x) + 2*P2*x*y
        dy = P2*(r2 + 2*y*y) + 2*P1*x*y

        x = (xp - dx) / D
        y = (yp - dy) / D

    return x, y


# =========================================
# Normalize
# =========================================
def normalize(v):
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


# =========================================
# Projection models
# =========================================
def pinhole_to_rays(x, y):
    return normalize(np.stack([x, y, np.ones_like(x)], axis=-1))


def equidistant_to_rays(x, y):
    r = np.sqrt(x*x + y*y)
    theta = r
    phi = np.arctan2(y, x)

    sin_t = np.sin(theta)

    X = sin_t * np.cos(phi)
    Y = sin_t * np.sin(phi)
    Z = np.cos(theta)

    return np.stack([X, Y, Z], axis=-1)


def equisolid_to_rays(x, y):
    r = np.sqrt(x*x + y*y)
    theta = 2.0 * np.arcsin(np.clip(r/2.0, -1.0, 1.0))
    phi = np.arctan2(y, x)

    sin_t = np.sin(theta)

    X = sin_t * np.cos(phi)
    Y = sin_t * np.sin(phi)
    Z = np.cos(theta)

    return np.stack([X, Y, Z], axis=-1)


def equirectangular_to_rays(width, height):
    u = np.arange(width)
    v = np.arange(height)
    uu, vv = np.meshgrid(u, v)

    lam = 2*np.pi * (uu / width - 0.5)
    phi = np.pi * (vv / height - 0.5)

    cos_phi = np.cos(phi)

    X = cos_phi * np.sin(lam)
    Y = np.sin(phi)
    Z = cos_phi * np.cos(lam)

    rays = np.stack([X, Y, Z], axis=-1)

    return rays, phi


# =========================================
# Ray → quaternion
# =========================================
def rays_to_quaternion(rays):
    """Phase 1B (antipodal-safe).

    Builds the quaternion that rotates +Z to each input ray. The naive
    construction `w = 1 + dot(z, ray)` / `xyz = cross(z, ray)` collapses
    to a zero quaternion when `ray == -Z` (or very close to it), which
    after normalization produces NaN-adjacent garbage. This is reachable
    in the test set because the X5 lens is used at 204 deg FOV.

    For rays with `dot(z, ray) < -1 + eps` we substitute a stable 180-deg
    rotation around a perpendicular axis (here `+X`, picked because it is
    always orthogonal to `+Z`).
    """
    z = np.array([0.0, 0.0, 1.0])

    cross = np.cross(np.broadcast_to(z, rays.shape), rays)
    dot = np.sum(rays * z, axis=-1)

    w = 1.0 + dot
    xyz = cross

    quat = np.concatenate([w[..., None], xyz], axis=-1)

    # Antipodal-safe substitution. Threshold mirrors the 1e-10 round-trip
    # target in the plan's acceptance criteria for Phase 1B.
    antipodal = dot < (-1.0 + 1e-6)
    if np.any(antipodal):
        # 180-deg rotation about +X: quat = (w=0, x=1, y=0, z=0)
        antipodal_quat = np.zeros(4, dtype=quat.dtype)
        antipodal_quat[1] = 1.0
        quat[antipodal] = antipodal_quat

    norm = np.linalg.norm(quat, axis=-1, keepdims=True)
    quat /= np.maximum(norm, 1e-12)

    return quat


# =========================================
# Solid angle (finite diff)
# =========================================
def compute_solid_angle_fd(rays):
    d_du = np.gradient(rays, axis=1)
    d_dv = np.gradient(rays, axis=0)
    cross = np.cross(d_du, d_dv)
    return np.linalg.norm(cross, axis=-1)


# =========================================
# Build rays
# =========================================
def compute_rays(width, height, params, model):

    if model == "equirectangular":
        rays, phi = equirectangular_to_rays(width, height)
        return rays, phi

    f, cx, cy, K1, K2, K3, K4, P1, P2, B1, B2 = params

    u = np.arange(width)
    v = np.arange(height)
    uu, vv = np.meshgrid(u, v)

    up = uu - (width * 0.5 + cx)
    vp = vv - (height * 0.5 + cy)

    yp = vp / f
    xp = (up - B2 * yp) / (f + B1)

    x, y = undistort_points(xp, yp, K1, K2, K3, K4, P1, P2)

    if model == "pinhole":
        rays = pinhole_to_rays(x, y)
    elif model == "equidistant":
        rays = equidistant_to_rays(x, y)
    elif model == "equisolid":
        rays = equisolid_to_rays(x, y)
    else:
        raise ValueError("Unknown model")

    return rays, None


# =========================================
# Filename
# =========================================
def generate_filename(width, height, params, model):
    param_str = model + "_" + "_".join([f"{p:.6g}" for p in params])
    h = hashlib.md5(param_str.encode()).hexdigest()[:8]
    return f"camera_{model}_{width}x{height}_{h}.raw"


# =========================================
# Main
# =========================================
def generate_camera_data(width, height, params, model):

    print(f"Model: {model}")
    rays, phi = compute_rays(width, height, params, model)

    print("Computing solid angle...")

    if model == "equirectangular":
        dlon = 2*np.pi / width
        dlat = np.pi / height
        omega = dlon * dlat * np.cos(phi)
    else:
        omega = compute_solid_angle_fd(rays)

    print("Computing quaternions...")
    quat = rays_to_quaternion(rays)

    filename = generate_filename(width, height, params, model)

    print(f"Writing {filename}...")

    data = np.stack([
        omega,
        quat[...,0],
        quat[...,1],
        quat[...,2],
        quat[...,3]
    ], axis=0).astype(np.float32)

    data.tofile(filename)

    print("Done.")
    return filename
    
    
# =========================
# QUAT → RAY
# Note: Referencing: https://en.wikipedia.org/wiki/Conversion_between_quaternions_and_Euler_angles
# x, y and z are the last column of the rotation matrix.
# =========================
def quat_to_ray(qw, qx, qy, qz):
    x = 2*(qx*qz + qw*qy)
    y = 2*(qy*qz - qw*qx)
    z = 1 - 2*(qx*qx + qy*qy)

    rays = np.stack([x, y, z], axis=-1)
    norms = np.linalg.norm(rays, axis=-1, keepdims=True)
    return rays / norms

# =========================
# FACE INTERSECTION
# =========================
def project_to_face(ray, face):
    x, y, z = ray

    if face == "+Z" and z > 0:
        u = x / z
        v = y / z
    elif face == "+X" and x > 0:
        u = -z / x
        v = y / x
    elif face == "-X" and x < 0:
        u = z / -x
        v = y / -x
    elif face == "+Y" and y > 0:
        u = x / y
        v = -z / y
    elif face == "-Y" and y < 0:
        u = x / -y
        v = z / -y
    else:
        return None

    return u, v
    

def gaussian_kernel(r, eps):
    return np.exp(-(eps * r)**2)


def _normalize_chunk_weights(chunk_weights):
    """Normalize RBF rows, leaving zero-sum rows at zero without divide warnings."""
    sums = chunk_weights.sum(axis=-1, keepdims=True)
    normalized = np.zeros_like(chunk_weights)
    np.divide(chunk_weights, sums, out=normalized, where=sums != 0.0)
    return normalized


def compute_interpolation_function(facewidth, cubefaceintersections_x, cubefaceintersections_y):
    """Phase 5B: chunked RBF weight solve.

    The original solved a 9x9 positive-definite system per output pixel inside a
    Python for-loop. The full-batch vectorization would allocate
    `(n_pixels, 9, 9)` float64 ~= 2.8 GB for a 2100x2100 face, so we chunk at
    100_000 pixels (controlled by `RBF_CHUNK_SIZE`) to keep peak memory under
    1 GB. Output parity vs the pre-optimization path is `np.allclose` with
    `atol=1e-6` (documented tolerance; solver chains differ slightly).
    """
    n_neighbors = 9
    epsilon = 2.0  # Controls the "spread" of the Gaussian influence.

    # `np.mgrid` below produces target coordinates in image-index order:
    # first axis is row (y), second axis is column (x). The irregular tie
    # points must use that same (row, col) ordering or the cube faces come
    # out transposed relative to Mike's v0.2 / sourceV2 orientation.
    coords_irregular = np.stack((cubefaceintersections_y, cubefaceintersections_x), axis=1)

    grid_row, grid_col = np.mgrid[0:facewidth, 0:facewidth]
    coords_target = np.stack([grid_row.ravel(), grid_col.ravel()], axis=-1)

    # --- PRE-COMPUTE GEOMETRY & WEIGHTS (DO ONCE) ---
    tree = KDTree(coords_irregular)
    distances, indices = tree.query(coords_target, k=n_neighbors)

    n_pixels = len(coords_target)
    pixel_weights = np.zeros((n_pixels, n_neighbors), dtype=np.float64)
    eye = np.eye(n_neighbors) * 1e-10  # broadcastable ridge regularizer

    chunk_size = RBF_CHUNK_SIZE
    for start in range(0, n_pixels, chunk_size):
        end = min(start + chunk_size, n_pixels)

        chunk_indices = indices[start:end]           # (chunk, k)
        chunk_distances = distances[start:end]       # (chunk, k)

        # Gather neighbor coordinates for each target pixel in the chunk.
        all_local = coords_irregular[chunk_indices]  # (chunk, k, 2)

        # Pairwise distances within each neighborhood: (chunk, k, k).
        diffs = all_local[:, :, None, :] - all_local[:, None, :, :]
        d_neighbors = np.linalg.norm(diffs, axis=-1)

        K = gaussian_kernel(d_neighbors, epsilon)    # (chunk, k, k)
        K = K + eye                                  # add ridge; eye broadcasts
        k_target = gaussian_kernel(chunk_distances, epsilon)  # (chunk, k)

        # Batched solve. np.linalg.solve signature is (m,m),(m,n)->(m,n), so
        # k_target needs an explicit trailing dim to be interpreted as a batch
        # of column vectors; we add it here and squeeze after the solve.
        chunk_weights = np.linalg.solve(K, k_target[..., None])[..., 0]  # (chunk, k)

        chunk_weights = _normalize_chunk_weights(chunk_weights)

        pixel_weights[start:end] = chunk_weights

    logger.info("Finished solving for the weights to reproject (chunk_size=%d).", chunk_size)

    return indices, pixel_weights
    
    


# --- 3. RAPID REUSE FUNCTION ---
# In our case, the new values are the input (fisheye) pixel values for the
# sourceimage_x, sourceimage_y image locations. These could be for one channel {R,G,B} or the mask.

def apply_fast_gaussian_remap(indices, pixel_weights, imagedim, new_values):
    """
    Applies the pre-computed mapping to a new set of data points.
    Takes ~1ms for a 50x50 grid.
    """
    # Grab the specific values for each pixel's neighbors
    neighbor_values = new_values[indices] # Shape: (Pixels, Neighbors)
    
    # Dot product: Sum (weight * value) for each pixel
    remapped = np.einsum('ij,ij->i', pixel_weights, neighbor_values)
    
    return remapped.reshape(imagedim, imagedim)

def compute_metashape_rays_usefulpixmap(
    width, height, params, maxangle, outputbonusdirectory, model,
    maskpixelcount=None,
    image_derived_support=None,
):
    """Phase 6B: output paths now use pathlib. Phase 6C: status uses the module logger.

    Phase 7: when `maskpixelcount` is provided (mask-derived support mode), the
    function derives the maximum useful polar angle from the per-pixel mask
    count and uses that instead of the passed-in `maxangle`. The `maxangle`
    parameter is still required as the manual fallback for `--maxusefulfov`
    runs. The returned tuple includes the effective maxangle so the caller can
    feed it into the cache-key hash.

    Experimental image-derived support: when `image_derived_support` is provided (a uint8
    (h, w) mask of the central bright component(s) detected from sampled
    images), the function derives `maxangle` from `theta_deg[component > 0].max()`
    using the same pattern as the mask-derived branch. The `detected_lens_circle.png`
    diagnostic is written to `outputbonusdirectory` for inspection. This path is
    retained for experimentation but is no longer selected by the production CLI
    fallback chain. Only one of `maskpixelcount` and `image_derived_support`
    should be passed; if both are None the passed-in `maxangle` is used as-is
    for the manual-FOV fallback.
    """
    outputbonusdirectory = Path(outputbonusdirectory)
    outputbonusdirectory.mkdir(parents=True, exist_ok=True)

    theta_png = "POLARANGLE_theta.png"
    phi_png = "AZIMUTH_phi_scaled.png"
    useful_pixelmask_filename = "useful_pixel_mask.png"

    logger.info("Model: %s", model)
    rays, phi = compute_rays(width, height, params, model)

    logger.info("Computing solid angle...")

    if model == "equirectangular":
        dlon = 2 * np.pi / width
        dlat = np.pi / height
        omega = dlon * dlat * np.cos(phi)
    else:
        omega = compute_solid_angle_fd(rays)

    logger.info("Computing ray directions (quaternions)...")
    quat = rays_to_quaternion(rays)

    raw_path = outputbonusdirectory / (
        f"SolidAngleRayDirQuaternionwxyz_BandSequential_FLOAT_{width}x{height}x5.raw"
    )
    logger.info("Writing %s...", raw_path)

    data = np.stack([
        omega,
        quat[..., 0],
        quat[..., 1],
        quat[..., 2],
        quat[..., 3],
    ], axis=0).astype(np.float32)

    data.tofile(str(raw_path))

    logger.info("Transforming ray directions in quaternions to azimuth and polar angle visualizations...")

    qw = quat[..., 0]
    qx = quat[..., 1]
    qy = quat[..., 2]
    qz = quat[..., 3]

    vx = 2 * (qx * qz + qw * qy)
    vy = 2 * (qy * qz - qw * qx)
    vz = 1 - 2 * (qx ** 2 + qy ** 2)

    theta_deg = np.degrees(np.arccos(np.clip(vz, -1.0, 1.0)))
    phi_deg = np.degrees(np.arctan2(vy, vx))

    theta_vis = np.clip(theta_deg, 0, 255).astype(np.uint8)
    # Azimuth scaled from [-180, 180] deg into [0, 255] for visualization only.
    phi_scaled = (phi_deg + 255) / 2.0
    phi_vis = np.clip(phi_scaled, 0, 255).astype(np.uint8)

    theta_path = outputbonusdirectory / theta_png
    logger.info("Writing %s...", theta_path)
    cv2.imwrite(str(theta_path), theta_vis)

    phi_path = outputbonusdirectory / phi_png
    logger.info("Writing %s...", phi_path)
    cv2.imwrite(str(phi_path), phi_vis)

    # Phase 7 / Mike mask-model update: when masks are available for support
    # derivation, derive the support max-angle from the outermost pixel any mask
    # approves (vectorized; replaces Mike's Python double loop). In the current
    # source-priority model, mask-directory support and --lensonlymask both use
    # this branch; --maxusefulfov is only the final run-level support fallback.
    if maskpixelcount is not None:
        if maskpixelcount.shape != theta_deg.shape:
            raise SystemExit(
                f"Error: maskpixelcount shape {maskpixelcount.shape} does not "
                f"match theta_deg shape {theta_deg.shape}."
            )
        has_support = maskpixelcount > 0
        if not np.any(has_support):
            raise SystemExit(
                "Error: mask sum has no valid pixels. Either fix the masks or "
                "pass --maxusefulfov=<angle> to force the manual support path."
            )
        derived_angle = float(theta_deg[has_support].max())
        logger.info("Mask-derived maximum angle: %.4f deg", derived_angle)
        maxangle = derived_angle
    elif image_derived_support is not None:
        # Experimental image-derived support: same shape as the mask-derived branch, but
        # the support proxy comes from `_build_image_derived_support` instead
        # of `sum_thresholded_masks`. Diagnostic write is here so the artifact
        # lives next to the other per-lens bonusdata files.
        if image_derived_support.shape != theta_deg.shape:
            raise SystemExit(
                f"Error: image_derived_support shape {image_derived_support.shape} "
                f"does not match theta_deg shape {theta_deg.shape}."
            )
        has_support = image_derived_support > 0
        if not np.any(has_support):
            raise SystemExit(
                "Error: image-derived support component is empty. Pass "
                "--maxusefulfov=<angle> to force the manual support path."
            )
        derived_angle = float(theta_deg[has_support].max())
        logger.info("Image-derived maximum angle: %.4f deg", derived_angle)

        diagnostic_path = outputbonusdirectory / "detected_lens_circle.png"
        logger.info("Writing %s...", diagnostic_path)
        cv2.imwrite(str(diagnostic_path), image_derived_support)
        maxangle = derived_angle
    else:
        logger.info("Manual maximum angle: %.4f deg", float(maxangle))

    # Phase 7: threshold against the float `theta_deg` so a mask-derived float
    # angle (e.g. 102.37 deg) is not quantized to the nearest whole degree by
    # rounding through the uint8 `theta_vis` visualization.
    useful_pixel_mask = filter_center_component(theta_deg, maxangle)

    mask_path = outputbonusdirectory / useful_pixelmask_filename
    logger.info("Writing %s...", mask_path)
    cv2.imwrite(str(mask_path), useful_pixel_mask)

    return rays, useful_pixel_mask, maxangle

    
def compute_image2cubeface_remapping(width, height, rays, useful_pixel_mask, facewidth, face):

    F = facewidth / 2
    
    #############################################################################################
    # Compute the real valued coordinates of the rays that hit each of the cube faces. Each
    # of these ray directions has a source image integer location, so it can reference the source
    # image and mask data when we later reproject the images.
    #############################################################################################
    
    pad = 10
    center = facewidth / 2
    
    ############################################################################################
    # Here we go through each of the five faces and determine the reprojection.
    #
    # The approach we want to take is the following.
    #
    # We want to go through each of the five faces, and for each determine the remapping which
    # is basically how to compute the image or image mask values for each pixel in the cube face
    # image.
    #
    # We will go through every pixel coordinate in the input (say fisheye image) image and project this
    # pixel to a cube face. For each pixel this is a ray from the nodal point of the lens
    # (center of focus) through the input image pixel, going in the direction that the light
    # came from that was recorded in this pixel in the image. Where this ray hits a cube face is
    # where we want to project this image pixel (or mask) to. It might miss and thats fine. If it
    # hits, we want to store the real valued (floating point) coordinates of where it hits the
    # cube face. Along with those real valued intersection coordinates, we want to store the
    # integer valued pixel coordinates of the input image pixel that projects to this intersection
    # point. These can kind of be thought of as our tie points for the reprojection. We store
    # these intersection points because they will be useful for processing the color image and
    # the segementation mask. Of course, what we really want isn't these real valued cube face
    # intersection points, what we want is the image and mask values at the integer valued pixels
    # locations in the cube face. This will require a RESAMPLING.
    #
    # So for any integer valued pixel location in the cube face image or mask we are creating,
    # we need to essentially look around that pixels location to find nearby image locations
    # where these ray intersections occurred. Then we need to determine how to mix (some weighted sum)
    # the pixel values from the input image that correspond to these intersection points to determine
    # the cube face color image or mask pixel value for this pixel.
    #
    # There are many different algorithms to resample an image. They differ in their computational
    # effort/cost and the image quality one can achieve with it. We want to use an algorithm that
    # yeilds a high image quality (even if it runs slowly). A key here to making this workable is
    # to recognize that every image captured from one lens has the same resampling needed for
    # one cube face (well each of the five). Therefore we want to compute the "receipe" once and then
    # use it repeatedly to compute one cube face for each image captured with a lens. This receipe
    # is specific for each cube face image (or mask) and essitially identifies which pixels in
    # the image to sum (after multiplying each by a weight value) to combine them to create the output
    # pixel value.
    #
    # We want to compute the remapping once and then apply it later, over and over, without
    # needing to compute the way to do the remapping over and over again.
    #
    ############################################################################################
    # So, lets start by going through each of the five faces and creating a list of data for the
    # rays that hit this cube face. We will record the fisheye image coordinates and the
    # cube face intersection coordinates. Because really wide fisheye lens images have a lot
    # of "useless pixels" that are outside of the useful image area, we want to exclude using
    # rays that are "cast" through these "invalid" pixels. For the moment, for simplicity, I am 
    # assimuming a maximum angle from the optical axis to simply threshold and determine which
    # pixels to use, and which to discard. Later this decision could be made by examining the
    # mask images, or come other method. Its not a problem to work to a better solution at this
    # point in time.
    ############################################################################################

    logger.info(
        "Computing the remapping for face: %s (Note: This can take a couple of minutes)...",
        face,
    )

    # Phase 5A: vectorized pixel-to-face projection. The original walked every
    # source pixel in a Python double for-loop (~14.7M iterations on a
    # 3840x3840 input). We select valid pixels in one pass, apply the
    # face-specific half-space test and perspective divide as NumPy ops, then
    # bounds-check with a padded face rectangle. Output parity vs the original
    # loop is required on the Phase 0 baselines.
    #
    # Phase 6A: `buf_mask` and `buf_mw` were allocated but never referenced;
    # removed here.

    valid = useful_pixel_mask > 0
    ys, xs = np.where(valid)          # (N,), (N,); ys are rows, xs are cols.
    valid_rays = rays[valid]          # (N, 3)

    if valid_rays.size == 0:
        raise RuntimeError(
            f"Face {face}: no valid pixels in useful_pixel_mask. Check calibration "
            f"and --maxusefulfov (the mask ended up empty)."
        )

    rx = valid_rays[:, 0]
    ry = valid_rays[:, 1]
    rz = valid_rays[:, 2]

    if face == "+Z":
        hit = rz > 0
        denom = rz[hit]
        u = rx[hit] / denom
        v = ry[hit] / denom
    elif face == "+X":
        hit = rx > 0
        denom = rx[hit]
        u = -rz[hit] / denom
        v = ry[hit] / denom
    elif face == "-X":
        hit = rx < 0
        denom = -rx[hit]
        u = rz[hit] / denom
        v = ry[hit] / denom
    elif face == "+Y":
        hit = ry > 0
        denom = ry[hit]
        u = rx[hit] / denom
        v = -rz[hit] / denom
    elif face == "-Y":
        hit = ry < 0
        denom = -ry[hit]
        u = rx[hit] / denom
        v = rz[hit] / denom
    # Note: analogous blocks for +X, -X, +Y, -Y above. -Z is intentionally
    # excluded; adding a sixth face is a Phase 7 product decision, not a silent
    # side effect of vectorization.
    else:
        raise ValueError(f"Unknown face tag: {face}")

    src_y = ys[hit]
    src_x = xs[hit]

    px = u * F + center
    py = v * F + center

    # Padded-face bounds (kept identical to the original: reject if `px + pad < 0`
    # or `px - pad >= facewidth`, etc.).
    in_bounds = (
        (px + pad >= 0)
        & (px - pad < facewidth)
        & (py + pad >= 0)
        & (py - pad < facewidth)
    )

    sourceimage_x = src_x[in_bounds].astype(np.int32)
    sourceimage_y = src_y[in_bounds].astype(np.int32)
    cubefaceintersections_x = px[in_bounds].astype(np.float64)
    cubefaceintersections_y = py[in_bounds].astype(np.float64)

    numpixels_hitting_extended_face = sourceimage_x.size
    logger.info(
        "The number of rays through fisheye pixels that hit the extended face is %d",
        numpixels_hitting_extended_face,
    )

    if numpixels_hitting_extended_face == 0:
        raise RuntimeError(
            f"Face {face}: zero source pixels hit the padded face. Check facewidth "
            f"and calibration — this face would be entirely black."
        )

    indices, pixel_weights = compute_interpolation_function(
        facewidth, cubefaceintersections_x, cubefaceintersections_y
    )

    return sourceimage_x, sourceimage_y, indices, pixel_weights


def compute_image2cubeface_remapping_cached(
    width,
    height,
    rays,
    useful_pixel_mask,
    facewidth,
    face,
    model,
    params,
    maxangle,
    outputbonusdirectory,
    use_cache,
    support_origin,
    support_padding_px,
):
    """Phase 4A wrapper around `compute_image2cubeface_remapping`.

    Looks up a cache under `outputbonusdirectory` keyed by every input that
    affects the remap (including `REMAP_VERSION`). On hit, returns the cached
    arrays without recomputation. On miss, computes and writes the cache
    before returning.

    Caching is controlled by `use_cache`; callers can disable it (e.g. to force
    a regeneration). The cache files are ordinary `.npz`.

    Phase 7: `support_origin` and `support_padding_px` flow into the cache key
    so two runs that produce the same numerical `maxangle` from different
    sources still get distinct cache entries.
    """
    # Must match the hard-coded constants inside compute_image2cubeface_remapping /
    # compute_interpolation_function so that the cache key reflects every input.
    pad = 10
    n_neighbors = 9
    epsilon = 2.0

    cache_key = _build_remap_cache_key(
        width, height, model, params, maxangle, facewidth, face,
        n_neighbors, epsilon, pad,
        support_origin, support_padding_px,
    )
    cache_path = _remap_cache_path(outputbonusdirectory, face, cache_key)

    if use_cache:
        cached = _load_remap_cache(cache_path)
        if cached is not None:
            logger.info("Loaded cached remap for face %s from %s", face, cache_path)
            return cached

    start = time.perf_counter()
    result = compute_image2cubeface_remapping(
        width, height, rays, useful_pixel_mask, facewidth, face,
    )
    elapsed = time.perf_counter() - start
    logger.info("Remap precompute for face %s: %.3f s", face, elapsed)

    if use_cache:
        _save_remap_cache(cache_path, *result)
        logger.info("Saved remap cache for face %s to %s", face, cache_path)

    return result

def remap_image(sourceimage_x, sourceimage_y, indices, pixel_weights, facewidth, source_image_filename, destination_image_filename, expected_shape=None):
    """Remap a color source image into one cube face.

    Phase 1A: `sourceimage_x` is the column index and `sourceimage_y` the row
    index, so image indexing must be `img[y, x]` (not `img[x, y]` as in the
    original). Silently transposes on square inputs and crashes / mis-samples
    on rectangular inputs.

    Phase 3A: per-face `cv2.normalize(..., NORM_MINMAX)` has been replaced by
    clip-and-cast. The RBF interpolation operates on uint8 source values
    (0-255); any small numerical overshoot should be clipped, not used to
    rescale the whole face, because rescaling destroys cross-face radiometry.

    Phase 3C: output format is extension-driven (png / tiff / jpg). JPEG still
    uses quality 100. PNG is the recommended default (lossless).

    Image-derived support: `expected_shape` is the calibration XML's
    (height, width). When supplied, every image's shape is checked at the top
    of this function so a single misshaped image hard-fails before any output
    is written. Zero extra I/O — the image was going to be opened anyway.
    """
    logger.info("Reading source image: %s", source_image_filename)
    img = cv2.imread(str(source_image_filename))
    if img is None:
        raise SystemExit(f"Error: cv2.imread returned None for {source_image_filename}")

    if expected_shape is not None and img.shape[:2] != expected_shape:
        raise SystemExit(
            f"Error: image {source_image_filename} shape {img.shape[:2]} does "
            f"not match calibration shape {expected_shape}. Either re-export "
            f"the calibration XML for the actual image dimensions, or fix the "
            f"image set."
        )

    # Phase 1A: index as [row, col] = [y, x].
    selected_pixels = img[sourceimage_y, sourceimage_x]

    b_channel = selected_pixels[:, 0]
    g_channel = selected_pixels[:, 1]
    r_channel = selected_pixels[:, 2]

    red_cfimage = apply_fast_gaussian_remap(indices, pixel_weights, facewidth, r_channel)
    grn_cfimage = apply_fast_gaussian_remap(indices, pixel_weights, facewidth, g_channel)
    blu_cfimage = apply_fast_gaussian_remap(indices, pixel_weights, facewidth, b_channel)

    color_image = cv2.merge([blu_cfimage, grn_cfimage, red_cfimage])

    # Phase 3A: clip-and-cast. No per-face NORM_MINMAX.
    img8 = np.clip(color_image, 0, 255).astype(np.uint8)

    ext = Path(destination_image_filename).suffix.lower()
    if ext in (".jpg", ".jpeg"):
        cv2.imwrite(str(destination_image_filename), img8, [cv2.IMWRITE_JPEG_QUALITY, 100])
    else:
        # PNG and TIFF: OpenCV defaults are lossless; no extra params needed.
        cv2.imwrite(str(destination_image_filename), img8)


def remap_mask(sourceimage_x, sourceimage_y, indices, pixel_weights, facewidth, source_image_filename, destination_image_filename, expected_shape=None):
    """Remap a mask source image into one cube face.

    Phase 1A: same [y, x] indexing fix as `remap_image`.

    Phase 3B: after `cv2.threshold(THRESH_BINARY)` the array is exactly 0 or 255
    everywhere, so the subsequent `cv2.normalize(NORM_MINMAX)` in the original
    was redundant. Worse, on a face that receives no valid rays (all-zero
    output) NORM_MINMAX can rescale incorrectly. We now write the thresholded
    result directly.

    Image-derived support: `expected_shape` is the calibration XML's
    (height, width). Same per-call shape guard as `remap_image`.
    """
    logger.info("Reading source mask: %s", source_image_filename)
    img = cv2.imread(str(source_image_filename))
    if img is None:
        raise SystemExit(f"Error: cv2.imread returned None for {source_image_filename}")

    if expected_shape is not None and img.shape[:2] != expected_shape:
        raise SystemExit(
            f"Error: mask {source_image_filename} shape {img.shape[:2]} does "
            f"not match calibration shape {expected_shape}. Either re-export "
            f"the calibration XML for the actual image dimensions, or fix the "
            f"mask set."
        )

    # Phase 1A: index as [row, col] = [y, x].
    selected_pixels = img[sourceimage_y, sourceimage_x]
    m_channel = selected_pixels[:, 0]

    m_cfimage = apply_fast_gaussian_remap(indices, pixel_weights, facewidth, m_channel)

    _ret, thresh_img = cv2.threshold(m_cfimage, 127, 255, cv2.THRESH_BINARY)

    # Phase 3B: drop redundant NORM_MINMAX; cast straight to uint8.
    img8 = thresh_img.astype(np.uint8)

    cv2.imwrite(str(destination_image_filename), img8)


def split_mask_string(input_str):
    """
    Splits a string based on whether it ends with '_mask'.
    Returns (prefix, "_mask") or (original_string, "").
    """
    suffix = "_mask"
    if input_str.endswith(suffix):
        # Return string without suffix, and the suffix itself
        return input_str[:-len(suffix)], suffix
    else:
        # Return original string and an empty string
        return input_str, ""

def main():

    # Mike mask-model examples: useful_pixel_mask source priority plus per-image
    # mask fallback.
    #
    # Mask directory with one or more masks: derive support from all masks found;
    # use matching per-image masks where present and fallback masks where missing.
    # python ..\..\..\AM_ImageAndMask_to_cubemap_v3.py --prerelease --amlenscal=Metashape_X5-Cam1-Lens0_ExportedAdjustedLensCalibration.xml --lenslabel="Cam1-Lens0" --directoryfisheyeimages="images_lens0" --directoryfisheyemasks="masks_lens0" --facewidth=2100 --outputdir="workingoutput"
    #
    # Lens-only mask: derive support from one mask if the mask directory is absent
    # or empty.
    # python ..\..\..\AM_ImageAndMask_to_cubemap_v3.py --prerelease --amlenscal=Metashape_X5-Cam1-Lens0_ExportedAdjustedLensCalibration.xml --lenslabel="Cam1-Lens0" --directoryfisheyeimages="images_lens0" --lensonlymask="lens0_mask.png" --facewidth=2100 --outputdir="workingoutput"
    #
    # Manual FOV fallback: derive useful_pixel_mask from maxusefulfov/2 and
    # generate fallback source masks for every image.
    # python ..\..\..\AM_ImageAndMask_to_cubemap_v3.py --prerelease --amlenscal=Metashape_X5-Cam1-Lens0_ExportedAdjustedLensCalibration.xml --lenslabel="Cam1-Lens0" --directoryfisheyeimages="images_lens0" --maxusefulfov=204 --facewidth=2100 --outputdir="workingoutput"

    codeversion = (
        "AM_ImageAndMask_to_cubemap_v4.py: version 4.0 : date April 28, 2026 : "
        "(by Mike Heath @LaunchedPix and Alex Gee @Macgregor)"
    )

    # Phase 6C: structured logging. Progress lines (Phase 4C) keep their
    # `[PROGRESS]` prefix via `report_progress`; log lines use standard levels
    # so wrappers can tee-split them if desired.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(description="Process fisheye images and masks.")

    parser.add_argument("--outputdir", type=str)
    parser.add_argument("--amlenscal", type=str)
    parser.add_argument("--lenslabel", type=str)
    parser.add_argument("--directoryfisheyeimages", type=str)
    # Mike mask-model update: the mask directory may be partial. Any masks in
    # it are used for run-level support; matching masks are used per image.
    parser.add_argument("--directoryfisheyemasks", type=str, default=None)
    parser.add_argument("--lensonlymask", type=str, default=None)
    # --maxusefulfov is now the last explicit fallback for creating the one
    # run-level useful_pixel_mask when no mask directory images or lens-only mask
    # are available.
    parser.add_argument("--maxusefulfov", type=int, default=None)
    parser.add_argument("--facewidth", type=int, default=2100)
    parser.add_argument('--rigstructure', action='store_true')
    parser.add_argument('--version', action='store_true')
    parser.add_argument('--h', action='store_true')
    parser.add_argument('--usage', action='store_true')

    # Phase 3C: output format for color face images. Masks stay PNG (binary).
    parser.add_argument(
        '--outputformat',
        type=str,
        choices=tuple(_OUTPUT_FORMAT_EXTS.keys()),
        default='png',
        help="Color face image format. Default 'png' (lossless).",
    )
    # Phase 4B: resume override.
    parser.add_argument(
        '--force',
        action='store_true',
        help="Phase 4B: reprocess images whose full output set already exists.",
    )
    args = parser.parse_args()

    # Informational commands should be callable without a full processing
    # command line. Keep these before the prerelease gate and manual required
    # argument validation.
    if args.version:
        print()
        print(codeversion);
        print()
        return

    # Offer the user help and a long description of the purpose of the code and what data is needed
    # to run it.
    if args.h:
        print()
        print()    
        print("------------------------------------------------------------------------------------------------")
        print()
        print("This software can break up super-wide angle fisheye images for subsequent alignment as pinhole images")
        print("in Agisoft Metashape. After alignment these images and masks can be exported for Gaussian Splat training")
        print("without requiring the use of GUT.")
        print()       
        print("This software breaks up super-wide field of view images captured with fisheye lenses. Examples of")
        print("these images include, but are not limited to those captured by (1) 360 cameras that have not been")
        print("stitched into an Equirectangular format representation (2) Images captured with a full frame Nikon")
        print("DSLR paired with a Rokinon 8mm f/3.5 Aspherical Fisheye Lens (3) Fisheye images from a handheld Lidar")
        print("SLAM scanner such as the 3DmakerPro Eagle or Raven. Overall, the images should generally have a")
        print("180 degree (or wider) field of view to make it worth decomposing the images to pinhole images.")
        print()
        print("Each fisheye image is split into 5 pinhole camera images.")
        print()
        print("To use the software, you need to have a lens calibration data for each fisheye lens and image masks")
        print("for all of your images. Note: Only the parameters f, cx, cy, k1-k3 and p1-p2 are used.")
        print()
        print("Obtaining this lens calibration data requires aligning a set of masked fisheye images in Metashape")
        print("at lease once to get the lens calibration data that is needed for use in this software. Once you")
        print("do this for your camera, if you want to assume the lens intrinsics don't change, you can re-use")
        print("these lens calibration files for processing images from future datasets.")
        print()
        print("I'll assume that you know how to extract unstitched dual fisheye images from a 360 camera video")
        print("(say using FFMpeg), label the extracted fisheye images with which lens captured it so Metashape")
        print("can calibrate each lens separately (say using ExifTool")
        print("(exiftool.exe -EXIF:Make=\"Insta360\" -EXIF:Model=\"X5-Cam1-Lens0\" -overwrite_original *.png)),")
        print("load the images into Metashape, set the camera type appropriately as \"Equidistant Fisheye\" or")
        print("\"Equisolid Fisheye\" and mask each image to exclude the image area outside of the useful circular")
        print("area of the lens and mask out the camera operator and finally align the images. Then your ready")
        print("to export the lens calibration data.")
        print()
        print("To Export the Calibration data from Metashape:")
        print("To export the lens calibrations (separately for each lens) that were computed by Agisoft Metashape")
        print("from a set of images that were masked and then aligned. Tools->CameraCalibration (select a lens,")
        print("click the adjusted tab, click the little floppy disk icon to save). Do this for each fisheye lens.")
        print()
        print("You can create your fisheye image masks for a dataset you want to process with this code by")
        print("manually creating them in Metashape and then export them. If you know how, you might also be")
        print("able to create these masks using AI tools.")
        print()
        print("If you use Metashape for manually creating these masks, create them and then export them.")
        print("To export the Images (to separate those from one lens in a directory) and Masks from Metashape:")
        print("From Metashape File->Export->ExportCameras \"Colmap (*.txt)\" (check \"Masks\" and \"Images\"")
        print("and \"Export Select Cameras Only\" do NOT undistort). Do this twice with images from one lens")
        print("or the other selected, place the output in separate folders by fisheye lens.")
        print()
        print("Now: Regardless of the way you get the lens calibration and the image masks. They need to be")
        print("organized in separate folders by each fisheye lens.")
        print()
        print("Thus for EACH fisheye lens (and images created by the lens and masks you should have):")
        print()   
        print("    (1) An Agisoft Metashape lens calibration (.xml) file")
        print("    (2) A directory of images captured by one fisheye lens.")
        print("    (3) A directory of masks for every image captured with this fisheye lens.")
        print("        [optionally you can use the --lensonlymask or the --maxusefulfov options if you do not")
        print("         have a directory of masks or are missing masks for some images in that directory]")
        print()
        print("The following are some tips to set up the data for an alignment in Agisoft Metashape (standard).")
        print("After you run the code you will have pinhole images that are in a \"Frame\" projection. You should")
        print("set and lock the camera parameters to f=width/2 and set all of the other lens parameters to a")
        print("value of zero. You should also set each group of five pinhole images to have a group type of")
        print("\"station\" to constrain the alignment so the nodal points of the lenses are aligned.")
        print()
        print("Now, add the flag --usage to see what you need to specify on the command line to run the code:")
        print()
        print("------------------------------------------------------------------------------------------------")
        print()
        return

    if args.usage:
        print()
        print(
            "AM_ImageAndMask_to_cubemap_v4_nocache.py "
            "[--version] [--h] [--usage] [--rigstructure] [--force] [--outputformat={png|tiff|jpg}] "
            "--amlenscal=\"LensCalibration.xml\" --lenslabel=\"YourLabelForThisLens\" "
            "--directoryfisheyeimages=directorypath --facewidth=# --outputdir=outputdirectorypath"
            "[--directoryfisheyemasks=directorypath] [--lensonlymask=filepath] [--maxusefulfov=#]"
        )
        print()
        print("     Note: You must run this separately for each fisheye lens. Use --h for more information.")
        print("     Masking priority:")
        print("       mask directory with at least one mask   -> mask-derived support")
        print("       otherwise/additional --lensonlymask     -> mask-derived support from one mask")
        print("       otherwise/additional --maxusefulfov=#   -> radial support from maxusefulfov/2")
        print("       otherwise                               -> error")
        print("     Every image gets output masks: matching per-image mask if present,")
        print("     otherwise a fallback mask derived from those otherwise/additional options listed above.")
        print("     Resume options:")
        print("       --force         reprocess images whose full output set already exists")
        print()
        return

    required_for_processing = (
        ("--outputdir", args.outputdir),
        ("--amlenscal", args.amlenscal),
        ("--lenslabel", args.lenslabel),
        ("--directoryfisheyeimages", args.directoryfisheyeimages),
        ("--facewidth", args.facewidth),
    )
    missing = [flag for flag, value in required_for_processing if value is None]
    if missing:
        parser.error(
            "the following arguments are required for processing: "
            + ", ".join(missing)
        )
        
    outputdir = Path(args.outputdir)
    images_dir = Path(args.directoryfisheyeimages)
    masks_dir = Path(args.directoryfisheyemasks) if args.directoryfisheyemasks is not None else None
    lensonlymask_path = Path(args.lensonlymask) if args.lensonlymask is not None else None
    amlenscal = args.amlenscal
    lenslabel = args.lenslabel
    facewidth = args.facewidth
    maxusefulfov = args.maxusefulfov
    image_ext = _OUTPUT_FORMAT_EXTS[args.outputformat]
    use_cache = False
    force_reprocess = args.force

    if args.rigstructure:
        logger.info(
            "The pinhole images will be stored for convenient use in alignment using rigs (Metashape Pro Required)."
        )
    else:
        logger.info(
            "The pinhole images will be stored for convenient use in alignment using stations (Metashape Standard or Pro use)."
        )

    # Phase 6B: pathlib for directory creation.
    outputdir.mkdir(parents=True, exist_ok=True)

    work_items, mask_dir_paths = _collect_image_mask_inputs(images_dir, masks_dir)
    n_per_image_masks = sum(1 for item in work_items if item.mask_path is not None)
    logger.info(
        "Success: validated %d image files; %d matching per-image masks; "
        "%d total masks available in mask directory.",
        len(work_items), n_per_image_masks, len(mask_dir_paths),
    )

    #################################################################################################################
    # Form the output directories names and create them.
    #
    # If NOT args.rigstructure then we will store the output images in directories that are easily loaded into Metashape as "Camera Stations".
    #
    # The following filenames are just placeholders here for showing the directory structure.
    # Masks are stored in a single directoruy to make them easy to load into Metashape.
    # Images are stored with each set of five pinhole images in a directory together.
    #
    # outputdir/
    #   masks/
    #       mask_001_dir_plusZ.png
    #       mask_001_dir_minusX.png
    #       mask_001_dir_plusX.png
    #       mask_001_dir_minusY.png
    #       mask_001_dir_plusY.png
    #   lenslabel/
    #       bonusdata/
    #       images/
    #           image_001/
    #               image_001_dir_plusZ.png
    #               image_001_dir_minusX.png
    #               image_001_dir_plusX.png
    #               image_001_dir_minusY.png
    #               image_001_dir_plusY.png
    #           image_002/
    #               image_002_dir_plusZ.png
    #               image_002_dir_minusX.png
    #               image_002_dir_plusX.png
    #               image_002_dir_minusY.png
    #               image_002_dir_plusY.png
    #
    #
    # If args.rigstructure then we will store the output images in directories that are easily loaded into Metashape as RIG.
    #
    # outputdir/
    #   masks/
    #       mask_001_dir_plusZ.png
    #       mask_001_dir_minusX.png
    #       mask_001_dir_plusX.png
    #       mask_001_dir_minusY.png
    #       mask_001_dir_plusY.png
    #   lenslabel/
    #       bonusdata/
    #       images/
    #           dir_plusZ/
    #               image_001_dir_plusZ.png
    #               image_002_dir_plusZ.png
    #           dir_minusX/
    #               image_001_dir_minusX.png
    #               image_002_dir_minusX.png
    #           dir_plusX/
    #               image_001_dir_plusX.png
    #               image_002_dir_plusX.png
    #           dir_minusY/
    #               image_001_dir_minusY.png
    #               image_002_dir_minusY.png
    #           dir_plusY/
    #               image_001_dir_plusY.png
    #               image_002_dir_plusY.png
    #
    ################################################################################################################
    # Phase 6B: use Path for all derived directories.
    lensdirectory = outputdir / lenslabel
    outputimagesdirectory = lensdirectory / "images"
    outputbonusdirectory = lensdirectory / "bonusdata"
    outputmasksdirectory = outputdir / "masks"

    for d in (lensdirectory, outputimagesdirectory, outputbonusdirectory):
        d.mkdir(parents=True, exist_ok=True)
    outputmasksdirectory.mkdir(parents=True, exist_ok=True)

    # Rig-structure face directories (only used when --rigstructure is set).
    rig_face_dirs = {
        face: outputimagesdirectory / _FACE_FILENAME_SUFFIX[face].lstrip("_")
        for face in _FACE_TAGS
    }
    if args.rigstructure:
        for d in rig_face_dirs.values():
            d.mkdir(parents=True, exist_ok=True)

    ############################################################################################
    # Read in the data from the Agisoft Metashape lens calibration file.
    ############################################################################################
    # Phase 2A: hard return on calibration parse failure. The original only
    # bound `projection, width, ...` inside an `if result:` block but used them
    # unconditionally below, so a parse failure silently proceeded with
    # undefined variables.
    result = get_metashape_calibration_data(amlenscal)
    if not result:
        raise SystemExit(
            f"Error: failed to load Metashape calibration from {amlenscal}. "
            "See earlier log lines for the specific parse failure."
        )

    projection, width, height, f, cx, cy, k1, k2, k3, p1, p2, date = result
    logger.info("Successfully loaded Agisoft Metashape calibration from %s", date)
    logger.info(
        "Calibration: projection=%s %dx%d f=%.6g cx=%.6g cy=%.6g "
        "k1=%.6g k2=%.6g k3=%.6g p1=%.6g p2=%.6g",
        projection, width, height, f, cx, cy, k1, k2, k3, p1, p2,
    )

    params = (
        f,
        cx,
        cy,
        k1, k2, k3, 0.0,  # K1-K4
        p1, p2,            # P1, P2
        0.0, 0.0,          # B1, B2
    )

    if projection not in ('equidistant_fisheye', 'equisolid_fisheye'):
        raise SystemExit(f'Projection "{projection}" is not supported.')

    pending_pairs = _pairs_requiring_processing(
        work_items,
        outputimagesdirectory,
        outputmasksdirectory,
        args.rigstructure,
        rig_face_dirs,
        image_ext,
        force_reprocess,
    )
    if pending_pairs:
        logger.info(
            "Preflight: validating source dimensions for %d image(s) needing processing.",
            len(pending_pairs),
        )
        _validate_source_dimensions(pending_pairs, (height, width))
    else:
        logger.info(
            "Preflight: all source outputs are already complete; no source dimensions checked."
        )

    support_origin, support_mask_paths, maxangle_initial = _resolve_support_inputs(
        mask_dir_paths,
        lensonlymask_path,
        maxusefulfov,
    )
    logger.info("useful_pixel_mask source: %s", support_origin)

    # Mike mask-model update: compute the one run-level useful_pixel_mask from
    # the best available source. A mask directory with at least one mask wins;
    # then a single lens-only mask; then maxusefulfov/2 as the final fallback.
    maskpixelcount_for_derivation = None
    image_derived_support = None
    if support_mask_paths:
        maskpixelcount_for_derivation = sum_thresholded_masks(
            support_mask_paths, (height, width)
        )
        diagnostic_path = (
            outputbonusdirectory / "validpixelcountimage_frommasks_16bit.tif"
        )
        logger.info("Writing %s...", diagnostic_path)
        cv2.imwrite(
            str(diagnostic_path),
            maskpixelcount_for_derivation.astype(np.uint16),
        )
        # The actual derived angle is computed inside
        # compute_metashape_rays_usefulpixmap (where theta_deg lives).
        maxangle_initial = None

    ########################################################################################################
    # Compute the ray directions from the lens nodal point to the scene location where the light came from
    # and got recorded in each pixel of the image. The ray directions are analogous to the points on a
    # sphere cenetered at the nodal point where these light rays would have intersected the sphere. These
    # ray directions are stored in a quaternions form. The solid angle of the "light cone" for each pixel
    # is calculated too. The ray directions are also converted into an azimuth and an angle from the
    # optical axis. The angle from the optical axis image is then thresholded (and connected components
    # are used) to identify the useful pixels in the image for (later) reprojection into a cube map.
    # We do have masks for each fisheye image, but these combine the useful pixels from the lens limitations
    # and operator/person masking. Therefore we compute this "useful pixel mask" for the reprojection.
    # The user supplied masks will be reprojected too so that information will be carried into the cubemap
    # masks.
    ########################################################################################################
    # Phase 4C: structured progress for the ray / useful-pixel phase.
    report_progress("RAYS", 0, 1, f"building ray field for {projection}")
    model = "equidistant" if projection == "equidistant_fisheye" else "equisolid"
    rays, useful_pixel_mask, maxangle = compute_metashape_rays_usefulpixmap(
        width, height, params, maxangle_initial, str(outputbonusdirectory),
        model=model, maskpixelcount=maskpixelcount_for_derivation,
        image_derived_support=image_derived_support,
    )
    report_progress("RAYS", 1, 1, "ray field and useful-pixel mask complete")

    missing_mask_count = sum(1 for item in work_items if item.mask_path is None)
    if missing_mask_count:
        fallback_mask_path = outputbonusdirectory / "fallback_mask_from_useful_pixel_mask.png"
        _write_fallback_mask_from_useful_pixel_mask(useful_pixel_mask, fallback_mask_path)
        work_items = _apply_fallback_mask_to_missing_items(work_items, fallback_mask_path)
        logger.info(
            "Resolved %d missing per-image mask(s) using %s.",
            missing_mask_count,
            fallback_mask_path,
        )

    ########################################################################################################
    # Compute the projection remapping functions. For each cube face we will have:
    #
    #   sourceimage_x, sourceimage_y, indices, pixel_weights
    #
    # The pixels from the source image at image[sourceimage_y, sourceimage_x] are combined via the
    # precomputed RBF weights to form each cube face pixel. The precompute is expensive (minutes per face)
    # but the apply is cheap (seconds per image) — Phase 4A caches the precompute to disk so that second
    # and later runs skip it entirely.
    #
    # The five copy-pasted blocks below (one per face) are intentional: refactoring into a single loop is a
    # structural change deferred to Phase 8 (package migration) per synthesized_fix_plan.md. The cache and
    # progress machinery are common enough to live in `compute_image2cubeface_remapping_cached`.
    ########################################################################################################

    n_faces = len(_FACE_TAGS)

    # +Z
    face = "+Z"
    report_progress("REMAP_PRECOMPUTE", 0, n_faces, f"starting face {face}")
    (
        plusz_sourceimage_x,
        plusz_sourceimage_y,
        plusz_indices,
        plusz_pixel_weights,
    ) = compute_image2cubeface_remapping_cached(
        width, height, rays, useful_pixel_mask, facewidth, face,
        model, params, maxangle, outputbonusdirectory, use_cache,
        support_origin, SUPPORT_PADDING_PX,
    )

    # -X
    face = "-X"
    report_progress("REMAP_PRECOMPUTE", 1, n_faces, f"starting face {face}")
    (
        minusx_sourceimage_x,
        minusx_sourceimage_y,
        minusx_indices,
        minusx_pixel_weights,
    ) = compute_image2cubeface_remapping_cached(
        width, height, rays, useful_pixel_mask, facewidth, face,
        model, params, maxangle, outputbonusdirectory, use_cache,
        support_origin, SUPPORT_PADDING_PX,
    )

    # +X
    face = "+X"
    report_progress("REMAP_PRECOMPUTE", 2, n_faces, f"starting face {face}")
    (
        plusx_sourceimage_x,
        plusx_sourceimage_y,
        plusx_indices,
        plusx_pixel_weights,
    ) = compute_image2cubeface_remapping_cached(
        width, height, rays, useful_pixel_mask, facewidth, face,
        model, params, maxangle, outputbonusdirectory, use_cache,
        support_origin, SUPPORT_PADDING_PX,
    )

    # -Y
    face = "-Y"
    report_progress("REMAP_PRECOMPUTE", 3, n_faces, f"starting face {face}")
    (
        minusy_sourceimage_x,
        minusy_sourceimage_y,
        minusy_indices,
        minusy_pixel_weights,
    ) = compute_image2cubeface_remapping_cached(
        width, height, rays, useful_pixel_mask, facewidth, face,
        model, params, maxangle, outputbonusdirectory, use_cache,
        support_origin, SUPPORT_PADDING_PX,
    )

    # +Y
    face = "+Y"
    report_progress("REMAP_PRECOMPUTE", 4, n_faces, f"starting face {face}")
    (
        plusy_sourceimage_x,
        plusy_sourceimage_y,
        plusy_indices,
        plusy_pixel_weights,
    ) = compute_image2cubeface_remapping_cached(
        width, height, rays, useful_pixel_mask, facewidth, face,
        model, params, maxangle, outputbonusdirectory, use_cache,
        support_origin, SUPPORT_PADDING_PX,
    )

    report_progress("REMAP_PRECOMPUTE", n_faces, n_faces, "all five faces ready")

    ########################################################################################################
    # Phase 4B + 4C: iterate the validated pairs with resume-skip and structured progress.
    ########################################################################################################
    n_images = len(work_items)
    skipped = 0
    processed = 0

    for indx, item in enumerate(work_items, start=1):
        image_path = item.image_path
        mask_path = item.mask_path
        image_stem = image_path.stem

        expected_outputs = _expected_output_paths(
            image_stem,
            item.mask_stem_base,
            item.mask_suffix,
            outputimagesdirectory,
            outputmasksdirectory,
            args.rigstructure,
            rig_face_dirs,
            image_ext,
        )

        if not force_reprocess and _all_paths_exist(expected_outputs):
            skipped += 1
            n_expected = len(expected_outputs)
            report_progress(
                "REMAP_APPLY",
                indx,
                n_images,
                f"skip {image_stem} (all {n_expected} outputs present; pass --force to reprocess)",
            )
            continue

        report_progress("REMAP_APPLY", indx, n_images, f"processing {image_stem}")
        logger.info(
            "Processing pair [%d/%d]: %s + %s (%s)",
            indx, n_images, image_path.name, mask_path.name, item.mask_source,
        )
        start_time = time.perf_counter()

        # If we are outputting in the "camera stations" layout we need a per-source-image directory.
        if not args.rigstructure:
            thisimagedirectory = outputimagesdirectory / image_stem
            thisimagedirectory.mkdir(parents=True, exist_ok=True)

        def _image_out(face_suffix, _image_ext=image_ext, _image_stem=image_stem):
            if args.rigstructure:
                return rig_face_dirs[_face_from_suffix(face_suffix)] / f"{_image_stem}{face_suffix}{_image_ext}"
            return thisimagedirectory / f"{_image_stem}{face_suffix}{_image_ext}"

        def _mask_out(
            face_suffix,
            _mask_stem_base=item.mask_stem_base,
            _mask_suffix=item.mask_suffix,
        ):
            return outputmasksdirectory / f"{_mask_stem_base}{face_suffix}{_mask_suffix}.png"

        plusz_suffix = _FACE_FILENAME_SUFFIX["+Z"]
        minusx_suffix = _FACE_FILENAME_SUFFIX["-X"]
        plusx_suffix = _FACE_FILENAME_SUFFIX["+X"]
        minusy_suffix = _FACE_FILENAME_SUFFIX["-Y"]
        plusy_suffix = _FACE_FILENAME_SUFFIX["+Y"]

        # +Z
        remap_image(
            plusz_sourceimage_x, plusz_sourceimage_y, plusz_indices, plusz_pixel_weights,
            facewidth, image_path, _image_out(plusz_suffix), expected_shape=(height, width),
        )
        remap_mask(
            plusz_sourceimage_x, plusz_sourceimage_y, plusz_indices, plusz_pixel_weights,
            facewidth, mask_path, _mask_out(plusz_suffix), expected_shape=(height, width),
        )

        # -X
        remap_image(
            minusx_sourceimage_x, minusx_sourceimage_y, minusx_indices, minusx_pixel_weights,
            facewidth, image_path, _image_out(minusx_suffix), expected_shape=(height, width),
        )
        remap_mask(
            minusx_sourceimage_x, minusx_sourceimage_y, minusx_indices, minusx_pixel_weights,
            facewidth, mask_path, _mask_out(minusx_suffix), expected_shape=(height, width),
        )

        # +X
        remap_image(
            plusx_sourceimage_x, plusx_sourceimage_y, plusx_indices, plusx_pixel_weights,
            facewidth, image_path, _image_out(plusx_suffix), expected_shape=(height, width),
        )
        remap_mask(
            plusx_sourceimage_x, plusx_sourceimage_y, plusx_indices, plusx_pixel_weights,
            facewidth, mask_path, _mask_out(plusx_suffix), expected_shape=(height, width),
        )

        # -Y
        remap_image(
            minusy_sourceimage_x, minusy_sourceimage_y, minusy_indices, minusy_pixel_weights,
            facewidth, image_path, _image_out(minusy_suffix), expected_shape=(height, width),
        )
        remap_mask(
            minusy_sourceimage_x, minusy_sourceimage_y, minusy_indices, minusy_pixel_weights,
            facewidth, mask_path, _mask_out(minusy_suffix), expected_shape=(height, width),
        )

        # +Y
        remap_image(
            plusy_sourceimage_x, plusy_sourceimage_y, plusy_indices, plusy_pixel_weights,
            facewidth, image_path, _image_out(plusy_suffix), expected_shape=(height, width),
        )
        remap_mask(
            plusy_sourceimage_x, plusy_sourceimage_y, plusy_indices, plusy_pixel_weights,
            facewidth, mask_path, _mask_out(plusy_suffix), expected_shape=(height, width),
        )

        elapsed = time.perf_counter() - start_time
        logger.info("Finished %s in %.3f s", image_stem, elapsed)
        processed += 1

    report_progress(
        "DONE",
        n_images,
        n_images,
        f"processed={processed} skipped={skipped}",
    )

    logger.info(
        "These pinhole images have the following lens parameters which should be set and FIXED for alignment:"
    )
    logger.info("projection = Frame")
    logger.info("width  = %d", facewidth)
    logger.info("height = %d", facewidth)
    logger.info("f      = %s", facewidth / 2)
    logger.info("cx = 0.0  cy = 0.0")
    logger.info("k1 = k2 = k3 = p1 = p2 = 0.0")


# Small helper used inside main(). Kept at module scope so it is cheap and
# testable, but does not belong to the public API.
def _face_from_suffix(suffix):
    for face, s in _FACE_FILENAME_SUFFIX.items():
        if s == suffix:
            return face
    raise ValueError(f"Unknown face suffix: {suffix}")

if __name__ == "__main__":
    main()

