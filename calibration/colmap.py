from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .core import Calibration, CalibrationSourceGeometry


@dataclass(frozen=True)
class ColmapCamera:
    camera_id: int
    model: str
    width: int
    height: int
    params: tuple[float, ...]


_SUPPORTED_MODELS = {
    "SIMPLE_PINHOLE",
    "PINHOLE",
    "SIMPLE_FISHEYE",
    "FISHEYE",
    "SIMPLE_RADIAL_FISHEYE",
    "RADIAL_FISHEYE",
    "OPENCV_FISHEYE",
}


def load_colmap_calibration(path: str | Path, camera_id: int | None = None) -> Calibration:
    path = Path(path)
    cameras_path = path / "cameras.txt" if path.is_dir() else path
    if not cameras_path.is_file():
        raise ValueError(f"COLMAP calibration path does not exist: {cameras_path}")

    cameras = _read_cameras_txt(cameras_path)
    if not cameras:
        raise ValueError(f"No cameras found in COLMAP cameras file {cameras_path}.")

    if camera_id is None:
        if len(cameras) != 1:
            ids = ", ".join(str(c.camera_id) for c in cameras)
            raise ValueError(
                f"COLMAP cameras file contains multiple cameras ({ids}); pass --camera-id."
            )
        camera = cameras[0]
    else:
        matches = [c for c in cameras if c.camera_id == camera_id]
        if not matches:
            ids = ", ".join(str(c.camera_id) for c in cameras)
            raise ValueError(
                f"COLMAP camera_id {camera_id} not found in {cameras_path}. Available: {ids}."
            )
        camera = matches[0]

    if camera.model not in _SUPPORTED_MODELS:
        raise ValueError(
            f"COLMAP camera model {camera.model!r} is not supported yet. "
            f"Supported first-wave models: {', '.join(sorted(_SUPPORTED_MODELS))}."
        )

    rays, params_dict = _rays_for_colmap_camera(camera)
    warnings = (
        "COLMAP coefficients use COLMAP camera-model semantics. Do not "
        "substitute them into Metashape XML.",
    )

    return Calibration(
        provider="colmap",
        model=camera.model,
        width=camera.width,
        height=camera.height,
        params={
            "camera_id": camera.camera_id,
            "params": list(camera.params),
            **params_dict,
        },
        source_path=cameras_path,
        source_geometry=CalibrationSourceGeometry(
            image_width=camera.width,
            image_height=camera.height,
            pixel_center_convention="colmap_pixel_centers",
            source_image_state="original_distorted",
        ),
        warnings=warnings,
        raw={
            "camera_id": camera.camera_id,
            "model": camera.model,
            "width": camera.width,
            "height": camera.height,
            "params": list(camera.params),
        },
        rays=rays,
    )


def _read_cameras_txt(path: Path) -> list[ColmapCamera]:
    cameras: list[ColmapCamera] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 5:
            raise ValueError(f"Malformed COLMAP cameras.txt line: {line}")
        camera_id = int(parts[0])
        model = parts[1].upper()
        width = int(parts[2])
        height = int(parts[3])
        params = tuple(float(p) for p in parts[4:])
        cameras.append(ColmapCamera(camera_id, model, width, height, params))
    return cameras


