"""
Adaptive Pinhole Undistortion — Runtime Module

Production importable implementation of the adaptive single-pinhole (Path B)
fisheye reprojection. Mirrors the math from AM_ImageAndMask_to_cubemap_v4.py
but produces ONE adaptive pinhole output per source image instead of five
cubefaces.

Companion documents:
  docs/superpowers/research/2026-05-14-fisheye-undistortion-vs-splitting.md
  docs/superpowers/research/Fisheye-to-PinholeRoutingfor3DGS-research.md
  docs/superpowers/plans/Adaptive_Pinhole_Undistort_Plan.md

Architectural choice (see plan, Gap 2): this module imports the shared
ray/solid-angle/remap math from AM_ImageAndMask_to_cubemap_v4 rather than
duplicating it. v4 is treated as a stable library dependency. When v4's
cubeface processing is the right answer (wide FOV, high stretch, oversized
output), the exporter dispatch layer routes to cubemap-split rather than
calling the adaptive Path B orchestrator.
"""

import numpy as np
import cv2
from pathlib import Path
from scipy.spatial import KDTree

# v4 library imports — treat v4 as a stable dependency, do not refactor it.
# These functions are already module-level and public-shaped in v4; importing
# them does not require any v4 changes.
from AM_ImageAndMask_to_cubemap_v4 import (
    compute_rays as _v4_compute_rays,
    compute_solid_angle_fd as _v4_compute_solid_angle_fd,
    filter_center_component as _v4_filter_center_component,
    sum_thresholded_masks as _v4_sum_thresholded_masks,
)
from gui.corrected_rays import compute_rays_with_corrections as _compute_rays_with_corrections

# =============================================================================
# NOTATION & VARIABLE GUIDE
# =============================================================================
# theta_max_deg      : Maximum useful half-angle from optical axis (degrees).
# center_solid_angle : Solid angle (steradians) of the optical-center pixel.
# f_target           : Focal length (pixels) of the adaptive output, matched
#                      to the source's center angular resolution.
# w_out              : Pixel width/height of the square adaptive output image.
# rx, ry, rz         : 3D ray direction components in camera space.
#                      +Z forward (into the scene), +X right, +Y down.
# u, v               : Normalized device coordinates on the projection plane (Z=1).
# px, py             : Continuous (floating-point) pixel coordinates on the
#                      output image.
# K                  : Gaussian kernel matrix used for RBF interpolation.
# pixel_weights      : Solved weights for the N nearest neighbors used to
#                      perform resampling.
# indices            : Array indices mapping output pixels to the N nearest
#                      source pixels.
# calibration_type   : Projection-model string from the Metashape XML
#                      ('equisolid', 'equidistant', ...). Controls which
#                      radial-stretch formula the routing gateway applies.
# =============================================================================

# Routing thresholds — see Fisheye-to-PinholeRoutingfor3DGS-research.md.
# 55 deg half-angle pairs with a 3.04x radial EWA-Jacobian deviation; this
# is the principled upper bound on where vanilla 3DGS's affine splat
# approximation begins to materially deviate (Zwicker et al.; 3DGUT analysis).
# COLMAP's docs imply a looser ~60 deg / 4.0x handoff; 55/3.0 is the
# conservative pairing. Exposed as module-level constants so a GUI/config
# layer can override before calling evaluate_shortfall_routing.
THETA_MAX_THRESHOLD_DEG = 55.0
STRETCH_THRESHOLD = 3.0
W_OUT_BUDGET = 6000

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


# --- 1. Dimension Math ---

def calculate_adaptive_dimensions(theta_max_deg, center_solid_angle):
    """
    Calculates the required focal length and output width to preserve
    center resolution without cropping the useful FOV.
    """
    # 1. Match output focal length to source center resolution to prevent downsampling
    f_target = 1.0 / np.sqrt(center_solid_angle)

    # 2. Calculate required width to encompass the max useful angle
    theta_max_rad = np.radians(theta_max_deg)
    w_out = 2.0 * f_target * np.tan(theta_max_rad)

    return f_target, int(np.ceil(w_out))


# --- 2. Projection Math ---

