"""Reactive routing cache for fisheye adaptive pinhole decisions.

This module owns the Meshroom-style UID invalidation layer described in
docs/superpowers/plans/Adaptive_Pinhole_Undistort_Plan.md. It consumes
sensor records from gui.sensor_discovery and returns RoutingDecision instances
for gui.scene_manifest.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from gui.fourier_corrections import corrections_cache_hash

import cv2
import numpy as np

from gui.adaptive_undistort import (
    calculate_adaptive_dimensions,
    evaluate_shortfall_routing,
    extract_lens_characteristics,
)
from gui.scene_manifest import RoutingDecision


THETA_MAX_THRESHOLD_DEG = 55.0
STRETCH_THRESHOLD = 3.0
W_OUT_BUDGET = 6000
ANALYSIS_MAX_DIM = 1600
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

_CALIBRATION_HASH_FIELDS = (
    "f", "cx", "cy",
    "k1", "k2", "k3", "k4",
    "p1", "p2",
    "b1", "b2",
)

_ROUTING_CACHE: dict[str, RoutingDecision] = {}


def _canonical_sha256(payload: object) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _thresholds(
    theta_max_threshold: float | None,
    stretch_threshold: float | None,
    w_out_budget: int | None,
) -> tuple[float, float, int]:
    return (
        float(THETA_MAX_THRESHOLD_DEG if theta_max_threshold is None else theta_max_threshold),
        float(STRETCH_THRESHOLD if stretch_threshold is None else stretch_threshold),
        int(W_OUT_BUDGET if w_out_budget is None else w_out_budget),
    )


def compute_mask_digest(
    mask_dirs: list[Path] | None,
    lens_only_mask: Path | None,
) -> str:
    """Return a deterministic digest for mask-directory metadata."""
    items = []
    for directory in mask_dirs or []:
        for p in sorted(Path(directory).iterdir()):
            if p.is_file():
                stat = p.stat()
                items.append((p.name, stat.st_size, stat.st_mtime_ns))
    if lens_only_mask is not None:
        p = Path(lens_only_mask)
        stat = p.stat()
        items.append(("__lens_only__", stat.st_size, stat.st_mtime_ns))
    return _canonical_sha256(items)


def compute_routing_uid(
    calibration: dict,
    mask_digest: str,
    thresholds: tuple[float, float, int],
    analysis_max_dim: int | None = ANALYSIS_MAX_DIM,
) -> str:
    """Return the deterministic UID for a routing decision."""
    calibration_payload = {
        name: float(calibration.get(name, 0.0))
        for name in _CALIBRATION_HASH_FIELDS
    }
    calibration_payload["projection"] = calibration.get("projection", "")

    # Include corrections hash — changes the ray field and routing answer
    corr_hash = corrections_cache_hash(calibration.get("corrections"))

    payload = {
        "calibration": calibration_payload,
        "mask_digest": mask_digest,
        "thresholds": thresholds,
        "analysis_max_dim": analysis_max_dim,
        "corrections_hash": corr_hash,
    }
    return _canonical_sha256(payload)


def clear_cache() -> None:
    """Clear the in-memory routing cache."""
    _ROUTING_CACHE.clear()


def _nearest_even(value: float) -> int:
    return max(2, int(round(float(value) / 2.0) * 2))


def _analysis_calibration(
    calibration: dict,
    max_dim: int | None,
) -> tuple[dict, float]:
    """Return a scaled calibration for GUI-safe routing analysis.

    The scale is analysis_pixels / original_pixels. Solid angles computed on
    the scaled grid must be multiplied by scale**2 to recover original
    per-pixel solid angle.
    """
    if max_dim is None:
        return dict(calibration), 1.0
    width = int(calibration["width"])
    height = int(calibration["height"])
    longest = max(width, height)
    if longest <= max_dim:
        return dict(calibration), 1.0

    scale = float(max_dim) / float(longest)
    scaled = dict(calibration)
    scaled["width"] = max(2, int(round(width * scale)))
    scaled["height"] = max(2, int(round(height * scale)))
    for key in ("f", "cx", "cy", "b1", "b2"):
        if key in scaled:
            scaled[key] = float(scaled[key]) * scale
    return scaled, scale


def _resize_mask(mask_path: Path, expected_shape: tuple[int, int]) -> np.ndarray:
    img = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise SystemExit(f"Error: failed to read mask {mask_path}.")
    height, width = expected_shape
    if img.shape != expected_shape:
        img = cv2.resize(img, (width, height), interpolation=cv2.INTER_NEAREST)
    return img > 127


def _load_analysis_useful_pixel_mask(
    width: int,
    height: int,
    mask_dirs: list[Path] | None,
    lens_only_mask: Path | None,
) -> np.ndarray | None:
    expected_shape = (height, width)
    union_mask = None
    for directory in mask_dirs or []:
        d = Path(directory)
        if not d.is_dir():
            continue
        for path in sorted(d.iterdir()):
            if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS:
                mask = _resize_mask(path, expected_shape)
                union_mask = mask if union_mask is None else (union_mask | mask)

    lens_mask = None
    if lens_only_mask is not None:
        lens_mask = _resize_mask(Path(lens_only_mask), expected_shape)

    if union_mask is not None and lens_mask is not None:
        return (union_mask & lens_mask).astype(np.uint8)
    if union_mask is not None:
        return union_mask.astype(np.uint8)
    if lens_mask is not None:
        return lens_mask.astype(np.uint8)
    return None


def get_routing(
    sensor_record: dict,
    mask_dirs: list[Path] | None = None,
    lens_only_mask: Path | None = None,
    *,
    theta_max_threshold: float | None = None,
    stretch_threshold: float | None = None,
    w_out_budget: int | None = None,
    analysis_max_dim: int | None = ANALYSIS_MAX_DIM,
) -> RoutingDecision:
    """Return the cached routing decision for a sensor record.

    Reads only ``sensor_record["calibration"]`` plus mask metadata and threshold
    overrides. Sensor labels, prefixes, camera ids, and counts are intentionally
    excluded from the UID because they do not affect the routing answer.
    """
    calibration = sensor_record.get("calibration")
    if calibration is None:
        raise ValueError("Cannot route sensor without calibration data.")

    threshold_values = _thresholds(
        theta_max_threshold,
        stretch_threshold,
        w_out_budget,
    )
    mask_digest = compute_mask_digest(mask_dirs, lens_only_mask)
    uid = compute_routing_uid(
        calibration,
        mask_digest,
        threshold_values,
        analysis_max_dim=analysis_max_dim,
    )

    cached = _ROUTING_CACHE.get(uid)
    if cached is not None:
        return cached

    analysis_calibration, analysis_scale = _analysis_calibration(
        calibration,
        analysis_max_dim,
    )
    useful_pixel_mask = None
    try:
        useful_pixel_mask = _load_analysis_useful_pixel_mask(
            int(analysis_calibration["width"]),
            int(analysis_calibration["height"]),
            mask_dirs=mask_dirs,
            lens_only_mask=lens_only_mask,
        )
    except SystemExit as exc:
        sensor_id = sensor_record.get("sensor_id", "?")
        print(
            f"Routing masks unusable for sensor {sensor_id}: {exc}. "
            "Falling back to geometric default.",
            file=sys.stderr,
        )

    characteristics = extract_lens_characteristics(
        analysis_calibration,
        useful_pixel_mask=useful_pixel_mask,
    )
    if characteristics is None:
        raise ValueError("Cannot route sensor with unsupported or empty fisheye support.")

    if analysis_scale != 1.0:
        characteristics = dict(characteristics)
        characteristics["center_solid_angle"] = (
            float(characteristics["center_solid_angle"]) * (analysis_scale ** 2)
        )

    f_resolution, w_single = calculate_adaptive_dimensions(
        characteristics["theta_max_deg"],
        characteristics["center_solid_angle"],
    )
    routing_mode, f_target, w_out = evaluate_shortfall_routing(
        characteristics["theta_max_deg"],
        characteristics["center_solid_angle"],
        characteristics["calibration_type"],
        theta_max_threshold=threshold_values[0],
        stretch_threshold=threshold_values[1],
        w_out_budget=threshold_values[2],
    )

    processing_mode = (
        "single_pinhole" if routing_mode == "SINGLE_PINHOLE" else "multi_pinhole"
    )
    recommended_output_width = (
        int(w_out)
        if processing_mode == "single_pinhole" and w_out is not None
        else _nearest_even(2.0 * float(f_resolution))
    )
    decision = RoutingDecision(
        processing_mode=processing_mode,
        f_target=float(f_target) if f_target is not None else None,
        w_out=int(w_out) if w_out is not None else None,
        recommended_output_width=recommended_output_width,
        theta_max_deg=float(characteristics["theta_max_deg"]),
        routing_uid=uid,
    )
    _ROUTING_CACHE[uid] = decision
    return decision


__all__ = [
    "THETA_MAX_THRESHOLD_DEG",
    "STRETCH_THRESHOLD",
    "W_OUT_BUDGET",
    "ANALYSIS_MAX_DIM",
    "get_routing",
    "compute_mask_digest",
    "compute_routing_uid",
    "clear_cache",
]
