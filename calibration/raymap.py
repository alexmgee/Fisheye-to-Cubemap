from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .core import Calibration, CalibrationSourceGeometry

RAYMAP_VERSION = "fisheye-to-cubemap-raymap-v1"


def _decode_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, np.ndarray):
        value = value.item() if value.shape == () else value.tolist()
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _scalar_str(data: Any, key: str, default: str = "") -> str:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, np.ndarray):
        value = value.item() if value.shape == () else value.tolist()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _npz_get(data: Any, key: str) -> Any | None:
    return data[key] if key in data.files else None


def load_raymap_calibration(path: str | Path) -> Calibration:
    path = Path(path)
    with np.load(str(path), allow_pickle=False) as data:
        version = _scalar_str(data, "version")
        if version != RAYMAP_VERSION:
            raise ValueError(
                f"Unsupported raymap version {version!r}; expected {RAYMAP_VERSION!r}."
            )
        rays = np.asarray(data["rays"], dtype=np.float32)
        if rays.ndim != 3 or rays.shape[2] != 3:
            raise ValueError(f"Raymap rays must have shape HxWx3, got {rays.shape}.")
        height, width, _ = rays.shape
        if not np.all(np.isfinite(rays)):
            raise ValueError("Raymap contains non-finite ray values.")
        norms = np.linalg.norm(rays, axis=-1)
        if np.any(norms <= 0):
            raise ValueError("Raymap contains zero-length rays.")
        max_norm_error = float(np.max(np.abs(norms - 1.0)))
        if max_norm_error > 1e-3:
            raise ValueError(
                f"Raymap rays are not unit length enough; max norm error={max_norm_error:.6g}."
            )
        if max_norm_error > 1e-6:
            rays = rays / norms[..., None]

        params = _decode_json(_npz_get(data, "params_json"), {})
        source_geometry_data = _decode_json(_npz_get(data, "source_geometry_json"), {})
        source_geometry = CalibrationSourceGeometry(
            image_width=int(source_geometry_data.get("image_width", width)),
            image_height=int(source_geometry_data.get("image_height", height)),
            pixel_center_convention=source_geometry_data.get(
                "pixel_center_convention", "raymap_dense_pixel_centers"
            ),
            source_image_state=source_geometry_data.get("source_image_state", "original_distorted"),
            rotation_applied_deg=float(source_geometry_data.get("rotation_applied_deg", 0.0)),
            crop_rect=source_geometry_data.get("crop_rect"),
            scale_from_calibration=float(source_geometry_data.get("scale_from_calibration", 1.0)),
            lens_side=source_geometry_data.get("lens_side", "unknown"),
            warnings=tuple(source_geometry_data.get("warnings", [])),
        )

        warnings = []
        if max_norm_error > 1e-6:
            warnings.append(
                f"Raymap rays were renormalized on load; max norm error was {max_norm_error:.6g}."
            )

        return Calibration(
            provider="raymap",
            model=_scalar_str(data, "model", "dense-raymap"),
            width=width,
            height=height,
            params={
                **params,
                "source_provider": _scalar_str(data, "provider", "unknown"),
                "source": _scalar_str(data, "source", ""),
                "notes": _scalar_str(data, "notes", ""),
                "max_norm_error": max_norm_error,
            },
            source_path=path,
            source_geometry=source_geometry,
            warnings=tuple(warnings),
            raw={"version": version},
            rays=rays,
        )


def save_raymap(
    path: str | Path,
    calibration: Calibration,
    rays: np.ndarray,
    solid_angle: np.ndarray | None = None,
    compression: str = "compressed",
    notes: str = "",
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rays = np.asarray(rays, dtype=np.float32)
    payload = {
        "version": np.array(RAYMAP_VERSION),
        "width": np.array(calibration.width, dtype=np.int32),
        "height": np.array(calibration.height, dtype=np.int32),
        "rays": rays,
        "provider": np.array(calibration.provider),
        "model": np.array(calibration.model),
        "source": np.array(str(calibration.source_path) if calibration.source_path else ""),
        "source_geometry_json": np.array(json.dumps(calibration.source_geometry.__dict__, sort_keys=True)),
        "params_json": np.array(json.dumps(calibration.params, sort_keys=True, default=str)),
        "notes": np.array(notes),
    }
    if solid_angle is not None:
        payload["solid_angle"] = np.asarray(solid_angle, dtype=np.float32)
    if compression == "stored":
        np.savez(str(path), **payload)
    elif compression == "compressed":
        np.savez_compressed(str(path), **payload)
    else:
        raise ValueError(f"Unknown raymap compression mode: {compression}")
    return path