def project_to_adaptive_plane(rays, f_target, w_out):
    """
    Projects 3D rays to a single forward-facing plane (Z=1).
    Replaces the 90-degree limited cubeface projection (+Z) from the v4 script.
    """
    rx = rays[:, 0]
    ry = rays[:, 1]
    rz = rays[:, 2]

    # Only project forward-facing rays (hemisphere check).
    # If rz <= 0, the ray is pointing backwards/sideways and cannot hit a forward plane.
    hit = rz > 0
    denom = rz[hit]

    # Standard pinhole projection: divide by Z to hit the Z=1 plane
    u = rx[hit] / denom
    v = ry[hit] / denom

    # Scale from normalized coordinates to pixel coordinates
    center = w_out / 2.0
    px = u * f_target + center
    py = v * f_target + center

    return hit, px, py


# --- 3. RBF Interpolation Math (Imported/Adapted from v4) ---

def gaussian_kernel(r, eps):
    # Transforms distance 'r' into a weight using a Gaussian spread 'eps'
    return np.exp(-(eps * r)**2)

def _normalize_chunk_weights(chunk_weights):
    sums = chunk_weights.sum(axis=-1, keepdims=True)
    normalized = np.zeros_like(chunk_weights)
    np.divide(chunk_weights, sums, out=normalized, where=sums != 0.0)
    return normalized

def compute_interpolation_function(w_out, intersection_x, intersection_y):
    """
    Solves the RBF weights to map scattered tie-points to the regular output pixel grid.
    """
    n_neighbors = 9
    epsilon = 2.0  # Controls the spread of the Gaussian influence

    coords_irregular = np.stack((intersection_y, intersection_x), axis=1)
    grid_row, grid_col = np.mgrid[0:w_out, 0:w_out]
    coords_target = np.stack([grid_row.ravel(), grid_col.ravel()], axis=-1)

    tree = KDTree(coords_irregular)
    distances, indices = tree.query(coords_target, k=n_neighbors)

    n_pixels = len(coords_target)
    pixel_weights = np.zeros((n_pixels, n_neighbors), dtype=np.float64)
    eye = np.eye(n_neighbors) * 1e-10

    chunk_size = 100_000  # Keeps VRAM usage low during the batched matrix solve

    for start in range(0, n_pixels, chunk_size):
        end = min(start + chunk_size, n_pixels)

        chunk_indices = indices[start:end]
        chunk_distances = distances[start:end]

        all_local = coords_irregular[chunk_indices]
        diffs = all_local[:, :, None, :] - all_local[:, None, :, :]
        d_neighbors = np.linalg.norm(diffs, axis=-1)

        K = gaussian_kernel(d_neighbors, epsilon)
        K = K + eye
        k_target = gaussian_kernel(chunk_distances, epsilon)

        chunk_weights = np.linalg.solve(K, k_target[..., None])[..., 0]
        chunk_weights = _normalize_chunk_weights(chunk_weights)
        pixel_weights[start:end] = chunk_weights

    return indices, pixel_weights


# --- 4. The Core Precomputation ---

def compute_adaptive_pinhole_remap(width, height, rays, useful_pixel_mask, f_target, w_out):
    """
    Generates the RBF indices and weights for the single adaptive pinhole.
    Replaces 'compute_image2cubeface_remapping'.
    """
    pad = 10  # Padding tolerance for points sitting just outside the pixel boundary

    valid = useful_pixel_mask > 0
    ys, xs = np.where(valid)
    valid_rays = rays[valid]

    if valid_rays.size == 0:
        raise RuntimeError("No valid pixels found in useful_pixel_mask.")

    # Project the 3D valid rays down onto our flat 2D output plane
    hit, px, py = project_to_adaptive_plane(valid_rays, f_target, w_out)

    # Retrieve the source image pixel coordinates that successfully hit the plane
    src_y = ys[hit]
    src_x = xs[hit]

    # Filter out any projection points that fell outside our adaptive w_out boundaries
    in_bounds = (
        (px + pad >= 0) & (px - pad < w_out) &
        (py + pad >= 0) & (py - pad < w_out)
    )

    sourceimage_x = src_x[in_bounds].astype(np.int32)
    sourceimage_y = src_y[in_bounds].astype(np.int32)
    intersection_x = px[in_bounds].astype(np.float64)
    intersection_y = py[in_bounds].astype(np.float64)

    # Solve for the exact interpolation weights
    indices, pixel_weights = compute_interpolation_function(w_out, intersection_x, intersection_y)

    return sourceimage_x, sourceimage_y, indices, pixel_weights


