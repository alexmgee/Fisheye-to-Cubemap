from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .core import Calibration, CalibrationSourceGeometry


def load_opencv_fisheye_calibration(path: str | Path) -> Calibration:
    path = Path(path)
    data = _read_opencv_payload(path)

    width = _first_int(data, ("width", "image_width", "ImageWidth"))
    height = _first_int(data, ("height", "image_height", "ImageHeight"))
    if width is None or height is None:
        raise ValueError(
            "OpenCV fisheye calibration requires width/height or image_width/image_height."
        )

    k = _first_matrix(data, ("K", "camera_matrix", "cameraMatrix"))
    d = _first_matrix(data, ("D", "distortion_coefficients", "distCoeffs", "distortion"))
    if k is None:
        raise ValueError("OpenCV fisheye calibration requires camera matrix K.")
    if d is None:
        raise ValueError("OpenCV fisheye calibration requires distortion vector D.")

    k = np.asarray(k, dtype=np.float64).reshape(3, 3)
    d = np.asarray(d, dtype=np.float64).reshape(-1)
    if d.size < 4:
        raise ValueError(f"OpenCV fisheye D must contain at least 4 coefficients, got {d.size}.")
    if d.size > 4:
        d = d[:4]

    rays = _opencv_fisheye_rays(width, height, k, d)

    warnings = (
        "OpenCV fisheye coefficients use OpenCV's theta-distortion model. Do not "
        "substitute them into Metashape XML.",
    )
    fx = float(k[0, 0])
    fy = float(k[1, 1])
    if abs(fx - fy) / max(abs(fx), abs(fy), 1.0) > 0.01:
        warnings = warnings + (
            f"OpenCV fisheye calibration has fx/fy differing by more than 1% ({fx} vs {fy}); supported, but verify outputs.",
        )

    return Calibration(
        provider="opencv-fisheye",
        model="opencv_fisheye",
        width=width,
        height=height,
        params={
            "K": k.tolist(),
            "D": d.tolist(),
            "fx": fx,
            "fy": fy,
            "cx": float(k[0, 2]),
            "cy": float(k[1, 2]),
        },
        source_path=path,
        source_geometry=CalibrationSourceGeometry(
            image_width=width,
            image_height=height,
            pixel_center_convention="opencv_pixel_centers",
            source_image_state="original_distorted",
        ),
        warnings=warnings,
        raw=_json_safe(data),
        rays=rays,
    )


def _opencv_fisheye_rays(width: int, height: int, k: np.ndarray, d: np.ndarray) -> np.ndarray:
    u = np.arange(width, dtype=np.float64)
    v = np.arange(height, dtype=np.float64)
    uu, vv = np.meshgrid(u, v)
    pts = np.stack([uu, vv], axis=-1).reshape(-1, 1, 2)
    undistorted = cv2.fisheye.undistortPoints(pts, k, d).reshape(height, width, 2)
    x = undistorted[..., 0]
    y = undistorted[..., 1]
    rays = np.stack([x, y, np.ones_like(x)], axis=-1)
    norms = np.linalg.norm(rays, axis=-1, keepdims=True)
    return (rays / np.maximum(norms, 1e-12)).astype(np.float32)


def _read_opencv_payload(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(path.read_text())
    if suffix in {".yml", ".yaml", ".xml"}:
        return _read_opencv_filestorage(path)
    raise ValueError(
        f"Unsupported OpenCV fisheye calibration extension {path.suffix!r}; "
        "use JSON, YAML/YML, or XML."
    )


def _read_opencv_filestorage(path: Path) -> dict[str, Any]:
    fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
    if not fs.isOpened():
        raise ValueError(f"Could not open OpenCV calibration file {path}.")
    try:
        keys = (
            "width",
            "height",
            "image_width",
            "image_height",
            "K",
            "camera_matrix",
            "cameraMatrix",
            "D",
            "distortion_coefficients",
            "distCoeffs",
            "distortion",
        )
        data: dict[str, Any] = {}
        for key in keys:
            node = fs.getNode(key)
            if node.empty():
                continue
            if node.isInt():
                data[key] = int(node.real())
            elif node.isReal():
                data[key] = float(node.real())
            else:
                mat = node.mat()
                if mat is not None:
                    data[key] = mat.tolist()
        return data
    finally:
        fs.release()


def _first_int(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in data and data[key] is not None:
            return int(data[key])
    return None


def _first_matrix(data: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value
