"""Corrected ray computation with Fourier displacement.

Provides compute_rays_with_corrections() — a drop-in replacement for
v4.compute_rays() that applies Metashape's Fourier additional corrections
before Brown's undistortion. When corrections is None, delegates directly
to v4 with zero overhead.

Also provides derive_useful_pixel_mask() which replicates the mask-derivation
logic from v4's compute_metashape_rays_usefulpixmap() so that we can use a
corrected ray field without calling the monolithic v4 function.

Reference:
  docs/plans/2026-05-18-fourier-corrections-plan.md (Phase 2)
"""

from __future__ import annotations

import numpy as np

from gui.fourier_corrections import FourierCorrections, apply_fourier_displacement

from AM_ImageAndMask_to_cubemap_v4 import (
    compute_rays as _v4_compute_rays,
    compute_solid_angle_fd as _v4_compute_solid_angle_fd,
    equidistant_to_rays as _v4_equidistant_to_rays,
    equisolid_to_rays as _v4_equisolid_to_rays,
    filter_center_component as _v4_filter_center_component,
    pinhole_to_rays as _v4_pinhole_to_rays,
    undistort_points as _v4_undistort_points,
)


def compute_rays_with_corrections(
    width: int,
    height: int,
    params: tuple,
    model: str,
    corrections: FourierCorrections | None = None,
) -> tuple[np.ndarray, None]:
    """Compute rays with optional Fourier correction.

    When corrections is None, delegates directly to v4.compute_rays()
    with zero overhead. When corrections is provided, applies the
    Fourier displacement to pixel coordinates before running the
    standard Brown's undistortion and projection.

    This duplicates the meshgrid/undistortion/projection logic from
    v4.compute_rays() (lines 978-1001) because we need to insert the
    Fourier step between the meshgrid and the principal-point
    subtraction. The undistortion and projection functions themselves
    are imported from v4, not duplicated.

    Args:
        width, height: image dimensions in pixels
        params: 11-tuple (f, cx, cy, K1, K2, K3, K4, P1, P2, B1, B2)
        model: 'pinhole', 'equidistant', or 'equisolid'
        corrections: optional FourierCorrections instance

    Returns:
        (rays, None) — rays is HxWx3 array of unit ray directions
    """
    if corrections is None:
        return _v4_compute_rays(width, height, params, model)

    f, cx, cy, K1, K2, K3, K4, P1, P2, B1, B2 = params

    u = np.arange(width, dtype=np.float64)
    v = np.arange(height, dtype=np.float64)
    uu, vv = np.meshgrid(u, v)

    # Apply Fourier correction in pixel space
    uu, vv = apply_fourier_displacement(uu, vv, corrections)

    # From here: identical to v4.compute_rays() lines 984-1001
    up = uu - (width * 0.5 + cx)
    vp = vv - (height * 0.5 + cy)

    yp = vp / f
    xp = (up - B2 * yp) / (f + B1)

    x, y = _v4_undistort_points(xp, yp, K1, K2, K3, K4, P1, P2)

    if model == "pinhole":
        rays = _v4_pinhole_to_rays(x, y)
    elif model == "equidistant":
        rays = _v4_equidistant_to_rays(x, y)
    elif model == "equisolid":
        rays = _v4_equisolid_to_rays(x, y)
    else:
        raise ValueError(f"Unknown model: {model}")

    return rays, None


def derive_useful_pixel_mask(
    rays: np.ndarray,
    maskpixelcount: np.ndarray | None = None,
    image_derived_support: np.ndarray | None = None,
    maxangle: float | None = None,
    monotonic_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Derive the useful-pixel mask from a ray field.

    Mirrors the mask-derivation logic from v4's
    compute_metashape_rays_usefulpixmap() without calling that function,
    so we can use a corrected ray field as input.

    Args:
        rays: HxWx3 ray direction array
        maskpixelcount: optional HxW uint16 array from sum_thresholded_masks
        image_derived_support: optional HxW uint8 mask from image content
        maxangle: optional manual maximum angle in degrees
        monotonic_mask: optional HxW boolean mask from compute_monotonic_mask().
            Excludes pixels beyond the distortion polynomial's fold boundary.
            Applied before maxangle derivation so corrupted rays cannot
            inflate the support angle.

    Returns:
        (useful_pixel_mask, omega, maxangle_deg)
        - useful_pixel_mask: HxW uint8 array (0 or 255)
        - omega: HxW float64 solid angle per pixel
        - maxangle_deg: the effective maximum polar angle used
    """
    omega = _v4_compute_solid_angle_fd(rays)

    norms = np.linalg.norm(rays, axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        rz_normalized = np.clip(rays[..., 2] / norms[..., 0], -1.0, 1.0)
    theta_deg = np.degrees(np.arccos(rz_normalized))

    if monotonic_mask is not None:
        mono_bool = monotonic_mask > 0 if monotonic_mask.dtype != bool else monotonic_mask
    else:
        mono_bool = np.ones(rays.shape[:2], dtype=bool)

    if maskpixelcount is not None:
        support = (maskpixelcount > 0) & mono_bool
        if np.any(support):
            derived_angle = float(theta_deg[support].max())
        else:
            derived_angle = float(theta_deg[omega > 0].max()) if np.any(omega > 0) else 90.0
        maxangle = derived_angle
    elif image_derived_support is not None:
        support = (image_derived_support > 0) & mono_bool
        if np.any(support):
            maxangle = float(theta_deg[support].max())
        else:
            maxangle = float(theta_deg[omega > 0].max()) if np.any(omega > 0) else 90.0
    elif maxangle is None:
        valid = (omega > 0) & np.isfinite(theta_deg) & mono_bool
        maxangle = float(theta_deg[valid].max()) if np.any(valid) else 90.0

    useful_pixel_mask = _v4_filter_center_component(theta_deg, maxangle, dilation_px=1)

    if monotonic_mask is not None:
        useful_pixel_mask = useful_pixel_mask & (mono_bool.astype(np.uint8) * 255)

    return useful_pixel_mask, omega, maxangle


__all__ = [
    "compute_rays_with_corrections",
    "derive_useful_pixel_mask",
]