# --- 5. The Gateway Logic ---

def edge_stretch_factor(theta_max_deg, calibration_type="equidistant"):
    """
    Projection-aware radial edge stretch factor.

    The base term 1/cos^2(theta_max) is the radial Jacobian of the gnomonic
    (pinhole) projection. It is exact for an equidistant source. For an
    equisolid source, dr_source/dtheta = f_fisheye * cos(theta/2), so the
    source pixel covers a larger dtheta at the periphery and the true stretch
    is the equidistant value multiplied by 1/cos(theta_max/2). DJI Osmo 360
    and Insta360 X5 lenses are equisolid, so this projection-awareness matters
    in practice.

    This is the radial worst-case stretch only. Tangential stretch at the
    periphery is tan(theta_max)/theta_max for equidistant (1.49x at 55 deg),
    notably smaller than the radial value. The routing gateway uses the
    radial value as the conservative anisotropic bound.

    See Fisheye-to-PinholeRoutingfor3DGS-research.md Topic E.3 for derivation.
    """
    theta_rad = np.radians(theta_max_deg)
    base = 1.0 / (np.cos(theta_rad) ** 2)
    cal_lower = (calibration_type or "").lower()
    if "equisolid" in cal_lower:
        return base / np.cos(theta_rad / 2.0)
    # Equidistant (Metashape default) and other linear-in-theta source models
    return base


def evaluate_shortfall_routing(
    theta_max_deg,
    center_solid_angle,
    calibration_type="equidistant",
    *,
    theta_max_threshold=None,
    stretch_threshold=None,
    w_out_budget=None,
):
    """
    The pre-flight check to determine if the image qualifies for Path B.
    Prioritizes data integrity by capping edge stretch, FOV, and memory.

    Args:
        theta_max_deg: maximum half-angle of useful FOV (degrees)
        center_solid_angle: solid angle of the optical-center pixel (steradians)
        calibration_type: projection-model string from the Metashape XML
                          ('equisolid', 'equidistant', etc.). Controls which
                          radial-stretch formula is applied. Defaults to
                          'equidistant' to preserve backwards compatibility
                          with callers that don't yet pass calibration type.
        theta_max_threshold: optional half-angle threshold override.
        stretch_threshold: optional radial-stretch threshold override.
        w_out_budget: optional adaptive output-width budget override.

    Returns:
        ("SINGLE_PINHOLE", f_target, w_out) if all thresholds pass.
        ("CUBEMAP_SPLIT", None, None) otherwise.
    """
    f_target, w_out = calculate_adaptive_dimensions(theta_max_deg, center_solid_angle)
    stretch_factor = edge_stretch_factor(theta_max_deg, calibration_type)
    theta_limit = THETA_MAX_THRESHOLD_DEG if theta_max_threshold is None else theta_max_threshold
    stretch_limit = STRETCH_THRESHOLD if stretch_threshold is None else stretch_threshold
    width_limit = W_OUT_BUDGET if w_out_budget is None else w_out_budget

    print(f"Evaluated Target Width: {w_out}px")
    print(f"Evaluated Edge Stretch: {stretch_factor:.2f}x ({calibration_type})")
    print(f"Evaluated Max Half-Angle: {theta_max_deg:.2f} degrees")

    # Thresholds pulled from module-level constants so GUI/config can override.
    is_path_b_eligible = (
        theta_max_deg <= theta_limit and
        w_out <= width_limit and
        stretch_factor <= stretch_limit
    )

    if is_path_b_eligible:
        print("Routing: Path B (Adaptive Single Pinhole)")
        return "SINGLE_PINHOLE", f_target, w_out
    else:
        print("Routing: Path A (Multi-Face Cubemap Split)")
        return "CUBEMAP_SPLIT", None, None


# --- 6. Lens Characteristics Extraction (fills Gap 1) ---
#
# extract_lens_characteristics() turns a calibration dict into the two scalar
# values (theta_max_deg, center_solid_angle) that both the routing gateway and
# the dimension math require. It also returns the supporting ray/omega/theta
# arrays so the orchestrator can pass them to compute_adaptive_pinhole_remap
# without recomputing.
#
# This is the function the plan calls out as missing: the existing
# compute_metashape_rays_usefulpixmap() in v4 computes the same values
# internally but does not return them.

