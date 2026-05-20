#!/usr/bin/env python3
"""Metashape equisolid fisheye / pinhole scene exporter for COLMAP text models.

The equisolid path preserves the original fisheye-to-cubeface converter's face
logic, then composes each Metashape fisheye pose with the fixed cubeface
rotations. Frame/pinhole Metashape cameras can be exported as passthrough
images in the same sparse model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


FACE_TAGS = ("+Z", "-X", "+X", "-Y", "+Y")
FACE_FILENAME_SUFFIX = {
    "+Z": "_dir_plusZ",
    "-X": "_dir_minusY",
    "+X": "_dir_plusY",
    "-Y": "_dir_minusX",
    "+Y": "_dir_plusX",
}
SUFFIX_TO_FACE = {suffix: face for face, suffix in FACE_FILENAME_SUFFIX.items()}
KNOWN_SUFFIXES = tuple(FACE_FILENAME_SUFFIX.values())
KNOWN_FACE_DIRS = tuple(suffix.lstrip("_") for suffix in KNOWN_SUFFIXES)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff")
SOURCE_IMAGE_MAP_FILENAME = "source_image_map.json"
CUBEFACE_RE = re.compile(
    r"^(?P<stem>.+?)(?P<suffix>_dir_(?:plusZ|minusX|minusY|plusX|plusY))(?P<ext>\.[^.]+)$",
    re.IGNORECASE,
)
SCENE_SCALE_DIAGNOSTICS_SCHEMA = "fisheye_to_cubemap.scene_scale_diagnostics.v1"
SCENE_NORMALIZATION_TRANSFORM_SCHEMA = "fisheye_to_cubemap.scene_normalization_transform.v1"
DEFAULT_SCENE_NORMALIZATION_CAMERA_RADIUS = 5.0
SCENE_SCALE_EPSILON = 1e-9

# Columns are the per-view pinhole camera's +X, +Y, +Z axes expressed in the
# source sensor's camera frame. Each sensor type registers its own labels:
#   - Fisheye sensors use the 5 cube faces (+Z, +X, -X, +Y, -Y) below, solved
#     from the project_to_face() equations in AM_ImageAndMask_to_cubemap_v4.py.
#   - Equirectangular sensors register angle-keyed labels
#     ("yaw000.0_pitch-35", etc.) at module load via gui.erp_reframe.
# face_world_to_camera_pose() composes the source-camera world pose with this
# basis to obtain each per-view COLMAP pose.
FACE_BASIS_SOURCE_FROM_FACE = {
    "+Z": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "-X": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)),
    "+X": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
    "-Y": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
    "+Y": ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
}

# Register ERP view entries (6 cubemap + 16 reframe = 22 angle-keyed labels
# like ``"yaw022.5_pitch-35"``) so face_world_to_camera_pose() can resolve
# them via the same shared composition path as fisheye cube faces.
try:
    from gui.erp_reframe import register_erp_face_entries as _register_erp_face_entries
    _register_erp_face_entries(FACE_BASIS_SOURCE_FROM_FACE)
except Exception:
    # The exporter is also runnable in environments without the GUI package
    # available; ERP support is then unavailable but fisheye/frame still work.
    pass

PLY_TYPE_FORMATS = {
    "char": ("b", 1),
    "int8": ("b", 1),
    "uchar": ("B", 1),
    "uint8": ("B", 1),
    "short": ("h", 2),
    "int16": ("h", 2),
    "ushort": ("H", 2),
    "uint16": ("H", 2),
    "int": ("i", 4),
    "int32": ("i", 4),
    "uint": ("I", 4),
    "uint32": ("I", 4),
    "float": ("f", 4),
    "float32": ("f", 4),
    "double": ("d", 8),
    "float64": ("d", 8),
}


class ValidationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _text(element: ET.Element, tag: str) -> Optional[str]:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _float(element: ET.Element, tag: str) -> Optional[float]:
    value = _text(element, tag)
    return None if value is None else float(value)


def _tuple3_from_text(text: Optional[str]) -> Optional[Tuple[float, float, float]]:
    if not text:
        return None
    values = tuple(float(part) for part in text.split())
    if len(values) != 3:
        return None
    return values


def _matrix3_from_text(text: Optional[str]) -> Optional[Tuple[Tuple[float, float, float], ...]]:
    if not text:
        return None
    values = tuple(float(part) for part in text.split())
    if len(values) != 9:
        return None
    return (
        (values[0], values[1], values[2]),
        (values[3], values[4], values[5]),
        (values[6], values[7], values[8]),
    )


def _parse_chunk_transform(chunk: Optional[ET.Element]) -> Optional[Dict[str, object]]:
    if chunk is None:
        return None
    transform = chunk.find("transform")
    if transform is None:
        return None
    rotation = _matrix3_from_text(_text(transform, "rotation"))
    translation = _tuple3_from_text(_text(transform, "translation"))
    scale = _float(transform, "scale")
    if rotation is None and translation is None and scale is None:
        return None
    if rotation is None:
        rotation = (
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        )
    if translation is None:
        translation = (0.0, 0.0, 0.0)
    if scale is None:
        scale = 1.0
    return {
        "rotation": rotation,
        "translation": translation,
        "scale": scale,
    }


def _orthonormal_error(transform: Sequence[float]) -> float:
    if len(transform) != 16:
        return math.inf
    rows = (
        (transform[0], transform[1], transform[2]),
        (transform[4], transform[5], transform[6]),
        (transform[8], transform[9], transform[10]),
    )
    max_error = 0.0
    for i in range(3):
        for j in range(3):
            dot = sum(rows[i][k] * rows[j][k] for k in range(3))
            max_error = max(max_error, abs(dot - (1.0 if i == j else 0.0)))
    return max_error


def face_from_suffix(suffix: str) -> str:
    if suffix not in SUFFIX_TO_FACE:
        raise ValidationError(f"Unknown cubeface suffix: {suffix}")
    return SUFFIX_TO_FACE[suffix]


def face_basis_source_from_face(face: str) -> Tuple[Tuple[float, float, float], ...]:
    if face not in FACE_BASIS_SOURCE_FROM_FACE:
        raise ValidationError(f"Unknown internal cubeface tag: {face}")
    return FACE_BASIS_SOURCE_FROM_FACE[face]


def apply_face_basis(face: str, ray_face: Sequence[float]) -> Tuple[float, float, float]:
    """Transform a face-camera ray into the converter's fisheye ray frame."""
    if len(ray_face) != 3:
        raise ValidationError("Face ray must contain exactly three values")
    basis = face_basis_source_from_face(face)
    return tuple(
        ray_face[0] * basis[0][axis] + ray_face[1] * basis[1][axis] + ray_face[2] * basis[2][axis]
        for axis in range(3)
    )


def project_ray_to_internal_face(ray: Sequence[float], face: str) -> Tuple[float, float]:
    """Project a fisheye-frame ray with the original script's face equations."""
    if len(ray) != 3:
        raise ValidationError("Ray must contain exactly three values")
    x, y, z = (float(ray[0]), float(ray[1]), float(ray[2]))
    if face == "+Z":
        return x / z, y / z
    if face == "+X":
        return -z / x, y / x
    if face == "-X":
        return z / -x, y / -x
    if face == "+Y":
        return x / y, -z / y
    if face == "-Y":
        return x / -y, z / -y
    raise ValidationError(f"Unknown internal cubeface tag: {face}")


def project_face_camera_ray_to_pixels(
    face: str,
    ray_face: Sequence[float],
    facewidth: int,
) -> Tuple[float, float]:
    ray_fisheye = apply_face_basis(face, ray_face)
    u, v = project_ray_to_internal_face(ray_fisheye, face)
    center = facewidth / 2.0
    focal = facewidth / 2.0
    return center + focal * u, center + focal * v


def face_basis_determinant(face: str) -> float:
    c0, c1, c2 = face_basis_source_from_face(face)
    matrix = (
        (c0[0], c1[0], c2[0]),
        (c0[1], c1[1], c2[1]),
        (c0[2], c1[2], c2[2]),
    )
    return (
        matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
    )


def face_geometry_summary(facewidth: int = 2100) -> Dict[str, object]:
    return {
        suffix: {
            "internal_face": face,
            "basis_source_from_face": face_basis_source_from_face(face),
            "determinant": face_basis_determinant(face),
            "center_pixel": project_face_camera_ray_to_pixels(face, (0.0, 0.0, 1.0), facewidth),
            "right_pixel": project_face_camera_ray_to_pixels(face, (0.1, 0.0, 1.0), facewidth),
            "down_pixel": project_face_camera_ray_to_pixels(face, (0.0, 0.1, 1.0), facewidth),
        }
        for face, suffix in FACE_FILENAME_SUFFIX.items()
    }


def _transpose3(matrix: Sequence[Sequence[float]]) -> Tuple[Tuple[float, float, float], ...]:
    return tuple(tuple(float(matrix[j][i]) for j in range(3)) for i in range(3))


