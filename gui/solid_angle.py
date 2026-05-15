"""Compute optimal cubeface output width from sensor calibration.

The "optimal" width is the one whose center pixel matches the source's
center angular resolution — i.e. each output pixel covers the same solid
angle as the source's best-resolved pixel near the optical axis. Bigger
wastes pixels and processing time; smaller throws away source detail.

Formula:
    width = 2 * sqrt(1 / max_solid_angle_in_central_region)

The central region (inner 50% radius) is used to avoid the periphery,
where solid angle per pixel can spike artificially due to distortion. The
result is rounded to the nearest even integer.

This module imports ray and solid-angle primitives from
AM_ImageAndMask_to_cubemap_v4 rather than duplicating them, matching the
architectural choice made in the adaptive-pinhole groundwork. v4 is
treated as a stable library dependency.

Used by the redesigned COLMAP-export tab to auto-fill cubeface width
fields the moment a Metashape XML is loaded.
"""

import math
import numpy as np

# v4 library imports — treat v4 as a stable dependency.
from AM_ImageAndMask_to_cubemap_v4 import (
    compute_rays as _v4_compute_rays,
    compute_solid_angle_fd as _v4_compute_solid_angle_fd,
    equirectangular_to_rays as _v4_equirectangular_to_rays,
)


# Calibration parameter order expected by v4.compute_rays. Missing fields
# default to zero (matching Metashape's "absent parameter" convention).
_V4_PARAM_ORDER = ("f", "cx", "cy", "k1", "k2", "k3", "k4", "p1", "p2", "b1", "b2")


def _calibration_to_v4_params(calibration: dict) -> tuple:
    """Convert a calibration dict to v4's 11-tuple params format."""
    return tuple(float(calibration.get(name, 0.0)) for name in _V4_PARAM_ORDER)


def _projection_to_v4_model(projection: str) -> str | None:
    """Map a Metashape calibration projection string to a v4 model name.

    Returns None for frame/pinhole sensors (no reprojection needed).
    """
    projection = (projection or "").lower()
    if projection in ("frame", "pinhole") or "frame" in projection:
        return None
    if "equirectangular" in projection:
        return "equirectangular"
    if "equisolid" in projection:
        return "equisolid"
    if "equidistant" in projection or projection == "fisheye":
        return "equidistant"
    return None


def compute_optimal_width(calibration: dict) -> int | None:
    """Compute the optimal cubeface output width from a sensor calibration.

    Args:
        calibration: dict matching the shape that
                     `gui.sensor_discovery.extract_sensor_calibration` returns.
                     Required keys: projection, width, height, f, cx, cy.
                     Optional: k1..k4, p1, p2, b1, b2.

    Returns:
        Even integer width for fisheye and equirectangular sensors.
        None for frame/pinhole sensors (no reprojection), or for any
        projection the function does not recognize.

    The "optimal" width matches the source's center angular resolution:
        width = 2 * sqrt(1 / max_solid_angle_in_central_region)

    where the central region is the inner 50% radius from the optical
    center. Using the central region rather than the whole frame avoids
    peripheral solid-angle spikes from heavy distortion.
    """
    model = _projection_to_v4_model(calibration.get("projection", ""))
    if model is None:
        return None

    width = int(calibration["width"])
    height = int(calibration["height"])

    if model == "equirectangular":
        # Equirectangular has analytic per-pixel solid angle:
        #     omega(phi) = (2*pi/W) * (pi/H) * cos(phi)
        # v4's equirectangular_to_rays returns the latitude array phi for us.
        _, phi = _v4_equirectangular_to_rays(width, height)
        dlon = 2.0 * np.pi / width
        dlat = np.pi / height
        omega = dlon * dlat * np.cos(phi)
    else:
        params = _calibration_to_v4_params(calibration)
        rays, _ = _v4_compute_rays(width, height, params, model)
        omega = _v4_compute_solid_angle_fd(rays)

    # Mask to the central region: inner 50% radius from the geometric center.
    cy_center = height / 2.0
    cx_center = width / 2.0
    yy, xx = np.mgrid[0:height, 0:width]
    r = np.sqrt((xx - cx_center) ** 2 + (yy - cy_center) ** 2)
    max_r = min(cx_center, cy_center)
    central_mask = r < (max_r * 0.5)

    valid = central_mask & (omega > 0) & np.isfinite(omega)
    if not np.any(valid):
        return None

    max_omega = float(np.max(omega[valid]))
    if max_omega <= 0:
        return None

    optimal = 2.0 * math.sqrt(1.0 / max_omega)
    # Round to the nearest even integer — keeps cubeface dimensions
    # divisible by 2 for downstream block-based codecs/training tools.
    return int(round(optimal / 2.0) * 2)