# Projection-string -> v4 model-name mapping. The redesigned
# gui.sensor_discovery.extract_sensor_calibration produces dicts whose
# 'projection' field reads like 'equisolid_fisheye' or 'equidistant' (from
# the Metashape XML's <calibration type="..."> attribute). v4.compute_rays
# expects the bare model name. This adapter keeps v4 untouched.
_PROJECTION_TO_MODEL = {
    "equisolid_fisheye": "equisolid",
    "equisolid": "equisolid",
    "equidistant_fisheye": "equidistant",
    "equidistant": "equidistant",
    "fisheye": "equidistant",        # Metashape's legacy generic 'fisheye' -> equidistant
    "frame": "pinhole",
    "pinhole": "pinhole",
    "equirectangular": "equirectangular",
}


def _calibration_to_v4_params(calibration: dict) -> tuple:
    """
    Convert a sensor-discovery calibration dict to v4's 11-tuple params format.

    v4.compute_rays signature: compute_rays(width, height, params, model)
    where params = (f, cx, cy, K1, K2, K3, K4, P1, P2, B1, B2).

    Missing fields default to zero, matching Metashape's "absent parameter"
    convention (an unspecified k4 / b1 / b2 in the XML means zero).
    """
    g = calibration.get
    return (
        float(g("f", 0.0)),
        float(g("cx", 0.0)),
        float(g("cy", 0.0)),
        float(g("k1", 0.0)),
        float(g("k2", 0.0)),
        float(g("k3", 0.0)),
        float(g("k4", 0.0)),
        float(g("p1", 0.0)),
        float(g("p2", 0.0)),
        float(g("b1", 0.0)),
        float(g("b2", 0.0)),
    )


def load_useful_pixel_mask(
    width: int,
    height: int,
    mask_dirs: list[Path] | None = None,
    lens_only_mask: Path | None = None,
) -> np.ndarray | None:
    """Load and aggregate masks into the useful-pixel support mask.

    Per-image masks from ``mask_dirs`` are OR-combined. ``lens_only_mask`` is
    a constraint: when present, it is AND-combined with the per-image union.
    Returns a uint8 HxW mask with values 0/1, or None when no mask inputs are
    provided.
    """
    expected_shape = (height, width)
    mask_paths = []
    for directory in mask_dirs or []:
        d = Path(directory)
        if not d.is_dir():
            continue
        mask_paths.extend(
            sorted(
                p for p in d.iterdir()
                if p.is_file() and p.suffix.lower() in _IMAGE_EXTENSIONS
            )
        )

    union_mask = None
    if mask_paths:
        union_mask = _v4_sum_thresholded_masks(mask_paths, expected_shape) > 0

    lens_mask = None
    if lens_only_mask is not None:
        p = Path(lens_only_mask)
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise SystemExit(f"Error: failed to read lens-only mask {p}.")
        if img.shape != expected_shape:
            raise SystemExit(
                f"Error: lens-only mask {p} shape {img.shape} does not match "
                f"calibration shape {expected_shape}."
            )
        lens_mask = img > 127

    if union_mask is not None and lens_mask is not None:
        useful = union_mask & lens_mask
    elif union_mask is not None:
        useful = union_mask
    elif lens_mask is not None:
        useful = lens_mask
    else:
        return None

    return useful.astype(np.uint8)