def _rays_for_colmap_camera(camera: ColmapCamera) -> tuple[np.ndarray, dict[str, object]]:
    p = camera.params
    model = camera.model

    if model == "SIMPLE_PINHOLE":
        _expect_param_count(camera, 3)
        f, cx, cy = p
        return _pinhole_rays(camera.width, camera.height, f, f, cx, cy), {
            "fx": f,
            "fy": f,
            "cx": cx,
            "cy": cy,
        }

    if model == "PINHOLE":
        _expect_param_count(camera, 4)
        fx, fy, cx, cy = p
        return _pinhole_rays(camera.width, camera.height, fx, fy, cx, cy), {
            "fx": fx,
            "fy": fy,
            "cx": cx,
            "cy": cy,
        }

    if model == "SIMPLE_FISHEYE":
        _expect_param_count(camera, 3)
        f, cx, cy = p
        return _opencv_fisheye_rays(camera.width, camera.height, f, f, cx, cy, (0, 0, 0, 0)), {
            "fx": f,
            "fy": f,
            "cx": cx,
            "cy": cy,
            "D": [0.0, 0.0, 0.0, 0.0],
        }

    if model == "FISHEYE":
        _expect_param_count(camera, 4)
        fx, fy, cx, cy = p
        return _opencv_fisheye_rays(camera.width, camera.height, fx, fy, cx, cy, (0, 0, 0, 0)), {
            "fx": fx,
            "fy": fy,
            "cx": cx,
            "cy": cy,
            "D": [0.0, 0.0, 0.0, 0.0],
        }

    if model == "SIMPLE_RADIAL_FISHEYE":
        _expect_param_count(camera, 4)
        f, cx, cy, k = p
        return _opencv_fisheye_rays(camera.width, camera.height, f, f, cx, cy, (k, 0, 0, 0)), {
            "fx": f,
            "fy": f,
            "cx": cx,
            "cy": cy,
            "D": [k, 0.0, 0.0, 0.0],
        }

    if model == "RADIAL_FISHEYE":
        _expect_param_count(camera, 5)
        f, cx, cy, k1, k2 = p
        return _opencv_fisheye_rays(camera.width, camera.height, f, f, cx, cy, (k1, k2, 0, 0)), {
            "fx": f,
            "fy": f,
            "cx": cx,
            "cy": cy,
            "D": [k1, k2, 0.0, 0.0],
        }

    if model == "OPENCV_FISHEYE":
        _expect_param_count(camera, 8)
        fx, fy, cx, cy, k1, k2, k3, k4 = p
        return _opencv_fisheye_rays(camera.width, camera.height, fx, fy, cx, cy, (k1, k2, k3, k4)), {
            "fx": fx,
            "fy": fy,
            "cx": cx,
            "cy": cy,
            "D": [k1, k2, k3, k4],
        }

    raise AssertionError(f"Unhandled COLMAP model: {model}")


def _expect_param_count(camera: ColmapCamera, expected: int) -> None:
    if len(camera.params) != expected:
        raise ValueError(
            f"COLMAP model {camera.model} expects {expected} params, "
            f"got {len(camera.params)} for camera {camera.camera_id}."
        )


def _pinhole_rays(width: int, height: int, fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    u = np.arange(width, dtype=np.float64)
    v = np.arange(height, dtype=np.float64)
    uu, vv = np.meshgrid(u, v)
    x = (uu - cx) / fx
    y = (vv - cy) / fy
    rays = np.stack([x, y, np.ones_like(x)], axis=-1)
    norms = np.linalg.norm(rays, axis=-1, keepdims=True)
    return (rays / np.maximum(norms, 1e-12)).astype(np.float32)


def _opencv_fisheye_rays(
    width: int,
    height: int,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    d: tuple[float, float, float, float],
) -> np.ndarray:
    k = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float64)
    dist = np.asarray(d, dtype=np.float64)
    u = np.arange(width, dtype=np.float64)
    v = np.arange(height, dtype=np.float64)
    uu, vv = np.meshgrid(u, v)
    pts = np.stack([uu, vv], axis=-1).reshape(-1, 1, 2)
    undistorted = cv2.fisheye.undistortPoints(pts, k, dist).reshape(height, width, 2)
    x = undistorted[..., 0]
    y = undistorted[..., 1]
    rays = np.stack([x, y, np.ones_like(x)], axis=-1)
    norms = np.linalg.norm(rays, axis=-1, keepdims=True)
    return (rays / np.maximum(norms, 1e-12)).astype(np.float32)