def _matmul3(
    left: Sequence[Sequence[float]],
    right: Sequence[Sequence[float]],
) -> Tuple[Tuple[float, float, float], ...]:
    return tuple(
        tuple(sum(float(left[i][k]) * float(right[k][j]) for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _matvec3(
    matrix: Sequence[Sequence[float]],
    vector: Sequence[float],
) -> Tuple[float, float, float]:
    return tuple(
        sum(float(matrix[i][k]) * float(vector[k]) for k in range(3))
        for i in range(3)
    )


def _neg3(vector: Sequence[float]) -> Tuple[float, float, float]:
    return (-float(vector[0]), -float(vector[1]), -float(vector[2]))


PointTransform = Callable[[Tuple[float, float, float]], Tuple[float, float, float]]


def _sub3(left: Sequence[float], right: Sequence[float]) -> Tuple[float, float, float]:
    return (
        float(left[0]) - float(right[0]),
        float(left[1]) - float(right[1]),
        float(left[2]) - float(right[2]),
    )


def _distance3(left: Sequence[float], right: Sequence[float]) -> float:
    delta = _sub3(left, right)
    return math.sqrt(delta[0] * delta[0] + delta[1] * delta[1] + delta[2] * delta[2])


def _finite_point3(point: Sequence[float], *, label: str = "point") -> Tuple[float, float, float]:
    values = (float(point[0]), float(point[1]), float(point[2]))
    if not all(math.isfinite(value) for value in values):
        raise ValidationError(f"{label} contains non-finite coordinates: {point}")
    return values


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _percentile(values: Sequence[float], percent: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (float(percent) / 100.0)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _coordinate_median(points: Sequence[Sequence[float]]) -> Optional[Tuple[float, float, float]]:
    if not points:
        return None
    medians = tuple(_median([float(point[axis]) for point in points]) for axis in range(3))
    if any(value is None for value in medians):
        return None
    return tuple(float(value) for value in medians)  # type: ignore[arg-type]


def _empty_bounds() -> Dict[str, Optional[List[float]]]:
    return {"min": None, "max": None}


def _update_bounds(bounds: Dict[str, Optional[List[float]]], point: Sequence[float]) -> None:
    values = _finite_point3(point)
    if bounds["min"] is None or bounds["max"] is None:
        bounds["min"] = [values[0], values[1], values[2]]
        bounds["max"] = [values[0], values[1], values[2]]
        return
    for axis in range(3):
        bounds["min"][axis] = min(float(bounds["min"][axis]), values[axis])
        bounds["max"][axis] = max(float(bounds["max"][axis]), values[axis])


def _bounds_diagonal(bounds: Mapping[str, object]) -> Optional[float]:
    min_values = bounds.get("min")
    max_values = bounds.get("max")
    if min_values is None or max_values is None:
        return None
    return _distance3(min_values, max_values)  # type: ignore[arg-type]


def _finalize_bounds(bounds: Dict[str, Optional[List[float]]]) -> Optional[Dict[str, object]]:
    if bounds["min"] is None or bounds["max"] is None:
        return None
    return {
        "min": list(bounds["min"]),
        "max": list(bounds["max"]),
        "diagonal": _bounds_diagonal(bounds),
    }


def _safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or abs(float(denominator)) <= SCENE_SCALE_EPSILON:
        return None
    return float(numerator) / float(denominator)


def _compose_point_transforms(
    first: Optional[PointTransform],
    second: Optional[PointTransform],
) -> Optional[PointTransform]:
    if first is None:
        return second
    if second is None:
        return first

    def transform(point: Tuple[float, float, float]) -> Tuple[float, float, float]:
        return second(first(point))

    return transform


def wgs84_lonlat_height_to_ecef(
    longitude_deg: float,
    latitude_deg: float,
    height_m: float,
) -> Tuple[float, float, float]:
    """Convert WGS84 lon/lat/ellipsoidal height to ECEF meters."""
    semi_major = 6378137.0
    flattening = 1.0 / 298.257223563
    eccentricity_sq = flattening * (2.0 - flattening)
    lon = math.radians(float(longitude_deg))
    lat = math.radians(float(latitude_deg))
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    normal = semi_major / math.sqrt(1.0 - eccentricity_sq * sin_lat * sin_lat)
    x = (normal + float(height_m)) * cos_lat * math.cos(lon)
    y = (normal + float(height_m)) * cos_lat * math.sin(lon)
    z = (normal * (1.0 - eccentricity_sq) + float(height_m)) * sin_lat
    return x, y, z


def _is_wgs84_geographic_reference(reference: object) -> bool:
    text = str(reference or "").upper()
    return "GEOGCS" in text and ("WGS 84" in text or 'EPSG","4326' in text)


def point_transform_from_document(document: Mapping[str, object]) -> Optional[PointTransform]:
    """Return a PLY point transform into Metashape local coordinates when needed."""
    if not _is_wgs84_geographic_reference(document.get("chunk_reference")):
        return None
    chunk_transform = document.get("chunk_transform")
    if not isinstance(chunk_transform, Mapping):
        return None
    rotation = chunk_transform["rotation"]  # type: ignore[index]
    translation = tuple(float(value) for value in chunk_transform["translation"])  # type: ignore[index]
    scale = float(chunk_transform["scale"])  # type: ignore[index]
    if abs(scale) <= 1e-15:
        raise ValidationError("Metashape chunk transform scale is zero")
    rotation_t = _transpose3(rotation)  # type: ignore[arg-type]

    def transform(point: Tuple[float, float, float]) -> Tuple[float, float, float]:
        ecef = wgs84_lonlat_height_to_ecef(point[0], point[1], point[2])
        shifted = tuple(ecef[i] - translation[i] for i in range(3))
        local_scaled = _matvec3(rotation_t, shifted)
        return tuple(value / scale for value in local_scaled)

    return transform


def _is_local_reference(reference: object) -> bool:
    text = str(reference or "").upper()
    return "LOCAL_CS" in text or "LOCAL COORDINATES" in text


def camera_world_transform_from_document(document: Mapping[str, object]) -> Optional[Mapping[str, object]]:
    """Return the Metashape internal-camera to exported-local-PLY transform.

    Metashape XML camera transforms are stored in the chunk's internal frame.
    A locally exported sparse cloud can be written in the chunk/display frame
    when the XML has a local chunk transform. In that case the cameras need
    the same chunk transform so COLMAP cameras and points share one frame.
    Geographic WGS84 PLY exports take the opposite path: points are converted
    back into the internal frame and cameras stay untouched.
    """
    if _is_wgs84_geographic_reference(document.get("chunk_reference")):
        return None
    if not _is_local_reference(document.get("chunk_reference")):
        return None
    chunk_transform = document.get("chunk_transform")
    if not isinstance(chunk_transform, Mapping):
        return None
    scale = float(chunk_transform["scale"])  # type: ignore[index]
    if abs(scale) <= 1e-15:
        raise ValidationError("Metashape chunk transform scale is zero")
    rotation = chunk_transform["rotation"]  # type: ignore[index]
    translation = tuple(float(value) for value in chunk_transform["translation"])  # type: ignore[index]
    identity = (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    is_identity_rotation = all(
        abs(float(rotation[row][column]) - identity[row][column]) <= 1e-12  # type: ignore[index]
        for row in range(3)
        for column in range(3)
    )
    is_identity_translation = all(abs(value) <= 1e-12 for value in translation)
    if is_identity_rotation and is_identity_translation and abs(scale - 1.0) <= 1e-12:
        return None
    return {
        "rotation": rotation,
        "translation": translation,
        "scale": scale,
        "description": "Metashape local chunk transform -> exported PLY coordinate frame",
    }


def _apply_camera_world_transform(
    r_world_from_camera: Sequence[Sequence[float]],
    camera_center_world: Sequence[float],
    camera_world_transform: Optional[Mapping[str, object]],
) -> Tuple[Tuple[Tuple[float, float, float], ...], Tuple[float, float, float]]:
    if camera_world_transform is None:
        return tuple(tuple(float(value) for value in row) for row in r_world_from_camera), (
            float(camera_center_world[0]),
            float(camera_center_world[1]),
            float(camera_center_world[2]),
        )
    rotation = camera_world_transform["rotation"]  # type: ignore[index]
    translation = tuple(float(value) for value in camera_world_transform["translation"])  # type: ignore[index]
    scale = float(camera_world_transform["scale"])  # type: ignore[index]
    r_transformed = _matmul3(rotation, r_world_from_camera)  # type: ignore[arg-type]
    rotated_center = _matvec3(rotation, camera_center_world)  # type: ignore[arg-type]
    center_transformed = tuple(scale * rotated_center[index] + translation[index] for index in range(3))
    return r_transformed, center_transformed


def _apply_point_transform(
    point: Tuple[float, float, float],
    point_transform: Optional[PointTransform],
) -> Tuple[float, float, float]:
    return point_transform(point) if point_transform is not None else point


def _basis_columns_to_matrix(columns: Sequence[Sequence[float]]) -> Tuple[Tuple[float, float, float], ...]:
    return (
        (float(columns[0][0]), float(columns[1][0]), float(columns[2][0])),
        (float(columns[0][1]), float(columns[1][1]), float(columns[2][1])),
        (float(columns[0][2]), float(columns[1][2]), float(columns[2][2])),
    )


def _camera_transform_parts(camera: Mapping[str, object]) -> Tuple[Tuple[Tuple[float, float, float], ...], Tuple[float, float, float]]:
    transform = tuple(float(value) for value in camera["transform"])  # type: ignore[index]
    if len(transform) != 16:
        raise ValidationError(f"Camera {camera.get('id', '?')} does not have a 4x4 transform")
    rotation = (
        (transform[0], transform[1], transform[2]),
        (transform[4], transform[5], transform[6]),
        (transform[8], transform[9], transform[10]),
    )
    translation = (transform[3], transform[7], transform[11])
    return rotation, translation


def _camera_world_from_xml(
    camera: Mapping[str, object],
    convention: str,
    camera_world_transform: Optional[Mapping[str, object]] = None,
) -> Tuple[Tuple[Tuple[float, float, float], ...], Tuple[float, float, float]]:
    """Return camera-to-world rotation and camera center for a Metashape camera."""
    r_xml, t_xml = _camera_transform_parts(camera)
    if convention == "metashape_camera_to_world":
        return _apply_camera_world_transform(r_xml, t_xml, camera_world_transform)
    if convention == "metashape_world_to_camera":
        r_world_from_camera = _transpose3(r_xml)
        camera_center_world = _matvec3(r_world_from_camera, _neg3(t_xml))
        return _apply_camera_world_transform(r_world_from_camera, camera_center_world, camera_world_transform)
    raise ValidationError(f"Unknown pose convention: {convention}")


def passthrough_world_to_camera_pose(
    camera: Mapping[str, object],
    convention: str,
    camera_world_transform: Optional[Mapping[str, object]] = None,
) -> Tuple[Tuple[Tuple[float, float, float], ...], Tuple[float, float, float]]:
    """Return COLMAP-style world-to-camera pose for a non-fisheye camera."""
    r_world_from_camera, camera_center_world = _camera_world_from_xml(camera, convention, camera_world_transform)
    r_camera_from_world = _transpose3(r_world_from_camera)
    t_camera_from_world = _matvec3(r_camera_from_world, _neg3(camera_center_world))
    return r_camera_from_world, t_camera_from_world


def face_world_to_camera_pose(
    camera: Mapping[str, object],
    internal_face: str,
    convention: str,
    camera_world_transform: Optional[Mapping[str, object]] = None,
) -> Tuple[Tuple[Tuple[float, float, float], ...], Tuple[float, float, float]]:
    """Return COLMAP-style world-to-face-camera pose for a cubeface.

    Supported conventions describe the meaning of Metashape's XML transform.
    The selected convention is intentionally validated statistically before real
    exports use it.
    """
    r_world_from_source, camera_center_world = _camera_world_from_xml(camera, convention, camera_world_transform)
    r_source_from_face = _basis_columns_to_matrix(face_basis_source_from_face(internal_face))
    r_world_from_face = _matmul3(r_world_from_source, r_source_from_face)
    r_face_from_world = _transpose3(r_world_from_face)
    t_face_from_world = _matvec3(r_face_from_world, _neg3(camera_center_world))
    return r_face_from_world, t_face_from_world


def rotation_matrix_to_colmap_qvec(
    rotation: Sequence[Sequence[float]],
) -> Tuple[float, float, float, float]:
    r00, r01, r02 = rotation[0]
    r10, r11, r12 = rotation[1]
    r20, r21, r22 = rotation[2]
    trace = r00 + r11 + r22
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * s
        qx = (r21 - r12) / s
        qy = (r02 - r20) / s
        qz = (r10 - r01) / s
    elif r00 > r11 and r00 > r22:
        s = math.sqrt(1.0 + r00 - r11 - r22) * 2.0
        qw = (r21 - r12) / s
        qx = 0.25 * s
        qy = (r01 + r10) / s
        qz = (r02 + r20) / s
    elif r11 > r22:
        s = math.sqrt(1.0 + r11 - r00 - r22) * 2.0
        qw = (r02 - r20) / s
        qx = (r01 + r10) / s
        qy = 0.25 * s
        qz = (r12 + r21) / s
    else:
        s = math.sqrt(1.0 + r22 - r00 - r11) * 2.0
        qw = (r10 - r01) / s
        qx = (r02 + r20) / s
        qy = (r12 + r21) / s
        qz = 0.25 * s
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    qvec = (qw / norm, qx / norm, qy / norm, qz / norm)
    if qvec[0] < 0:
        qvec = tuple(-value for value in qvec)
    return qvec  # type: ignore[return-value]


def classify_sensor(sensor: Mapping[str, object]) -> str:
    """Return the export path for a Metashape sensor.

    The exporter is mixed-scene oriented: fisheye sensors use the cubeface
    expansion path, while ordinary rectilinear sensors are passthrough images.
    """
    sensor_type = str(sensor.get("sensor_type", "")).lower()
    calibration_type = str(sensor.get("calibration_type", "")).lower()
    combined = f"{sensor_type} {calibration_type}"
    if "equisolid" in combined and "fisheye" in combined:
        return "equisolid_cubeface"
    if sensor_type in {"frame", "pinhole"} or calibration_type in {"frame", "pinhole"}:
        return "passthrough_pinhole"
    return "unsupported"


def parse_metashape_cameras_xml(path: Path) -> Dict[str, object]:
    root = ET.parse(path).getroot()
    chunk = root.find(".//chunk")
    chunk_reference = _text(chunk, "reference") if chunk is not None else None
    chunk_transform = _parse_chunk_transform(chunk)
    sensors: Dict[int, Dict[str, object]] = {}
    cameras: Dict[int, Dict[str, object]] = {}

    for sensor in root.findall(".//sensor"):
        sensor_id = int(sensor.attrib["id"])
        calibration = sensor.find("calibration")
        if calibration is None:
            continue
        resolution = calibration.find("resolution")
        if resolution is None:
            resolution = sensor.find("resolution")
        if resolution is None:
            raise ValidationError(f"Sensor {sensor_id} has no resolution")
        params = {}
        for tag in ("f", "cx", "cy", "k1", "k2", "k3", "k4", "p1", "p2", "b1", "b2"):
            value = _float(calibration, tag)
            if value is not None:
                params[tag] = value
        sensor_record = {
            "id": sensor_id,
            "label": sensor.attrib.get("label", ""),
            "sensor_type": sensor.attrib.get("type", ""),
            "calibration_type": calibration.attrib.get("type", ""),
            "width": int(resolution.attrib["width"]),
            "height": int(resolution.attrib["height"]),
            "params": params,
        }
        sensor_record["export_path"] = classify_sensor(sensor_record)
        sensors[sensor_id] = sensor_record

    for camera in root.findall(".//camera"):
        camera_id = int(camera.attrib["id"])
        transform_text = _text(camera, "transform") or ""
        transform = tuple(float(part) for part in transform_text.split())
        component = camera.attrib.get("component_id")
        reference_elem = camera.find("reference")
        reference = None
        if reference_elem is not None:
            try:
                reference = (
                    float(reference_elem.attrib["x"]),
                    float(reference_elem.attrib["y"]),
                    float(reference_elem.attrib["z"]),
                )
            except KeyError:
                reference = None
        cameras[camera_id] = {
            "id": camera_id,
            "label": camera.attrib.get("label", ""),
            "sensor_id": int(camera.attrib.get("sensor_id", "-1")),
            "component_id": int(component) if component is not None else None,
            "transform": transform,
            "center": (transform[3], transform[7], transform[11]) if len(transform) == 16 else None,
            "reference": reference,
            "rotation_orthonormal_error": _orthonormal_error(transform),
        }

    labels_to_ids: Dict[str, List[int]] = defaultdict(list)
    for camera_id, camera in cameras.items():
        labels_to_ids[str(camera["label"])].append(camera_id)
    labels_to_ids = {label: sorted(ids) for label, ids in labels_to_ids.items()}
    duplicate_labels = {label: ids for label, ids in labels_to_ids.items() if len(ids) > 1}

    return {
        "path": str(path),
        "version": root.attrib.get("version", ""),
        "chunk_label": chunk.attrib.get("label", "") if chunk is not None else "",
        "chunk_reference": chunk_reference,
        "chunk_transform": chunk_transform,
        "sensors": sensors,
        "cameras": cameras,
        "labels_to_ids": labels_to_ids,
        "duplicate_labels": duplicate_labels,
    }


def parse_ply_summary(path: Path) -> Dict[str, object]:
    with path.open("rb") as handle:
        header_lines: List[str] = []
        while True:
            raw = handle.readline()
            if not raw:
                raise ValidationError("PLY header ended before end_header")
            line = raw.decode("ascii").rstrip("\r\n")
            header_lines.append(line)
            if line == "end_header":
                break

        if not header_lines or header_lines[0] != "ply":
            raise ValidationError("Not a PLY file")

        ply_format = ""
        vertex_count = None
        properties: List[Tuple[str, str]] = []
        in_vertex = False
        for line in header_lines[1:]:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "format":
                ply_format = parts[1]
            elif parts[0] == "element":
                in_vertex = parts[1] == "vertex"
                if in_vertex:
                    vertex_count = int(parts[2])
            elif parts[0] == "property" and in_vertex:
                if parts[1] == "list":
                    raise ValidationError("List properties on vertex elements are not supported")
                properties.append((parts[2], parts[1]))

        if ply_format != "binary_little_endian":
            raise ValidationError(f"Unsupported PLY format: {ply_format}")
        if vertex_count is None:
            raise ValidationError("PLY has no vertex element")

        try:
            row_format = "<" + "".join(PLY_TYPE_FORMATS[prop_type][0] for _, prop_type in properties)
            record_size = sum(PLY_TYPE_FORMATS[prop_type][1] for _, prop_type in properties)
        except KeyError as exc:
            raise ValidationError(f"Unsupported PLY property type: {exc.args[0]}") from exc

        names = [name for name, _ in properties]
        if not {"x", "y", "z"}.issubset(names):
            raise ValidationError("PLY vertices must include x, y, z")
        indexes = {name: names.index(name) for name in names}
        has_rgb = {"red", "green", "blue"}.issubset(names)

        unpack = struct.Struct(row_format).unpack_from
        bounds_min = [math.inf, math.inf, math.inf]
        bounds_max = [-math.inf, -math.inf, -math.inf]
        rgb_total = [0.0, 0.0, 0.0]
        for index in range(vertex_count):
            data = handle.read(record_size)
            if len(data) != record_size:
                raise ValidationError(f"PLY ended inside vertex record {index}")
            row = unpack(data)
            xyz = (float(row[indexes["x"]]), float(row[indexes["y"]]), float(row[indexes["z"]]))
            for axis, value in enumerate(xyz):
                bounds_min[axis] = min(bounds_min[axis], value)
                bounds_max[axis] = max(bounds_max[axis], value)
            if has_rgb:
                rgb_total[0] += float(row[indexes["red"]])
                rgb_total[1] += float(row[indexes["green"]])
                rgb_total[2] += float(row[indexes["blue"]])

    return {
        "path": str(path),
        "format": ply_format,
        "vertex_count": vertex_count,
        "properties": tuple(properties),
        "record_size": record_size,
        "bounds_min": tuple(bounds_min),
        "bounds_max": tuple(bounds_max),
        "mean_rgb": tuple(value / vertex_count for value in rgb_total) if has_rgb and vertex_count else None,
    }


def _read_ply_vertex_layout(handle) -> Dict[str, object]:
    header_lines: List[str] = []
    while True:
        raw = handle.readline()
        if not raw:
            raise ValidationError("PLY header ended before end_header")
        line = raw.decode("ascii").rstrip("\r\n")
        header_lines.append(line)
        if line == "end_header":
            break

    if not header_lines or header_lines[0] != "ply":
        raise ValidationError("Not a PLY file")

    ply_format = ""
    vertex_count = None
    properties: List[Tuple[str, str]] = []
    in_vertex = False
    for line in header_lines[1:]:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "format":
            ply_format = parts[1]
        elif parts[0] == "element":
            in_vertex = parts[1] == "vertex"
            if in_vertex:
                vertex_count = int(parts[2])
        elif parts[0] == "property" and in_vertex:
            if parts[1] == "list":
                raise ValidationError("List properties on vertex elements are not supported")
            properties.append((parts[2], parts[1]))

    if ply_format != "binary_little_endian":
        raise ValidationError(f"Unsupported PLY format: {ply_format}")
    if vertex_count is None:
        raise ValidationError("PLY has no vertex element")

    try:
        row_format = "<" + "".join(PLY_TYPE_FORMATS[prop_type][0] for _, prop_type in properties)
        record_size = sum(PLY_TYPE_FORMATS[prop_type][1] for _, prop_type in properties)
    except KeyError as exc:
        raise ValidationError(f"Unsupported PLY property type: {exc.args[0]}") from exc

    names = [name for name, _ in properties]
    if not {"x", "y", "z"}.issubset(names):
        raise ValidationError("PLY vertices must include x, y, z")
    return {
        "vertex_count": vertex_count,
        "properties": tuple(properties),
        "record_size": record_size,
        "struct": struct.Struct(row_format),
        "indexes": {name: names.index(name) for name in names},
    }


def iter_ply_points(path: Optional[Path]):
    """Yield `(x, y, z, r, g, b)` from the Metashape sparse PLY.

    Returns no points when ``path`` is None — the exporter supports manifests
    without a sparse_ply (e.g. ERP-only exports prior to alignment).
    """
    if path is None:
        return
    with path.open("rb") as handle:
        layout = _read_ply_vertex_layout(handle)
        indexes = layout["indexes"]
        unpack = layout["struct"].unpack_from
        record_size = int(layout["record_size"])
        has_rgb = {"red", "green", "blue"}.issubset(indexes)
        for index in range(int(layout["vertex_count"])):
            data = handle.read(record_size)
            if len(data) != record_size:
                raise ValidationError(f"PLY ended inside vertex record {index}")
            row = unpack(data)
            x = float(row[indexes["x"]])
            y = float(row[indexes["y"]])
            z = float(row[indexes["z"]])
            if has_rgb:
                r = int(row[indexes["red"]])
                g = int(row[indexes["green"]])
                b = int(row[indexes["blue"]])
            else:
                r = g = b = 255
            yield x, y, z, r, g, b


def read_image_size(path: Path) -> Tuple[int, int]:
    with path.open("rb") as handle:
        head = handle.read(32)
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return struct.unpack(">II", head[16:24])
        if head[:2] == b"\xff\xd8":
            handle.seek(2)
            while True:
                marker_start = handle.read(1)
                if not marker_start:
                    break
                if marker_start != b"\xff":
                    continue
                marker = handle.read(1)
                while marker == b"\xff":
                    marker = handle.read(1)
                if marker in (b"\xd8", b"\xd9"):
                    continue
                length_raw = handle.read(2)
                if len(length_raw) != 2:
                    break
                length = struct.unpack(">H", length_raw)[0]
                if marker and marker[0] in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    payload = handle.read(5)
                    if len(payload) != 5:
                        break
                    height, width = struct.unpack(">HH", payload[1:5])
                    return width, height
                handle.seek(length - 2, 1)
    raise ValidationError(f"Unsupported image format or unreadable dimensions: {path}")


def parse_cubeface_filename(path: Path) -> Optional[Tuple[str, str, str]]:
    match = CUBEFACE_RE.match(path.name)
    if not match:
        return None
    suffix = match.group("suffix")
    if suffix not in SUFFIX_TO_FACE:
        return None
    return match.group("stem"), suffix, path.suffix.lower()


def parse_run_report(path: Path) -> Dict[str, object]:
    result: Dict[str, object] = {
        "path": str(path),
        "lens": None,
        "support_source": None,
        "max_angle_deg": None,
        "max_angle_source": None,
        "processed": None,
        "skipped": None,
        "wall_clock_s": None,
        "projection": None,
        "width": None,
        "height": None,
        "focal": None,
        "cx": None,
        "cy": None,
        "distortion_zero": False,
    }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("Lens:"):
            result["lens"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Support source:"):
            result["support_source"] = stripped.split(":", 1)[1].strip()
        elif "maximum angle:" in stripped:
            result["max_angle_deg"] = float(stripped.rsplit(":", 1)[1].strip().split()[0])
            result["max_angle_source"] = "mask-derived" if stripped.startswith("Mask-derived") else "manual"
        elif stripped.startswith("Processed:"):
            result["processed"] = int(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("Skipped:"):
            result["skipped"] = int(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("Wall clock:"):
            result["wall_clock_s"] = float(stripped.split(":", 1)[1].strip().rstrip("s"))
        elif "projection =" in stripped:
            result["projection"] = stripped.split("projection =", 1)[1].strip()
        elif "width" in stripped and "=" in stripped:
            match = re.search(r"width\s*=\s*(\d+)", stripped)
            if match:
                result["width"] = int(match.group(1))
        elif "height" in stripped and "=" in stripped:
            match = re.search(r"height\s*=\s*(\d+)", stripped)
            if match:
                result["height"] = int(match.group(1))
        elif re.search(r"\bf\s*=", stripped):
            match = re.search(r"\bf\s*=\s*([-+0-9.eE]+)", stripped)
            if match:
                result["focal"] = float(match.group(1))
        elif "cx" in stripped and "cy" in stripped:
            match = re.search(r"cx\s*=\s*([-+0-9.eE]+)\s+cy\s*=\s*([-+0-9.eE]+)", stripped)
            if match:
                result["cx"] = float(match.group(1))
                result["cy"] = float(match.group(2))
        elif "k1 = k2 = k3 = p1 = p2 = 0.0" in stripped:
            result["distortion_zero"] = True
    return result


def _image_files(root: Path) -> Iterable[Path]:
    return (
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def _direct_image_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def _is_mask_or_layer_path(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        parts = path.parts[:-1]
    return any("mask" in part.lower() or part.lower() == "layers" for part in parts)


def _classify_layout(images_dir: Path) -> str:
    child_dirs = [child.name for child in images_dir.iterdir() if child.is_dir()]
    face_dir_count = sum(1 for name in child_dirs if name in KNOWN_FACE_DIRS)
    if face_dir_count == 5:
        return "rig"
    if child_dirs and face_dir_count == 0:
        return "station"
    return "unknown"


def _validate_complete_faces(lens_label: str, images: Sequence[Dict[str, object]]) -> None:
    by_stem: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for image in images:
        by_stem[str(image["stem"])].append(image)
    expected = set(KNOWN_SUFFIXES)
    for stem, items in by_stem.items():
        suffixes = {str(item["suffix"]) for item in items}
        if suffixes != expected:
            raise ValidationError(
                f"Lens {lens_label} stem {stem} does not have exactly five faces; "
                f"missing={sorted(expected - suffixes)} extra={sorted(suffixes - expected)}"
            )


def _cubeface_lens_label(root: Path, lens_dir: Path) -> str:
    try:
        return lens_dir.relative_to(root).as_posix()
    except ValueError:
        return lens_dir.name


def _load_source_image_map(lens_dir: Path) -> Dict[str, Dict[str, object]]:
    path = lens_dir / SOURCE_IMAGE_MAP_FILENAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Could not read {path}: {exc}") from exc
    entries = raw.get("stems", []) if isinstance(raw, Mapping) else []
    source_map: Dict[str, Dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        output_stem = str(entry.get("output_stem", "")).strip()
        if output_stem:
            source_map[output_stem] = dict(entry)
    return source_map


def discover_cubefaces(root: Path) -> Dict[str, object]:
    if not root.is_dir():
        raise ValidationError(f"Cubeface root is not a directory: {root}")
    lenses = []
    # Skip anything under non-cubeface projection trees. ERP and adaptive
    # outputs are handled by dedicated map paths and use plain stem filenames
    # that would fail cubeface validation.
    def _is_non_cubeface_path(path: Path) -> bool:
        try:
            rel = path.relative_to(root)
        except ValueError:
            return False
        return any(
            part.startswith("erp_sensor_") or part.startswith("adaptive_sensor_")
            for part in rel.parts
        )

    lens_dirs = sorted(
        path for path in root.rglob("*")
        if path.is_dir() and (path / "images").is_dir() and not _is_non_cubeface_path(path)
    )
    if (root / "images").is_dir():
        lens_dirs.insert(0, root)
    for lens_dir in lens_dirs:
        images_dir = lens_dir / "images"
        layout = _classify_layout(images_dir)
        if layout == "unknown":
            raise ValidationError(f"Cannot classify cubeface layout under {images_dir}")
        lens_label = _cubeface_lens_label(root, lens_dir)
        masks_dir = lens_dir / "masks"
        source_image_map = _load_source_image_map(lens_dir)
        images = []
        seen = set()
        for image_path in sorted(_image_files(images_dir)):
            parsed = parse_cubeface_filename(image_path)
            if parsed is None:
                continue
            stem, suffix, ext = parsed
            key = (stem, suffix)
            if key in seen:
                raise ValidationError(f"Duplicate cubeface image for {lens_label} {stem} {suffix}")
            seen.add(key)
            width, height = read_image_size(image_path)
            mask_path = masks_dir / f"{stem}{suffix}.png"
            if not mask_path.is_file():
                mask_path = masks_dir / f"{stem}{suffix}_mask.png"
            image_name = _relative_colmap_image_name(image_path, root)
            source_entry = source_image_map.get(stem, {})
            image_record = {
                "lens_label": lens_label,
                "source_lens_label": lens_dir.name,
                "layout": layout,
                "stem": stem,
                "suffix": suffix,
                "internal_face": SUFFIX_TO_FACE[suffix],
                "filename_face_dir": suffix.lstrip("_"),
                "extension": ext,
                "image_path": str(image_path),
                "image_name": image_name,
                "mask_path": str(mask_path) if mask_path.is_file() else None,
                "width": width,
                "height": height,
            }
            if source_entry:
                image_record["source_stem"] = source_entry.get("source_stem")
                if source_entry.get("camera_id") is not None:
                    image_record["source_camera_id"] = int(source_entry["camera_id"])
                if source_entry.get("camera_label") is not None:
                    image_record["source_camera_label"] = str(source_entry["camera_label"])
            images.append(image_record)
        if not images:
            continue
        _validate_complete_faces(lens_label, images)
        report_path = lens_dir / "run_report.txt"
        report = parse_run_report(report_path) if report_path.is_file() else None
        stems = tuple(sorted({str(image["stem"]) for image in images}))
        stem_camera_ids = {
            stem: int(entry["camera_id"])
            for stem, entry in source_image_map.items()
            if entry.get("camera_id") is not None
        }
        face_size_set = tuple(sorted({(int(image["width"]), int(image["height"])) for image in images}))
        lenses.append({
            "lens_label": lens_label,
            "source_lens_label": lens_dir.name,
            "path": str(lens_dir),
            "layout": layout,
            "images": tuple(images),
            "image_count": len(images),
            "mask_count": sum(1 for image in images if image["mask_path"] is not None),
            "stems": stems,
            "stem_camera_ids": stem_camera_ids,
            "face_size_set": face_size_set,
            "suffix_counts": dict(sorted(Counter(str(image["suffix"]) for image in images).items())),
            "run_report": report,
        })
    if not lenses:
        raise ValidationError(f"No cubeface outputs found under {root}")
    return {
        "root": str(root),
        "lenses": tuple(lenses),
        "lens_count": len(lenses),
        "image_count": sum(int(lens["image_count"]) for lens in lenses),
        "layout_counts": dict(sorted(Counter(str(lens["layout"]) for lens in lenses).items())),
    }


def empty_cubeface_discovery(root: Optional[Path] = None) -> Dict[str, object]:
    return {
        "root": str(root) if root is not None else "",
        "lenses": tuple(),
        "lens_count": 0,
        "image_count": 0,
        "layout_counts": {},
    }


def discover_passthrough_images(roots: Sequence[Path]) -> Dict[str, object]:
    normalized_roots = tuple(Path(root) for root in roots)
    image_records = []
    by_stem: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for root in normalized_roots:
        if not root.is_dir():
            raise ValidationError(f"Passthrough image root is not a directory: {root}")
        for image_path in sorted(_image_files(root)):
            if _is_mask_or_layer_path(image_path, root):
                continue
            width, height = read_image_size(image_path)
            image_name = _relative_to_any_root(image_path, normalized_roots)
            record = {
                "stem": image_path.stem,
                "image_path": str(image_path),
                "image_name": image_name,
                "root": str(root),
                "width": width,
                "height": height,
            }
            image_records.append(record)
            by_stem[image_path.stem].append(record)

    duplicate_stems = {
        stem: tuple(item["image_path"] for item in records)
        for stem, records in sorted(by_stem.items())
        if len(records) > 1
    }
    return {
        "roots": tuple(str(root) for root in normalized_roots),
        "images": tuple(image_records),
        "image_count": len(image_records),
        "unique_stem_count": len(by_stem),
        "duplicate_stems": duplicate_stems,
        "by_stem": {stem: tuple(records) for stem, records in by_stem.items()},
    }


def _passthrough_sensor_ids(
    document: Mapping[str, object],
    requested_sensor_ids: Optional[Sequence[int]] = None,
) -> Tuple[int, ...]:
    sensors: Mapping[int, Mapping[str, object]] = document["sensors"]  # type: ignore[assignment]
    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    camera_counts_by_sensor = Counter(int(camera["sensor_id"]) for camera in cameras.values())
    if requested_sensor_ids is not None:
        ids = tuple(sorted(int(sensor_id) for sensor_id in requested_sensor_ids))
    else:
        ids = tuple(
            sorted(
                int(sensor_id)
                for sensor_id, sensor in sensors.items()
                if str(sensor.get("export_path")) == "passthrough_pinhole"
                and camera_counts_by_sensor.get(int(sensor_id), 0) > 0
            )
        )
    for sensor_id in ids:
        if sensor_id not in sensors:
            raise ValidationError(f"Requested passthrough sensor id does not exist: {sensor_id}")
        if str(sensors[sensor_id].get("export_path")) != "passthrough_pinhole":
            raise ValidationError(f"Sensor {sensor_id} is not a passthrough/frame sensor")
    return ids


def validate_passthrough_images(
    document: Mapping[str, object],
    discovery: Mapping[str, object],
    requested_sensor_ids: Optional[Sequence[int]] = None,
) -> Dict[str, object]:
    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    by_stem: Mapping[str, Sequence[Mapping[str, object]]] = discovery["by_stem"]  # type: ignore[assignment]
    sensor_ids = set(_passthrough_sensor_ids(document, requested_sensor_ids))
    resolutions = []
    missing = []
    duplicate_matches = []
    used_stems = set()
    skipped_unaligned = 0
    for camera_id, camera in sorted(cameras.items()):
        if int(camera["sensor_id"]) not in sensor_ids:
            continue
        if len(camera.get("transform", ())) != 16:
            skipped_unaligned += 1
            continue
        label = str(camera["label"])
        matches = tuple(by_stem.get(label, ()))
        if len(matches) == 0:
            missing.append({"camera_id": camera_id, "label": label})
            continue
        if len(matches) > 1:
            duplicate_matches.append({
                "camera_id": camera_id,
                "label": label,
                "paths": tuple(str(item["image_path"]) for item in matches),
            })
            continue
        image = matches[0]
        used_stems.add(label)
        resolutions.append({
            "camera_id": camera_id,
            "camera_label": label,
            "sensor_id": int(camera["sensor_id"]),
            "image_path": image["image_path"],
            "image_name": image["image_name"],
            "width": image["width"],
            "height": image["height"],
        })

    if missing:
        sample = ", ".join(f"{item['camera_id']}:{item['label']}" for item in missing[:10])
        raise ValidationError(f"Missing passthrough images for {len(missing)} cameras: {sample}")
    if duplicate_matches:
        sample = ", ".join(f"{item['camera_id']}:{item['label']}" for item in duplicate_matches[:10])
        raise ValidationError(f"Ambiguous passthrough image matches for {len(duplicate_matches)} cameras: {sample}")

    all_stems = set(str(stem) for stem in by_stem)
    return {
        "sensor_ids": tuple(sorted(sensor_ids)),
        "resolved_count": len(resolutions),
        "resolutions": tuple(resolutions),
        "extra_image_stems": tuple(sorted(all_stems - used_stems)),
    }


def _slugify(value: str, fallback: str = "media_set") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or fallback


def _with_unique_slugs(media_sets: Sequence[Mapping[str, object]]) -> Tuple[Dict[str, object], ...]:
    counts: Dict[str, int] = defaultdict(int)
    normalized = []
    for index, media_set in enumerate(media_sets):
        name = str(media_set.get("name") or f"Media Set {index + 1}").strip()
        base_slug = _slugify(name, f"media_set_{index + 1}")
        counts[base_slug] += 1
        slug = base_slug if counts[base_slug] == 1 else f"{base_slug}_{counts[base_slug]}"
        image_root = Path(str(media_set["image_root"]))
        mask_root = media_set.get("mask_root")
        normalized.append({
            "name": name,
            "slug": slug,
            "image_root": image_root,
            "mask_root": Path(str(mask_root)) if mask_root else None,
        })
    return tuple(normalized)


def load_passthrough_media_manifest(path: Path) -> Tuple[Dict[str, object], ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_media_sets = data.get("media_sets") if isinstance(data, dict) else None
    if not isinstance(raw_media_sets, list):
        raise ValidationError("Passthrough media manifest must contain a media_sets list")
    media_sets = []
    for index, item in enumerate(raw_media_sets, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"Media set {index} must be an object")
        image_root = item.get("image_root")
        if not image_root:
            raise ValidationError(f"Media set {index} is missing image_root")
        name = str(item.get("name") or Path(str(image_root)).name or f"Media Set {index}")
        media_sets.append({
            "name": name,
            "image_root": _resolve_manifest_path(path, str(image_root)),
            "mask_root": _resolve_manifest_path(path, str(item["mask_root"])) if item.get("mask_root") else None,
        })
    return _with_unique_slugs(media_sets)


def media_sets_from_passthrough_roots(roots: Sequence[Path]) -> Tuple[Dict[str, object], ...]:
    return _with_unique_slugs(
        tuple({
            "name": Path(root).name or f"passthrough_{index + 1}",
            "image_root": Path(root),
            "mask_root": None,
        } for index, root in enumerate(roots))
    )


def _resolve_manifest_path(manifest_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return manifest_path.parent / path


def _match_key(value: str) -> str:
    return value.replace("\\", "/").strip().lower()


def _drop_extension(value: str) -> str:
    slash = value.rfind("/")
    dot = value.rfind(".")
    if dot > slash:
        return value[:dot]
    return value


def _add_index_key(index: Dict[str, List[Dict[str, object]]], key: str, record: Dict[str, object]) -> None:
    if key:
        index[key].append(record)


def _index_media_file(record: Dict[str, object], index: Dict[str, List[Dict[str, object]]]) -> None:
    relative = _match_key(str(record["relative_path"]))
    filename = _match_key(Path(str(record["path"])).name)
    stem = _match_key(Path(str(record["path"])).stem)
    _add_index_key(index, f"rel:{relative}", record)
    _add_index_key(index, f"relstem:{_drop_extension(relative)}", record)
    _add_index_key(index, f"name:{filename}", record)
    _add_index_key(index, f"stem:{stem}", record)
    # Also index with _mask suffix stripped so "img_mask.png" matches image
    # stem "img".  Consistent with v4's split_mask_string convention.
    if stem.endswith("_mask"):
        _add_index_key(index, f"stem:{stem[:-5]}", record)
    relstem = _drop_extension(relative)
    if relstem.endswith("_mask"):
        _add_index_key(index, f"relstem:{relstem[:-5]}", record)


def _label_candidate_keys(label: str) -> Tuple[str, ...]:
    normalized = _match_key(label)
    filename = normalized.rsplit("/", 1)[-1]
    stem = _drop_extension(filename)
    keys = []
    if "/" in normalized:
        keys.append(f"rel:{normalized}")
        keys.append(f"relstem:{_drop_extension(normalized)}")
    if "." in filename:
        keys.append(f"name:{filename}")
    keys.append(f"stem:{stem}")
    return tuple(dict.fromkeys(keys))


def _resolve_index_match(
    index: Mapping[str, Sequence[Dict[str, object]]],
    candidate_keys: Sequence[str],
) -> Tuple[Optional[Dict[str, object]], Tuple[Dict[str, object], ...], Optional[str]]:
    for key in candidate_keys:
        matches = tuple(index.get(key, ()))
        if matches:
            return (matches[0] if len(matches) == 1 else None), matches, key
    return None, tuple(), None


def _mask_files(root: Path) -> Iterable[Path]:
    root_name = root.name.lower()
    mask_root_is_explicit = "mask" in root_name or root_name == "layers"
    for path in _image_files(root):
        if mask_root_is_explicit or _is_mask_or_layer_path(path, root):
            yield path


def discover_passthrough_media_sets(media_sets: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    normalized_sets = _with_unique_slugs(media_sets)
    image_index: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    mask_indexes: Dict[int, Dict[str, List[Dict[str, object]]]] = {}
    image_records = []
    mask_records = []
    by_stem: Dict[str, List[Dict[str, object]]] = defaultdict(list)

    for set_index, media_set in enumerate(normalized_sets):
        image_root = Path(str(media_set["image_root"]))
        if not image_root.is_dir():
            raise ValidationError(f"Passthrough media image folder is not a directory: {image_root}")
        mask_root = media_set.get("mask_root")
        if mask_root is not None and not Path(str(mask_root)).is_dir():
            raise ValidationError(f"Passthrough media mask folder is not a directory: {mask_root}")

        for image_path in sorted(_image_files(image_root)):
            if _is_mask_or_layer_path(image_path, image_root):
                continue
            width, height = read_image_size(image_path)
            relative = _relative_colmap_image_name(image_path, image_root)
            record = {
                "media_set_index": set_index,
                "media_set_name": media_set["name"],
                "media_set_slug": media_set["slug"],
                "path": str(image_path),
                "image_path": str(image_path),
                "relative_path": relative,
                "image_name": relative,
                "root": str(image_root),
                "stem": image_path.stem,
                "width": width,
                "height": height,
            }
            image_records.append(record)
            by_stem[image_path.stem].append(record)
            _index_media_file(record, image_index)

        set_mask_index: Dict[str, List[Dict[str, object]]] = defaultdict(list)
        if mask_root is not None:
            mask_root_path = Path(str(mask_root))
            for mask_path in sorted(_mask_files(mask_root_path)):
                width, height = read_image_size(mask_path)
                relative = _relative_colmap_image_name(mask_path, mask_root_path)
                record = {
                    "media_set_index": set_index,
                    "media_set_name": media_set["name"],
                    "media_set_slug": media_set["slug"],
                    "path": str(mask_path),
                    "mask_path": str(mask_path),
                    "relative_path": relative,
                    "root": str(mask_root_path),
                    "stem": mask_path.stem,
                    "width": width,
                    "height": height,
                }
                mask_records.append(record)
                _index_media_file(record, set_mask_index)
        mask_indexes[set_index] = set_mask_index

    duplicate_stems = {
        stem: tuple(item["image_path"] for item in records)
        for stem, records in sorted(by_stem.items())
        if len(records) > 1
    }
    return {
        "media_sets": normalized_sets,
        "roots": tuple(str(media_set["image_root"]) for media_set in normalized_sets),
        "images": tuple(image_records),
        "masks": tuple(mask_records),
        "image_count": len(image_records),
        "mask_count": len(mask_records),
        "unique_stem_count": len(by_stem),
        "duplicate_stems": duplicate_stems,
        "by_stem": {stem: tuple(records) for stem, records in by_stem.items()},
        "image_index": {key: tuple(value) for key, value in image_index.items()},
        "mask_indexes": {
            index: {key: tuple(value) for key, value in mask_index.items()}
            for index, mask_index in mask_indexes.items()
        },
    }


def _mask_candidate_keys_for_image(image_record: Mapping[str, object]) -> Tuple[str, ...]:
    relative = _match_key(str(image_record["relative_path"]))
    relstem = _drop_extension(relative)
    stem = _match_key(str(image_record["stem"]))
    candidates = [
        f"relstem:{relstem}",
    ]
    parts = relative.split("/")
    for index, part in enumerate(parts[:-1]):
        if part in {"frames", "images"}:
            replaced = list(parts)
            replaced[index] = "masks"
            candidates.append(f"relstem:{_drop_extension('/'.join(replaced))}")
    candidates.append(f"stem:{stem}")
    return tuple(dict.fromkeys(candidates))


def resolve_passthrough_media_sets(
    document: Mapping[str, object],
    media_sets: Sequence[Mapping[str, object]],
    requested_sensor_ids: Optional[Sequence[int]] = None,
    *,
    require_masks: bool = False,
) -> Dict[str, object]:
    discovery = discover_passthrough_media_sets(media_sets)
    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    image_index: Mapping[str, Sequence[Dict[str, object]]] = discovery["image_index"]  # type: ignore[assignment]
    mask_indexes: Mapping[int, Mapping[str, Sequence[Dict[str, object]]]] = discovery["mask_indexes"]  # type: ignore[assignment]
    sensor_ids = set(_passthrough_sensor_ids(document, requested_sensor_ids))
    resolutions = []
    missing = []
    duplicate_matches = []
    missing_masks = []
    duplicate_masks = []
    used_stems = set()

    skipped_unaligned = 0
    for camera_id, camera in sorted(cameras.items()):
        if int(camera["sensor_id"]) not in sensor_ids:
            continue
        if len(camera.get("transform", ())) != 16:
            skipped_unaligned += 1
            continue
        label = str(camera["label"])
        image, image_matches, image_key = _resolve_index_match(image_index, _label_candidate_keys(label))
        if not image_matches:
            missing.append({"camera_id": camera_id, "label": label})
            continue
        if image is None:
            duplicate_matches.append({
                "camera_id": camera_id,
                "label": label,
                "paths": tuple(str(item["image_path"]) for item in image_matches),
            })
            continue

        mask = None
        mask_key = None
        mask_matches: Tuple[Dict[str, object], ...] = tuple()
        mask_index = mask_indexes.get(int(image["media_set_index"]), {})
        if mask_index:
            mask, mask_matches, mask_key = _resolve_index_match(mask_index, _mask_candidate_keys_for_image(image))
            if mask is None and len(mask_matches) > 1:
                duplicate_masks.append({
                    "camera_id": camera_id,
                    "label": label,
                    "paths": tuple(str(item["mask_path"]) for item in mask_matches),
                })
                continue
        if mask is None and require_masks:
            missing_masks.append({"camera_id": camera_id, "label": label, "image_path": image["image_path"]})
            continue
        if mask is not None and (int(mask["width"]), int(mask["height"])) != (int(image["width"]), int(image["height"])):
            raise ValidationError(
                f"Mask dimensions do not match image for camera {camera_id}:{label}: "
                f"{mask['width']}x{mask['height']} vs {image['width']}x{image['height']}"
            )

        used_stems.add(str(image["stem"]))
        resolutions.append({
            "camera_id": camera_id,
            "camera_label": label,
            "sensor_id": int(camera["sensor_id"]),
            "image_path": image["image_path"],
            "image_name": image["image_name"],
            "image_relative_path": image["relative_path"],
            "mask_path": mask["mask_path"] if mask is not None else None,
            "mask_relative_path": mask["relative_path"] if mask is not None else None,
            "media_set_name": image["media_set_name"],
            "media_set_slug": image["media_set_slug"],
            "match_key": image_key,
            "mask_match_key": mask_key,
            "width": image["width"],
            "height": image["height"],
        })

    if missing:
        sample = ", ".join(f"{item['camera_id']}:{item['label']}" for item in missing[:10])
        raise ValidationError(f"Missing passthrough media images for {len(missing)} cameras: {sample}")
    if duplicate_matches:
        sample = ", ".join(f"{item['camera_id']}:{item['label']}" for item in duplicate_matches[:10])
        raise ValidationError(f"Ambiguous passthrough media image matches for {len(duplicate_matches)} cameras: {sample}")
    if missing_masks:
        sample = ", ".join(f"{item['camera_id']}:{item['label']}" for item in missing_masks[:10])
        raise ValidationError(f"Missing passthrough media masks for {len(missing_masks)} cameras: {sample}")
    if duplicate_masks:
        sample = ", ".join(f"{item['camera_id']}:{item['label']}" for item in duplicate_masks[:10])
        raise ValidationError(f"Ambiguous passthrough media mask matches for {len(duplicate_masks)} cameras: {sample}")

    all_stems = set(str(stem) for stem in discovery["by_stem"])  # type: ignore[index]
    media_counts = Counter(str(item["media_set_slug"]) for item in resolutions)
    mask_counts = Counter(str(item["media_set_slug"]) for item in resolutions if item["mask_path"])
    return {
        "sensor_ids": tuple(sorted(sensor_ids)),
        "resolved_count": len(resolutions),
        "mask_resolved_count": sum(1 for item in resolutions if item["mask_path"]),
        "resolutions": tuple(resolutions),
        "extra_image_stems": tuple(sorted(all_stems - used_stems)),
        "media_sets": discovery["media_sets"],
        "media_set_image_counts": dict(sorted(media_counts.items())),
        "media_set_mask_counts": dict(sorted(mask_counts.items())),
        "discovery": discovery,
    }


def metashape_camera_runs(
    document: Mapping[str, object],
    export_path: Optional[str] = None,
) -> Tuple[Dict[str, object], ...]:
    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    sensors: Mapping[int, Mapping[str, object]] = document["sensors"]  # type: ignore[assignment]
    runs = []
    current: List[Mapping[str, object]] = []
    current_sensor_id: Optional[int] = None
    current_prefix: Optional[str] = None
    last_number: Optional[int] = None

    def split_label(label: str) -> Tuple[str, Optional[int]]:
        match = re.match(r"^(?P<prefix>.*?)(?P<number>\d+)$", label)
        if not match:
            return label, None
        return match.group("prefix"), int(match.group("number"))

    def finish_run() -> None:
        if not current:
            return
        ids = tuple(int(camera["id"]) for camera in current)
        labels = tuple(str(camera["label"]) for camera in current)
        sensor_id = int(current[0]["sensor_id"])
        sensor = sensors.get(sensor_id)
        runs.append({
            "sensor_id": sensor_id,
            "export_path": sensor.get("export_path", "unsupported") if sensor is not None else "unsupported",
            "count": len(current),
            "camera_ids": ids,
            "start_camera_id": ids[0],
            "end_camera_id": ids[-1],
            "start_label": labels[0],
            "end_label": labels[-1],
        })

    for camera in cameras.values():
        sensor_id = int(camera["sensor_id"])
        sensor_export_path = str(sensors.get(sensor_id, {}).get("export_path", "unsupported"))
        if export_path is not None and sensor_export_path != export_path:
            continue
        prefix, number = split_label(str(camera["label"]))
        continues = (
            current
            and sensor_id == current_sensor_id
            and prefix == current_prefix
            and number is not None
            and last_number is not None
            and number == last_number + 1
        )
        if not continues:
            finish_run()
            current = []
        current.append(camera)
        current_sensor_id = sensor_id
        current_prefix = prefix
        last_number = number
    finish_run()
    return tuple(runs)


def infer_dual_fisheye_lens_map_from_raw(
    document: Mapping[str, object],
    raw_root: Path,
) -> Dict[str, object]:
    """Build an auditable lens-map scaffold from raw dual-fisheye folder counts.

    This intentionally reports an assumption instead of silently applying it:
    Metashape XML group order must match sorted capture folder order, with
    `back` before `front` inside each capture.
    """
    if not raw_root.is_dir():
        raise ValidationError(f"Dual-fisheye raw root is not a directory: {raw_root}")
    runs = list(metashape_camera_runs(document, export_path="equisolid_cubeface"))
    lens_entries = []
    for capture_dir in sorted(child for child in raw_root.iterdir() if child.is_dir()):
        for lens_name in ("back", "front"):
            frames_dir = capture_dir / lens_name / "frames"
            if not frames_dir.is_dir():
                continue
            frame_count = sum(1 for path in frames_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)
            lens_entries.append({
                "lens_label": f"{capture_dir.name}/{lens_name}",
                "frame_count": frame_count,
            })
    if len(lens_entries) != len(runs):
        raise ValidationError(
            f"Raw dual-fisheye lens count {len(lens_entries)} does not match XML equisolid run count {len(runs)}"
        )
    mappings = []
    for lens, run in zip(lens_entries, runs):
        if int(lens["frame_count"]) != int(run["count"]):
            raise ValidationError(
                f"Raw lens {lens['lens_label']} has {lens['frame_count']} frames but XML run "
                f"{run['start_camera_id']}-{run['end_camera_id']} has {run['count']} cameras"
            )
        mappings.append({
            "lens_label": lens["lens_label"],
            "camera_ids": run["camera_ids"],
            "range": f"{run['start_camera_id']}-{run['end_camera_id']}",
            "start_label": run["start_label"],
            "end_label": run["end_label"],
        })
    spec = ",".join(f"{item['lens_label']}={item['range']}" for item in mappings)
    return {
        "assumption": "XML equisolid run order matches sorted capture folders, with back before front.",
        "mapping_spec": spec,
        "mappings": tuple(mappings),
    }


def _split_lens_map_entries(spec: str) -> List[Tuple[str, str]]:
    entries: List[Tuple[str, str]] = []
    current_name = None
    current_values: List[str] = []
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "=" in part:
            if current_name is not None:
                entries.append((current_name, ",".join(current_values)))
            current_name, value = part.split("=", 1)
            current_name = current_name.strip()
            current_values = [value.strip()]
        else:
            if current_name is None:
                raise ValidationError(f"Invalid lens-camera-map segment: {part}")
            current_values.append(part)
    if current_name is not None:
        entries.append((current_name, ",".join(current_values)))
    return entries


def parse_lens_camera_map(spec: str) -> Dict[str, Tuple[int, ...]]:
    mapping = {}
    for lens_label, values in _split_lens_map_entries(spec):
        ids = set()
        for segment in re.split(r"[\s,]+", values):
            if not segment:
                continue
            if "-" in segment:
                start_text, end_text = segment.split("-", 1)
                start, end = int(start_text), int(end_text)
                if end < start:
                    raise ValidationError(f"Descending camera id range: {segment}")
                ids.update(range(start, end + 1))
            else:
                ids.add(int(segment))
        if not ids:
            raise ValidationError(f"Lens {lens_label} has no camera ids")
        if lens_label in mapping:
            raise ValidationError(f"Repeated lens label in map: {lens_label}")
        mapping[lens_label] = tuple(sorted(ids))
    if not mapping:
        raise ValidationError("Empty lens-camera-map")
    return mapping


def validate_lens_camera_map(
    document: Mapping[str, object],
    discovery: Mapping[str, object],
    mapping: Mapping[str, Sequence[int]],
) -> Dict[str, object]:
    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    all_camera_ids = set(cameras)
    assigned = {}
    normalized = {}
    for lens_label, ids_raw in mapping.items():
        ids = tuple(sorted(int(camera_id) for camera_id in ids_raw))
        normalized[lens_label] = ids
        for camera_id in ids:
            if camera_id not in all_camera_ids:
                raise ValidationError(f"Lens {lens_label} maps missing camera id {camera_id}")
            if camera_id in assigned:
                raise ValidationError(
                    f"Camera id {camera_id} assigned to both {assigned[camera_id]} and {lens_label}"
                )
            assigned[camera_id] = lens_label

    labels_by_lens: Dict[str, Dict[str, List[int]]] = {}
    for lens_label, ids in normalized.items():
        by_label: Dict[str, List[int]] = defaultdict(list)
        for camera_id in ids:
            by_label[str(cameras[camera_id]["label"])].append(camera_id)
        labels_by_lens[lens_label] = {label: sorted(values) for label, values in by_label.items()}

    # Build a lookup of all unaligned camera labels for skip detection.
    unaligned_labels: set = set()
    for cam in cameras.values():
        if len(cam.get("transform", ())) != 16:
            unaligned_labels.add(str(cam["label"]))

    # Build a set of ALL camera labels in this lens (mapped IDs only) for
    # distinguishing "stem absent from XML" from "stem maps to multiple cameras".
    all_mapped_labels: Dict[str, set] = {}
    for lens_label, ids in normalized.items():
        all_mapped_labels[lens_label] = {str(cameras[cid]["label"]) for cid in ids}

    resolutions = []
    skipped_unaligned_stems: List[Dict[str, str]] = []
    skipped_absent_stems: List[Dict[str, str]] = []
    used = set()
    for lens in discovery["lenses"]:  # type: ignore[index]
        lens_label = str(lens["lens_label"])
        if lens_label not in normalized:
            raise ValidationError(f"Cubeface lens {lens_label} has no lens-camera-map entry")
        stem_camera_ids = {
            str(stem): int(camera_id)
            for stem, camera_id in dict(lens.get("stem_camera_ids", {})).items()
        }
        for stem in lens["stems"]:
            if str(stem) in stem_camera_ids:
                candidate_ids = [stem_camera_ids[str(stem)]]
                if candidate_ids[0] not in normalized[lens_label]:
                    raise ValidationError(
                        f"Cubeface lens {lens_label} stem {stem} maps to camera "
                        f"{candidate_ids[0]}, which is not assigned to that lens"
                    )
            else:
                candidate_ids = labels_by_lens[lens_label].get(str(stem), [])
            if len(candidate_ids) == 0 and str(stem) in unaligned_labels:
                skipped_unaligned_stems.append({"lens_label": lens_label, "stem": str(stem)})
                continue
            if len(candidate_ids) == 0 and str(stem) not in all_mapped_labels[lens_label]:
                skipped_absent_stems.append({"lens_label": lens_label, "stem": str(stem)})
                continue
            if len(candidate_ids) != 1:
                raise ValidationError(
                    f"Lens {lens_label} stem {stem} resolved to {len(candidate_ids)} cameras: {candidate_ids}"
                )
            camera_id = candidate_ids[0]
            used.add(camera_id)
            resolutions.append({
                "lens_label": lens_label,
                "stem": str(stem),
                "camera_id": camera_id,
                "camera_label": cameras[camera_id]["label"],
            })

    mapped_ids = set(assigned)
    return {
        "mapping": {key: list(value) for key, value in sorted(normalized.items())},
        "resolved_count": len(resolutions),
        "resolutions": tuple(sorted(resolutions, key=lambda item: (item["lens_label"], item["stem"]))),
        "skipped_unaligned_stems": tuple(skipped_unaligned_stems),
        "skipped_absent_stems": tuple(skipped_absent_stems),
        "unused_mapped_camera_ids": tuple(sorted(mapped_ids - used)),
        "unused_xml_camera_ids": tuple(sorted(all_camera_ids - used)),
    }


def ordered_cubeface_images(discovery: Mapping[str, object]) -> List[Mapping[str, object]]:
    face_order = {FACE_FILENAME_SUFFIX[face]: index for index, face in enumerate(FACE_TAGS)}
    images: List[Mapping[str, object]] = []
    for lens in discovery["lenses"]:  # type: ignore[index]
        images.extend(lens["images"])
    return sorted(
        images,
        key=lambda image: (
            str(image["lens_label"]),
            str(image["stem"]),
            face_order[str(image["suffix"])],
        ),
    )


def _resolution_lookup(lens_map: Mapping[str, object]) -> Dict[Tuple[str, str], int]:
    return {
        (str(item["lens_label"]), str(item["stem"])): int(item["camera_id"])
        for item in lens_map["resolutions"]  # type: ignore[index]
    }


def sample_ply_xyz(
    path: Path,
    max_points: int = 15000,
    point_transform: Optional[PointTransform] = None,
) -> List[Tuple[float, float, float]]:
    summary = parse_ply_summary(path)
    vertex_count = int(summary["vertex_count"])
    if max_points <= 0 or max_points >= vertex_count:
        stride = 1
        limit = vertex_count
    else:
        stride = max(1, vertex_count // max_points)
        limit = max_points
    points: List[Tuple[float, float, float]] = []
    for index, point in enumerate(iter_ply_points(path)):
        if index % stride == 0:
            points.append(_apply_point_transform((point[0], point[1], point[2]), point_transform))
            if len(points) >= limit:
                break
    return points


def build_pose_records(
    document: Mapping[str, object],
    discovery: Mapping[str, object],
    lens_map: Mapping[str, object],
    convention: str,
    camera_world_transform: Optional[Mapping[str, object]] = None,
) -> List[Dict[str, object]]:
    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    camera_by_lens_stem = _resolution_lookup(lens_map)
    skipped_stems = {
        (str(s["lens_label"]), str(s["stem"]))
        for s in lens_map.get("skipped_unaligned_stems", ())  # type: ignore[union-attr]
    } | {
        (str(s["lens_label"]), str(s["stem"]))
        for s in lens_map.get("skipped_absent_stems", ())  # type: ignore[union-attr]
    }
    records: List[Dict[str, object]] = []
    for image in ordered_cubeface_images(discovery):
        key = (str(image["lens_label"]), str(image["stem"]))
        if key in skipped_stems:
            continue
        if key not in camera_by_lens_stem:
            raise ValidationError(f"No resolved Metashape camera for cubeface image {key}")
        camera_id = camera_by_lens_stem[key]
        r_cw, t_cw = face_world_to_camera_pose(
            cameras[camera_id],
            str(image["internal_face"]),
            convention,
            camera_world_transform,
        )
        records.append({
            "metashape_camera_id": camera_id,
            "qvec": rotation_matrix_to_colmap_qvec(r_cw),
            "tvec": t_cw,
            "rotation": r_cw,
            "image": image,
        })
    return records


def pose_projection_stats(
    document: Mapping[str, object],
    discovery: Mapping[str, object],
    lens_map: Mapping[str, object],
    metashape_points: Path,
    convention: str,
    max_points: int = 15000,
) -> Dict[str, object]:
    camera = _single_cubeface_camera(discovery)
    fx, fy, cx, cy = (float(value) for value in camera["params"])
    width = int(camera["width"])
    height = int(camera["height"])
    points = sample_ply_xyz(
        metashape_points,
        max_points=max_points,
        point_transform=point_transform_from_document(document),
    )
    pose_records = build_pose_records(
        document,
        discovery,
        lens_map,
        convention,
        camera_world_transform_from_document(document),
    )

    total_tests = 0
    positive_depth = 0
    in_bounds = 0
    per_image_in_bounds: List[int] = []
    per_image_positive: List[int] = []
    for record in pose_records:
        r_cw = record["rotation"]
        t_cw = record["tvec"]
        image_positive = 0
        image_in_bounds = 0
        for point in points:
            rotated = _matvec3(r_cw, point)  # type: ignore[arg-type]
            x = rotated[0] + t_cw[0]  # type: ignore[index]
            y = rotated[1] + t_cw[1]  # type: ignore[index]
            z = rotated[2] + t_cw[2]  # type: ignore[index]
            total_tests += 1
            if z <= 1e-9:
                continue
            positive_depth += 1
            image_positive += 1
            u = fx * x / z + cx
            v = fy * y / z + cy
            if 0.0 <= u < width and 0.0 <= v < height:
                in_bounds += 1
                image_in_bounds += 1
        per_image_positive.append(image_positive)
        per_image_in_bounds.append(image_in_bounds)

    return {
        "convention": convention,
        "sampled_point_count": len(points),
        "image_count": len(pose_records),
        "total_tests": total_tests,
        "positive_depth": positive_depth,
        "in_bounds": in_bounds,
        "positive_depth_rate": positive_depth / total_tests if total_tests else 0.0,
        "in_bounds_rate": in_bounds / total_tests if total_tests else 0.0,
        "images_with_positive_depth": sum(1 for value in per_image_positive if value > 0),
        "images_with_in_bounds": sum(1 for value in per_image_in_bounds if value > 0),
        "mean_in_bounds_per_image": sum(per_image_in_bounds) / len(per_image_in_bounds) if per_image_in_bounds else 0.0,
        "min_in_bounds_per_image": min(per_image_in_bounds) if per_image_in_bounds else 0,
        "max_in_bounds_per_image": max(per_image_in_bounds) if per_image_in_bounds else 0,
    }


def _camera_center_containment(
    document: Mapping[str, object],
    lens_map: Mapping[str, object],
    points: Sequence[Tuple[float, float, float]],
    convention: str,
    camera_world_transform: Optional[Mapping[str, object]],
) -> float:
    """Fraction of unique fisheye camera centers inside the point cloud bounding box.

    With the correct convention, cameras should sit within or near the scene.
    With the wrong convention, camera positions spread far outside the point cloud.
    """
    if not points:
        return 0.0
    p_min = [min(p[i] for p in points) for i in range(3)]
    p_max = [max(p[i] for p in points) for i in range(3)]
    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    seen_ids: set = set()
    inside = 0
    total = 0
    for item in lens_map["resolutions"]:  # type: ignore[index]
        cam_id = int(item["camera_id"])
        if cam_id in seen_ids:
            continue
        seen_ids.add(cam_id)
        camera = cameras[cam_id]
        if len(camera.get("transform", ())) != 16:
            continue
        _rotation, center = _camera_world_from_xml(camera, convention, camera_world_transform)
        total += 1
        if all(p_min[i] <= center[i] <= p_max[i] for i in range(3)):
            inside += 1
    return inside / total if total else 0.0


def validate_pose_conventions(
    document: Mapping[str, object],
    discovery: Mapping[str, object],
    lens_map: Mapping[str, object],
    metashape_points: Path,
    max_points: int = 15000,
    min_score_ratio: float = 1.05,
) -> Dict[str, object]:
    conventions = ("metashape_camera_to_world", "metashape_world_to_camera")
    candidates = [
        pose_projection_stats(document, discovery, lens_map, metashape_points, convention, max_points=max_points)
        for convention in conventions
    ]
    ranked = sorted(candidates, key=lambda item: (item["in_bounds"], item["images_with_in_bounds"]), reverse=True)
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    ratio = math.inf
    if second is not None:
        ratio = (float(best["in_bounds"]) + 1.0) / (float(second["in_bounds"]) + 1.0)
    if int(best["in_bounds"]) == 0:
        selected = None
    else:
        selected = best["convention"]
    return {
        "selected_convention": selected,
        "score_ratio": ratio,
        "min_score_ratio": min_score_ratio,
        "candidates": tuple(candidates),
    }


def _relative_colmap_image_name(image_path: Path, root: Path) -> str:
    try:
        relative = image_path.relative_to(root)
    except ValueError:
        relative = image_path
    return relative.as_posix()


def _relative_to_any_root(image_path: Path, roots: Sequence[Path]) -> str:
    candidates = []
    for root in roots:
        try:
            candidates.append(image_path.relative_to(root).as_posix())
        except ValueError:
            continue
    if candidates:
        return min(candidates, key=len)
    return image_path.as_posix()


def build_cubeface_camera_records(
    discovery: Mapping[str, object],
    start_camera_id: int,
) -> Tuple[List[Dict[str, object]], Dict[str, int], int]:
    """Build one PINHOLE camera record per cubeface lens.

    Each lens (= one fisheye sensor's cubeface output directory) gets its own
    PINHOLE entry. Different lenses are allowed to have different face widths;
    each lens internally still must have square cubefaces at a single size.

    Intrinsics per lens: ``fx = fy = W/2``, ``cx = cy = W/2`` where W is that
    lens's face width.

    Returns:
        camera_records: list of PINHOLE camera dicts, one per lens
        camera_id_by_lens: maps lens_label -> camera_id
        next_camera_id: first camera_id not yet allocated
    """
    camera_records: List[Dict[str, object]] = []
    camera_id_by_lens: Dict[str, int] = {}
    cid = start_camera_id
    for lens in discovery["lenses"]:  # type: ignore[index]
        lens_label = str(lens["lens_label"])
        sizes = tuple(tuple(size) for size in lens["face_size_set"])
        if len(sizes) != 1:
            raise ValidationError(
                f"Lens {lens_label} has multiple cubeface image sizes "
                f"{sorted(sizes)} — expected exactly one per lens"
            )
        width, height = sizes[0]
        if width != height:
            raise ValidationError(
                f"Lens {lens_label} cubefaces must be square, found {width}x{height}"
            )
        focal = float(width) / 2.0
        camera_records.append({
            "camera_id": cid,
            "model": "PINHOLE",
            "width": int(width),
            "height": int(height),
            "params": (focal, focal, float(width) / 2.0, float(height) / 2.0),
        })
        camera_id_by_lens[lens_label] = cid
        cid += 1
    return camera_records, camera_id_by_lens, cid


def _single_cubeface_camera(discovery: Mapping[str, object]) -> Dict[str, object]:
    """Return a single representative cubeface camera record.

    Used only by diagnostic helpers — ``pose_projection_stats`` (line 1922)
    and ``write_colmap_skeleton`` (line 4311) — that need one camera to
    derive intrinsics for projection sanity-checks or to write a minimal
    skeleton scaffold. Uses the FIRST lens's face size as the representative.

    For the main scene-write path, use :func:`build_cubeface_camera_records`
    which emits one record per lens and supports differing face widths
    across lenses.
    """
    lenses = discovery["lenses"]  # type: ignore[index]
    if not lenses:
        raise ValidationError("Discovery has no cubeface lenses")
    lens = lenses[0]
    sizes = tuple(tuple(size) for size in lens["face_size_set"])
    if len(sizes) != 1:
        raise ValidationError(
            f"Lens {lens['lens_label']} has multiple face sizes: {sorted(sizes)}"
        )
    width, height = sizes[0]
    if width != height:
        raise ValidationError(f"Cubeface images must be square, found {width}x{height}")
    focal = float(width) / 2.0
    return {
        "camera_id": 1,
        "model": "PINHOLE",
        "width": int(width),
        "height": int(height),
        "params": (focal, focal, float(width) / 2.0, float(height) / 2.0),
    }


def colmap_camera_from_metashape_sensor(
    sensor: Mapping[str, object],
    camera_id: int,
    model_mode: str = "auto",
    *,
    centered_principal_point: bool = False,
) -> Dict[str, object]:
    params: Mapping[str, float] = sensor["params"]  # type: ignore[assignment]
    width = int(sensor["width"])
    height = int(sensor["height"])
    f = float(params.get("f", 0.0))
    if f <= 0.0:
        raise ValidationError(f"Sensor {sensor.get('id', '?')} has no usable focal length")
    b1 = float(params.get("b1", 0.0))
    b2 = float(params.get("b2", 0.0))
    if abs(b2) > 1e-12:
        raise ValidationError(
            f"Sensor {sensor.get('id', '?')} has non-zero skew/affinity b2={b2}, "
            "which this COLMAP text exporter does not model yet"
        )
    fx = f + b1
    fy = f
    if centered_principal_point:
        cx = width / 2.0
        cy = height / 2.0
    else:
        cx = width / 2.0 + float(params.get("cx", 0.0))
        cy = height / 2.0 + float(params.get("cy", 0.0))
    k1 = float(params.get("k1", 0.0))
    k2 = float(params.get("k2", 0.0))
    k3 = float(params.get("k3", 0.0))
    k4 = float(params.get("k4", 0.0))
    p1 = float(params.get("p1", 0.0))
    p2 = float(params.get("p2", 0.0))
    has_distortion = any(abs(value) > 1e-12 for value in (k1, k2, k3, k4, p1, p2))
    if model_mode == "auto":
        if any(abs(value) > 1e-12 for value in (k3, k4)):
            model = "FULL_OPENCV"
        elif has_distortion:
            model = "OPENCV"
        else:
            model = "PINHOLE"
    else:
        model = model_mode.upper()
    if model == "PINHOLE":
        colmap_params = (fx, fy, cx, cy)
    elif model == "OPENCV":
        colmap_params = (fx, fy, cx, cy, k1, k2, p1, p2)
    elif model == "FULL_OPENCV":
        colmap_params = (fx, fy, cx, cy, k1, k2, p1, p2, k3, k4, 0.0, 0.0)
    else:
        raise ValidationError(f"Unsupported passthrough camera model mode: {model_mode}")
    return {
        "camera_id": camera_id,
        "model": model,
        "width": width,
        "height": height,
        "params": colmap_params,
        "metashape_sensor_id": int(sensor["id"]),
    }


def metashape_sensor_pinhole_intrinsics(sensor: Mapping[str, object]) -> Tuple[Tuple[float, float, float, float], Tuple[float, ...]]:
    params: Mapping[str, float] = sensor["params"]  # type: ignore[assignment]
    width = int(sensor["width"])
    height = int(sensor["height"])
    f = float(params.get("f", 0.0))
    if f <= 0.0:
        raise ValidationError(f"Sensor {sensor.get('id', '?')} has no usable focal length")
    b1 = float(params.get("b1", 0.0))
    b2 = float(params.get("b2", 0.0))
    if abs(b2) > 1e-12:
        raise ValidationError(
            f"Sensor {sensor.get('id', '?')} has non-zero skew/affinity b2={b2}, "
            "which passthrough undistortion does not model yet"
        )
    fx = f + b1
    fy = f
    cx = width / 2.0 + float(params.get("cx", 0.0))
    cy = height / 2.0 + float(params.get("cy", 0.0))
    distortion = (
        float(params.get("k1", 0.0)),
        float(params.get("k2", 0.0)),
        float(params.get("p1", 0.0)),
        float(params.get("p2", 0.0)),
        float(params.get("k3", 0.0)),
        float(params.get("k4", 0.0)),
        0.0,
        0.0,
    )
    return (fx, fy, cx, cy), distortion


def metashape_sensor_has_distortion(sensor: Mapping[str, object], *, epsilon: float = 1e-12) -> bool:
    _intrinsics, distortion = metashape_sensor_pinhole_intrinsics(sensor)
    return any(abs(value) > epsilon for value in distortion)


def _passthrough_should_undistort(sensor: Mapping[str, object], mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "auto":
        return metashape_sensor_has_distortion(sensor)
    if mode == "never":
        return False
    raise ValidationError(f"Unsupported passthrough undistortion mode: {mode}")


def _format_floats(values: Sequence[object]) -> str:
    return " ".join(f"{float(value):.17g}" for value in values)


def colmap_qvec_to_rotation_matrix(
    qvec: Sequence[float],
) -> Tuple[Tuple[float, float, float], ...]:
    qw, qx, qy, qz = (float(value) for value in qvec)
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if norm <= 0.0:
        raise ValidationError("Cannot convert zero-length qvec to rotation matrix")
    qw, qx, qy, qz = (qw / norm, qx / norm, qy / norm, qz / norm)
    return (
        (
            1.0 - 2.0 * qy * qy - 2.0 * qz * qz,
            2.0 * qx * qy - 2.0 * qz * qw,
            2.0 * qx * qz + 2.0 * qy * qw,
        ),
        (
            2.0 * qx * qy + 2.0 * qz * qw,
            1.0 - 2.0 * qx * qx - 2.0 * qz * qz,
            2.0 * qy * qz - 2.0 * qx * qw,
        ),
        (
            2.0 * qx * qz - 2.0 * qy * qw,
            2.0 * qy * qz + 2.0 * qx * qw,
            1.0 - 2.0 * qx * qx - 2.0 * qy * qy,
        ),
    )


def _colmap_camera_center(
    qvec: Sequence[float],
    tvec: Sequence[float],
) -> Tuple[float, float, float]:
    rotation = colmap_qvec_to_rotation_matrix(qvec)
    return _matvec3(_transpose3(rotation), _neg3(tvec))


def _colmap_tvec_from_center(
    qvec: Sequence[float],
    center: Sequence[float],
) -> Tuple[float, float, float]:
    rotation = colmap_qvec_to_rotation_matrix(qvec)
    return _matvec3(rotation, _neg3(center))


class SceneSimilarityTransform:
    """Uniform scene transform applied to both COLMAP cameras and 3D points."""

    def __init__(
        self,
        *,
        origin: Sequence[float],
        scale: float,
        target_camera_radius: float = DEFAULT_SCENE_NORMALIZATION_CAMERA_RADIUS,
        center_method: str = "camera_coordinate_median",
        scale_method: str = "camera_p95_radius",
    ) -> None:
        self.origin = _finite_point3(origin, label="scene normalization origin")
        self.scale = float(scale)
        self.target_camera_radius = float(target_camera_radius)
        self.center_method = center_method
        self.scale_method = scale_method
        if not math.isfinite(self.scale) or abs(self.scale) <= SCENE_SCALE_EPSILON:
            raise ValidationError(f"Invalid scene normalization scale: {scale}")

    def apply_point(self, point: Tuple[float, float, float]) -> Tuple[float, float, float]:
        source = _finite_point3(point)
        return (
            (source[0] - self.origin[0]) * self.scale,
            (source[1] - self.origin[1]) * self.scale,
            (source[2] - self.origin[2]) * self.scale,
        )

    def transform_image_record(self, image: Mapping[str, object]) -> Dict[str, object]:
        qvec = tuple(float(value) for value in image["qvec"])  # type: ignore[index]
        tvec = tuple(float(value) for value in image["tvec"])  # type: ignore[index]
        center = _colmap_camera_center(qvec, tvec)
        normalized_center = self.apply_point(center)
        normalized = dict(image)
        normalized["qvec"] = qvec
        normalized["tvec"] = _colmap_tvec_from_center(qvec, normalized_center)
        return normalized

    def as_manifest(self) -> Dict[str, object]:
        return {
            "schema": SCENE_NORMALIZATION_TRANSFORM_SCHEMA,
            "type": "similarity",
            "formula": "normalized = (original - origin) * scale",
            "inverse_formula": "original = normalized / scale + origin",
            "origin": list(self.origin),
            "scale": self.scale,
            "target_camera_radius": self.target_camera_radius,
            "center_method": self.center_method,
            "scale_method": self.scale_method,
        }


def _scene_scale_metrics(
    metashape_points: Path,
    image_records: Sequence[Mapping[str, object]],
    point_transform: Optional[PointTransform],
) -> Dict[str, object]:
    camera_centers = [
        _colmap_camera_center(record["qvec"], record["tvec"])  # type: ignore[arg-type]
        for record in image_records
    ]
    camera_bounds = _empty_bounds()
    combined_bounds = _empty_bounds()
    for center in camera_centers:
        _update_bounds(camera_bounds, center)
        _update_bounds(combined_bounds, center)
    camera_origin = _coordinate_median(camera_centers)
    camera_distances = [
        _distance3(center, camera_origin)
        for center in camera_centers
        if camera_origin is not None
    ]
    camera_bounds_final = _finalize_bounds(camera_bounds)
    camera_span_diagonal = (
        float(camera_bounds_final["diagonal"]) if camera_bounds_final is not None else None
    )

    point_bounds = _empty_bounds()
    point_distances: List[float] = []
    point_count = 0
    for x, y, z, _r, _g, _b in iter_ply_points(metashape_points):
        point_count += 1
        point = _apply_point_transform((x, y, z), point_transform)
        _update_bounds(point_bounds, point)
        _update_bounds(combined_bounds, point)
        if camera_origin is not None:
            point_distances.append(_distance3(point, camera_origin))

    point_bounds_final = _finalize_bounds(point_bounds)
    combined_bounds_final = _finalize_bounds(combined_bounds)
    point_bounds_diagonal = (
        float(point_bounds_final["diagonal"]) if point_bounds_final is not None else None
    )
    combined_bounds_diagonal = (
        float(combined_bounds_final["diagonal"]) if combined_bounds_final is not None else None
    )
    camera_radius_p95 = _percentile(camera_distances, 95.0)
    point_radius_p95 = _percentile(point_distances, 95.0)
    metrics = {
        "camera_count": len(camera_centers),
        "point_count": point_count,
        "camera_center_median": list(camera_origin) if camera_origin is not None else None,
        "camera_bounds": camera_bounds_final,
        "camera_span_diagonal": camera_span_diagonal,
        "camera_radius_median": _median(camera_distances),
        "camera_radius_p95": camera_radius_p95,
        "camera_radius_max": max(camera_distances) if camera_distances else None,
        "point_bounds": point_bounds_final,
        "point_bounds_diagonal": point_bounds_diagonal,
        "point_radius_median": _median(point_distances),
        "point_radius_p95": point_radius_p95,
        "point_radius_p99": _percentile(point_distances, 99.0),
        "point_radius_max": max(point_distances) if point_distances else None,
        "combined_bounds": combined_bounds_final,
        "combined_bounds_diagonal": combined_bounds_diagonal,
    }
    metrics["point_to_camera_radius_ratio"] = _safe_ratio(point_radius_p95, camera_radius_p95)
    metrics["point_to_camera_diagonal_ratio"] = _safe_ratio(point_bounds_diagonal, camera_span_diagonal)
    metrics["combined_to_camera_diagonal_ratio"] = _safe_ratio(combined_bounds_diagonal, camera_span_diagonal)
    return metrics


def _scene_scale_warnings(metrics: Mapping[str, object]) -> List[Dict[str, object]]:
    warnings: List[Dict[str, object]] = []
    camera_radius_p95 = metrics.get("camera_radius_p95")
    camera_span_diagonal = metrics.get("camera_span_diagonal")
    point_ratio = metrics.get("point_to_camera_radius_ratio")
    combined_diagonal = metrics.get("combined_bounds_diagonal")
    if int(metrics.get("camera_count", 0)) <= 1:
        warnings.append({
            "severity": "warning",
            "code": "TOO_FEW_CAMERAS",
            "message": "Only one camera center is available, so scene scale cannot be judged reliably.",
        })
    if camera_radius_p95 is None or float(camera_radius_p95) <= SCENE_SCALE_EPSILON:
        warnings.append({
            "severity": "warning",
            "code": "ZERO_CAMERA_SPREAD",
            "message": "Camera centers have near-zero spread. This usually means placeholder poses or an invalid pose export.",
        })
    if camera_span_diagonal is not None and float(camera_span_diagonal) <= SCENE_SCALE_EPSILON:
        warnings.append({
            "severity": "warning",
            "code": "ZERO_CAMERA_BOUNDS",
            "message": "Camera bounds are effectively a single point.",
        })
    if point_ratio is not None and (float(point_ratio) > 100.0 or float(point_ratio) < 0.01):
        warnings.append({
            "severity": "warning",
            "code": "POINT_CAMERA_SCALE_MISMATCH",
            "message": (
                "The sparse cloud radius is far from the camera-spread radius. "
                "This can be normal for some captures, but it is a sign to inspect scale and alignment."
            ),
            "ratio": float(point_ratio),
        })
    if combined_diagonal is not None and float(combined_diagonal) > 100000.0:
        warnings.append({
            "severity": "info",
            "code": "VERY_LARGE_COORDINATES",
            "message": "Scene coordinates are very large; viewer navigation may feel slow or imprecise.",
            "combined_bounds_diagonal": float(combined_diagonal),
        })
    return warnings


def _make_scene_normalization_transform(
    metrics: Mapping[str, object],
    *,
    target_camera_radius: float = DEFAULT_SCENE_NORMALIZATION_CAMERA_RADIUS,
) -> SceneSimilarityTransform:
    origin = metrics.get("camera_center_median")
    camera_radius_p95 = metrics.get("camera_radius_p95")
    if origin is None:
        raise ValidationError("Cannot normalize scene scale: no camera center median is available")
    if camera_radius_p95 is None or float(camera_radius_p95) <= SCENE_SCALE_EPSILON:
        raise ValidationError(
            "Cannot normalize scene scale: camera poses have near-zero spread. "
            "Use real Metashape poses, not placeholder poses."
        )
    scale = float(target_camera_radius) / float(camera_radius_p95)
    return SceneSimilarityTransform(
        origin=origin,  # type: ignore[arg-type]
        scale=scale,
        target_camera_radius=target_camera_radius,
    )


def _scene_scale_recommendation(
    diagnostics: Mapping[str, object],
) -> str:
    normalization = diagnostics.get("normalization", {})
    if isinstance(normalization, Mapping) and normalization.get("applied"):
        return "Normalization was applied. Use the normalized COLMAP scene for training/viewing, and keep the transform manifest for audit."
    warnings = diagnostics.get("warnings", ())
    if warnings:
        return "Review the scale diagnostics before training. If navigation feels awkward, rerun with scene normalization enabled."
    return "Scene scale diagnostics did not find obvious scale/navigation concerns."


def _build_scene_scale_diagnostics(
    original_metrics: Mapping[str, object],
    *,
    normalization_requested: bool,
    normalization_transform: Optional[SceneSimilarityTransform],
    normalized_metrics: Optional[Mapping[str, object]],
) -> Dict[str, object]:
    diagnostics: Dict[str, object] = {
        "schema": SCENE_SCALE_DIAGNOSTICS_SCHEMA,
        "source_coordinate_frame": "COLMAP export coordinates after Metashape chunk/geographic point transforms",
        "original": original_metrics,
        "normalized": normalized_metrics,
        "normalization": {
            "requested": bool(normalization_requested),
            "applied": normalization_transform is not None,
            "target_camera_radius": DEFAULT_SCENE_NORMALIZATION_CAMERA_RADIUS,
            "transform": normalization_transform.as_manifest() if normalization_transform is not None else None,
        },
        "warnings": _scene_scale_warnings(original_metrics),
    }
    diagnostics["recommendation"] = _scene_scale_recommendation(diagnostics)
    return diagnostics


def _scene_scale_report_lines(
    diagnostics: Mapping[str, object],
    *,
    diagnostics_path: Path,
    normalization_transform_path: Optional[Path],
) -> List[str]:
    normalization = diagnostics.get("normalization", {})
    original = diagnostics.get("original", {})
    normalized = diagnostics.get("normalized")
    if not isinstance(original, Mapping):
        original = {}
    if not isinstance(normalization, Mapping):
        normalization = {}
    lines = [
        "",
        "scene_scale:",
        f"  diagnostics_manifest: {diagnostics_path}",
        f"  normalization_requested: {bool(normalization.get('requested'))}",
        f"  normalization_applied: {bool(normalization.get('applied'))}",
        f"  original_camera_radius_p95: {original.get('camera_radius_p95')}",
        f"  original_point_radius_p95: {original.get('point_radius_p95')}",
        f"  original_point_to_camera_radius_ratio: {original.get('point_to_camera_radius_ratio')}",
        f"  original_combined_bounds_diagonal: {original.get('combined_bounds_diagonal')}",
    ]
    if normalization_transform_path is not None:
        lines.append(f"  normalization_transform_manifest: {normalization_transform_path}")
    if isinstance(normalized, Mapping):
        lines.extend([
            f"  normalized_camera_radius_p95: {normalized.get('camera_radius_p95')}",
            f"  normalized_point_radius_p95: {normalized.get('point_radius_p95')}",
            f"  normalized_combined_bounds_diagonal: {normalized.get('combined_bounds_diagonal')}",
        ])
    warnings = diagnostics.get("warnings", ())
    lines.append(f"  warnings: {len(warnings) if isinstance(warnings, (list, tuple)) else 0}")
    recommendation = diagnostics.get("recommendation")
    if recommendation:
        lines.append(f"  recommendation: {recommendation}")
    return lines


def _project_colmap_camera_point(
    point: Sequence[float],
    image: Mapping[str, object],
    camera: Mapping[str, object],
    *,
    min_depth: float = 1e-6,
) -> Optional[Tuple[float, float]]:
    rotation = image["rotation"]  # type: ignore[index]
    tvec = image["tvec"]  # type: ignore[index]
    rotated = _matvec3(rotation, point)  # type: ignore[arg-type]
    x = rotated[0] + float(tvec[0])  # type: ignore[index]
    y = rotated[1] + float(tvec[1])  # type: ignore[index]
    z = rotated[2] + float(tvec[2])  # type: ignore[index]
    if z <= min_depth:
        return None

    model = str(camera["model"]).upper()
    params = tuple(float(value) for value in camera["params"])  # type: ignore[index]
    xn = x / z
    yn = y / z
    if model == "PINHOLE":
        fx, fy, cx, cy = params[:4]
        u = fx * xn + cx
        v = fy * yn + cy
    elif model == "OPENCV":
        fx, fy, cx, cy, k1, k2, p1, p2 = params[:8]
        r2 = xn * xn + yn * yn
        radial = 1.0 + k1 * r2 + k2 * r2 * r2
        x_distorted = xn * radial + 2.0 * p1 * xn * yn + p2 * (r2 + 2.0 * xn * xn)
        y_distorted = yn * radial + p1 * (r2 + 2.0 * yn * yn) + 2.0 * p2 * xn * yn
        u = fx * x_distorted + cx
        v = fy * y_distorted + cy
    elif model == "FULL_OPENCV":
        fx, fy, cx, cy, k1, k2, p1, p2, k3, k4, k5, k6 = params[:12]
        r2 = xn * xn + yn * yn
        r4 = r2 * r2
        r6 = r4 * r2
        r8 = r4 * r4
        r10 = r8 * r2
        r12 = r6 * r6
        radial = 1.0 + k1 * r2 + k2 * r4 + k3 * r6 + k4 * r8 + k5 * r10 + k6 * r12
        x_distorted = xn * radial + 2.0 * p1 * xn * yn + p2 * (r2 + 2.0 * xn * xn)
        y_distorted = yn * radial + p1 * (r2 + 2.0 * yn * yn) + 2.0 * p2 * xn * yn
        u = fx * x_distorted + cx
        v = fy * y_distorted + cy
    else:
        raise ValidationError(f"Projected tracks do not support COLMAP camera model {model}")

    if not (0.0 <= u < int(camera["width"]) and 0.0 <= v < int(camera["height"])):
        return None
    return u, v


def _prepared_track_images(
    camera_records: Sequence[Mapping[str, object]],
    image_records: Sequence[Mapping[str, object]],
) -> Tuple[Dict[int, Dict[str, object]], List[Dict[str, object]], List[Dict[str, object]]]:
    cameras = {int(camera["camera_id"]): dict(camera) for camera in camera_records}
    prepared_images = []
    for image in image_records:
        prepared = dict(image)
        prepared["image_id"] = int(image["image_id"])
        prepared["rotation"] = colmap_qvec_to_rotation_matrix(image["qvec"])  # type: ignore[arg-type]
        prepared_images.append(prepared)

    groups_by_key: Dict[object, List[Dict[str, object]]] = defaultdict(list)
    for image in prepared_images:
        group_key = image.get("metashape_camera_id")
        if group_key is None:
            group_key = image["image_id"]
        groups_by_key[group_key].append(image)

    groups = []
    for group_key, images in groups_by_key.items():
        first = images[0]
        rotation = first["rotation"]  # type: ignore[index]
        tvec = first["tvec"]  # type: ignore[index]
        center = _matvec3(_transpose3(rotation), _neg3(tvec))  # type: ignore[arg-type]
        groups.append({
            "group_key": group_key,
            "center": center,
            "images": tuple(sorted(images, key=lambda item: int(item["image_id"]))),
        })
    return cameras, prepared_images, groups


def build_projected_tracks(
    metashape_points: Path,
    camera_records: Sequence[Mapping[str, object]],
    image_records: Sequence[Mapping[str, object]],
    *,
    max_points: int = 0,
    max_camera_groups_per_point: int = 8,
    max_observations_per_point: int = 6,
    min_track_length: int = 2,
    max_observations_per_image: int = 0,
    default_point_error: float = 0.0,
    point_transform: Optional[PointTransform] = None,
) -> Dict[str, object]:
    """Project PLY sparse points into nearby cameras and build COLMAP tracks.

    These are synthetic observations derived from Metashape camera poses and
    sparse point locations. They are not original Metashape tie-point tracks.
    """
    if min_track_length < 1:
        raise ValidationError("--track-min-length must be at least 1")
    if max_observations_per_point < min_track_length:
        raise ValidationError("--track-max-observations-per-point must be >= --track-min-length")
    if max_camera_groups_per_point < 1:
        raise ValidationError("--track-max-camera-groups-per-point must be at least 1")

    try:
        import numpy as np  # type: ignore
        from scipy.spatial import cKDTree  # type: ignore
    except Exception as exc:  # pragma: no cover - environment fallback guard
        raise ValidationError("Projected tracks require numpy and scipy.spatial.cKDTree") from exc

    cameras, _prepared_images, groups = _prepared_track_images(camera_records, image_records)
    if not groups:
        raise ValidationError("No image groups available for projected track generation")
    centers = np.asarray([group["center"] for group in groups], dtype=float)
    tree = cKDTree(centers)
    query_k = min(max_camera_groups_per_point, len(groups))

    image_observations: Dict[int, List[Tuple[float, float, int]]] = {
        int(image["image_id"]): [] for image in image_records
    }
    tracked_points: List[Dict[str, object]] = []
    candidate_point_count = 0
    projected_candidate_count = 0
    accepted_projection_count = 0

    for source_index, (x, y, z, r, g, b) in enumerate(iter_ply_points(metashape_points), start=1):
        if max_points > 0 and candidate_point_count >= max_points:
            break
        candidate_point_count += 1
        point = _apply_point_transform((x, y, z), point_transform)
        _distances, candidate_indexes_raw = tree.query(np.asarray(point, dtype=float), k=query_k)
        candidate_indexes = np.atleast_1d(candidate_indexes_raw).astype(int).tolist()
        observations: List[Tuple[int, float, float]] = []
        used_images = set()
        for group_index in candidate_indexes:
            group = groups[group_index]
            for image in group["images"]:  # type: ignore[index]
                image_id = int(image["image_id"])
                if image_id in used_images:
                    continue
                if max_observations_per_image > 0 and len(image_observations[image_id]) >= max_observations_per_image:
                    continue
                camera = cameras[int(image["camera_id"])]
                projected_candidate_count += 1
                pixel = _project_colmap_camera_point(point, image, camera)
                if pixel is None:
                    continue
                observations.append((image_id, pixel[0], pixel[1]))
                used_images.add(image_id)
                if len(observations) >= max_observations_per_point:
                    break
            if len(observations) >= max_observations_per_point:
                break
        if len(observations) < min_track_length:
            continue

        point3d_id = len(tracked_points) + 1
        track = []
        for image_id, u, v in observations:
            point2d_index = len(image_observations[image_id])
            image_observations[image_id].append((u, v, point3d_id))
            track.append((image_id, point2d_index))
        accepted_projection_count += len(track)
        tracked_points.append({
            "point3D_id": point3d_id,
            "source_point_index": source_index,
            "xyz": point,
            "rgb": (r, g, b),
            "error": default_point_error,
            "track": tuple(track),
        })

    total_observations = sum(len(items) for items in image_observations.values())
    images_with_observations = sum(1 for items in image_observations.values() if items)
    return {
        "points": tuple(tracked_points),
        "observations_by_image": image_observations,
        "candidate_point_count": candidate_point_count,
        "tracked_point_count": len(tracked_points),
        "total_observation_count": total_observations,
        "images_with_observations": images_with_observations,
        "projected_candidate_count": projected_candidate_count,
        "accepted_projection_count": accepted_projection_count,
        "max_points": max_points,
        "max_camera_groups_per_point": query_k,
        "max_observations_per_point": max_observations_per_point,
        "min_track_length": min_track_length,
        "max_observations_per_image": max_observations_per_image,
    }


def _write_colmap_text_model(
    metashape_points: Path,
    output_dir: Path,
    camera_records: Sequence[Mapping[str, object]],
    image_records: Sequence[Mapping[str, object]],
    *,
    default_point_error: float = 0.0,
    report_lines: Optional[Sequence[str]] = None,
    report_path: Optional[Path] = None,
    projected_tracks: Optional[Mapping[str, object]] = None,
    point_transform: Optional[PointTransform] = None,
) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cameras_path = output_dir / "cameras.txt"
    images_path = output_dir / "images.txt"
    points_path = output_dir / "points3D.txt"
    if report_path is None:
        report_path = output_dir / "conversion_report.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with cameras_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Camera list with one line of data per camera:\n")
        handle.write("#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n")
        handle.write(f"# Number of cameras: {len(camera_records)}\n")
        for camera in sorted(camera_records, key=lambda item: int(item["camera_id"])):
            params = _format_floats(camera["params"])  # type: ignore[arg-type]
            handle.write(
                f"{camera['camera_id']} {camera['model']} {camera['width']} {camera['height']} {params}\n"
            )

    with images_path.open("w", encoding="utf-8", newline="\n") as handle:
        observations_by_image: Mapping[int, Sequence[Tuple[float, float, int]]] = {}
        if projected_tracks is not None:
            observations_by_image = projected_tracks["observations_by_image"]  # type: ignore[assignment]
        total_observations = sum(len(items) for items in observations_by_image.values())
        mean_observations = total_observations / len(image_records) if image_records else 0.0
        handle.write("# Image list with two lines of data per image:\n")
        handle.write("#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, IMAGE_NAME\n")
        handle.write("#   POINTS2D[] as (X, Y, POINT3D_ID)\n")
        handle.write(f"# Number of images: {len(image_records)}, mean observations per image: {mean_observations:.6f}\n")
        for image_id_fallback, image in enumerate(image_records, start=1):
            image_id = int(image.get("image_id", image_id_fallback))
            q = _format_floats(image["qvec"])  # type: ignore[arg-type]
            t = _format_floats(image["tvec"])  # type: ignore[arg-type]
            handle.write(f"{image_id} {q} {t} {image['camera_id']} {image['image_name']}\n")
            observations = observations_by_image.get(image_id, ())
            if observations:
                handle.write(
                    " ".join(
                        f"{u:.6f} {v:.6f} {point3d_id}"
                        for u, v, point3d_id in observations
                    )
                )
            handle.write("\n")

    point_count = 0
    with points_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# 3D point list with one line of data per point:\n")
        handle.write("#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[]\n")
        if projected_tracks is not None:
            points = projected_tracks["points"]  # type: ignore[index]
            total_track_length = sum(len(point["track"]) for point in points)  # type: ignore[index]
            mean_track_length = total_track_length / len(points) if points else 0.0
            handle.write(f"# Number of points: {len(points)}, mean track length: {mean_track_length:.6f}\n")
            for point in points:  # type: ignore[assignment]
                point_id = int(point["point3D_id"])
                x, y, z = (float(value) for value in point["xyz"])
                r, g, b = (int(value) for value in point["rgb"])
                track = " ".join(
                    f"{int(image_id)} {int(point2d_index)}"
                    for image_id, point2d_index in point["track"]
                )
                handle.write(
                    f"{point_id} {x:.17g} {y:.17g} {z:.17g} "
                    f"{r} {g} {b} {float(point['error']):.17g}"
                    f"{(' ' + track) if track else ''}\n"
                )
                point_count = point_id
        else:
            handle.write("# Number of points: pending, mean track length: 0\n")
            for point_id, (x, y, z, r, g, b) in enumerate(iter_ply_points(metashape_points), start=1):
                point = _apply_point_transform((x, y, z), point_transform)
                handle.write(
                    f"{point_id} {point[0]:.17g} {point[1]:.17g} {point[2]:.17g} "
                    f"{r} {g} {b} {default_point_error:.17g}\n"
                )
                point_count = point_id

    if report_lines is None:
        report_lines = ()
    report_path.write_text("\n".join(report_lines) + ("\n" if report_lines else ""), encoding="utf-8", newline="\n")
    return {
        "output_dir": str(output_dir),
        "cameras_path": str(cameras_path),
        "images_path": str(images_path),
        "points3D_path": str(points_path),
        "report_path": str(report_path),
        "camera_count": len(camera_records),
        "image_count": len(image_records),
        "point_count": point_count,
        "track_observation_count": int(projected_tracks["total_observation_count"]) if projected_tracks else 0,
        "images_with_observations": int(projected_tracks["images_with_observations"]) if projected_tracks else 0,
    }


def _common_image_root(image_records: Sequence[Mapping[str, object]]) -> Optional[Path]:
    paths = [str(Path(str(record["image_path"]))) for record in image_records if record.get("image_path")]
    if not paths:
        return None
    return Path(os.path.commonpath(paths))


def _normalize_image_names_to_common_root(image_records: Sequence[Mapping[str, object]]) -> Tuple[List[Dict[str, object]], Optional[Path]]:
    common_root = _common_image_root(image_records)
    normalized = []
    for record in image_records:
        item = dict(record)
        if common_root is not None and item.get("image_path"):
            item["image_name"] = _relative_colmap_image_name(Path(str(item["image_path"])), common_root)
        normalized.append(item)
    return normalized, common_root


def build_passthrough_image_records(
    document: Mapping[str, object],
    passthrough_map: Mapping[str, object],
    camera_id_by_sensor: Mapping[int, int],
    *,
    convention: Optional[str],
    placeholder_poses: bool = False,
    camera_world_transform: Optional[Mapping[str, object]] = None,
) -> List[Dict[str, object]]:
    if convention is None and not placeholder_poses:
        raise ValidationError("Passthrough export needs a pose convention or --allow-placeholder-poses")
    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    records = []
    for item in passthrough_map["resolutions"]:  # type: ignore[index]
        metashape_camera_id = int(item["camera_id"])
        camera = cameras[metashape_camera_id]
        if convention is None:
            qvec = (1.0, 0.0, 0.0, 0.0)
            tvec = (0.0, 0.0, 0.0)
        else:
            r_cw, t_cw = passthrough_world_to_camera_pose(camera, convention, camera_world_transform)
            qvec = rotation_matrix_to_colmap_qvec(r_cw)
            tvec = t_cw
        record = {
            "kind": "passthrough",
            "metashape_camera_id": metashape_camera_id,
            "camera_id": camera_id_by_sensor[int(item["sensor_id"])],
            "image_name": item["image_name"],
            "image_path": item["image_path"],
            "qvec": qvec,
            "tvec": tvec,
        }
        if item.get("mask_name") is not None:
            record["mask_name"] = item["mask_name"]
        if item.get("mask_output_path") is not None:
            record["mask_output_path"] = item["mask_output_path"]
        records.append(record)
    return records


def build_cubeface_image_records(
    discovery: Mapping[str, object],
    *,
    camera_id_by_lens: Mapping[str, int],
    pose_records: Optional[Sequence[Mapping[str, object]]] = None,
    placeholder_poses: bool = False,
    skipped_stems: Optional[set] = None,
) -> List[Dict[str, object]]:
    """Build COLMAP image records for every cubeface image.

    Each image's ``camera_id`` is looked up by its lens via the
    ``camera_id_by_lens`` map (produced by
    :func:`build_cubeface_camera_records`). This is what enables one PINHOLE
    camera entry per fisheye sensor instead of one shared across all sensors.
    """
    images = ordered_cubeface_images(discovery)
    if skipped_stems:
        images = [
            img for img in images
            if (str(img["lens_label"]), str(img["stem"])) not in skipped_stems
        ]
    if pose_records is None and not placeholder_poses:
        raise ValidationError("Cubeface export needs pose records or --allow-placeholder-poses")
    if pose_records is not None and len(pose_records) != len(images):
        raise ValidationError(
            f"Pose record count {len(pose_records)} does not match image count {len(images)}"
        )
    cubeface_root = Path(str(discovery["root"]))
    records = []
    for index, image in enumerate(images):
        if pose_records is None:
            qvec = (1.0, 0.0, 0.0, 0.0)
            tvec = (0.0, 0.0, 0.0)
            metashape_camera_id = None
        else:
            qvec = pose_records[index]["qvec"]  # type: ignore[index]
            tvec = pose_records[index]["tvec"]  # type: ignore[index]
            metashape_camera_id = pose_records[index]["metashape_camera_id"]  # type: ignore[index]
        image_name = str(
            image.get("image_name")
            or _relative_colmap_image_name(Path(str(image["image_path"])), cubeface_root)
        )
        lens_label = str(image["lens_label"])
        if lens_label not in camera_id_by_lens:
            raise ValidationError(
                f"No camera_id allocated for lens {lens_label!r}; the camera_id_by_lens "
                f"map must include every lens in the discovery"
            )
        records.append({
            "kind": "cubeface",
            "metashape_camera_id": metashape_camera_id,
            "camera_id": camera_id_by_lens[lens_label],
            "image_name": image_name,
            "image_path": image["image_path"],
            "qvec": qvec,
            "tvec": tvec,
        })
    return records


def _replace_extension_posix(path: str, extension: str) -> str:
    if not extension.startswith("."):
        extension = f".{extension}"
    slash = path.rfind("/")
    dot = path.rfind(".")
    if dot > slash:
        return f"{path[:dot]}{extension}"
    return f"{path}{extension}"


def _same_file_size(source: Path, dest: Path) -> bool:
    try:
        return source.stat().st_size == dest.stat().st_size
    except OSError:
        return False


def _link_or_copy_file(source: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if _same_file_size(source, dest):
            return "reused"
        dest.unlink()
    try:
        os.link(source, dest)
        return "linked"
    except OSError:
        shutil.copy2(source, dest)
        return "copied"


def _read_cv_image(path: Path, flags: int):
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, flags)
    if image is None:
        raise ValidationError(f"OpenCV could not read image: {path}")
    return image


def _write_cv_image(path: Path, image, output_format: str = "png") -> None:
    import cv2  # type: ignore

    fmt = output_format.lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in {"png", "jpg", "tiff", "tif"}:
        raise ValidationError(f"Unsupported OpenCV output format: {output_format}")

    path.parent.mkdir(parents=True, exist_ok=True)
    params = []
    if fmt == "jpg":
        params = [cv2.IMWRITE_JPEG_QUALITY, 100]
    ok, encoded = cv2.imencode(f".{fmt}", image, params)
    if not ok:
        raise ValidationError(f"OpenCV could not encode {fmt.upper()}: {path}")
    encoded.tofile(str(path))


def _write_cv_png(path: Path, image) -> None:
    _write_cv_image(path, image, "png")


def _image_dimensions_match(path: Path, expected_width: int, expected_height: int) -> bool:
    if not path.is_file():
        return False
    try:
        return read_image_size(path) == (expected_width, expected_height)
    except ValidationError:
        return False


def _undistort_metadata_path(path: Path, metadata_root: Optional[Path] = None) -> Path:
    if metadata_root is not None:
        parts = path.parts
        lowered = [part.lower() for part in parts]
        for index in range(len(parts) - 1, -1, -1):
            if lowered[index] in {"images", "masks"}:
                relative = Path(*parts[index + 1:])
                return metadata_root / relative.with_suffix(f"{relative.suffix}.meta.json")
        return metadata_root / path.name
    return path.with_suffix(f"{path.suffix}.meta.json")


def _undistort_cache_signature(
    source: Path,
    sensor: Mapping[str, object],
    is_mask: bool,
    output_format: str = "png",
) -> Dict[str, object]:
    stat = source.stat()
    params = {
        str(key): float(value)
        for key, value in sorted(dict(sensor.get("params", {})).items())
    }
    return {
        "version": 1,
        "operation": "opencv_initUndistortRectifyMap_pinhole_v1",
        "source": {
            "path": str(source.resolve()),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        },
        "sensor": {
            "id": int(sensor.get("id", -1)),
            "width": int(sensor["width"]),
            "height": int(sensor["height"]),
            "params": params,
        },
        "output": {
            "format": output_format.lower().lstrip("."),
            "is_mask": bool(is_mask),
        },
    }


def _generated_valid_mask_signature(source: Path, sensor: Mapping[str, object]) -> Dict[str, object]:
    signature = _undistort_cache_signature(source, sensor, True)
    signature["version"] = 1
    signature["operation"] = "opencv_initUndistortRectifyMap_valid_mask_v1"
    signature["output"] = {
        "format": "png",
        "is_mask": True,
        "generated_valid_pixels": True,
    }
    return signature


def _undistort_cache_matches(
    dest: Path,
    signature: Mapping[str, object],
    metadata_root: Optional[Path] = None,
) -> bool:
    meta_path = _undistort_metadata_path(dest, metadata_root)
    if not meta_path.is_file():
        return False
    try:
        cached = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return cached == signature


def _write_undistort_metadata(
    dest: Path,
    signature: Mapping[str, object],
    metadata_root: Optional[Path] = None,
) -> None:
    meta_path = _undistort_metadata_path(dest, metadata_root)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(signature, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _emit_progress(enabled: bool, phase: str, current: int, total: int, label: str = "") -> None:
    if not enabled:
        return
    suffix = f": {label}" if label else ""
    print(f"[PROGRESS] {phase} {current}/{total}{suffix}", file=sys.stderr, flush=True)


def _centered_new_camera_matrix(
    fx: float, fy: float, width: int, height: int,
):
    """Return a camera matrix with the principal point at the image center.

    Using a centered new camera matrix during undistortion produces an image
    where cx=w/2, cy=h/2 — compatible with 3DGS trainers that assume centered
    principal points (including the original Inria implementation).
    """
    import numpy as np  # type: ignore

    return np.array(
        ((fx, 0.0, width / 2.0), (0.0, fy, height / 2.0), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )


def _undistort_to_image(
    source: Path,
    dest: Path,
    sensor: Mapping[str, object],
    *,
    is_mask: bool,
    output_format: str = "png",
    force: bool = False,
    metadata_root: Optional[Path] = None,
) -> str:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    sensor_width = int(sensor["width"])
    sensor_height = int(sensor["height"])
    signature = _undistort_cache_signature(source, sensor, is_mask, output_format)
    if (
        not force
        and _image_dimensions_match(dest, sensor_width, sensor_height)
        and _undistort_cache_matches(dest, signature, metadata_root)
    ):
        return "reused_undistorted"

    flags = cv2.IMREAD_UNCHANGED
    image = _read_cv_image(source, flags)
    height, width = image.shape[:2]
    if (width, height) != (sensor_width, sensor_height):
        raise ValidationError(
            f"Image dimensions do not match sensor {sensor.get('id', '?')}: "
            f"{source} is {width}x{height}, sensor is {sensor['width']}x{sensor['height']}"
        )
    fx_fy_cx_cy, distortion = metashape_sensor_pinhole_intrinsics(sensor)
    fx, fy, cx, cy = fx_fy_cx_cy
    camera_matrix = np.array(
        ((fx, 0.0, cx), (0.0, fy, cy), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    new_camera_matrix = _centered_new_camera_matrix(fx, fy, width, height)
    dist = np.array(distortion, dtype=np.float64)
    map1, map2 = cv2.initUndistortRectifyMap(
        camera_matrix,
        dist,
        None,
        new_camera_matrix,
        (width, height),
        cv2.CV_32FC1,
    )
    interpolation = cv2.INTER_NEAREST if is_mask else cv2.INTER_LINEAR
    remapped = cv2.remap(
        image,
        map1,
        map2,
        interpolation,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    _write_cv_image(dest, remapped, output_format)
    _write_undistort_metadata(dest, signature, metadata_root)
    return "undistorted"


def _write_undistort_valid_mask_to_png(
    source: Path,
    dest: Path,
    sensor: Mapping[str, object],
    *,
    force: bool = False,
    metadata_root: Optional[Path] = None,
) -> str:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    sensor_width = int(sensor["width"])
    sensor_height = int(sensor["height"])
    signature = _generated_valid_mask_signature(source, sensor)
    if (
        not force
        and _image_dimensions_match(dest, sensor_width, sensor_height)
        and _undistort_cache_matches(dest, signature, metadata_root)
    ):
        return "reused_generated_valid_mask"

    # Read the source only to verify that this mask matches the exact pixel
    # dimensions being undistorted; the mask itself is generated from remap
    # validity, not from image brightness or content.
    image = _read_cv_image(source, cv2.IMREAD_UNCHANGED)
    height, width = image.shape[:2]
    if (width, height) != (sensor_width, sensor_height):
        raise ValidationError(
            f"Image dimensions do not match sensor {sensor.get('id', '?')}: "
            f"{source} is {width}x{height}, sensor is {sensor['width']}x{sensor['height']}"
        )
    fx_fy_cx_cy, distortion = metashape_sensor_pinhole_intrinsics(sensor)
    fx, fy, cx, cy = fx_fy_cx_cy
    camera_matrix = np.array(
        ((fx, 0.0, cx), (0.0, fy, cy), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    new_camera_matrix = _centered_new_camera_matrix(fx, fy, width, height)
    dist = np.array(distortion, dtype=np.float64)
    map1, map2 = cv2.initUndistortRectifyMap(
        camera_matrix,
        dist,
        None,
        new_camera_matrix,
        (width, height),
        cv2.CV_32FC1,
    )
    source_mask = np.full((height, width), 255, dtype=np.uint8)
    valid_mask = cv2.remap(
        source_mask,
        map1,
        map2,
        cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    _write_cv_png(dest, valid_mask)
    _write_undistort_metadata(dest, signature, metadata_root)
    return "generated_valid_mask"


def _scene_asset_path(output_scene: Path, category: str, image_name: str) -> Path:
    return output_scene / category / Path(*image_name.split("/"))


def _asset_name_token(value: str, fallback: str = "asset") -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_")
    return token or fallback


def _short_stable_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:4]


def _unique_flat_asset_name(
    base_name: str,
    used_asset_names: Set[str],
    identity: str,
) -> str:
    base_path = Path(base_name)
    stem = base_path.stem
    suffix = base_path.suffix
    candidate = f"{stem}{suffix}"
    key = candidate.casefold()
    if key not in used_asset_names:
        used_asset_names.add(key)
        return candidate

    hashed = f"{stem}_{_short_stable_hash(identity)}{suffix}"
    key = hashed.casefold()
    if key not in used_asset_names:
        used_asset_names.add(key)
        return hashed

    counter = 2
    while True:
        candidate = f"{stem}_{_short_stable_hash(identity)}_{counter}{suffix}"
        key = candidate.casefold()
        if key not in used_asset_names:
            used_asset_names.add(key)
            return candidate
        counter += 1


def _cubeface_flat_asset_name(
    image: Mapping[str, object],
    used_asset_names: Set[str],
) -> str:
    lens = _asset_name_token(str(image.get("lens_label", "")), "lens")
    stem = _asset_name_token(str(image.get("stem", "")), "image")
    suffix = str(image.get("suffix", ""))
    extension = str(image.get("extension") or Path(str(image["image_path"])).suffix).lower()
    base = f"{lens}_{stem}{suffix}{extension}"
    return _unique_flat_asset_name(base, used_asset_names, str(image.get("image_path", base)))


def _passthrough_flat_asset_name(
    resolution: Mapping[str, object],
    source_image: Path,
    *,
    undistort: bool,
    output_format: str,
    used_asset_names: Set[str],
) -> str:
    slug = _asset_name_token(str(resolution.get("media_set_slug") or resolution.get("media_set_name") or "passthrough"), "passthrough")
    source_relative = str(resolution.get("image_relative_path") or resolution.get("image_name") or source_image.name)
    stem = _asset_name_token(Path(source_relative).stem, "image")
    extension = f".{output_format.lower().lstrip('.')}" if undistort else source_image.suffix.lower()
    base = f"{slug}_{stem}{extension}"
    return _unique_flat_asset_name(base, used_asset_names, str(source_image))


def _archive_cubeface_support_files(
    discovery: Mapping[str, object],
    remap_cache_dir: Path,
    logs_dir: Path,
) -> List[str]:
    report_lines = []
    used_lens_names: Set[str] = set()
    for lens in discovery.get("lenses", ()):
        lens_label = str(lens.get("lens_label", "lens"))
        lens_token = _unique_flat_asset_name(
            _asset_name_token(lens_label, "lens"),
            used_lens_names,
            lens_label,
        )
        if "." in lens_token:
            lens_token = Path(lens_token).stem
        lens_path = Path(str(lens["path"]))
        bonus_dir = lens_path / "bonusdata"
        if bonus_dir.is_dir():
            dest = remap_cache_dir / lens_token
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(bonus_dir, dest)
            report_lines.append(f"remap_support_copied: {bonus_dir} -> remap_cache/{lens_token}")
        run_report = lens_path / "run_report.txt"
        if run_report.is_file():
            dest = logs_dir / f"cubeface_{lens_token}_run_report.txt"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(run_report, dest)
            report_lines.append(f"cubeface_run_report_copied: {run_report} -> logs/{dest.name}")
    return report_lines


def _package_cubeface_assets(
    output_scene: Path,
    discovery: Mapping[str, object],
    image_records: Sequence[Mapping[str, object]],
    *,
    package_assets: bool,
    progress: bool = False,
    progress_interval: int = 250,
    skipped_stems: Optional[set] = None,
    used_asset_names: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, object]], List[str], int, int]:
    if used_asset_names is None:
        used_asset_names = set()
    final_records = []
    report_lines = []
    packaged_images = 0
    packaged_masks = 0
    ordered_images = ordered_cubeface_images(discovery)
    if skipped_stems:
        ordered_images = [
            img for img in ordered_images
            if (str(img["lens_label"]), str(img["stem"])) not in skipped_stems
        ]
    total = len(ordered_images)
    interval = max(1, progress_interval)
    for index, (record, image) in enumerate(zip(image_records, ordered_images), start=1):
        item = dict(record)
        source_image = Path(str(image["image_path"]))
        final_name = _cubeface_flat_asset_name(image, used_asset_names)
        item["image_name"] = final_name
        item["image_path"] = str(_scene_asset_path(output_scene, "images", final_name))
        if package_assets:
            action = _link_or_copy_file(source_image, _scene_asset_path(output_scene, "images", final_name))
            packaged_images += 1
            report_lines.append(f"image_{action}: {source_image} -> images/{final_name}")
            mask_path = image.get("mask_path")
            if mask_path:
                mask_source = Path(str(mask_path))
                mask_name = Path(final_name).with_suffix(".png").name
                item["mask_name"] = mask_name
                item["mask_output_path"] = str(_scene_asset_path(output_scene, "masks", mask_name))
                mask_action = _link_or_copy_file(mask_source, _scene_asset_path(output_scene, "masks", mask_name))
                packaged_masks += 1
                report_lines.append(f"mask_{mask_action}: {mask_source} -> masks/{mask_name}")
        final_records.append(item)
        if index == total or index == 1 or index % interval == 0:
            _emit_progress(progress, "PACKAGE_CUBEFACES", index, total, final_name)
    return final_records, report_lines, packaged_images, packaged_masks


def build_erp_camera_records(
    erp_view_map: Mapping[str, object],
    start_camera_id: int,
) -> Tuple[List[Dict[str, object]], Dict[int, int], int]:
    """Build one PINHOLE camera record per ERP sensor.

    All views within a single ERP sensor + split mode share intrinsics
    (same FOV, same crop size). Per Method E, we register a single PINHOLE
    entry per ERP sensor and let every view image record reference that one
    entry; per-view rotation lives entirely in each image record's qvec
    (composed by ``face_world_to_camera_pose`` keyed on the view label).

    All views within a sensor are validated to share intrinsics — a
    ValidationError is raised if they don't (which should never happen
    given the preset-driven view geometry, but the check is cheap).

    Returns:
        camera_records: list of PINHOLE camera dicts, one per ERP sensor
        sensor_camera_ids: maps sensor_id -> camera_id
        next_camera_id: first camera_id not yet allocated
    """
    camera_records: List[Dict[str, object]] = []
    sensor_camera_ids: Dict[int, int] = {}
    cid = start_camera_id
    for erp_sensor in erp_view_map.get("sensors", ()) or ():
        sid = int(erp_sensor["sensor_id"])
        views = erp_sensor.get("views", ()) or ()
        if not views:
            continue
        # All views in this sensor must share intrinsics. Validate.
        first = views[0]
        for view in views[1:]:
            for key in ("width", "height", "fx", "fy", "cx", "cy"):
                if float(view[key]) != float(first[key]):
                    raise ValidationError(
                        f"ERP sensor {sid} views disagree on intrinsic '{key}': "
                        f"view {first['label']} has {first[key]} but "
                        f"view {view['label']} has {view[key]}"
                    )
        camera_records.append({
            "camera_id": cid,
            "model": "PINHOLE",
            "width": int(first["width"]),
            "height": int(first["height"]),
            "params": (
                float(first["fx"]), float(first["fy"]),
                float(first["cx"]), float(first["cy"]),
            ),
        })
        sensor_camera_ids[sid] = cid
        cid += 1
    return camera_records, sensor_camera_ids, cid


def build_erp_image_records(
    document: Mapping[str, object],
    erp_view_map: Mapping[str, object],
    sensor_camera_ids: Mapping[int, int],
    *,
    pose_convention: Optional[str],
    placeholder_poses: bool,
    camera_world_transform: Optional[Mapping[str, object]] = None,
) -> List[Dict[str, object]]:
    """Emit one COLMAP image record per (source ERP camera x view-slot).

    Per Method E, every view of a given ERP sensor references the same
    ``camera_id`` (allocated by :func:`build_erp_camera_records`); per-view
    rotation lives entirely in each image record's ``qvec``, composed via
    ``face_world_to_camera_pose`` keyed on the angle-encoded view label.

    Source camera records whose matching crop file is missing on disk are
    silently skipped (e.g. when a frame was filtered out during a partial
    export).
    """
    cameras = document["cameras"]  # type: ignore[index]
    cameras_by_sensor: Dict[int, List[Tuple[object, Mapping[str, object]]]] = {}
    for camera_id, camera in cameras.items():
        cameras_by_sensor.setdefault(int(camera["sensor_id"]), []).append((camera_id, camera))

    records: List[Dict[str, object]] = []
    for erp_sensor in erp_view_map.get("sensors", ()) or ():
        sid = int(erp_sensor["sensor_id"])
        for_sensor = cameras_by_sensor.get(sid, [])
        cid = sensor_camera_ids[sid]
        for view in erp_sensor["views"]:
            view_label_str = str(view["label"])
            images_dir = Path(str(view["images_dir"]))
            masks_dir = Path(str(view["masks_dir"])) if view.get("masks_dir") else None
            dir_name = str(view["dir_name"])
            for metashape_camera_id, camera in for_sensor:
                stem = str(camera["label"])
                source_image = images_dir / f"{stem}.png"
                if not source_image.is_file():
                    continue
                if placeholder_poses or pose_convention is None:
                    qvec = (1.0, 0.0, 0.0, 0.0)
                    tvec = (0.0, 0.0, 0.0)
                else:
                    r_face_from_world, t_face_from_world = face_world_to_camera_pose(
                        camera,
                        view_label_str,
                        pose_convention,
                        camera_world_transform,
                    )
                    qvec = rotation_matrix_to_colmap_qvec(r_face_from_world)
                    tvec = t_face_from_world
                mask_source = (masks_dir / f"{stem}.png") if masks_dir is not None else None
                records.append({
                    "kind": "erp",
                    "metashape_camera_id": metashape_camera_id,
                    "camera_id": cid,
                    "image_name": f"erp_sensor_{sid}/{dir_name}/{stem}.png",
                    "image_path": str(source_image),
                    "mask_path": str(mask_source) if mask_source and mask_source.is_file() else None,
                    "qvec": qvec,
                    "tvec": tvec,
                    "erp_sensor_id": sid,
                    "erp_view_label": view_label_str,
                    "erp_stem": stem,
                })
    return records


def build_adaptive_camera_records(
    adaptive_map: Mapping[str, object],
    start_camera_id: int,
) -> Tuple[List[Dict[str, object]], Dict[int, int], int]:
    """Build one PINHOLE camera record per adaptive single-pinhole sensor."""
    camera_records: List[Dict[str, object]] = []
    sensor_camera_ids: Dict[int, int] = {}
    cid = start_camera_id
    for adaptive_sensor in adaptive_map.get("sensors", ()) or ():
        sid = int(adaptive_sensor["sensor_id"])
        f_target = adaptive_sensor.get("f_target")
        w_out = adaptive_sensor.get("w_out")
        if f_target is None or w_out is None:
            raise ValidationError(
                f"Adaptive sensor {sid} requires f_target and w_out for PINHOLE intrinsics"
            )
        width = int(w_out)
        height = int(adaptive_sensor.get("h_out", w_out))
        focal = float(f_target)
        camera_records.append({
            "camera_id": cid,
            "model": "PINHOLE",
            "width": width,
            "height": height,
            "params": (focal, focal, float(width) / 2.0, float(height) / 2.0),
        })
        sensor_camera_ids[sid] = cid
        cid += 1
    return camera_records, sensor_camera_ids, cid


def build_adaptive_image_records(
    document: Mapping[str, object],
    adaptive_map: Mapping[str, object],
    sensor_camera_ids: Mapping[int, int],
    *,
    pose_convention: Optional[str],
    placeholder_poses: bool,
    camera_world_transform: Optional[Mapping[str, object]] = None,
) -> List[Dict[str, object]]:
    """Emit one COLMAP image record per adaptive output image.

    Adaptive Path B keeps the source camera pose directly; no cubeface rotation
    is composed into the image record.
    """
    if pose_convention is None and not placeholder_poses:
        raise ValidationError("Adaptive export needs a pose convention or --allow-placeholder-poses")
    cameras = document["cameras"]  # type: ignore[index]
    cameras_by_sensor: Dict[int, List[Tuple[object, Mapping[str, object]]]] = {}
    for camera_id, camera in cameras.items():
        cameras_by_sensor.setdefault(int(camera["sensor_id"]), []).append((camera_id, camera))

    records: List[Dict[str, object]] = []
    for adaptive_sensor in adaptive_map.get("sensors", ()) or ():
        sid = int(adaptive_sensor["sensor_id"])
        cid = sensor_camera_ids[sid]
        images_dir = Path(str(adaptive_sensor["images_dir"]))
        masks_dir = Path(str(adaptive_sensor["masks_dir"])) if adaptive_sensor.get("masks_dir") else None
        for metashape_camera_id, camera in cameras_by_sensor.get(sid, []):
            stem = str(camera["label"])
            source_image = images_dir / f"{stem}.png"
            if not source_image.is_file():
                continue
            if placeholder_poses or pose_convention is None:
                qvec = (1.0, 0.0, 0.0, 0.0)
                tvec = (0.0, 0.0, 0.0)
            else:
                r_camera_from_world, t_camera_from_world = passthrough_world_to_camera_pose(
                    camera,
                    pose_convention,
                    camera_world_transform,
                )
                qvec = rotation_matrix_to_colmap_qvec(r_camera_from_world)
                tvec = t_camera_from_world

            mask_source = None
            if masks_dir is not None:
                candidate = masks_dir / f"{stem}_mask.png"
                if not candidate.is_file():
                    candidate = masks_dir / f"{stem}.png"
                if candidate.is_file():
                    mask_source = candidate
            records.append({
                "kind": "adaptive",
                "metashape_camera_id": metashape_camera_id,
                "camera_id": cid,
                "image_name": f"adaptive_sensor_{sid}/{stem}.png",
                "image_path": str(source_image),
                "mask_path": str(mask_source) if mask_source is not None else None,
                "qvec": qvec,
                "tvec": tvec,
                "adaptive_sensor_id": sid,
                "adaptive_stem": stem,
            })
    return records


def _package_adaptive_assets(
    output_scene: Path,
    adaptive_records: Sequence[Mapping[str, object]],
    *,
    package_assets: bool,
    used_asset_names: Set[str],
) -> Tuple[List[Dict[str, object]], List[str], int, int]:
    """Copy/link adaptive Path B images and masks into the final scene."""
    final_records: List[Dict[str, object]] = []
    report_lines: List[str] = []
    packaged_images = 0
    packaged_masks = 0
    for record in adaptive_records:
        item = dict(record)
        source_image = Path(str(item["image_path"]))
        sid_token = _asset_name_token(f"adaptive_{item['adaptive_sensor_id']}", "adaptive")
        stem_token = _asset_name_token(str(item["adaptive_stem"]), "image")
        base_name = f"{sid_token}_{stem_token}.png"
        identity = f"{item['adaptive_sensor_id']}|{item['adaptive_stem']}"
        final_name = _unique_flat_asset_name(base_name, used_asset_names, identity)
        item["image_name"] = final_name
        item["image_path"] = str(_scene_asset_path(output_scene, "images", final_name))
        if package_assets:
            action = _link_or_copy_file(source_image, _scene_asset_path(output_scene, "images", final_name))
            packaged_images += 1
            report_lines.append(f"adaptive_image_{action}: {source_image} -> images/{final_name}")
            mask_source = item.get("mask_path")
            if mask_source:
                mask_action = _link_or_copy_file(
                    Path(str(mask_source)),
                    _scene_asset_path(output_scene, "masks", final_name),
                )
                packaged_masks += 1
                report_lines.append(f"adaptive_mask_{mask_action}: {mask_source} -> masks/{final_name}")
        for key in ("adaptive_sensor_id", "adaptive_stem", "mask_path"):
            item.pop(key, None)
        final_records.append(item)
    return final_records, report_lines, packaged_images, packaged_masks


def _package_erp_assets(
    output_scene: Path,
    erp_records: Sequence[Mapping[str, object]],
    *,
    package_assets: bool,
    used_asset_names: Set[str],
) -> Tuple[List[Dict[str, object]], List[str], int, int]:
    """Copy/link ERP crops into ``output_scene/images`` and masks into
    ``output_scene/masks``. Returns the final records with their packaged
    image_name plus a report line per file."""
    final_records: List[Dict[str, object]] = []
    report_lines: List[str] = []
    packaged_images = 0
    packaged_masks = 0
    for record in erp_records:
        item = dict(record)
        source_image = Path(str(item["image_path"]))
        sid_token = _asset_name_token(f"erp_{item['erp_sensor_id']}", "erp")
        view_token = _asset_name_token(str(item["erp_view_label"]), "view")
        stem_token = _asset_name_token(str(item["erp_stem"]), "image")
        base_name = f"{sid_token}_{view_token}_{stem_token}.png"
        identity = f"{item['erp_sensor_id']}|{item['erp_view_label']}|{item['erp_stem']}"
        final_name = _unique_flat_asset_name(base_name, used_asset_names, identity)
        item["image_name"] = final_name
        item["image_path"] = str(_scene_asset_path(output_scene, "images", final_name))
        if package_assets:
            action = _link_or_copy_file(source_image, _scene_asset_path(output_scene, "images", final_name))
            packaged_images += 1
            report_lines.append(f"erp_image_{action}: {source_image} -> images/{final_name}")
            mask_source = item.get("mask_path")
            if mask_source:
                mask_action = _link_or_copy_file(
                    Path(str(mask_source)),
                    _scene_asset_path(output_scene, "masks", final_name),
                )
                packaged_masks += 1
                report_lines.append(f"erp_mask_{mask_action}: {mask_source} -> masks/{final_name}")
        for key in ("erp_sensor_id", "erp_view_label", "erp_stem", "mask_path"):
            item.pop(key, None)
        final_records.append(item)
    return final_records, report_lines, packaged_images, packaged_masks


def _package_passthrough_resolution(
    output_scene: Path,
    resolution: Mapping[str, object],
    sensor: Mapping[str, object],
    *,
    package_assets: bool,
    undistort: bool,
    output_format: str,
    require_masks: bool,
    force_assets: bool = False,
    metadata_root: Optional[Path] = None,
    used_asset_names: Optional[Set[str]] = None,
) -> Tuple[Dict[str, object], List[str], int, int, int, int]:
    if used_asset_names is None:
        used_asset_names = set()
    source_image = Path(str(resolution["image_path"]))
    source_mask = Path(str(resolution["mask_path"])) if resolution.get("mask_path") else None
    final_name = _passthrough_flat_asset_name(
        resolution,
        source_image,
        undistort=undistort,
        output_format=output_format,
        used_asset_names=used_asset_names,
    )
    final_image_dest = _scene_asset_path(output_scene, "images", final_name)
    mask_name = Path(final_name).with_suffix(".png").name
    final_mask_dest = _scene_asset_path(output_scene, "masks", mask_name)
    report_lines = []
    packaged_images = 0
    packaged_masks = 0
    undistorted_count = 0
    reused_undistorted_count = 0

    if package_assets:
        if undistort:
            action = _undistort_to_image(
                source_image,
                final_image_dest,
                sensor,
                is_mask=False,
                output_format=output_format,
                force=force_assets,
                metadata_root=metadata_root,
            )
            if action == "reused_undistorted":
                reused_undistorted_count += 1
            else:
                undistorted_count += 1
        else:
            action = _link_or_copy_file(source_image, final_image_dest)
        packaged_images += 1
        report_lines.append(f"image_{action}: {source_image} -> images/{final_name}")

        if source_mask is not None:
            if undistort:
                mask_action = _undistort_to_image(
                    source_mask,
                    final_mask_dest,
                    sensor,
                    is_mask=True,
                    output_format="png",
                    force=force_assets,
                    metadata_root=metadata_root,
                )
            else:
                mask_action = _link_or_copy_file(source_mask, final_mask_dest)
            packaged_masks += 1
            report_lines.append(f"mask_{mask_action}: {source_mask} -> masks/{mask_name}")
        elif undistort:
            mask_action = _write_undistort_valid_mask_to_png(
                source_image,
                final_mask_dest,
                sensor,
                force=force_assets,
                metadata_root=metadata_root,
            )
            packaged_masks += 1
            report_lines.append(f"mask_{mask_action}: {source_image} -> masks/{mask_name}")
        elif require_masks:
            raise ValidationError(f"Missing mask for passthrough image {source_image}")

    final = dict(resolution)
    final["source_image_path"] = str(source_image)
    final["source_mask_path"] = str(source_mask) if source_mask is not None else None
    final["image_path"] = str(final_image_dest)
    final["image_name"] = final_name
    final["mask_name"] = mask_name if source_mask is not None or undistort else None
    final["mask_output_path"] = str(final_mask_dest) if source_mask is not None or undistort else None
    final["undistorted"] = undistort
    return final, report_lines, packaged_images, packaged_masks, undistorted_count, reused_undistorted_count


def _validate_packaged_scene_assets(
    output_scene: Path,
    image_records: Sequence[Mapping[str, object]],
    *,
    require_masks: bool,
) -> Dict[str, object]:
    missing_images = []
    missing_masks = []
    for record in image_records:
        image_name = str(record["image_name"])
        if not _scene_asset_path(output_scene, "images", image_name).is_file():
            missing_images.append(image_name)
        mask_name = str(record.get("mask_name") or image_name)
        if require_masks and not _scene_asset_path(output_scene, "masks", mask_name).is_file():
            missing_masks.append(mask_name)
    if missing_images:
        sample = ", ".join(missing_images[:10])
        raise ValidationError(f"Packaged scene is missing {len(missing_images)} images: {sample}")
    if missing_masks:
        sample = ", ".join(missing_masks[:10])
        raise ValidationError(f"Packaged scene is missing {len(missing_masks)} masks: {sample}")
    return {
        "missing_images": 0,
        "missing_masks": 0,
        "image_file_count": len(image_records),
        "mask_file_count": sum(
            1 for record in image_records
            if _scene_asset_path(
                output_scene,
                "masks",
                str(record.get("mask_name") or record["image_name"]),
            ).is_file()
        ),
    }


def _cleanup_legacy_scene_support_files(output_scene: Path) -> None:
    for filename in (
        "asset_link_report.txt",
        "validation_report.txt",
        "passthrough_media_manifest.json",
    ):
        legacy_path = output_scene / filename
        if legacy_path.is_file():
            legacy_path.unlink()
    for asset_dir_name in ("images", "masks"):
        asset_dir = output_scene / asset_dir_name
        if not asset_dir.is_dir():
            continue
        for metadata_path in asset_dir.rglob("*.meta.json"):
            if metadata_path.is_file():
                metadata_path.unlink()
    legacy_sparse_cache = output_scene / "sparse" / "0" / "undistort_cache"
    if legacy_sparse_cache.is_dir():
        shutil.rmtree(legacy_sparse_cache)


def _default_support_output_dir(output_scene: Path) -> Path:
    if output_scene.name.lower() == "colmap":
        return output_scene.parent / "processing"
    return output_scene.with_name(f"{output_scene.name}_support")


def _default_reports_output_dir(output_scene: Path, support_dir: Path) -> Path:
    if output_scene.name.lower() == "colmap":
        return output_scene.parent / "reports"
    return support_dir


def write_colmap_training_scene(
    metashape_points: Path,
    document: Mapping[str, object],
    discovery: Mapping[str, object],
    output_scene: Path,
    *,
    lens_map: Optional[Mapping[str, object]] = None,
    passthrough_map: Optional[Mapping[str, object]] = None,
    erp_view_map: Optional[Mapping[str, object]] = None,
    adaptive_map: Optional[Mapping[str, object]] = None,
    pose_convention: Optional[str] = None,
    placeholder_poses: bool = False,
    default_point_error: float = 0.0,
    passthrough_camera_model: str = "auto",
    undistort_passthrough: str = "auto",
    passthrough_output_format: str = "jpg",
    strict_pinhole: bool = True,
    package_assets: bool = True,
    force_assets: bool = False,
    support_output_dir: Optional[Path] = None,
    reports_output_dir: Optional[Path] = None,
    keep_processing_files: bool = True,
    progress: bool = False,
    progress_interval: int = 250,
    require_masks: bool = False,
    normalize_scene: bool = False,
    projected_tracks: bool = False,
    track_max_points: int = 0,
    track_max_camera_groups_per_point: int = 8,
    track_max_observations_per_point: int = 6,
    track_min_length: int = 2,
    track_max_observations_per_image: int = 0,
) -> Dict[str, object]:
    if pose_convention is None and not placeholder_poses:
        raise ValidationError("COLMAP scene export needs a pose convention or --allow-placeholder-poses")
    normalized_passthrough_format = passthrough_output_format.lower().lstrip(".")
    if normalized_passthrough_format == "jpeg":
        normalized_passthrough_format = "jpg"
    if normalized_passthrough_format not in {"png", "jpg", "tif", "tiff"}:
        raise ValidationError(
            f"Unsupported passthrough output format: {passthrough_output_format}"
        )
    passthrough_output_format = normalized_passthrough_format

    output_scene.mkdir(parents=True, exist_ok=True)
    _cleanup_legacy_scene_support_files(output_scene)
    support_dir = support_output_dir or _default_support_output_dir(output_scene)
    reports_dir = reports_output_dir or _default_reports_output_dir(output_scene, support_dir)
    remap_cache_dir = support_dir / "remap_cache"
    manifest_dir = support_dir / "manifests"
    logs_dir = support_dir / "logs"
    tmp_dir = support_dir / "tmp"
    support_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    remap_cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    undistort_metadata_root = remap_cache_dir
    if package_assets:
        (output_scene / "images").mkdir(parents=True, exist_ok=True)
        (output_scene / "masks").mkdir(parents=True, exist_ok=True)
    sparse_dir = output_scene / "sparse" / "0"

    sensors: Mapping[int, Mapping[str, object]] = document["sensors"]  # type: ignore[assignment]
    camera_records: List[Dict[str, object]] = []
    image_records: List[Dict[str, object]] = []
    asset_report_lines: List[str] = [
        "Equisolid Metashape To Cubeface COLMAP - Asset Packaging Report",
        "",
        f"output_scene: {output_scene}",
        f"package_assets: {package_assets}",
        f"force_assets: {force_assets}",
        f"keep_processing_files: {keep_processing_files}",
        f"undistort_passthrough: {undistort_passthrough}",
        f"strict_pinhole: {strict_pinhole}",
        "",
    ]
    asset_report_lines.extend(
        _archive_cubeface_support_files(discovery, remap_cache_dir, logs_dir)
    )
    next_camera_id = 1
    packaged_image_count = 0
    packaged_mask_count = 0
    adaptive_image_count = 0
    adaptive_mask_count = 0
    undistorted_passthrough_count = 0
    reused_undistorted_passthrough_count = 0
    passthrough_final_resolutions = []
    used_asset_names: Set[str] = set()

    _emit_progress(progress, "SCENE_EXPORT", 0, 1, "starting")
    camera_world_transform = camera_world_transform_from_document(document)
    cubeface_pose_records = None
    if int(discovery.get("image_count", 0)) > 0:
        _emit_progress(progress, "BUILD_CUBEFACE_POSES", 0, int(discovery.get("image_count", 0)), "starting")
        # Method E: one PINHOLE camera entry per fisheye lens (= per sensor),
        # not one shared across all lenses. Allows multi-fisheye scenes where
        # different sensors use different face widths.
        cube_cameras, cube_camera_id_by_lens, next_camera_id = build_cubeface_camera_records(
            discovery, next_camera_id,
        )
        camera_records.extend(cube_cameras)
        if pose_convention is not None:
            if lens_map is None:
                raise ValidationError("--lens-camera-map is required for real cubeface pose export")
            cubeface_pose_records = build_pose_records(
                document,
                discovery,
                lens_map,
                pose_convention,
                camera_world_transform,
            )
        _skipped = {
            (str(s["lens_label"]), str(s["stem"]))
            for s in (lens_map or {}).get("skipped_unaligned_stems", ())
        } | {
            (str(s["lens_label"]), str(s["stem"]))
            for s in (lens_map or {}).get("skipped_absent_stems", ())
        } or None
        cubeface_records = build_cubeface_image_records(
            discovery,
            camera_id_by_lens=cube_camera_id_by_lens,
            pose_records=cubeface_pose_records,
            placeholder_poses=placeholder_poses,
            skipped_stems=_skipped,
        )
        final_cubefaces, report_lines, image_count, mask_count = _package_cubeface_assets(
            output_scene,
            discovery,
            cubeface_records,
            package_assets=package_assets,
            progress=progress,
            progress_interval=progress_interval,
            skipped_stems=_skipped,
            used_asset_names=used_asset_names,
        )
        image_records.extend(final_cubefaces)
        asset_report_lines.extend(report_lines)
        packaged_image_count += image_count
        packaged_mask_count += mask_count

    # ─── Adaptive single-pinhole block ─────────────────────────────────
    # Path B emits one undistorted PINHOLE image per source fisheye image,
    # retaining the source camera pose directly.
    if adaptive_map is not None and adaptive_map.get("sensors"):
        adaptive_camera_records, adaptive_sensor_camera_ids, next_camera_id = build_adaptive_camera_records(
            adaptive_map, next_camera_id,
        )
        camera_records.extend(adaptive_camera_records)
        raw_adaptive_records = build_adaptive_image_records(
            document,
            adaptive_map,
            adaptive_sensor_camera_ids,
            pose_convention=pose_convention,
            placeholder_poses=placeholder_poses,
            camera_world_transform=camera_world_transform,
        )
        _emit_progress(progress, "PACKAGE_ADAPTIVE", 0, len(raw_adaptive_records), "starting")
        final_adaptive_records, adaptive_report_lines, adaptive_packaged_images, adaptive_packaged_masks = _package_adaptive_assets(
            output_scene,
            raw_adaptive_records,
            package_assets=package_assets,
            used_asset_names=used_asset_names,
        )
        image_records.extend(final_adaptive_records)
        asset_report_lines.extend(adaptive_report_lines)
        packaged_image_count += adaptive_packaged_images
        packaged_mask_count += adaptive_packaged_masks
        adaptive_image_count += len(final_adaptive_records)
        adaptive_mask_count += adaptive_packaged_masks
        _emit_progress(progress, "PACKAGE_ADAPTIVE", len(raw_adaptive_records), len(raw_adaptive_records), "complete")

    # ─── ERP view-slot block ───────────────────────────────────────────
    # One PINHOLE camera per (ERP sensor, view-slot), one image record per
    # (source ERP camera × view-slot). Poses are composed by the shared
    # face_world_to_camera_pose() with the angle-keyed view label resolving
    # to the precomputed basis matrix registered at module load.
    if erp_view_map is not None and erp_view_map.get("sensors"):
        # Method E: one PINHOLE camera entry per ERP sensor (not per view-slot).
        erp_camera_records, erp_sensor_camera_ids, next_camera_id = build_erp_camera_records(
            erp_view_map, next_camera_id,
        )
        camera_records.extend(erp_camera_records)
        raw_erp_records = build_erp_image_records(
            document,
            erp_view_map,
            erp_sensor_camera_ids,
            pose_convention=pose_convention,
            placeholder_poses=placeholder_poses,
            camera_world_transform=camera_world_transform,
        )
        _emit_progress(progress, "PACKAGE_ERP", 0, len(raw_erp_records), "starting")
        final_erp_records, erp_report_lines, erp_image_count, erp_mask_count = _package_erp_assets(
            output_scene,
            raw_erp_records,
            package_assets=package_assets,
            used_asset_names=used_asset_names,
        )
        image_records.extend(final_erp_records)
        asset_report_lines.extend(erp_report_lines)
        packaged_image_count += erp_image_count
        packaged_mask_count += erp_mask_count
        _emit_progress(progress, "PACKAGE_ERP", len(raw_erp_records), len(raw_erp_records), "complete")

    camera_id_by_sensor: Dict[int, int] = {}
    if passthrough_map is not None:
        passthrough_total = int(passthrough_map.get("resolved_count", 0))
        undistort_by_sensor = {}
        for sensor_id in passthrough_map["sensor_ids"]:  # type: ignore[index]
            sensor_id = int(sensor_id)
            sensor = sensors[sensor_id]
            should_undistort = _passthrough_should_undistort(sensor, undistort_passthrough)
            has_distortion = metashape_sensor_has_distortion(sensor)
            if strict_pinhole and has_distortion and not should_undistort:
                raise ValidationError(
                    f"Passthrough sensor {sensor_id} has distortion, but undistortion is disabled "
                    "and --strict-pinhole is enabled"
                )
            final_model_mode = "pinhole" if (strict_pinhole or should_undistort or not has_distortion) else passthrough_camera_model
            camera_id_by_sensor[sensor_id] = next_camera_id
            camera_records.append(
                colmap_camera_from_metashape_sensor(
                    sensor,
                    next_camera_id,
                    model_mode=final_model_mode,
                    centered_principal_point=should_undistort,
                )
            )
            undistort_by_sensor[sensor_id] = should_undistort
            next_camera_id += 1

        for index, resolution in enumerate(passthrough_map["resolutions"], start=1):  # type: ignore[index]
            sensor_id = int(resolution["sensor_id"])
            final_resolution, report_lines, image_count, mask_count, undistorted_count, reused_undistorted_count = _package_passthrough_resolution(
                output_scene,
                resolution,
                sensors[sensor_id],
                package_assets=package_assets,
                undistort=bool(undistort_by_sensor[sensor_id]),
                output_format=passthrough_output_format,
                require_masks=require_masks,
                force_assets=force_assets,
                metadata_root=undistort_metadata_root,
                used_asset_names=used_asset_names,
            )
            passthrough_final_resolutions.append(final_resolution)
            asset_report_lines.extend(report_lines)
            packaged_image_count += image_count
            packaged_mask_count += mask_count
            undistorted_passthrough_count += undistorted_count
            reused_undistorted_passthrough_count += reused_undistorted_count
            if index == passthrough_total or index == 1 or index % max(1, progress_interval) == 0:
                phase = "PASSTHROUGH_UNDISTORT" if bool(undistort_by_sensor[sensor_id]) else "PACKAGE_PASSTHROUGH"
                _emit_progress(progress, phase, index, passthrough_total, str(final_resolution["image_name"]))

        final_passthrough_map = dict(passthrough_map)
        final_passthrough_map["resolutions"] = tuple(passthrough_final_resolutions)
        image_records.extend(
            build_passthrough_image_records(
                document,
                final_passthrough_map,
                camera_id_by_sensor,
                convention=pose_convention,
                placeholder_poses=placeholder_poses,
                camera_world_transform=camera_world_transform,
            )
        )

    if not image_records:
        raise ValidationError("No COLMAP scene images to write")
    image_records = [
        {**record, "image_id": image_id}
        for image_id, record in enumerate(image_records, start=1)
    ]
    if strict_pinhole:
        non_pinhole = [camera for camera in camera_records if str(camera["model"]).upper() != "PINHOLE"]
        if non_pinhole:
            models = ", ".join(f"{camera['camera_id']}:{camera['model']}" for camera in non_pinhole)
            raise ValidationError(f"Strict pinhole scene contains non-PINHOLE cameras: {models}")

    ply_point_transform = point_transform_from_document(document)
    original_scale_metrics = _scene_scale_metrics(metashape_points, image_records, ply_point_transform)
    normalization_transform = None
    normalized_scale_metrics = None
    final_point_transform = ply_point_transform
    if normalize_scene:
        if placeholder_poses:
            raise ValidationError(
                "--normalize-scene cannot be used with placeholder poses. "
                "Scene normalization requires real camera poses."
            )
        normalization_transform = _make_scene_normalization_transform(original_scale_metrics)
        image_records = [
            normalization_transform.transform_image_record(record)
            for record in image_records
        ]
        final_point_transform = _compose_point_transforms(
            ply_point_transform,
            normalization_transform.apply_point,
        )
        normalized_scale_metrics = _scene_scale_metrics(metashape_points, image_records, final_point_transform)

    scene_scale_diagnostics = _build_scene_scale_diagnostics(
        original_scale_metrics,
        normalization_requested=normalize_scene,
        normalization_transform=normalization_transform,
        normalized_metrics=normalized_scale_metrics,
    )
    scene_scale_diagnostics_path = manifest_dir / "scene_scale_diagnostics.json"
    scene_scale_diagnostics_path.write_text(
        json.dumps(scene_scale_diagnostics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    scene_normalization_transform_path = None
    if normalization_transform is not None:
        scene_normalization_transform_path = manifest_dir / "scene_normalization_transform.json"
        scene_normalization_transform_path.write_text(
            json.dumps(normalization_transform.as_manifest(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    tracks = None
    if projected_tracks:
        _emit_progress(progress, "PROJECT_TRACKS", 0, 1, "starting")
        tracks = build_projected_tracks(
            metashape_points,
            camera_records,
            image_records,
            max_points=track_max_points,
            max_camera_groups_per_point=track_max_camera_groups_per_point,
            max_observations_per_point=track_max_observations_per_point,
            min_track_length=track_min_length,
            max_observations_per_image=track_max_observations_per_image,
            default_point_error=default_point_error,
            point_transform=final_point_transform,
        )
        _emit_progress(
            progress,
            "PROJECT_TRACKS",
            int(tracks["tracked_point_count"]),
            int(tracks["candidate_point_count"]),
            f"observations={tracks['total_observation_count']}",
        )

    scene_asset_validation = {"image_file_count": 0, "mask_file_count": 0, "missing_images": 0, "missing_masks": 0}
    if package_assets:
        scene_asset_validation = _validate_packaged_scene_assets(
            output_scene,
            image_records,
            require_masks=require_masks,
        )

    report_lines = [
        "Equisolid Metashape To Cubeface COLMAP - Training Scene Report",
        "",
        (
            f"poses: real poses from convention {pose_convention}"
            if pose_convention is not None
            else "WARNING: poses are placeholders: q=(1,0,0,0), t=(0,0,0)."
        ),
        f"output_scene: {output_scene}",
        f"sparse_dir: {sparse_dir}",
        f"processing_dir: {support_dir}",
        f"reports_dir: {reports_dir}",
        f"keep_processing_files: {keep_processing_files}",
        f"cubeface_root: {discovery.get('root', '')}",
        f"cubeface_images: {discovery.get('image_count', 0)}",
        f"adaptive_images: {adaptive_image_count}",
        f"adaptive_masks: {adaptive_mask_count}",
        f"passthrough_images: {passthrough_map['resolved_count'] if passthrough_map is not None else 0}",
        f"passthrough_masks: {passthrough_map.get('mask_resolved_count', 0) if passthrough_map is not None else 0}",
        f"packaged_images: {packaged_image_count}",
        f"packaged_masks: {packaged_mask_count}",
        f"undistorted_passthrough_images: {undistorted_passthrough_count}",
        f"reused_undistorted_passthrough_images: {reused_undistorted_passthrough_count}",
        f"strict_pinhole: {strict_pinhole}",
        f"default_point_error: {default_point_error}",
        (
            f"camera_world_transform: {camera_world_transform['description']}"
            if camera_world_transform is not None
            else "camera_world_transform: none"
        ),
        (
            "ply_point_transform: WGS84 geographic PLY -> Metashape local coordinates"
            if ply_point_transform is not None
            else "ply_point_transform: none"
        ),
    ]
    report_lines.extend(
        _scene_scale_report_lines(
            scene_scale_diagnostics,
            diagnostics_path=scene_scale_diagnostics_path,
            normalization_transform_path=scene_normalization_transform_path,
        )
    )
    if passthrough_map is not None:
        report_lines.extend([
            "",
            "passthrough_media_sets:",
        ])
        for media_set in passthrough_map.get("media_sets", ()):
            slug = str(media_set["slug"])
            report_lines.append(
                f"  - {media_set['name']} ({slug}): "
                f"images={passthrough_map.get('media_set_image_counts', {}).get(slug, 0)} "
                f"masks={passthrough_map.get('media_set_mask_counts', {}).get(slug, 0)}"
            )
    if tracks is not None:
        report_lines.extend([
            "",
            "projected_tracks: enabled",
            f"track_candidate_points: {tracks['candidate_point_count']}",
            f"track_points_written: {tracks['tracked_point_count']}",
            f"track_observations_written: {tracks['total_observation_count']}",
            f"track_images_with_observations: {tracks['images_with_observations']}",
            f"track_min_length: {tracks['min_track_length']}",
            f"track_max_observations_per_point: {tracks['max_observations_per_point']}",
            f"track_max_camera_groups_per_point: {tracks['max_camera_groups_per_point']}",
            f"track_max_observations_per_image: {tracks['max_observations_per_image']}",
            "track_note: projected observations are synthetic, not original Metashape tie-point tracks.",
        ])

    _emit_progress(progress, "WRITE_COLMAP_MODEL", 0, 1, str(sparse_dir))
    legacy_conversion_report = sparse_dir / "conversion_report.txt"
    if legacy_conversion_report.is_file():
        legacy_conversion_report.unlink()
    result = _write_colmap_text_model(
        metashape_points,
        sparse_dir,
        camera_records,
        image_records,
        default_point_error=default_point_error,
        report_lines=report_lines,
        report_path=reports_dir / "conversion_report.txt",
        projected_tracks=tracks,
        point_transform=None if tracks is not None else final_point_transform,
    )
    _emit_progress(progress, "WRITE_COLMAP_MODEL", 1, 1, str(sparse_dir))
    asset_report_path = logs_dir / "asset_link_report.txt"
    asset_report_path.write_text("\n".join(asset_report_lines) + "\n", encoding="utf-8", newline="\n")
    validation_report_path = reports_dir / "validation_report.txt"
    validation_report_lines = [
        "Equisolid Metashape To Cubeface COLMAP - Scene Validation Report",
        "",
        f"camera_models: {','.join(sorted({str(camera['model']).upper() for camera in camera_records}))}",
        f"all_cameras_pinhole: {all(str(camera['model']).upper() == 'PINHOLE' for camera in camera_records)}",
        f"images_in_model: {len(image_records)}",
        f"image_files: {scene_asset_validation['image_file_count']}",
        f"mask_files: {scene_asset_validation['mask_file_count']}",
        f"missing_images: {scene_asset_validation['missing_images']}",
        f"missing_masks: {scene_asset_validation['missing_masks']}",
    ]
    validation_report_path.write_text("\n".join(validation_report_lines) + "\n", encoding="utf-8", newline="\n")
    run_summary_path = reports_dir / "run_summary.txt"
    run_summary_lines = [
        "Equisolid Metashape To Cubeface COLMAP - Run Summary",
        "",
        f"final_colmap_scene: {output_scene}",
        f"processing_dir: {support_dir}",
        f"reports_dir: {reports_dir}",
        f"images: {len(image_records)}",
        f"cameras: {len(camera_records)}",
        f"points: {result['point_count']}",
        f"packaged_images: {packaged_image_count}",
        f"packaged_masks: {packaged_mask_count}",
        f"conversion_report: {result['report_path']}",
        f"validation_report: {validation_report_path}",
        f"asset_log: {asset_report_path}",
        f"scene_scale_diagnostics: {scene_scale_diagnostics_path}",
        (
            f"scene_normalization_transform: {scene_normalization_transform_path}"
            if scene_normalization_transform_path is not None
            else "scene_normalization_transform: none"
        ),
        f"processing_files_kept: {keep_processing_files}",
    ]
    run_summary_path.write_text("\n".join(run_summary_lines) + "\n", encoding="utf-8", newline="\n")
    result.update({
        "output_scene": str(output_scene),
        "sparse_dir": str(sparse_dir),
        "support_output_dir": str(support_dir),
        "reports_output_dir": str(reports_dir),
        "remap_cache_dir": str(remap_cache_dir),
        "manifest_dir": str(manifest_dir),
        "logs_dir": str(logs_dir),
        "asset_report_path": str(asset_report_path),
        "validation_report_path": str(validation_report_path),
        "run_summary_path": str(run_summary_path),
        "placeholder_poses": pose_convention is None,
        "pose_convention": pose_convention,
        "cubeface_image_count": int(discovery.get("image_count", 0)),
        "adaptive_image_count": adaptive_image_count,
        "adaptive_mask_count": adaptive_mask_count,
        "passthrough_image_count": passthrough_map["resolved_count"] if passthrough_map is not None else 0,
        "passthrough_mask_count": passthrough_map.get("mask_resolved_count", 0) if passthrough_map is not None else 0,
        "packaged_image_count": packaged_image_count,
        "packaged_mask_count": packaged_mask_count,
        "undistorted_passthrough_image_count": undistorted_passthrough_count,
        "reused_undistorted_passthrough_image_count": reused_undistorted_passthrough_count,
        "strict_pinhole": strict_pinhole,
        "camera_world_transform": camera_world_transform is not None,
        "projected_tracks": tracks is not None,
        "scene_scale_diagnostics_path": str(scene_scale_diagnostics_path),
        "scene_normalization_requested": bool(normalize_scene),
        "scene_normalization_applied": normalization_transform is not None,
        "scene_normalization_transform_path": (
            str(scene_normalization_transform_path)
            if scene_normalization_transform_path is not None
            else ""
        ),
        "scene_scale_warning_count": len(scene_scale_diagnostics.get("warnings", ())),
        "scene_camera_radius_p95": (
            normalized_scale_metrics or original_scale_metrics
        ).get("camera_radius_p95"),
        "scene_point_to_camera_radius_ratio": original_scale_metrics.get("point_to_camera_radius_ratio"),
    })
    if tracks is not None:
        result.update({
            "track_candidate_points": tracks["candidate_point_count"],
            "track_observation_count": tracks["total_observation_count"],
            "images_with_observations": tracks["images_with_observations"],
        })
    if tmp_dir.is_dir():
        shutil.rmtree(tmp_dir)
    if not keep_processing_files:
        for path in (remap_cache_dir, manifest_dir, logs_dir):
            if path.is_dir():
                shutil.rmtree(path)
    _emit_progress(progress, "SCENE_EXPORT", 1, 1, "complete")
    return result


def write_colmap_mixed_scene(
    metashape_points: Path,
    document: Mapping[str, object],
    discovery: Mapping[str, object],
    output_dir: Path,
    *,
    lens_map: Optional[Mapping[str, object]] = None,
    passthrough_map: Optional[Mapping[str, object]] = None,
    pose_convention: Optional[str] = None,
    placeholder_poses: bool = False,
    default_point_error: float = 0.0,
    passthrough_camera_model: str = "auto",
    projected_tracks: bool = False,
    track_max_points: int = 0,
    track_max_camera_groups_per_point: int = 8,
    track_max_observations_per_point: int = 6,
    track_min_length: int = 2,
    track_max_observations_per_image: int = 0,
) -> Dict[str, object]:
    if pose_convention is None and not placeholder_poses:
        raise ValidationError("COLMAP export needs a pose convention or --allow-placeholder-poses")
    sensors: Mapping[int, Mapping[str, object]] = document["sensors"]  # type: ignore[assignment]
    camera_records: List[Dict[str, object]] = []
    image_records: List[Dict[str, object]] = []
    next_camera_id = 1
    camera_world_transform = camera_world_transform_from_document(document)
    cubeface_pose_records = None
    if int(discovery.get("image_count", 0)) > 0:
        # Method E: one PINHOLE camera entry per fisheye lens; see
        # build_cubeface_camera_records docstring for rationale.
        cube_cameras, cube_camera_id_by_lens, next_camera_id = build_cubeface_camera_records(
            discovery, next_camera_id,
        )
        camera_records.extend(cube_cameras)
        if pose_convention is not None:
            if lens_map is None:
                raise ValidationError("--lens-camera-map is required for real cubeface pose export")
            cubeface_pose_records = build_pose_records(
                document,
                discovery,
                lens_map,
                pose_convention,
                camera_world_transform,
            )
        _skipped2 = {
            (str(s["lens_label"]), str(s["stem"]))
            for s in (lens_map or {}).get("skipped_unaligned_stems", ())
        } | {
            (str(s["lens_label"]), str(s["stem"]))
            for s in (lens_map or {}).get("skipped_absent_stems", ())
        } or None
        image_records.extend(
            build_cubeface_image_records(
                discovery,
                camera_id_by_lens=cube_camera_id_by_lens,
                pose_records=cubeface_pose_records,
                placeholder_poses=placeholder_poses,
                skipped_stems=_skipped2,
            )
        )

    camera_id_by_sensor: Dict[int, int] = {}
    if passthrough_map is not None:
        for sensor_id in passthrough_map["sensor_ids"]:  # type: ignore[index]
            sensor_id = int(sensor_id)
            camera_id_by_sensor[sensor_id] = next_camera_id
            camera_records.append(
                colmap_camera_from_metashape_sensor(
                    sensors[sensor_id],
                    next_camera_id,
                    model_mode=passthrough_camera_model,
                )
            )
            next_camera_id += 1
        image_records.extend(
            build_passthrough_image_records(
                document,
                passthrough_map,
                camera_id_by_sensor,
                convention=pose_convention,
                placeholder_poses=placeholder_poses,
                camera_world_transform=camera_world_transform,
            )
        )

    if not image_records:
        raise ValidationError("No COLMAP images to write")
    image_records, image_name_root = _normalize_image_names_to_common_root(image_records)
    image_records = [
        {**record, "image_id": image_id}
        for image_id, record in enumerate(image_records, start=1)
    ]
    ply_point_transform = point_transform_from_document(document)
    tracks = None
    if projected_tracks:
        tracks = build_projected_tracks(
            metashape_points,
            camera_records,
            image_records,
            max_points=track_max_points,
            max_camera_groups_per_point=track_max_camera_groups_per_point,
            max_observations_per_point=track_max_observations_per_point,
            min_track_length=track_min_length,
            max_observations_per_image=track_max_observations_per_image,
            default_point_error=default_point_error,
            point_transform=ply_point_transform,
        )

    report_lines = [
        "Equisolid Metashape To Cubeface COLMAP - Mixed Scene Report",
        "",
        (
            f"poses: real poses from convention {pose_convention}"
            if pose_convention is not None
            else "WARNING: poses are placeholders: q=(1,0,0,0), t=(0,0,0)."
        ),
        f"cubeface_root: {discovery.get('root', '')}",
        f"image_name_root: {image_name_root if image_name_root is not None else ''}",
        f"cubeface_images: {discovery.get('image_count', 0)}",
        f"passthrough_images: {passthrough_map['resolved_count'] if passthrough_map is not None else 0}",
        f"output_dir: {output_dir}",
        f"default_point_error: {default_point_error}",
        (
            f"camera_world_transform: {camera_world_transform['description']}"
            if camera_world_transform is not None
            else "camera_world_transform: none"
        ),
        (
            "ply_point_transform: WGS84 geographic PLY -> Metashape local coordinates"
            if ply_point_transform is not None
            else "ply_point_transform: none"
        ),
    ]
    if tracks is not None:
        report_lines.extend([
            "",
            "projected_tracks: enabled",
            f"track_candidate_points: {tracks['candidate_point_count']}",
            f"track_points_written: {tracks['tracked_point_count']}",
            f"track_observations_written: {tracks['total_observation_count']}",
            f"track_images_with_observations: {tracks['images_with_observations']}",
            f"track_min_length: {tracks['min_track_length']}",
            f"track_max_observations_per_point: {tracks['max_observations_per_point']}",
            f"track_max_camera_groups_per_point: {tracks['max_camera_groups_per_point']}",
            f"track_max_observations_per_image: {tracks['max_observations_per_image']}",
            "track_note: projected observations are synthetic, not original Metashape tie-point tracks.",
        ])
    result = _write_colmap_text_model(
        metashape_points,
        output_dir,
        camera_records,
        image_records,
        default_point_error=default_point_error,
        report_lines=report_lines,
        projected_tracks=tracks,
        point_transform=ply_point_transform,
    )
    result.update({
        "placeholder_poses": pose_convention is None,
        "pose_convention": pose_convention,
        "image_name_root": str(image_name_root) if image_name_root is not None else None,
        "cubeface_image_count": int(discovery.get("image_count", 0)),
        "passthrough_image_count": passthrough_map["resolved_count"] if passthrough_map is not None else 0,
        "camera_world_transform": camera_world_transform is not None,
        "projected_tracks": tracks is not None,
    })
    if tracks is not None:
        result.update({
            "track_candidate_points": tracks["candidate_point_count"],
            "track_observation_count": tracks["total_observation_count"],
            "images_with_observations": tracks["images_with_observations"],
        })
    return result


def write_colmap_skeleton(
    metashape_points: Path,
    discovery: Mapping[str, object],
    output_dir: Path,
    *,
    default_point_error: float = 0.0,
    placeholder_poses: bool = False,
    pose_records: Optional[Sequence[Mapping[str, object]]] = None,
    pose_convention: Optional[str] = None,
    lens_map: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """Write parser-valid COLMAP text files with empty observations.

    Without pose records, callers must opt into placeholder poses so skeleton
    files cannot be mistaken for a geometry-valid final export.
    """
    if pose_records is None and not placeholder_poses:
        raise ValidationError(
            "COLMAP skeleton writing currently requires placeholder_poses=True. "
            "Real pose export is gated behind transform-convention validation."
        )

    camera = _single_cubeface_camera(discovery)
    images = ordered_cubeface_images(discovery)
    _skip = {
        (str(s["lens_label"]), str(s["stem"]))
        for s in (lens_map or {}).get("skipped_unaligned_stems", ())
    } | {
        (str(s["lens_label"]), str(s["stem"]))
        for s in (lens_map or {}).get("skipped_absent_stems", ())
    }
    if _skip:
        images = [
            img for img in images
            if (str(img["lens_label"]), str(img["stem"])) not in _skip
        ]
    if pose_records is not None and len(pose_records) != len(images):
        raise ValidationError(
            f"Pose record count {len(pose_records)} does not match image count {len(images)}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    cameras_path = output_dir / "cameras.txt"
    images_path = output_dir / "images.txt"
    points_path = output_dir / "points3D.txt"
    report_path = output_dir / "conversion_report.txt"
    cubeface_root = Path(str(discovery["root"]))

    with cameras_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Camera list with one line of data per camera:\n")
        handle.write("#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n")
        handle.write("# Number of cameras: 1\n")
        params = " ".join(f"{value:.17g}" for value in camera["params"])
        handle.write(
            f"{camera['camera_id']} {camera['model']} {camera['width']} {camera['height']} {params}\n"
        )

    with images_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Image list with two lines of data per image:\n")
        handle.write("#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, IMAGE_NAME\n")
        handle.write("#   POINTS2D[] as (X, Y, POINT3D_ID)\n")
        handle.write(f"# Number of images: {len(images)}, mean observations per image: 0\n")
        for image_id, image in enumerate(images, start=1):
            image_name = str(
                image.get("image_name")
                or _relative_colmap_image_name(Path(str(image["image_path"])), cubeface_root)
            )
            if pose_records is None:
                qvec = (1.0, 0.0, 0.0, 0.0)
                tvec = (0.0, 0.0, 0.0)
            else:
                qvec = pose_records[image_id - 1]["qvec"]  # type: ignore[index]
                tvec = pose_records[image_id - 1]["tvec"]  # type: ignore[index]
            q = " ".join(f"{float(value):.17g}" for value in qvec)
            t = " ".join(f"{float(value):.17g}" for value in tvec)
            handle.write(f"{image_id} {q} {t} {camera['camera_id']} {image_name}\n")
            handle.write("\n")

    point_count = 0
    with points_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# 3D point list with one line of data per point:\n")
        handle.write("#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[]\n")
        handle.write("# Number of points: pending, mean track length: 0\n")
        for point_id, (x, y, z, r, g, b) in enumerate(iter_ply_points(metashape_points), start=1):
            handle.write(
                f"{point_id} {x:.17g} {y:.17g} {z:.17g} "
                f"{r} {g} {b} {default_point_error:.17g}\n"
            )
            point_count = point_id

    report_lines = [
        "Equisolid Metashape To Cubeface COLMAP - Phase 3 Skeleton Report",
        "",
        (
            "poses: real cubeface poses from validated convention "
            f"{pose_convention}"
            if pose_records is not None
            else "WARNING: poses are placeholders: q=(1,0,0,0), t=(0,0,0)."
        ),
        (
            "Pose export passed the transform-convention validation gate."
            if pose_records is not None
            else "Real pose export is intentionally gated behind transform-convention validation."
        ),
        "",
        f"cubeface_root: {discovery['root']}",
        f"output_dir: {output_dir}",
        f"camera_model: {camera['model']}",
        f"camera_width: {camera['width']}",
        f"camera_height: {camera['height']}",
        f"camera_params: {camera['params']}",
        f"image_count: {len(images)}",
        f"point_count: {point_count}",
        f"default_point_error: {default_point_error}",
    ]
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8", newline="\n")

    return {
        "output_dir": str(output_dir),
        "cameras_path": str(cameras_path),
        "images_path": str(images_path),
        "points3D_path": str(points_path),
        "report_path": str(report_path),
        "camera_count": 1,
        "image_count": len(images),
        "point_count": point_count,
        "placeholder_poses": pose_records is None,
        "pose_convention": pose_convention,
    }


def parse_colmap_cameras(path: Path) -> Dict[int, Dict[str, object]]:
    cameras = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 5:
            raise ValidationError(f"Invalid cameras.txt line: {line}")
        camera_id = int(parts[0])
        cameras[camera_id] = {
            "model": parts[1],
            "width": int(parts[2]),
            "height": int(parts[3]),
            "params": tuple(float(value) for value in parts[4:]),
        }
    return cameras


def parse_colmap_images(path: Path) -> Dict[int, Dict[str, object]]:
    lines = [
        line.rstrip("\n")
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    if len(lines) % 2 != 0:
        raise ValidationError("images.txt must contain two non-comment lines per image")
    images = {}
    for index in range(0, len(lines), 2):
        header = lines[index].split(maxsplit=9)
        if len(header) != 10:
            raise ValidationError(f"Invalid images.txt image header: {lines[index]}")
        image_id = int(header[0])
        images[image_id] = {
            "qvec": tuple(float(value) for value in header[1:5]),
            "tvec": tuple(float(value) for value in header[5:8]),
            "camera_id": int(header[8]),
            "image_name": header[9],
            "points2D": lines[index + 1].split(),
        }
    return images


def parse_colmap_points3D(path: Path) -> Dict[int, Dict[str, object]]:
    points = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 8:
            raise ValidationError(f"Invalid points3D.txt line: {line}")
        point_id = int(parts[0])
        points[point_id] = {
            "xyz": tuple(float(value) for value in parts[1:4]),
            "rgb": tuple(int(value) for value in parts[4:7]),
            "error": float(parts[7]),
            "track": tuple(parts[8:]),
        }
    return points


def validate_colmap_skeleton(output_dir: Path, expected_images: int, expected_points: int) -> Dict[str, object]:
    cameras = parse_colmap_cameras(output_dir / "cameras.txt")
    images = parse_colmap_images(output_dir / "images.txt")
    points = parse_colmap_points3D(output_dir / "points3D.txt")
    if len(cameras) != 1:
        raise ValidationError(f"Expected 1 camera, found {len(cameras)}")
    if len(images) != expected_images:
        raise ValidationError(f"Expected {expected_images} images, found {len(images)}")
    if len(points) != expected_points:
        raise ValidationError(f"Expected {expected_points} points, found {len(points)}")
    missing_camera_ids = sorted({image["camera_id"] for image in images.values()} - set(cameras))
    if missing_camera_ids:
        raise ValidationError(f"images.txt references missing camera ids: {missing_camera_ids}")
    return {
        "camera_count": len(cameras),
        "image_count": len(images),
        "point_count": len(points),
    }


def validate_colmap_model(
    output_dir: Path,
    expected_cameras: int,
    expected_images: int,
    expected_points: int,
) -> Dict[str, object]:
    cameras = parse_colmap_cameras(output_dir / "cameras.txt")
    images = parse_colmap_images(output_dir / "images.txt")
    points = parse_colmap_points3D(output_dir / "points3D.txt")
    if len(cameras) != expected_cameras:
        raise ValidationError(f"Expected {expected_cameras} cameras, found {len(cameras)}")
    if len(images) != expected_images:
        raise ValidationError(f"Expected {expected_images} images, found {len(images)}")
    if len(points) != expected_points:
        raise ValidationError(f"Expected {expected_points} points, found {len(points)}")
    missing_camera_ids = sorted({image["camera_id"] for image in images.values()} - set(cameras))
    if missing_camera_ids:
        raise ValidationError(f"images.txt references missing camera ids: {missing_camera_ids}")
    return {
        "camera_count": len(cameras),
        "image_count": len(images),
        "point_count": len(points),
    }


def summarize_document(document: Mapping[str, object]) -> Dict[str, object]:
    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    sensors: Mapping[int, Mapping[str, object]] = document["sensors"]  # type: ignore[assignment]
    orth_errors = [float(camera["rotation_orthonormal_error"]) for camera in cameras.values()]
    duplicate_labels: Mapping[str, Sequence[int]] = document["duplicate_labels"]  # type: ignore[assignment]
    labels_to_ids: Mapping[str, Sequence[int]] = document["labels_to_ids"]  # type: ignore[assignment]
    camera_counts_by_sensor = Counter(int(camera["sensor_id"]) for camera in cameras.values())
    export_path_counts = Counter()
    unsupported_sensor_ids = []
    for sensor_id, sensor in sensors.items():
        export_path = str(sensor.get("export_path", "unsupported"))
        export_path_counts[export_path] += camera_counts_by_sensor.get(int(sensor_id), 0)
        if export_path == "unsupported":
            unsupported_sensor_ids.append(int(sensor_id))
    for sensor_id, count in camera_counts_by_sensor.items():
        if int(sensor_id) not in sensors:
            export_path_counts["unsupported"] += count
            unsupported_sensor_ids.append(int(sensor_id))
    return {
        "path": document["path"],
        "version": document["version"],
        "chunk_label": document["chunk_label"],
        "sensor_count": len(sensors),
        "camera_count": len(cameras),
        "unique_label_count": len(labels_to_ids),
        "duplicate_label_count": len(duplicate_labels),
        "duplicate_label_samples": dict(list(sorted(duplicate_labels.items()))[:5]),
        "max_rotation_orthonormal_error": max(orth_errors) if orth_errors else None,
        "camera_counts_by_sensor": {
            str(key): camera_counts_by_sensor[key] for key in sorted(camera_counts_by_sensor)
        },
        "export_path_counts": dict(sorted(export_path_counts.items())),
        "unsupported_sensor_ids": tuple(sorted(unsupported_sensor_ids)),
        "sensors": {str(key): value for key, value in sorted(sensors.items())},
    }


def inspect_inputs(
    metashape_cameras: Path,
    metashape_points: Path,
    cubeface_root: Optional[Path] = None,
    lens_camera_map: Optional[str] = None,
    passthrough_image_roots: Optional[Sequence[Path]] = None,
    passthrough_sensor_ids: Optional[Sequence[int]] = None,
    dual_fisheye_raw_root: Optional[Path] = None,
    passthrough_media_manifest: Optional[Path] = None,
    require_masks: bool = False,
) -> Dict[str, object]:
    document = parse_metashape_cameras_xml(metashape_cameras)
    ply = parse_ply_summary(metashape_points)
    discovery = discover_cubefaces(cubeface_root) if cubeface_root is not None else empty_cubeface_discovery()
    warnings = []
    lens_map = None
    if lens_camera_map and int(discovery["image_count"]) > 0:
        lens_map = validate_lens_camera_map(document, discovery, parse_lens_camera_map(lens_camera_map))
        skipped = lens_map.get("skipped_unaligned_stems", ())
        if skipped:
            stems_list = ", ".join(s["stem"] for s in skipped)
            warnings.append(f"Skipped {len(skipped)} cubeface stems with unaligned source cameras: {stems_list}")
        skipped_absent = lens_map.get("skipped_absent_stems", ())
        if skipped_absent:
            stems_list = ", ".join(s["stem"] for s in skipped_absent)
            warnings.append(f"Skipped {len(skipped_absent)} cubeface stems absent from Metashape XML: {stems_list}")
    elif int(discovery["image_count"]) > 0:
        warnings.append("No --lens-camera-map supplied; lens-to-camera resolution was not validated.")

    for lens in discovery["lenses"]:
        report = lens["run_report"]
        if not report:
            continue
        report_size = (report["width"], report["height"])
        if report["width"] is not None and report["height"] is not None and report_size not in lens["face_size_set"]:
            warnings.append(f"Run report size {report_size} does not match discovered sizes for {lens['lens_label']}")
        if report["processed"] is not None and report["processed"] != len(lens["stems"]):
            warnings.append(
                f"Run report processed={report['processed']} but discovered {len(lens['stems'])} stems for {lens['lens_label']}"
            )

    passthrough_discovery = None
    passthrough_map = None
    passthrough_media_sets = None
    if passthrough_media_manifest is not None:
        passthrough_media_sets = load_passthrough_media_manifest(passthrough_media_manifest)
        passthrough_discovery = discover_passthrough_media_sets(passthrough_media_sets)
        passthrough_map = resolve_passthrough_media_sets(
            document,
            passthrough_media_sets,
            passthrough_sensor_ids,
            require_masks=require_masks,
        )
        extra_count = len(passthrough_map["extra_image_stems"])
        if extra_count:
            warnings.append(f"Passthrough media sets contain {extra_count} extra images not present in the XML.")
    elif passthrough_image_roots:
        passthrough_discovery = discover_passthrough_images(passthrough_image_roots)
        passthrough_map = validate_passthrough_images(document, passthrough_discovery, passthrough_sensor_ids)
        extra_count = len(passthrough_map["extra_image_stems"])
        if extra_count:
            warnings.append(f"Passthrough image roots contain {extra_count} extra images not present in the XML.")

    raw_lens_map_scaffold = None
    if dual_fisheye_raw_root is not None:
        raw_lens_map_scaffold = infer_dual_fisheye_lens_map_from_raw(document, dual_fisheye_raw_root)

    result = {
        "metashape_xml": summarize_document(document),
        "ply": ply,
        "cubefaces": discovery,
        "lens_map": lens_map,
        "passthrough_images": passthrough_discovery,
        "passthrough_map": passthrough_map,
        "passthrough_media_sets": passthrough_media_sets,
        "metashape_camera_runs": metashape_camera_runs(document),
        "raw_lens_map_scaffold": raw_lens_map_scaffold,
        "warnings": tuple(warnings),
    }
    return result


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"Cannot JSON serialize {type(value)!r}")


def _compact_lens_summary(lens: Mapping[str, object]) -> Dict[str, object]:
    stems = tuple(str(stem) for stem in lens.get("stems", ()))
    return {
        "lens_label": lens.get("lens_label"),
        "source_lens_label": lens.get("source_lens_label"),
        "layout": lens.get("layout"),
        "image_count": lens.get("image_count"),
        "mask_count": lens.get("mask_count"),
        "stem_count": len(stems),
        "first_stem": stems[0] if stems else None,
        "last_stem": stems[-1] if stems else None,
        "face_size_set": lens.get("face_size_set"),
    }


def _compact_lens_map(lens_map: Optional[Mapping[str, object]]) -> Optional[Dict[str, object]]:
    if not lens_map:
        return None
    return {
        "resolved_count": lens_map.get("resolved_count"),
        "skipped_unaligned_stem_count": len(lens_map.get("skipped_unaligned_stems", ())),
        "skipped_absent_stem_count": len(lens_map.get("skipped_absent_stems", ())),
        "unused_mapped_camera_count": len(lens_map.get("unused_mapped_camera_ids", ())),
        "unused_xml_camera_count": len(lens_map.get("unused_xml_camera_ids", ())),
    }


def _compact_passthrough_discovery(discovery: Optional[Mapping[str, object]]) -> Optional[Dict[str, object]]:
    if not discovery:
        return None
    duplicate_stems = discovery.get("duplicate_stems", {})
    return {
        "roots": discovery.get("roots", ()),
        "image_count": discovery.get("image_count", 0),
        "mask_count": discovery.get("mask_count", 0),
        "unique_stem_count": discovery.get("unique_stem_count", 0),
        "duplicate_stem_count": len(duplicate_stems) if isinstance(duplicate_stems, Mapping) else 0,
    }


def _compact_passthrough_map(passthrough_map: Optional[Mapping[str, object]]) -> Optional[Dict[str, object]]:
    if not passthrough_map:
        return None
    media_sets = []
    for media_set in passthrough_map.get("media_sets", ()):
        media_sets.append({
            "name": media_set.get("name"),
            "slug": media_set.get("slug"),
            "image_root": str(media_set.get("image_root")),
            "mask_root": str(media_set.get("mask_root")) if media_set.get("mask_root") is not None else None,
        })
    return {
        "sensor_ids": passthrough_map.get("sensor_ids", ()),
        "resolved_count": passthrough_map.get("resolved_count", 0),
        "mask_resolved_count": passthrough_map.get("mask_resolved_count", 0),
        "extra_image_stem_count": len(passthrough_map.get("extra_image_stems", ())),
        "media_set_image_counts": passthrough_map.get("media_set_image_counts", {}),
        "media_set_mask_counts": passthrough_map.get("media_set_mask_counts", {}),
        "media_sets": tuple(media_sets),
    }


def _compact_camera_runs(runs: Sequence[Mapping[str, object]]) -> Tuple[Dict[str, object], ...]:
    compact = []
    for run in runs:
        compact.append({
            "sensor_id": run.get("sensor_id"),
            "export_path": run.get("export_path"),
            "count": run.get("count"),
            "start_camera_id": run.get("start_camera_id"),
            "end_camera_id": run.get("end_camera_id"),
            "start_label": run.get("start_label"),
            "end_label": run.get("end_label"),
        })
    return tuple(compact)


def compact_json_summary(summary: Mapping[str, object]) -> Dict[str, object]:
    cubefaces = summary["cubefaces"]
    compact: Dict[str, object] = {
        "metashape_xml": summary.get("metashape_xml"),
        "ply": summary.get("ply"),
        "cubefaces": {
            "root": cubefaces.get("root"),
            "lens_count": cubefaces.get("lens_count"),
            "image_count": cubefaces.get("image_count"),
            "layout_counts": cubefaces.get("layout_counts"),
            "lenses": tuple(_compact_lens_summary(lens) for lens in cubefaces.get("lenses", ())),
        },
        "lens_map": _compact_lens_map(summary.get("lens_map")),  # type: ignore[arg-type]
        "passthrough_images": _compact_passthrough_discovery(summary.get("passthrough_images")),  # type: ignore[arg-type]
        "passthrough_map": _compact_passthrough_map(summary.get("passthrough_map")),  # type: ignore[arg-type]
        "metashape_camera_runs": _compact_camera_runs(summary.get("metashape_camera_runs", ())),  # type: ignore[arg-type]
        "raw_lens_map_scaffold": summary.get("raw_lens_map_scaffold"),
        "warnings": summary.get("warnings", ()),
    }
    for key in ("pose_validation", "colmap_skeleton", "colmap_scene"):
        if summary.get(key):
            compact[key] = summary[key]
    return compact


def print_human_summary(summary: Mapping[str, object]) -> None:
    xml = summary["metashape_xml"]
    ply = summary["ply"]
    cubefaces = summary["cubefaces"]
    print("Metashape XML")
    print(f"  path: {xml['path']}")
    print(f"  version: {xml['version']}")
    print(f"  chunk: {xml['chunk_label']}")
    print(f"  cameras: {xml['camera_count']} ({xml['unique_label_count']} unique labels)")
    print(f"  duplicate labels: {xml['duplicate_label_count']}")
    print(f"  export paths: {xml['export_path_counts']}")
    print(f"  max rotation orthonormal error: {xml['max_rotation_orthonormal_error']}")
    print("")
    print("PLY")
    print(f"  path: {ply['path']}")
    print(f"  format: {ply['format']}")
    print(f"  vertices: {ply['vertex_count']}")
    print(f"  bounds min: {ply['bounds_min']}")
    print(f"  bounds max: {ply['bounds_max']}")
    print(f"  mean rgb: {ply['mean_rgb']}")
    print("")
    print("Cubefaces")
    print(f"  root: {cubefaces['root']}")
    print(f"  lenses: {cubefaces['lens_count']}")
    print(f"  images: {cubefaces['image_count']}")
    print(f"  layouts: {cubefaces['layout_counts']}")
    for lens in cubefaces["lenses"]:
        print(
            f"  - {lens['lens_label']}: layout={lens['layout']} stems={list(lens['stems'])} "
            f"images={lens['image_count']} masks={lens['mask_count']} sizes={list(lens['face_size_set'])}"
        )
        if lens["run_report"]:
            report = lens["run_report"]
            print(
                f"    run_report: processed={report['processed']} skipped={report['skipped']} "
                f"f={report['focal']} size=({report['width']}, {report['height']}) angle={report['max_angle_deg']}"
            )
    if summary["lens_map"]:
        lens_map = summary["lens_map"]
        print("")
        print("Lens-camera map")
        print(f"  resolved stems: {lens_map['resolved_count']}")
        skipped_unaligned = lens_map.get("skipped_unaligned_stems", ())
        if skipped_unaligned:
            print(f"  skipped unaligned stems: {len(skipped_unaligned)}")
            for s in skipped_unaligned:
                print(f"    - {s['lens_label']} stem {s['stem']}")
        skipped_absent = lens_map.get("skipped_absent_stems", ())
        if skipped_absent:
            print(f"  skipped absent stems (not in XML): {len(skipped_absent)}")
            for s in skipped_absent:
                print(f"    - {s['lens_label']} stem {s['stem']}")
        print(f"  unused mapped cameras: {len(lens_map['unused_mapped_camera_ids'])}")
        for item in lens_map["resolutions"]:
            print(f"  - {item['lens_label']} stem {item['stem']} -> camera {item['camera_id']}")
    if summary.get("passthrough_map"):
        passthrough_images = summary["passthrough_images"]
        passthrough_map = summary["passthrough_map"]
        print("")
        print("Passthrough images")
        if summary.get("passthrough_media_sets"):
            print("  media sets:")
            for media_set in summary["passthrough_media_sets"]:
                print(
                    f"    - {media_set['name']}: images={media_set['image_root']} "
                    f"masks={media_set['mask_root'] or ''}"
                )
        else:
            print(f"  roots: {list(passthrough_images['roots'])}")
        print(f"  discovered images: {passthrough_images['image_count']}")
        if "mask_count" in passthrough_images:
            print(f"  discovered masks: {passthrough_images['mask_count']}")
        print(f"  resolved XML cameras: {passthrough_map['resolved_count']}")
        if "mask_resolved_count" in passthrough_map:
            print(f"  resolved masks: {passthrough_map['mask_resolved_count']}")
        print(f"  sensors: {list(passthrough_map['sensor_ids'])}")
        print(f"  extra raw images: {len(passthrough_map['extra_image_stems'])}")
    if summary.get("raw_lens_map_scaffold"):
        scaffold = summary["raw_lens_map_scaffold"]
        print("")
        print("Raw dual-fisheye lens-map scaffold")
        print(f"  assumption: {scaffold['assumption']}")
        print(f"  spec: {scaffold['mapping_spec']}")
    if summary.get("metashape_camera_runs"):
        print("")
        print("Metashape camera runs")
        for run in summary["metashape_camera_runs"]:
            print(
                f"  - sensor={run['sensor_id']} export={run['export_path']} "
                f"count={run['count']} ids={run['start_camera_id']}-{run['end_camera_id']} "
                f"labels={run['start_label']}-{run['end_label']}"
            )
    if summary["warnings"]:
        print("")
        print("Warnings")
        for warning in summary["warnings"]:
            print(f"  - {warning}")
    if summary.get("pose_validation"):
        pose_validation = summary["pose_validation"]
        print("")
        print("Pose convention validation")
        print(f"  selected: {pose_validation['selected_convention']}")
        print(f"  score ratio: {pose_validation['score_ratio']:.4f}")
        for candidate in pose_validation["candidates"]:
            print(
                f"  - {candidate['convention']}: "
                f"in_bounds={candidate['in_bounds']} ({candidate['in_bounds_rate']:.6f}) "
                f"positive_depth={candidate['positive_depth']} ({candidate['positive_depth_rate']:.6f}) "
                f"images_with_in_bounds={candidate['images_with_in_bounds']}/{candidate['image_count']} "
                f"mean_in_bounds={candidate['mean_in_bounds_per_image']:.1f}"
            )
    if summary.get("colmap_skeleton"):
        colmap = summary["colmap_skeleton"]
        print("")
        print("COLMAP skeleton")
        print(f"  output: {colmap['output_dir']}")
        print(f"  cameras: {colmap['camera_count']}")
        print(f"  images: {colmap['image_count']}")
        print(f"  points: {colmap['point_count']}")
        if colmap.get("projected_tracks"):
            print(f"  projected track observations: {colmap['track_observation_count']}")
            print(f"  images with observations: {colmap['images_with_observations']}")
        if colmap["placeholder_poses"]:
            print("  poses: placeholder identity (not geometry-valid)")
        else:
            print(f"  poses: real ({colmap['pose_convention']})")
    if summary.get("colmap_scene"):
        scene = summary["colmap_scene"]
        print("")
        print("COLMAP training scene")
        print(f"  output: {scene['output_scene']}")
        print(f"  sparse: {scene['sparse_dir']}")
        print(f"  support: {scene['support_output_dir']}")
        print(f"  cameras: {scene['camera_count']}")
        print(f"  images: {scene['image_count']}")
        print(f"  points: {scene['point_count']}")
        print(f"  packaged images: {scene['packaged_image_count']}")
        print(f"  packaged masks: {scene['packaged_mask_count']}")
        print(f"  undistorted passthrough images: {scene['undistorted_passthrough_image_count']}")
        print(f"  reused undistorted passthrough images: {scene.get('reused_undistorted_passthrough_image_count', 0)}")
        print(f"  strict pinhole: {scene['strict_pinhole']}")
        print(
            "  scene normalization: "
            f"{'applied' if scene.get('scene_normalization_applied') else 'not applied'}"
        )
        if scene.get("scene_camera_radius_p95") is not None:
            print(f"  camera radius p95: {scene['scene_camera_radius_p95']}")
        if scene.get("scene_point_to_camera_radius_ratio") is not None:
            print(f"  point/camera radius ratio: {scene['scene_point_to_camera_radius_ratio']}")
        if scene.get("scene_scale_warning_count"):
            print(f"  scale warnings: {scene['scene_scale_warning_count']}")
        if scene.get("projected_tracks"):
            print(f"  projected track observations: {scene['track_observation_count']}")
            print(f"  images with observations: {scene['images_with_observations']}")


def _write_manifest_run_report(output_dir, manifest, sensor_results, total_elapsed):
    """Write a run_report.txt summarizing the manifest-driven export."""
    from datetime import datetime

    lines = [
        "COLMAP Export — Run Report",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"cameras.xml: {manifest.get('cameras_xml', '?')}",
        f"Output: {output_dir}",
        "",
        f"Total wall clock: {total_elapsed:.1f}s ({total_elapsed/60:.1f} min)",
        "",
    ]

    # Per-sensor summary table
    fisheye = [r for r in sensor_results if r["type"] == "fisheye"]
    frame = [r for r in sensor_results if r["type"] == "frame"]
    equirect = [r for r in sensor_results if r["type"] == "equirect"]

    total_processed = sum(r.get("processed", 0) for r in fisheye)
    total_skipped = sum(r.get("skipped", 0) for r in fisheye)

    if fisheye:
        lines.append(f"Fisheye sensors: {len(fisheye)}")
        lines.append(f"  Total images processed: {total_processed}")
        if total_skipped:
            lines.append(f"  Total images skipped (already complete): {total_skipped}")
        lines.append("")
        for r in fisheye:
            sid = r["sensor_id"]
            status = r["status"]
            if status == "ok":
                lines.append(f"  Sensor {sid}")
                for img in r.get("image_dirs", []):
                    lines.append(f"    Images: {img}")
                if r.get("multi_pinhole", True):
                    lines.append(f"    Face width: {r.get('face_width', '?')}px")
                    if "output_width_source" in r:
                        lines.append(f"    Width source: {r.get('output_width_source')}")
                else:
                    lines.append(f"    Adaptive width: {r.get('w_out', '?')}px")
                lines.append(f"    Output format: {r.get('output_format', 'jpg')}")
                lines.append(f"    multi_pinhole: {r.get('multi_pinhole', True)}")
                lines.append(f"    Processed: {r.get('processed', 0)}, "
                             f"Skipped: {r.get('skipped', 0)}")
                lines.append(f"    Time: {r.get('elapsed_s', 0)}s")
                lines.append(f"    Output: {r.get('output_dir', '?')}")
            else:
                lines.append(f"  Sensor {sid} — SKIPPED: {r.get('reason', '?')}")
            lines.append("")

    if frame:
        lines.append(f"Frame sensors: {len(frame)}")
        for r in frame:
            sid = r["sensor_id"]
            if r["status"] == "ok":
                lines.append(f"  Sensor {sid}: {r.get('file_count', 0)} files")
            else:
                lines.append(f"  Sensor {sid}: {r['status']} — {r.get('reason', '?')}")
        lines.append("")

    if equirect:
        lines.append(f"Equirect sensors: {len(equirect)}")
        for r in equirect:
            sid = r["sensor_id"]
            lines.append(f"  Sensor {sid}: {r['status']} — {r.get('reason', '?')}")
            if r["status"] == "ok":
                lines.append(f"    Split mode: {r.get('split_mode', '?')}")
                lines.append(f"    Split width: {r.get('split_width', '?')}px")
                if "split_width_source" in r:
                    lines.append(f"    Width source: {r.get('split_width_source')}")
        lines.append("")

    # Pinhole parameters reminder
    lines.append("Output pinhole camera parameters (set and lock in Metashape):")
    lines.append("  projection = Frame")
    widths_used = sorted(set(r.get("face_width", 2048) for r in fisheye if r["status"] == "ok"))
    for w in widths_used:
        lines.append(f"  {w}x{w}, f={w/2}")
    lines.append("  cx = 0.0  cy = 0.0")
    lines.append("  k1 = k2 = k3 = p1 = p2 = 0.0")
    lines.append("")

    report_path = output_dir / "run_report.txt"
    try:
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nRun report written to {report_path}", file=sys.stderr)
    except OSError as e:
        print(f"Warning: could not write run report: {e}", file=sys.stderr)


def load_scene_manifest(path: Path) -> dict:
    """Load and validate a v2 scene manifest JSON file.

    v2 keys: cameras_xml, sparse_ply, output_dir, fisheye_sensors,
    frame_sensors, equirect_sensors, options. The v1 'bodies' key is no
    longer accepted.
    """
    import json as _json
    data = _json.loads(Path(path).read_text())
    required_keys = ("cameras_xml", "output_dir")
    for key in required_keys:
        if key not in data:
            raise ValueError(f"Scene manifest missing required key: {key}")
    if "bodies" in data and "fisheye_sensors" not in data:
        raise ValueError(
            "v1 'bodies' manifest is no longer supported. The exporter expects "
            "a v2 manifest with 'fisheye_sensors' (see gui/scene_manifest.py).")
    data.setdefault("fisheye_sensors", [])
    data.setdefault("frame_sensors", [])
    data.setdefault("equirect_sensors", [])
    data.setdefault("options", {})
    data.setdefault("sparse_ply", None)
    return data


def _write_sensor_calibration_xml(sensor_elem, output_path: Path) -> None:
    """Write one sensor's calibration in the standalone v4 loader format."""
    import xml.etree.ElementTree as _ET
    calibration = sensor_elem.find("calibration")
    if calibration is None:
        raise ValueError("Sensor element has no <calibration> child")
    resolution = calibration.find("resolution")
    if resolution is None:
        resolution = sensor_elem.find("resolution")
    if resolution is None:
        raise ValueError("Sensor element has no <resolution> child")

    def _child_text(tag: str, default: str = "0") -> str:
        value = calibration.findtext(tag)
        if value is None or not str(value).strip():
            return default
        return str(value).strip()

    root = _ET.Element("calibration")
    fields = {
        "projection": calibration.attrib.get("type", sensor_elem.attrib.get("type", "unknown")),
        "date": calibration.attrib.get("date", "manifest_export"),
        "width": str(resolution.attrib["width"]),
        "height": str(resolution.attrib["height"]),
        "f": _child_text("f"),
        "cx": _child_text("cx"),
        "cy": _child_text("cy"),
        "k1": _child_text("k1"),
        "k2": _child_text("k2"),
        "k3": _child_text("k3"),
        "p1": _child_text("p1"),
        "p2": _child_text("p2"),
    }
    for tag, value in fields.items():
        child = _ET.SubElement(root, tag)
        child.text = value

    # Include k4, b1, b2 when present in the source calibration.
    # These are optional in Metashape XML (absent means zero).
    for optional_tag in ("k4", "b1", "b2"):
        value = calibration.findtext(optional_tag)
        if value is not None and str(value).strip():
            child = _ET.SubElement(root, optional_tag)
            child.text = str(value).strip()

    # Preserve the <corrections> block if present in the source calibration
    corrections_elem = calibration.find("corrections")
    if corrections_elem is not None:
        import copy
        root.append(copy.deepcopy(corrections_elem))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _ET.ElementTree(root).write(
        str(output_path), encoding="utf-8", xml_declaration=True)


def _extract_manifest_fisheye_calibration(sensor_elem, sensor_id: int) -> Dict[str, object]:
    from gui.sensor_discovery import extract_sensor_calibration

    calibration = extract_sensor_calibration(sensor_elem)
    if calibration is None:
        raise ValidationError(
            f"Fisheye sensor {sensor_id} has no usable calibration for adaptive routing"
        )
    return calibration


def _manifest_useful_pixel_mask(
    calibration: Mapping[str, object],
    mask_dirs: Sequence[Path],
    lens_only_path: Optional[Path],
):
    from gui.adaptive_undistort import load_useful_pixel_mask

    return load_useful_pixel_mask(
        int(calibration["width"]),
        int(calibration["height"]),
        mask_dirs=mask_dirs,
        lens_only_mask=lens_only_path,
    )


def _source_token(value: str, fallback: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return token or fallback


def _unique_dir_tokens(image_dirs: Sequence[Path]) -> List[str]:
    paths = [Path(path) for path in image_dirs]
    if not paths:
        return []
    parts_by_dir = [path.parts for path in paths]
    max_depth = max(len(parts) for parts in parts_by_dir)
    for depth in range(1, max_depth + 1):
        tokens = []
        for index, parts in enumerate(parts_by_dir):
            raw = parts[-depth] if len(parts) >= depth else f"dir{index}"
            tokens.append(_source_token(raw, f"dir{index}"))
        if len({token.lower() for token in tokens}) == len(tokens):
            return tokens
    return [f"dir{index}" for index, _path in enumerate(paths)]


def _build_fisheye_stem_plan(
    document: Mapping[str, object],
    sensor_id: int,
    image_dirs: Sequence[Path],
) -> Tuple[Dict[str, str], List[Dict[str, object]]]:
    """Return output-stem overrides plus a sidecar map for fisheye sources.

    Duplicate camera labels are common in dual-fisheye video exports where
    ``front/000001.jpg`` and ``back/000001.jpg`` are separate Metashape
    cameras under one sensor. The output stem convention is:

      - unique source stem: keep ``000001``
      - duplicate source stem: prefix the nearest unique directory token,
        e.g. ``front_000001`` and ``back_000001``

    Camera IDs are assigned by XML order within each duplicate label, matched
    to image directory order. This keeps the source files untouched while
    making generated cubeface outputs unambiguous.
    """
    tokens = _unique_dir_tokens(image_dirs)
    source_records: List[Dict[str, object]] = []
    for dir_index, image_dir in enumerate(image_dirs):
        token = tokens[dir_index] if dir_index < len(tokens) else f"dir{dir_index}"
        for image_path in _direct_image_files(Path(image_dir)):
            source_records.append({
                "source_path": str(image_path),
                "source_stem": image_path.stem,
                "image_dir_index": dir_index,
                "image_dir_token": token,
            })

    records_by_stem: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for record in source_records:
        records_by_stem[str(record["source_stem"])].append(record)

    cameras: Mapping[int, Mapping[str, object]] = document["cameras"]  # type: ignore[assignment]
    cameras_by_label: Dict[str, List[Mapping[str, object]]] = defaultdict(list)
    for camera in cameras.values():
        if int(camera.get("sensor_id", -1)) != int(sensor_id):
            continue
        if len(camera.get("transform", ())) != 16:
            continue
        cameras_by_label[str(camera.get("label", ""))].append(camera)

    stem_overrides: Dict[str, str] = {}
    sidecar_entries: List[Dict[str, object]] = []
    used_output_stems: set[str] = set()

    for source_stem, records in sorted(records_by_stem.items()):
        records = sorted(records, key=lambda item: (int(item["image_dir_index"]), str(item["source_path"])))
        camera_candidates = cameras_by_label.get(source_stem, [])
        requires_disambiguation = len(records) > 1 or len(camera_candidates) > 1
        if requires_disambiguation and len(camera_candidates) != len(records):
            raise ValidationError(
                f"Fisheye sensor {sensor_id} stem {source_stem} has "
                f"{len(records)} source files but {len(camera_candidates)} aligned "
                "XML cameras; cannot assign duplicate-stem outputs safely"
            )

        for index, record in enumerate(records):
            output_stem = source_stem
            if requires_disambiguation:
                output_stem = f"{record['image_dir_token']}_{source_stem}"
            if output_stem in used_output_stems:
                output_stem = f"{output_stem}_{index}"
            used_output_stems.add(output_stem)

            source_path = str(record["source_path"])
            stem_overrides[source_path] = output_stem
            camera = camera_candidates[index] if index < len(camera_candidates) else None
            entry = {
                "output_stem": output_stem,
                "source_stem": source_stem,
                "source_path": source_path,
                "image_dir_index": int(record["image_dir_index"]),
                "image_dir_token": str(record["image_dir_token"]),
            }
            if camera is not None:
                entry["camera_id"] = int(camera["id"])
                entry["camera_label"] = str(camera.get("label", source_stem))
            sidecar_entries.append(entry)

    return stem_overrides, sidecar_entries


def _write_source_image_map(output_dir: Path, entries: Sequence[Mapping[str, object]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "naming": "unique source stems are preserved; duplicate stems use '<dir-token>_<stem>'",
        "stems": list(entries),
    }
    (output_dir / SOURCE_IMAGE_MAP_FILENAME).write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def _compute_manifest_fisheye_routing(
    sensor_id: int,
    calibration: Mapping[str, object],
    useful_pixel_mask=None,
) -> Dict[str, object]:
    from gui.adaptive_undistort import (
        evaluate_shortfall_routing,
        extract_lens_characteristics,
    )

    characteristics = extract_lens_characteristics(
        dict(calibration),
        useful_pixel_mask=useful_pixel_mask,
    )
    if characteristics is None:
        raise ValidationError(
            f"Fisheye sensor {sensor_id} is not a supported adaptive-routing projection"
        )
    decision, f_target, w_out = evaluate_shortfall_routing(
        characteristics["theta_max_deg"],
        characteristics["center_solid_angle"],
        characteristics["calibration_type"],
    )
    processing_mode = "single_pinhole" if decision == "SINGLE_PINHOLE" else "multi_pinhole"
    routing: Dict[str, object] = {
        "processing_mode": processing_mode,
        "theta_max_deg": characteristics["theta_max_deg"],
        "f_target": f_target,
        "w_out": w_out,
    }
    if processing_mode == "single_pinhole":
        routing["recommended_output_width"] = w_out
    return routing


def _manifest_auto_int(value, *, field_name: str, sensor_id: int) -> Optional[int]:
    """Parse manifest width fields where 0/empty means auto."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(
            f"Manifest sensor {sensor_id} {field_name} must be a positive integer "
            "or 0 for auto"
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped in ("", "0"):
            return None
        if not re.fullmatch(r"[+-]?\d+", stripped):
            raise ValidationError(
                f"Manifest sensor {sensor_id} {field_name} must be a positive "
                f"integer or 0 for auto; got {value!r}"
            )
        parsed = int(stripped)
    else:
        raise ValidationError(
            f"Manifest sensor {sensor_id} {field_name} must be a positive integer "
            f"or 0 for auto; got {value!r}"
        )

    if parsed == 0:
        return None
    if parsed < 0:
        raise ValidationError(
            f"Manifest sensor {sensor_id} {field_name} must be a positive integer "
            f"or 0 for auto; got {parsed}"
        )
    return parsed


def _positive_width_or_error(
    value,
    *,
    field_name: str,
    sensor_id: int,
    source: str,
) -> int:
    if value is None or isinstance(value, bool):
        raise ValidationError(
            f"Manifest sensor {sensor_id} {field_name} auto resolution from "
            f"{source} did not produce a positive width"
        )
    try:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        width = int(value)
    except (TypeError, ValueError):
        raise ValidationError(
            f"Manifest sensor {sensor_id} {field_name} auto resolution from "
            f"{source} produced invalid width {value!r}"
        )
    if width <= 0:
        raise ValidationError(
            f"Manifest sensor {sensor_id} {field_name} auto resolution from "
            f"{source} produced non-positive width {width}"
        )
    return width


def _compute_manifest_optimal_width(
    sensor_id: int,
    sensor_elem,
    calibration: Optional[Mapping[str, object]] = None,
    useful_pixel_mask=None,
) -> Optional[int]:
    from gui.solid_angle import compute_optimal_width

    if calibration is None:
        calibration = _extract_manifest_fisheye_calibration(sensor_elem, sensor_id)
    try:
        return compute_optimal_width(dict(calibration), useful_pixel_mask=useful_pixel_mask)
    except ValueError as exc:
        raise ValidationError(
            f"Fisheye sensor {sensor_id} could not compute auto output_width: {exc}"
        ) from exc


def _resolve_manifest_fisheye_output_width(
    sensor_record: Mapping[str, object],
    sensor_elem,
    routing: Mapping[str, object],
    calibration: Optional[Mapping[str, object]] = None,
    useful_pixel_mask=None,
) -> Tuple[int, str]:
    sensor_id = int(sensor_record.get("sensor_id", sensor_elem.attrib.get("id", -1)))
    explicit = _manifest_auto_int(
        sensor_record.get("output_width", 0),
        field_name="output_width",
        sensor_id=sensor_id,
    )
    if explicit is not None:
        return explicit, "manifest.output_width"

    recommended = routing.get("recommended_output_width") if routing is not None else None
    if recommended is not None:
        return (
            _positive_width_or_error(
                recommended,
                field_name="output_width",
                sensor_id=sensor_id,
                source="routing.recommended_output_width",
            ),
            "routing.recommended_output_width",
        )

    computed = _compute_manifest_optimal_width(
        sensor_id,
        sensor_elem,
        calibration=calibration,
        useful_pixel_mask=useful_pixel_mask,
    )
    return (
        _positive_width_or_error(
            computed,
            field_name="output_width",
            sensor_id=sensor_id,
            source="compute_optimal_width",
        ),
        "compute_optimal_width",
    )


def _resolve_manifest_equirect_split_width(
    sensor_record: Mapping[str, object],
    sensor_elem,
) -> Tuple[int, str]:
    from gui.sensor_discovery import extract_sensor_calibration, recommended_equirect_width

    sensor_id = int(sensor_record.get("sensor_id", sensor_elem.attrib.get("id", -1)))
    explicit = _manifest_auto_int(
        sensor_record.get("split_width", 0),
        field_name="split_width",
        sensor_id=sensor_id,
    )
    if explicit is not None:
        return explicit, "manifest.split_width"

    split_mode = str(sensor_record.get("split_mode", "reframe"))
    if split_mode not in {"cubemap", "reframe"}:
        raise ValidationError(
            f"Manifest equirect sensor {sensor_id} has unsupported split_mode: {split_mode}"
        )
    calibration = extract_sensor_calibration(sensor_elem)
    if calibration is None:
        raise ValidationError(
            f"Equirect sensor {sensor_id} cannot auto-resolve split_width without "
            "calibration or sensor resolution"
        )
    return (
        _positive_width_or_error(
            recommended_equirect_width(calibration, split_mode),
            field_name="split_width",
            sensor_id=sensor_id,
            source="recommended_equirect_width",
        ),
        "recommended_equirect_width",
    )


def _effective_manifest_fisheye_mode(
    sensor_record: Mapping[str, object],
    routing: Mapping[str, object],
) -> str:
    """Return the export mode for a manifest fisheye sensor.

    A missing routing record means no persisted commitment, so the freshly
    computed routing decision is authoritative. When routing is present, the
    persisted multi_pinhole checkbox is treated as a deliberate user override.
    """
    if not sensor_record.get("routing"):
        return str(routing["processing_mode"])
    default_multi = str(routing.get("processing_mode")) == "multi_pinhole"
    return "multi_pinhole" if bool(sensor_record.get("multi_pinhole", default_multi)) else "single_pinhole"


def _adaptive_intrinsics_from_routing(
    sensor_id: int,
    routing: Mapping[str, object],
) -> Tuple[float, int]:
    f_target = routing.get("f_target")
    w_out = routing.get("w_out")
    if f_target is None or w_out is None:
        raise ValidationError(
            f"Fisheye sensor {sensor_id} requested single_pinhole export, "
            "but routing.f_target or routing.w_out is missing"
        )
    try:
        focal = float(f_target)
    except (TypeError, ValueError):
        raise ValidationError(
            f"Fisheye sensor {sensor_id} routing.f_target must be positive"
        )
    if focal <= 0:
        raise ValidationError(
            f"Fisheye sensor {sensor_id} routing.f_target must be positive"
        )
    try:
        width = _positive_width_or_error(
            w_out,
            field_name="routing.w_out",
            sensor_id=sensor_id,
            source="routing.w_out",
        )
    except ValidationError as exc:
        raise ValidationError(
            f"Fisheye sensor {sensor_id} routing.w_out must be positive"
        ) from exc
    return focal, width


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metashape-cameras", type=Path, required=False)
    parser.add_argument("--metashape-points", type=Path, required=False)
    parser.add_argument(
        "--scene-manifest",
        type=Path,
        default=None,
        help="JSON scene manifest for multi-camera COLMAP export. Replaces --metashape-cameras/--metashape-points.",
    )
    parser.add_argument("--cubeface-root", type=Path, default=None)
    parser.add_argument("--lens-camera-map", default=None)
    parser.add_argument(
        "--passthrough-image-root",
        action="append",
        type=Path,
        default=[],
        help="Image root for non-fisheye Metashape cameras. Repeat for multiple drone/frame folders.",
    )
    parser.add_argument(
        "--passthrough-media-manifest",
        type=Path,
        default=None,
        help="JSON manifest declaring explicit passthrough media sets with image_root and optional mask_root.",
    )
    parser.add_argument(
        "--passthrough-sensor-id",
        action="append",
        type=int,
        default=None,
        help="Restrict passthrough export to this Metashape sensor id. Defaults to all frame/pinhole sensors.",
    )
    parser.add_argument(
        "--passthrough-camera-model",
        choices=("auto", "pinhole", "opencv", "full_opencv"),
        default="auto",
        help="COLMAP model for passthrough frame sensors. auto preserves distortion with OPENCV/FULL_OPENCV.",
    )
    parser.add_argument(
        "--undistort-passthrough",
        choices=("auto", "always", "never"),
        default="auto",
        help="For --output-scene, undistort passthrough frame cameras before writing final PINHOLE records.",
    )
    parser.add_argument(
        "--passthrough-output-format",
        choices=("png",),
        default="png",
        help="Image format for generated undistorted passthrough assets.",
    )
    parser.add_argument(
        "--strict-pinhole",
        action="store_true",
        help="For --output-scene, fail unless every final COLMAP camera is PINHOLE.",
    )
    parser.add_argument(
        "--no-strict-pinhole",
        action="store_true",
        help="Allow non-PINHOLE passthrough cameras when undistortion is disabled.",
    )
    parser.add_argument(
        "--require-masks",
        action="store_true",
        help="Fail scene export if any final image lacks a matching mask.",
    )
    parser.add_argument(
        "--normalize-scene",
        action="store_true",
        help=(
            "For --output-scene, recenter and uniformly scale camera poses and 3D points "
            "so the exported scene is easier to navigate in viewers/training tools."
        ),
    )
    parser.add_argument(
        "--dual-fisheye-raw-root",
        type=Path,
        default=None,
        help="Optional raw dual-fisheye root used only to print an auditable lens-map scaffold.",
    )
    parser.add_argument(
        "--output-colmap",
        type=Path,
        default=None,
        help="Write Phase 3 COLMAP text skeleton to this directory.",
    )
    parser.add_argument(
        "--output-scene",
        type=Path,
        default=None,
        help="Write a training-ready COLMAP scene with images/, masks/, and sparse/0.",
    )
    parser.add_argument(
        "--support-output-dir",
        type=Path,
        default=None,
        help=(
            "Directory for processing files: remap cache, manifests, logs, and temporary files. "
            "Defaults to <output>/processing when --output-scene is <output>/colmap."
        ),
    )
    parser.add_argument(
        "--reports-output-dir",
        type=Path,
        default=None,
        help=(
            "Directory for human-readable conversion and validation reports. "
            "Defaults to <output>/reports when --output-scene is <output>/colmap."
        ),
    )
    parser.add_argument(
        "--package-assets",
        dest="package_assets",
        action="store_true",
        default=True,
        help="Package scene images and masks under --output-scene. Enabled by default.",
    )
    parser.add_argument(
        "--no-package-assets",
        dest="package_assets",
        action="store_false",
        help="Write only sparse/0 under --output-scene without copying/linking/undistorting assets.",
    )
    parser.add_argument(
        "--force-assets",
        action="store_true",
        help="Regenerate or relink packaged scene assets even when reusable outputs already exist.",
    )
    parser.add_argument(
        "--keep-processing-files",
        dest="keep_processing_files",
        action="store_true",
        default=True,
        help="Keep processing/remap cache, manifests, and logs after a successful scene export. Default.",
    )
    parser.add_argument(
        "--clean-processing-files",
        dest="keep_processing_files",
        action="store_false",
        help="Remove processing/remap cache, manifests, and logs after a successful scene export.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Emit parseable [PROGRESS] lines to stderr during scene export.",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=250,
        help="Asset count interval for progress output.",
    )
    parser.add_argument(
        "--allow-placeholder-poses",
        action="store_true",
        help="Required for Phase 3 skeleton output. Poses are identity placeholders until Phase 4 validation.",
    )
    parser.add_argument(
        "--validate-pose-convention",
        action="store_true",
        help="Score supported Metashape transform conventions by projecting sparse points into cubeface cameras.",
    )
    parser.add_argument(
        "--pose-convention",
        choices=("metashape_camera_to_world", "metashape_world_to_camera"),
        default="metashape_camera_to_world",
        help="Metashape XML transform convention. Metashape stores camera-to-world transforms.",
    )
    parser.add_argument(
        "--pose-sample-points",
        type=int,
        default=15000,
        help="Deterministic PLY point sample size used for pose-convention validation. Use 0 for all points.",
    )
    parser.add_argument(
        "--min-pose-score-ratio",
        type=float,
        default=1.05,
        help="Required in-bounds score ratio for automatic pose-convention selection.",
    )
    parser.add_argument(
        "--default-point-error",
        type=float,
        default=0.0,
        help="Reprojection error value written for PLY-derived points in skeleton output.",
    )
    parser.add_argument(
        "--projected-tracks",
        action="store_true",
        help=(
            "Project Metashape sparse points into exported COLMAP images and write synthetic 2D tracks. "
            "These are not original Metashape tie-point observations."
        ),
    )
    parser.add_argument(
        "--track-max-points",
        type=int,
        default=0,
        help="Maximum PLY points to try for projected tracks. Use 0 for all points.",
    )
    parser.add_argument(
        "--track-max-camera-groups-per-point",
        type=int,
        default=8,
        help="Nearby Metashape camera groups to test for each projected-track point.",
    )
    parser.add_argument(
        "--track-max-observations-per-point",
        type=int,
        default=6,
        help="Maximum COLMAP 2D observations written per projected 3D point.",
    )
    parser.add_argument(
        "--track-min-length",
        type=int,
        default=2,
        help="Minimum successful projected observations required to keep a 3D point.",
    )
    parser.add_argument(
        "--track-max-observations-per-image",
        type=int,
        default=0,
        help="Maximum projected 2D observations per image. Use 0 for unlimited.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--json-full",
        action="store_true",
        help="With --json, include verbose internal discovery records instead of compact GUI-friendly output.",
    )
    parser.add_argument("--validate", action="store_true", help="Accepted for CLI clarity; parsing always validates.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Conditional requirement: --scene-manifest OR --metashape-cameras
    if args.scene_manifest is None and args.metashape_cameras is None:
        parser.error("one of --scene-manifest or --metashape-cameras is required")

    try:
        # Scene manifest path (v2): three sensor types, multi-dir, no Body
        if args.scene_manifest is not None:
            manifest = load_scene_manifest(args.scene_manifest)
            print(f"Loaded v2 scene manifest from {args.scene_manifest}",
                  file=sys.stderr)
            print(f"Manifest: {len(manifest['fisheye_sensors'])} fisheye, "
                  f"{len(manifest['frame_sensors'])} frame, "
                  f"{len(manifest['equirect_sensors'])} equirect sensors",
                  file=sys.stderr)

            from gui.adaptive_undistort import process_sensor_adaptive as _process_sensor_adaptive
            from gui.cubeface_processing import process_cubeface_sensor as _process_cubeface_sensor
            import time as _time
            import xml.etree.ElementTree as _ET

            output_dir = Path(manifest["output_dir"])
            output_dir.mkdir(parents=True, exist_ok=True)
            opts = manifest.get("options", {})

            run_start = _time.perf_counter()
            sensor_results = []

            # Parse cameras.xml once to extract per-sensor calibration
            cameras_xml_path = Path(manifest["cameras_xml"])
            xml_tree = _ET.parse(cameras_xml_path)
            xml_root = xml_tree.getroot()
            sensor_elements = {
                int(s.attrib["id"]): s for s in xml_root.findall(".//sensor")
            }
            document = parse_metashape_cameras_xml(cameras_xml_path)

            # ── Phase 1: Fisheye sensors, routed per sensor ───────────
            adaptive_view_map_sensors: List[Dict[str, object]] = []
            processed_cubeface_sensor_count = 0
            for fs in manifest["fisheye_sensors"]:
                sid = fs["sensor_id"]
                image_dirs = [Path(p) for p in fs.get("image_dirs", [])]
                mask_dirs = [Path(p) for p in fs.get("mask_dirs", [])]
                lens_only = fs.get("lens_only_mask")
                lens_only_path = Path(lens_only) if lens_only else None
                output_format = str(fs.get("output_format", "jpg")).lower().lstrip(".")

                if not image_dirs:
                    print(f"  Skipping fisheye sensor {sid}: no image_dirs",
                          file=sys.stderr)
                    sensor_results.append({
                        "type": "fisheye", "sensor_id": sid, "status": "skipped",
                        "reason": "no image_dirs",
                    })
                    continue
                sensor_elem = sensor_elements.get(sid)
                if sensor_elem is None:
                    print(f"  Skipping fisheye sensor {sid}: not found in cameras.xml",
                          file=sys.stderr)
                    sensor_results.append({
                        "type": "fisheye", "sensor_id": sid, "status": "skipped",
                        "reason": "sensor_id not in cameras.xml",
                    })
                    continue

                raw_routing = fs.get("routing")
                routing = dict(raw_routing) if raw_routing else None
                calibration = None
                useful_pixel_mask = None
                # Routing is incomplete if f_target is absent (old manifests
                # only stored intrinsics for the recommended path).  Treat
                # incomplete the same as missing — recompute from calibration.
                needs_routing = routing is None or routing.get("f_target") is None
                if needs_routing or not bool(fs.get("multi_pinhole", True)):
                    calibration = _extract_manifest_fisheye_calibration(sensor_elem, sid)
                    useful_pixel_mask = _manifest_useful_pixel_mask(
                        calibration,
                        mask_dirs,
                        lens_only_path,
                    )
                if needs_routing:
                    routing = _compute_manifest_fisheye_routing(
                        sid,
                        calibration,
                        useful_pixel_mask=useful_pixel_mask,
                    )
                processing_mode = _effective_manifest_fisheye_mode(fs, routing)
                if processing_mode not in {"multi_pinhole", "single_pinhole"}:
                    raise ValidationError(
                        f"Fisheye sensor {sid} has unsupported processing_mode: {processing_mode}"
                    )

                if processing_mode == "multi_pinhole":
                    output_width, output_width_source = _resolve_manifest_fisheye_output_width(
                        fs,
                        sensor_elem,
                        routing,
                        calibration=calibration,
                        useful_pixel_mask=useful_pixel_mask,
                    )
                    # Write a temp single-sensor calibration XML for v4.
                    sensor_output = output_dir / "processing" / f"sensor_{sid}"
                    temp_cal = sensor_output / "calibration.xml"
                    _write_sensor_calibration_xml(sensor_elem, temp_cal)
                    stem_overrides, source_map_entries = _build_fisheye_stem_plan(
                        document=document,
                        sensor_id=sid,
                        image_dirs=image_dirs,
                    )

                    t0 = _time.perf_counter()
                    print(f"  Processing fisheye sensor {sid} "
                          f"({len(image_dirs)} image dir(s)) as multi_pinhole -> {sensor_output}",
                          file=sys.stderr)
                    result = _process_cubeface_sensor(
                        calibration_xml=temp_cal,
                        image_dirs=image_dirs,
                        output_dir=sensor_output,
                        face_width=output_width,
                        mask_dirs=mask_dirs,
                        lens_only_mask=lens_only_path,
                        output_format=output_format,
                        force=opts.get("force_assets", False),
                        cache_remapping=True,
                        stem_overrides=stem_overrides,
                        progress_callback=lambda msg: print(f"    {msg}", file=sys.stderr),
                    )
                    _write_source_image_map(sensor_output, source_map_entries)
                    if temp_cal.is_file():
                        temp_cal.unlink()
                    processed = result.get("processed_count", 0)
                    skipped = result.get("skipped_count", 0)
                    elapsed = _time.perf_counter() - t0
                    processed_cubeface_sensor_count += 1
                    sensor_results.append({
                        "type": "fisheye", "sensor_id": sid, "status": "ok",
                        "face_width": output_width,
                        "requested_output_width": fs.get("output_width", 0),
                        "resolved_output_width": output_width,
                        "output_width_source": output_width_source,
                        "output_format": output_format,
                        "output_dir": str(sensor_output),
                        "image_dirs": [str(p) for p in image_dirs],
                        "routing_mode": routing.get("processing_mode"),
                        "processing_mode": processing_mode,
                        "multi_pinhole": True,
                        "support_origin": result.get("support_origin"),
                        "maxangle_deg": result.get("maxangle_deg"),
                        "processed": processed, "skipped": skipped,
                        "elapsed_s": round(elapsed, 1),
                    })
                    continue

                f_target, w_out = _adaptive_intrinsics_from_routing(sid, routing)
                # User's Width field overrides routing w_out.  When absent,
                # auto-compute at 45° half-angle (one cubeface equivalent).
                user_width = _manifest_auto_int(
                    fs.get("output_width", 0),
                    field_name="output_width",
                    sensor_id=sid,
                )
                if user_width is not None:
                    w_out = user_width
                elif w_out > int(2 * f_target):
                    import math
                    auto_w = int(math.ceil(2.0 * f_target * math.tan(math.radians(45.0))))
                    print(
                        f"  Fisheye sensor {sid}: auto single-pinhole width "
                        f"{auto_w}px (clamped to 45° half-angle, "
                        f"routing w_out was {w_out}px)",
                        file=sys.stderr,
                    )
                    w_out = auto_w
                if calibration is None:
                    calibration = _extract_manifest_fisheye_calibration(sensor_elem, sid)
                    useful_pixel_mask = _manifest_useful_pixel_mask(
                        calibration,
                        mask_dirs,
                        lens_only_path,
                    )

                sensor_output = output_dir / "processing" / f"adaptive_sensor_{sid}"
                t0 = _time.perf_counter()
                processed = skipped = 0
                for i, img_dir in enumerate(image_dirs):
                    mask_arg = mask_dirs[i] if i < len(mask_dirs) else None
                    print(f"  Processing fisheye sensor {sid} dir {i} "
                          f"({img_dir.name}) as single_pinhole -> {sensor_output}",
                          file=sys.stderr)
                    result = _process_sensor_adaptive(
                        calibration=calibration,
                        image_dir=img_dir,
                        output_dir=sensor_output,
                        mask_dir=mask_arg,
                        lens_only_mask=lens_only_path,
                        useful_pixel_mask=useful_pixel_mask,
                        f_target=f_target,
                        w_out=w_out,
                        progress_callback=lambda msg: print(f"    {msg}", file=sys.stderr),
                    )
                    processed += result.get("processed_count", 0)
                    skipped += result.get("skipped_count", 0)
                elapsed = _time.perf_counter() - t0
                actual_w = result.get("w_out", w_out)
                actual_h = result.get("h_out", actual_w)
                adaptive_view_map_sensors.append({
                    "sensor_id": sid,
                    "f_target": f_target,
                    "w_out": actual_w,
                    "h_out": actual_h,
                    "images_dir": str(sensor_output / "images"),
                    "masks_dir": str(sensor_output / "masks"),
                })
                sensor_results.append({
                    "type": "fisheye", "sensor_id": sid, "status": "ok",
                    "f_target": f_target,
                    "w_out": actual_w,
                    "h_out": actual_h,
                    "output_dir": str(sensor_output),
                    "bonusdata_dir": str(sensor_output / "bonusdata"),
                    "image_dirs": [str(p) for p in image_dirs],
                    "routing_mode": routing.get("processing_mode"),
                    "processing_mode": processing_mode,
                    "multi_pinhole": False,
                    "processed": processed, "skipped": skipped,
                    "elapsed_s": round(elapsed, 1),
                })

            adaptive_view_map = (
                {"sensors": adaptive_view_map_sensors}
                if adaptive_view_map_sensors else None
            )

            # ── Phase 1b: Equirect sensors — split into pinhole crops ───
            erp_view_map_sensors: List[Dict[str, object]] = []
            if manifest["equirect_sensors"]:
                from gui.erp_reframe import process_equirect_sensor as _process_erp_sensor
                for eq in manifest["equirect_sensors"]:
                    sid = eq["sensor_id"]
                    image_dirs = [Path(p) for p in eq.get("image_dirs", [])]
                    mask_dirs = [Path(p) for p in eq.get("mask_dirs", [])]
                    split_mode = str(eq.get("split_mode", "reframe"))

                    if not image_dirs:
                        print(f"  Skipping equirect sensor {sid}: no image_dirs",
                              file=sys.stderr)
                        sensor_results.append({
                            "type": "equirect", "sensor_id": sid, "status": "skipped",
                            "reason": "no image_dirs",
                        })
                        continue
                    if sid not in sensor_elements:
                        print(f"  Skipping equirect sensor {sid}: not found in cameras.xml",
                              file=sys.stderr)
                        sensor_results.append({
                            "type": "equirect", "sensor_id": sid, "status": "skipped",
                            "reason": "sensor_id not in cameras.xml",
                        })
                        continue
                    split_width, split_width_source = _resolve_manifest_equirect_split_width(
                        eq,
                        sensor_elements[sid],
                    )

                    sensor_output = output_dir / "processing" / f"erp_sensor_{sid}"
                    print(f"  Processing equirect sensor {sid} "
                          f"({split_mode}, {split_width}px) -> {sensor_output}",
                          file=sys.stderr)
                    t0 = _time.perf_counter()
                    erp_result = _process_erp_sensor(
                        image_dirs=image_dirs,
                        mask_dirs=mask_dirs,
                        split_mode=split_mode,
                        split_width=split_width,
                        output_dir=sensor_output,
                        force=opts.get("force_assets", False),
                        progress_callback=lambda msg: print(f"    {msg}", file=sys.stderr),
                    )
                    elapsed = _time.perf_counter() - t0
                    erp_view_map_sensors.append({
                        "sensor_id": sid,
                        "split_mode": split_mode,
                        "split_width": split_width,
                        "views": erp_result["views"],
                        "stems": erp_result["stems"],
                    })
                    sensor_results.append({
                        "type": "equirect", "sensor_id": sid, "status": "ok",
                        "split_mode": split_mode, "split_width": split_width,
                        "requested_split_width": eq.get("split_width", 0),
                        "resolved_split_width": split_width,
                        "split_width_source": split_width_source,
                        "output_dir": str(sensor_output),
                        "image_dirs": [str(p) for p in image_dirs],
                        "view_count": len(erp_result["views"]),
                        "processed": erp_result["processed"],
                        "skipped": erp_result["skipped"],
                        "elapsed_s": round(elapsed, 1),
                    })
            erp_view_map = {"sensors": erp_view_map_sensors} if erp_view_map_sensors else None

            # ── Phase 2: Parse XML and discover generated cubefaces ──
            sparse_ply_path = (Path(manifest["sparse_ply"])
                               if manifest.get("sparse_ply") else None)

            cubeface_root = output_dir / "processing"
            # Skip cubeface discovery if no fisheye sensors were processed —
            # the processing/ tree may contain only erp_sensor_*/ output which
            # discover_cubefaces filters out and would otherwise raise on.
            if cubeface_root.is_dir() and processed_cubeface_sensor_count > 0:
                discovery = discover_cubefaces(cubeface_root)
            else:
                discovery = empty_cubeface_discovery(cubeface_root)
            print(f"  Discovered {discovery['image_count']} cubeface images "
                  f"across {discovery['lens_count']} lenses", file=sys.stderr)

            # ── Phase 3: Build lens-camera mapping ────────────────────
            # One output dir per fisheye sensor → one lens_label per sensor.
            sensor_id_to_lens_label = {}
            for sr in sensor_results:
                if sr.get("type") != "fisheye" or sr.get("status") != "ok":
                    continue
                if sr.get("processing_mode") != "multi_pinhole":
                    continue
                sr_output = Path(sr["output_dir"])
                try:
                    rel = sr_output.relative_to(cubeface_root).as_posix()
                except ValueError:
                    rel = sr_output.name
                sensor_id_to_lens_label[sr["sensor_id"]] = rel

            lens_camera_mapping = {}
            for camera_id, camera in document["cameras"].items():
                sid = int(camera["sensor_id"])
                if sid in sensor_id_to_lens_label:
                    label = sensor_id_to_lens_label[sid]
                    lens_camera_mapping.setdefault(label, []).append(camera_id)
            for label in lens_camera_mapping:
                lens_camera_mapping[label] = tuple(sorted(lens_camera_mapping[label]))

            lens_map = validate_lens_camera_map(document, discovery, lens_camera_mapping)
            print(f"  Lens map: {len(lens_map['resolutions'])} cubeface stems resolved",
                  file=sys.stderr)

            # ── Phase 4: Passthrough map for frame sensors (multi-dir) ──
            passthrough_map = None
            media_sets = []
            frame_sensor_ids = []
            for fs in manifest["frame_sensors"]:
                fs_id = fs["sensor_id"]
                fs_image_dirs = [Path(p) for p in fs.get("image_dirs", [])]
                fs_mask_dirs = [Path(p) for p in fs.get("mask_dirs", [])]
                if not fs_image_dirs:
                    print(f"  Skipping frame sensor {fs_id}: no image_dirs",
                          file=sys.stderr)
                    continue
                for i, img_dir in enumerate(fs_image_dirs):
                    if not img_dir.is_dir():
                        print(f"  Frame sensor {fs_id} dir {i}: not a directory: "
                              f"{img_dir}", file=sys.stderr)
                        continue
                    mask_root = fs_mask_dirs[i] if i < len(fs_mask_dirs) else None
                    media_sets.append({
                        "name": f"sensor_{fs_id}_dir_{i}",
                        "image_root": img_dir,
                        "mask_root": mask_root,
                    })
                    frame_sensor_ids.append(fs_id)

            if media_sets:
                media_sets = _with_unique_slugs(media_sets)
                # require_masks dropped in v2 — exporter still warns on missing
                # but does not refuse to export.
                passthrough_map = resolve_passthrough_media_sets(
                    document, media_sets, frame_sensor_ids,
                    require_masks=False,
                )
                print(f"  Passthrough: {passthrough_map['resolved_count']} "
                      f"frame images resolved", file=sys.stderr)

            # ── Phase 5: Write COLMAP training scene ─────────────────
            scene_output = output_dir / "colmap"
            support_dir = output_dir / "processing"
            reports_dir = output_dir / "reports"

            print(f"  Writing COLMAP scene to {scene_output}", file=sys.stderr)
            colmap_result = write_colmap_training_scene(
                sparse_ply_path,
                document,
                discovery,
                scene_output,
                lens_map=lens_map,
                passthrough_map=passthrough_map,
                erp_view_map=erp_view_map,
                adaptive_map=adaptive_view_map,
                pose_convention=opts.get("pose_convention", "metashape_camera_to_world"),
                package_assets=True,
                force_assets=opts.get("force_assets", False),
                support_output_dir=support_dir,
                reports_output_dir=reports_dir,
                keep_processing_files=opts.get("keep_processing_files", True),
                progress=getattr(args, "progress", False),
                progress_interval=getattr(args, "progress_interval", 250),
                require_masks=False,
                normalize_scene=opts.get("normalize_scene", False),
                # projected_tracks: always-on when a sparse PLY is provided (v2)
                projected_tracks=sparse_ply_path is not None,
                strict_pinhole=True,
                undistort_passthrough="auto",
                passthrough_output_format="jpg",
            )

            print(f"  COLMAP scene: {colmap_result.get('camera_count', '?')} cameras, "
                  f"{colmap_result.get('image_count', '?')} images, "
                  f"{colmap_result.get('point_count', '?')} points", file=sys.stderr)

            # ── Phase 6: Validate output files ────────────────────────
            validate_colmap_model(
                scene_output / "sparse" / "0",
                expected_cameras=colmap_result["camera_count"],
                expected_images=colmap_result["image_count"],
                expected_points=colmap_result["point_count"],
            )
            print(f"  Validation passed", file=sys.stderr)

            sensor_results.append({
                "type": "colmap_scene", "status": "ok",
                "output_dir": str(scene_output),
                "camera_count": colmap_result.get("camera_count", 0),
                "image_count": colmap_result.get("image_count", 0),
                "point_count": colmap_result.get("point_count", 0),
            })
            total_elapsed = _time.perf_counter() - run_start
            _write_manifest_run_report(output_dir, manifest, sensor_results, total_elapsed)

            return 0

        summary = inspect_inputs(
            args.metashape_cameras,
            args.metashape_points,
            args.cubeface_root,
            args.lens_camera_map,
            args.passthrough_image_root,
            args.passthrough_sensor_id,
            args.dual_fisheye_raw_root,
            args.passthrough_media_manifest,
            args.require_masks,
        )
        pose_validation = None
        pose_records = None
        selected_pose_convention = args.pose_convention
        if args.validate_pose_convention:
            if not args.lens_camera_map:
                raise ValidationError("--lens-camera-map is required for pose-convention validation")
            if args.cubeface_root is None:
                raise ValidationError("--cubeface-root is required for pose-convention validation")
            document = parse_metashape_cameras_xml(args.metashape_cameras)
            discovery = discover_cubefaces(args.cubeface_root)
            lens_map = validate_lens_camera_map(document, discovery, parse_lens_camera_map(args.lens_camera_map))
            pose_validation = validate_pose_conventions(
                document,
                discovery,
                lens_map,
                args.metashape_points,
                max_points=args.pose_sample_points,
                min_score_ratio=args.min_pose_score_ratio,
            )
            summary = dict(summary)
            summary["pose_validation"] = pose_validation

        strict_pinhole = not args.no_strict_pinhole
        if args.strict_pinhole:
            strict_pinhole = True

        if args.output_scene is not None:
            if not selected_pose_convention and not args.allow_placeholder_poses:
                raise ValidationError("--pose-convention is required for --output-scene unless placeholder poses are allowed")
            document = parse_metashape_cameras_xml(args.metashape_cameras)
            colmap_scene = write_colmap_training_scene(
                args.metashape_points,
                document,
                summary["cubefaces"],
                args.output_scene,
                lens_map=summary.get("lens_map"),
                passthrough_map=summary.get("passthrough_map"),
                pose_convention=str(selected_pose_convention) if selected_pose_convention else None,
                placeholder_poses=args.allow_placeholder_poses,
                default_point_error=args.default_point_error,
                passthrough_camera_model=args.passthrough_camera_model,
                undistort_passthrough=args.undistort_passthrough,
                passthrough_output_format=args.passthrough_output_format,
                strict_pinhole=strict_pinhole,
                package_assets=args.package_assets,
                force_assets=args.force_assets,
                support_output_dir=args.support_output_dir,
                reports_output_dir=args.reports_output_dir,
                keep_processing_files=args.keep_processing_files,
                progress=args.progress,
                progress_interval=args.progress_interval,
                require_masks=args.require_masks,
                normalize_scene=args.normalize_scene,
                projected_tracks=args.projected_tracks,
                track_max_points=args.track_max_points,
                track_max_camera_groups_per_point=args.track_max_camera_groups_per_point,
                track_max_observations_per_point=args.track_max_observations_per_point,
                track_min_length=args.track_min_length,
                track_max_observations_per_image=args.track_max_observations_per_image,
            )
            validate_colmap_model(
                args.output_scene / "sparse" / "0",
                expected_cameras=colmap_scene["camera_count"],
                expected_images=colmap_scene["image_count"],
                expected_points=colmap_scene["point_count"] if args.projected_tracks else summary["ply"]["vertex_count"],
            )
            summary = dict(summary)
            summary["colmap_scene"] = colmap_scene

        if args.output_colmap is not None and selected_pose_convention:
            if not args.lens_camera_map:
                if int(summary["cubefaces"]["image_count"]) > 0:
                    raise ValidationError("--lens-camera-map is required for real cubeface pose export")
            if args.cubeface_root is None and int(summary["cubefaces"]["image_count"]) > 0:
                raise ValidationError("--cubeface-root is required for real cubeface pose export")
            document = parse_metashape_cameras_xml(args.metashape_cameras)
            if int(summary["cubefaces"]["image_count"]) > 0:
                discovery = discover_cubefaces(args.cubeface_root)
                lens_map = validate_lens_camera_map(document, discovery, parse_lens_camera_map(args.lens_camera_map))
                pose_records = build_pose_records(
                    document,
                    discovery,
                    lens_map,
                    str(selected_pose_convention),
                    camera_world_transform_from_document(document),
                )

        if args.output_colmap is not None:
            if summary.get("passthrough_map"):
                document = parse_metashape_cameras_xml(args.metashape_cameras)
                lens_map = summary.get("lens_map")
                colmap = write_colmap_mixed_scene(
                    args.metashape_points,
                    document,
                    summary["cubefaces"],
                    args.output_colmap,
                    lens_map=lens_map,
                    passthrough_map=summary["passthrough_map"],
                    pose_convention=str(selected_pose_convention) if selected_pose_convention else None,
                    placeholder_poses=args.allow_placeholder_poses,
                    default_point_error=args.default_point_error,
                    passthrough_camera_model=args.passthrough_camera_model,
                    projected_tracks=args.projected_tracks,
                    track_max_points=args.track_max_points,
                    track_max_camera_groups_per_point=args.track_max_camera_groups_per_point,
                    track_max_observations_per_point=args.track_max_observations_per_point,
                    track_min_length=args.track_min_length,
                    track_max_observations_per_image=args.track_max_observations_per_image,
                )
                validate_colmap_model(
                    args.output_colmap,
                    expected_cameras=colmap["camera_count"],
                    expected_images=colmap["image_count"],
                    expected_points=colmap["point_count"] if args.projected_tracks else summary["ply"]["vertex_count"],
                )
            else:
                colmap = write_colmap_skeleton(
                    args.metashape_points,
                    summary["cubefaces"],
                    args.output_colmap,
                    default_point_error=args.default_point_error,
                    placeholder_poses=args.allow_placeholder_poses,
                    pose_records=pose_records,
                    pose_convention=str(selected_pose_convention) if pose_records is not None else None,
                    lens_map=lens_map,
                )
                validate_colmap_skeleton(
                    args.output_colmap,
                    expected_images=summary["cubefaces"]["image_count"],
                    expected_points=summary["ply"]["vertex_count"],
                )
            summary = dict(summary)
            summary["colmap_skeleton"] = colmap
    except ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        payload = summary if args.json_full else compact_json_summary(summary)
        print(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))
    else:
        print_human_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