def extract_lens_characteristics(calibration: dict, useful_pixel_mask=None):
    """
    From a calibration dict, derive the values the routing gateway and the
    dimension math both need.

    Args:
        calibration: dict matching the shape that
                     gui.sensor_discovery.extract_sensor_calibration will return
                     after the COLMAP-tab redesign Task 1 lands. Required keys:
                       projection: e.g. 'equisolid_fisheye'
                       width, height: int (pixels)
                       f, cx, cy: float
                     Optional keys (default to 0 when absent):
                       k1, k2, k3, k4, p1, p2, b1, b2: float
        useful_pixel_mask: optional HxW boolean array marking pixels that
                     correspond to real lens content (from per-image masks,
                     a lens-only mask, or an externally-derived FOV). When
                     omitted, the geometric upper bound is used: every pixel
                     where compute_rays produced finite values with positive
                     solid angle. The geometric upper bound is conservative
                     (over-estimates theta_max for cropped sensors), so
                     passing a real mask tightens the routing decision.

    Returns:
        dict with keys:
            theta_max_deg     : float, max half-angle of useful pixels (degrees)
            center_solid_angle: float, omega at the optical-center pixel (sr)
            rays              : HxWx3 ray-direction array
            omega             : HxW solid-angle-per-pixel array (sr)
            theta_deg         : HxW polar-angle array from optical axis (degrees)
            useful_pixel_mask : HxW boolean array (same as input if provided,
                                otherwise the geometric upper bound)
            calibration_type  : the projection string from the input dict,
                                preserved so downstream routing can apply the
                                projection-aware stretch formula
            model             : the v4 model string used internally
                                ('equisolid', 'equidistant', ...)

        Returns None if the projection is one the adaptive path does not
        handle (frame/pinhole or equirectangular). Equirectangular sensors
        have their own splitting strategy (see Task 6 of the redesign plan);
        frame sensors do not need reprojection at all.

    Reuses v4.compute_rays() and v4.compute_solid_angle_fd() — does not
    duplicate the math.
    """
    projection = (calibration.get("projection") or "").lower()
    model = _PROJECTION_TO_MODEL.get(projection)
    if model is None:
        raise ValueError(f"Unknown projection type: {projection!r}")
    if model in ("pinhole", "equirectangular"):
        # Adaptive single-pinhole reprojection does not apply to these sensor
        # types. Caller is responsible for routing them to their own paths.
        return None

    width = int(calibration["width"])
    height = int(calibration["height"])
    params = _calibration_to_v4_params(calibration)

    # 1. Ray directions per pixel — uses corrected rays when Fourier
    #    corrections are present in the calibration dict.
    corrections = calibration.get("corrections")
    rays, _ = _compute_rays_with_corrections(width, height, params, model, corrections=corrections)

    # 2. Solid angle per pixel via finite-difference gradients (v4 function)
    omega = _v4_compute_solid_angle_fd(rays)

    # 3. Center solid angle: the omega value at the optical-center pixel.
    #    This is the value Formula 1 (f_target = 1/sqrt(omega_center)) uses
    #    to size the output focal length.
    cy_idx, cx_idx = height // 2, width // 2
    center_solid_angle = float(omega[cy_idx, cx_idx])

    # 4. Polar angle per pixel: theta = arccos(z_normalized).
    #    Rays from v4.compute_rays are not guaranteed unit-length, so
    #    normalize before taking arccos. Suppress invalid/divide-by-zero
    #    warnings at boundary pixels where norms can be zero.
    norms = np.linalg.norm(rays, axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        rz_normalized = np.clip(rays[..., 2] / norms[..., 0], -1.0, 1.0)
    theta_deg = np.degrees(np.arccos(rz_normalized))

    # 5. Useful-pixel mask. If the caller provided one (typically derived
    #    from segmentation masks or a lens-only mask), use it directly.
    #    Otherwise fall back to the geometric upper bound: any pixel where
    #    compute_rays returned a finite ray with positive solid angle.
    if useful_pixel_mask is None:
        useful_pixel_mask = (omega > 0) & np.isfinite(theta_deg)
    else:
        has_support = (np.asarray(useful_pixel_mask) > 0) & np.isfinite(theta_deg)
        if not np.any(has_support):
            return None
        derived_angle = float(theta_deg[has_support].max())
        useful_pixel_mask = (
            _v4_filter_center_component(theta_deg, derived_angle, dilation_px=1) > 0
        )

    if not np.any(useful_pixel_mask):
        # No valid pixels at all — caller cannot route this sensor.
        return None

    theta_max_deg = float(theta_deg[useful_pixel_mask].max())

    return {
        "theta_max_deg": theta_max_deg,
        "center_solid_angle": center_solid_angle,
        "rays": rays,
        "omega": omega,
        "theta_deg": theta_deg,
        "useful_pixel_mask": useful_pixel_mask,
        "calibration_type": calibration.get("projection", ""),
        "model": model,
    }


# --- 7. Image Application (fills Gap 2) ---
#
# The Section-4 precomputation produces (sourceimage_x, sourceimage_y, indices,
# pixel_weights) — the "recipe" describing which source pixels feed each output
# pixel and with what weights. The functions below apply that recipe to actual
# image and mask files. They mirror v4.remap_image() / v4.remap_mask() but write
# a single square output of side w_out rather than one of five cubeface tiles.

def apply_adaptive_remap(indices, pixel_weights, w_out, channel_values):
    """
    Apply precomputed RBF indices+weights to a single image channel.

    Identical math to v4.apply_fast_gaussian_remap. The only difference is the
    output shape: a single w_out x w_out image rather than a facewidth-square
    cubeface. Replicated here (rather than imported) so the adaptive script
    can use the canonical (w_out, w_out) shape semantics in its own namespace.
    """
    neighbor_values = channel_values[indices]
    remapped = np.einsum('ij,ij->i', pixel_weights, neighbor_values)
    return remapped.reshape(w_out, w_out)


def remap_adaptive_image(sourceimage_x, sourceimage_y, indices, pixel_weights,
                         w_out, source_path, dest_path, expected_shape=None):
    """
    Apply precomputed adaptive remap to a color image.

    Mirrors v4.remap_image() with two changes:
      - w_out replaces facewidth (the adaptive output is square of side w_out).
      - Output is one file per source. No face suffix in the filename.

    expected_shape (height, width) is checked against the source image; a
    mismatch raises RuntimeError because the precomputed indices are tied
    to a specific source resolution.
    """
    img = cv2.imread(str(source_path))
    if img is None:
        raise RuntimeError(f"Failed to read {source_path}")
    if expected_shape is not None and img.shape[:2] != expected_shape:
        raise RuntimeError(
            f"Image {source_path} shape {img.shape[:2]} != expected {expected_shape}"
        )

    selected = img[sourceimage_y, sourceimage_x]
    b = apply_adaptive_remap(indices, pixel_weights, w_out, selected[:, 0])
    g = apply_adaptive_remap(indices, pixel_weights, w_out, selected[:, 1])
    r = apply_adaptive_remap(indices, pixel_weights, w_out, selected[:, 2])

    output = np.clip(cv2.merge([b, g, r]), 0, 255).astype(np.uint8)
    cv2.imwrite(str(dest_path), output)


def remap_adaptive_mask(sourceimage_x, sourceimage_y, indices, pixel_weights,
                        w_out, source_path, dest_path, expected_shape=None):
    """
    Apply precomputed adaptive remap to a binary mask.

    Thresholds at 127 post-interpolation to preserve hard mask edges. Mirrors
    v4.remap_mask() with w_out replacing facewidth. Handles both single-channel
    and 3-channel mask images (takes the first channel of a 3-channel mask).
    """
    img = cv2.imread(str(source_path))
    if img is None:
        raise RuntimeError(f"Failed to read {source_path}")
    if expected_shape is not None and img.shape[:2] != expected_shape:
        raise RuntimeError(
            f"Mask {source_path} shape {img.shape[:2]} != expected {expected_shape}"
        )

    # Masks may be single-channel or 3-channel. Take the first channel.
    if img.ndim == 3:
        channel = img[:, :, 0]
    else:
        channel = img

    selected = channel[sourceimage_y, sourceimage_x]
    m = apply_adaptive_remap(indices, pixel_weights, w_out, selected)
    _, binary = cv2.threshold(m.astype(np.uint8), 127, 255, cv2.THRESH_BINARY)
    cv2.imwrite(str(dest_path), binary.astype(np.uint8))


# --- 8. End-to-End Orchestrator (fills Gap 3) ---
#
# process_sensor_adaptive() strings the pieces together for one sensor:
#   calibration -> lens characteristics -> routing gateway -> remap precompute
#   -> per-image / per-mask application.
#
# This is the function the redesign plan's Task 7 calls into for each fisheye
# sensor in the manifest. For sensors that route to CUBEMAP_SPLIT, the
# orchestrator currently raises NotImplementedError — production routing to
# cubeface dispatch lives in the exporter (Gap 4), not in the groundwork.
# The groundwork orchestrator is a standalone test harness for Path B.

def _list_image_files(directory):
    """Return a sorted list of image files in directory, or [] if missing."""
    if not directory:
        return []
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTENSIONS
    )


def process_sensor_adaptive(
    calibration: dict,
    image_dir,
    output_dir,
    mask_dir=None,
    lens_only_mask=None,
    useful_pixel_mask=None,
    f_target=None,
    w_out=None,
    progress_callback=None,
):
    """
    End-to-end processing for one fisheye sensor along the adaptive Path B.

    Steps:
      1. extract_lens_characteristics(calibration) -> theta_max_deg, center_solid_angle
      2. evaluate_shortfall_routing(...) -> SINGLE_PINHOLE or CUBEMAP_SPLIT
      3. If CUBEMAP_SPLIT: raise NotImplementedError (cubemap fallback belongs
         in the exporter integration, see Gap 4).
      4. If SINGLE_PINHOLE: compute_adaptive_pinhole_remap once for this lens,
         then iterate over the source images applying the remap.
      5. Iterate masks if mask_dir or lens_only_mask is provided.

    Args:
        calibration: calibration dict (see extract_lens_characteristics).
        image_dir: directory of source fisheye images for this sensor.
        output_dir: destination directory; created if missing. Will contain
                    output_dir/images/ and (if masks are applicable)
                    output_dir/masks/.
        mask_dir: optional per-image mask directory. Mask filenames are
                  matched to image filenames by stem.
        lens_only_mask: optional single mask defining the fisheye circle,
                        used as a fallback when per-image masks are absent.
        useful_pixel_mask: optional HxW boolean array passed through to
                           extract_lens_characteristics. When omitted, the
                           geometric upper bound is used.
        f_target, w_out: optional cached routing intrinsics from the scene
                         manifest. When both are provided, the exporter uses
                         them directly so generated image dimensions match
                         the COLMAP camera record.
        progress_callback: optional callable(str) for per-image progress
                           updates. Defaults to print().

    Returns:
        dict with keys:
            mode             : "single_pinhole"
            processed_count  : number of source images written
            skipped_count    : number of source images skipped due to errors
            output_dir       : str path written to
            f_target         : adaptive focal length (pixels)
            w_out            : adaptive output side length (pixels)
            theta_max_deg    : max useful half-angle the lens reached
            calibration_type : projection string from the input calibration
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    def _log(msg):
        if progress_callback is not None:
            progress_callback(msg)
        else:
            print(msg)

    # 1. Lens characteristics
    characteristics = extract_lens_characteristics(calibration, useful_pixel_mask=useful_pixel_mask)
    if characteristics is None:
        raise ValueError(
            "extract_lens_characteristics returned None — sensor is not a "
            "supported fisheye projection (frame/pinhole/equirectangular have "
            "their own paths)."
        )

    # 2. Routing gateway (projection-aware)
    if f_target is None or w_out is None:
        decision, f_target, w_out = evaluate_shortfall_routing(
            characteristics["theta_max_deg"],
            characteristics["center_solid_angle"],
            characteristics["calibration_type"],
        )
    else:
        decision = "SINGLE_PINHOLE"
        f_target = float(f_target)
        w_out = int(w_out)

    # 3. Cubemap fallback path — out of scope for the groundwork.
    if decision == "CUBEMAP_SPLIT":
        # The groundwork orchestrator is a standalone Path B test harness.
        # Production routing to cubeface processing lives in the COLMAP
        # exporter (metashape_cameras_to_colmap.py) where the manifest's
        # processing_mode field dispatches between the two paths. See
        # Gap 4 in Adaptive_Pinhole_Undistort_Plan.md.
        raise NotImplementedError(
            "Routing returned CUBEMAP_SPLIT. The groundwork orchestrator only "
            "runs the adaptive path. Wide-FOV sensors should be processed by "
            "cubeface processing via the exporter's dispatch layer (Gap 4)."
        )

    # 4. Adaptive remap precompute (runs once per lens)
    _log(f"Path B precompute: f_target={f_target:.2f}, w_out={w_out}")
    sourceimage_x, sourceimage_y, indices, pixel_weights = compute_adaptive_pinhole_remap(
        width=int(calibration["width"]),
        height=int(calibration["height"]),
        rays=characteristics["rays"],
        useful_pixel_mask=characteristics["useful_pixel_mask"],
        f_target=f_target,
        w_out=w_out,
    )

    expected_shape = (int(calibration["height"]), int(calibration["width"]))

    image_files = _list_image_files(image_dir)
    if not image_files:
        _log(f"No image files found in {image_dir}")
        return {
            "mode": "single_pinhole",
            "processed_count": 0,
            "skipped_count": 0,
            "output_dir": str(output_dir),
            "f_target": f_target,
            "w_out": w_out,
            "theta_max_deg": characteristics["theta_max_deg"],
            "calibration_type": characteristics["calibration_type"],
        }

    # 5. Per-image application
    images_out_dir = output_dir / "images"
    masks_out_dir = output_dir / "masks"
    images_out_dir.mkdir(exist_ok=True)

    have_per_image_masks = mask_dir is not None and Path(mask_dir).is_dir()
    have_any_mask = have_per_image_masks or lens_only_mask is not None
    if have_any_mask:
        masks_out_dir.mkdir(exist_ok=True)

    processed = 0
    skipped = 0
    for src in image_files:
        dest = images_out_dir / f"{src.stem}.png"
        try:
            remap_adaptive_image(
                sourceimage_x, sourceimage_y, indices, pixel_weights,
                w_out, src, dest, expected_shape=expected_shape,
            )
        except RuntimeError as e:
            _log(f"Skip {src.name}: {e}")
            skipped += 1
            continue

        # Mask handling: per-image mask if present, else lens-only fallback.
        mask_src = None
        if have_per_image_masks:
            mask_candidate = Path(mask_dir) / f"{src.stem}.png"
            if mask_candidate.is_file():
                mask_src = mask_candidate
        if mask_src is None and lens_only_mask is not None:
            mask_src = Path(lens_only_mask)

        if mask_src is not None:
            mask_dest = masks_out_dir / f"{src.stem}_mask.png"
            try:
                remap_adaptive_mask(
                    sourceimage_x, sourceimage_y, indices, pixel_weights,
                    w_out, mask_src, mask_dest, expected_shape=expected_shape,
                )
            except RuntimeError as e:
                _log(f"Mask skip {src.name}: {e}")

        processed += 1
        if processed % 25 == 0:
            _log(f"Processed {processed}/{len(image_files)}")

    _log(f"Done: {processed} processed, {skipped} skipped")

    return {
        "mode": "single_pinhole",
        "processed_count": processed,
        "skipped_count": skipped,
        "output_dir": str(output_dir),
        "f_target": f_target,
        "w_out": w_out,
        "theta_max_deg": characteristics["theta_max_deg"],
        "calibration_type": characteristics["calibration_type"],
    }


# =============================================================================
# Pointers for code that lives elsewhere — what this script intentionally
# does not implement, and where its consumers will sit.
# =============================================================================
#
# UID-based routing cache (lives in the GUI, NOT here):
#   The routing decision returned by evaluate_shortfall_routing() is a pure
#   function of its inputs. The GUI caches the decision per sensor, keyed by:
#
#       UID = hash(
#           intrinsics,        # (f, cx, cy, K1..K4, P1, P2, B1, B2)
#           calibration_type,  # 'equisolid', 'equidistant', ...
#           mask_digest,       # hash of mask directory contents (or '' if none)
#           (THETA_MAX_THRESHOLD_DEG, STRETCH_THRESHOLD, W_OUT_BUDGET),
#       )
#
#   On any input change (added mask directories, edited thresholds, reloaded
#   XML) the UID differs and the cached decision is invalidated; the GUI
#   recomputes lazily. Same pattern as Meshroom's per-node UID hashing
#   (meshroom/core/node.py). This is part of Task 7's GUI design — not a
#   deferred refinement. See "Routing decision lifecycle" in the companion
#   plan document.
#
# Exporter integration (lives in metashape_cameras_to_colmap.py, NOT here):
#   When the v2 scene manifest carries a sensor with processing_mode ==
#   "single_pinhole", the COLMAP exporter must:
#     - emit one PINHOLE camera entry per sensor (not five face entries)
#     - emit one image entry per source image (not five)
#     - use fx = fy = routing.f_target (not facewidth / 2)
#     - use the direct world-to-camera pose (no face-rotation composition)
#   The cached routing fields (f_target, w_out) are persisted in the manifest
#   so the exporter does not need to recompute them. This integration is
#   Gap 4 in the companion plan document.
