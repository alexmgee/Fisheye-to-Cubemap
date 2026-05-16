"""Equirectangular (ERP) splitting for the COLMAP export pipeline.

Equirectangular images (stitched 360° panoramas) are split into perspective
pinhole crops for COLMAP-based 3DGS training. Two split modes are supported:

  - ``cubemap``: 6 views at 90° FOV in cardinal directions plus zenith and
    nadir. Simplest, includes pole views.
  - ``reframe``: 16 views at 90° FOV in two staggered horizontal rings at
    ±35° pitch. No pole views. Concentrates overlap at the horizon where
    scene content lives. This is the LichtFeld 360 plugin's "medium" preset.

Each virtual view becomes a PINHOLE camera in the COLMAP output. Per-view
pose is composed from the source ERP camera's pose × the view's rotation
matrix. The same composition function used for fisheye cube faces handles
ERP views via the shared ``FACE_BASIS_SOURCE_FROM_FACE`` dict — this module
registers angle-keyed entries (e.g. ``"yaw022.5_pitch-35"``) into that dict
at module load.

Disk layout written by :func:`process_equirect_sensor`::

    <output_dir>/
      view_yaw000.0_pitch+00/
        images/
          DJI_0001.png
          DJI_0002.png
          ...
        masks/                  # only if mask_dirs provided
          DJI_0001.png
          ...
      view_yaw090.0_pitch+00/
        images/
          ...

The view-slot directory name encodes the (yaw, pitch) angles. Filenames inside
keep the source stem unchanged so a stem matches across all virtual views of
the same source frame — this is what enables COLMAP rig constraints.

See ``docs/superpowers/plans/2026-05-14-colmap-export-tab-redesign.md`` Task 6
for the design rationale and ``docs/superpowers/research/`` for the
fisheye-undistortion-vs-splitting research that motivated the two-preset
approach.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Preset geometry
# ─────────────────────────────────────────────────────────────────────────────

# Each view is (yaw_deg, pitch_deg, fov_deg). Yaw is canonicalised to (-180, 180].
# Cubemap: 4 cardinal directions at the horizon + zenith + nadir, all 90° FOV.
CUBEMAP_VIEWS: Tuple[Tuple[float, float, float], ...] = (
    (0.0, 0.0, 90.0),     # front
    (90.0, 0.0, 90.0),    # right
    (180.0, 0.0, 90.0),   # back
    (-90.0, 0.0, 90.0),   # left
    (0.0, 90.0, 90.0),    # zenith (looking up)
    (0.0, -90.0, 90.0),   # nadir (looking down)
)

# Reframe (LichtFeld 360 "medium" preset): two rings of 8 at ±35° pitch,
# 22.5° yaw stagger, 90° FOV.
_REFRAME_RING_LOWER = tuple((yaw, -35.0, 90.0) for yaw in (
    0.0, 45.0, 90.0, 135.0, 180.0, -135.0, -90.0, -45.0,
))
_REFRAME_RING_UPPER = tuple((yaw, 35.0, 90.0) for yaw in (
    22.5, 67.5, 112.5, 157.5, -157.5, -112.5, -67.5, -22.5,
))
REFRAME_VIEWS: Tuple[Tuple[float, float, float], ...] = _REFRAME_RING_LOWER + _REFRAME_RING_UPPER

SPLIT_MODE_VIEWS: Dict[str, Tuple[Tuple[float, float, float], ...]] = {
    "cubemap": CUBEMAP_VIEWS,
    "reframe": REFRAME_VIEWS,
}


# ─────────────────────────────────────────────────────────────────────────────
# Labels and suffixes
# ─────────────────────────────────────────────────────────────────────────────

def view_label(yaw_deg: float, pitch_deg: float) -> str:
    """Canonical label for an ERP view at (yaw, pitch).

    Format: ``yaw{NNN.N}_pitch{±NN}`` where yaw is signed one-decimal-place
    with leading-zero padding for non-negatives, and pitch is signed integer
    with two-digit width. Examples::

        view_label(0, 0)       == "yaw000.0_pitch+00"
        view_label(22.5, -35)  == "yaw022.5_pitch-35"
        view_label(-157.5, 35) == "yaw-157.5_pitch+35"
        view_label(0, 90)      == "yaw000.0_pitch+90"
    """
    return f"yaw{yaw_deg:05.1f}_pitch{int(round(pitch_deg)):+03d}"


def view_filename_suffix(yaw_deg: float, pitch_deg: float) -> str:
    """Suffix form used inside :class:`pathlib.Path` directory names.

    Currently identical to :func:`view_label` prefixed with ``view_``. Kept
    as a separate helper so the directory-naming convention can evolve
    independently of the basis-dict key.
    """
    return f"view_{view_label(yaw_deg, pitch_deg)}"


# ─────────────────────────────────────────────────────────────────────────────
# Rotation math
# ─────────────────────────────────────────────────────────────────────────────

def create_rotation_matrix(yaw_deg: float, pitch_deg: float, roll_deg: float = 0.0) -> np.ndarray:
    """Build a 3x3 rotation matrix from Euler angles in degrees.

    Convention matches ``convert_360_to_colmap.py`` and
    ``lichtfeld-360-plugin/core/reframer.py``: ``Rz @ Rx @ Ry`` where yaw
    rotates around the y-axis, pitch around the x-axis, and roll around z.
    The camera frame is y-down (positive y points downward in the image),
    matching the COLMAP / Metashape camera convention. In that frame,
    positive pitch tilts the view *up* and negative pitch tilts it down,
    so the reframe lower ring at pitch=-35° looks toward the ground.

    For (yaw=0, pitch=0) the result is the identity — the view looks down
    the source camera's +Z axis.
    """
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    roll = math.radians(roll_deg)

    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)

    ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]])
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cp, -sp], [0.0, sp, cp]])
    rz = np.array([[cr, -sr, 0.0], [sr, cr, 0.0], [0.0, 0.0, 1.0]])
    return rz @ rx @ ry


def view_basis_columns(yaw_deg: float, pitch_deg: float) -> Tuple[Tuple[float, float, float], ...]:
    """Return the per-view basis as the column-major tuple format used by
    :data:`FACE_BASIS_SOURCE_FROM_FACE`.

    The basis matrix is the rotation that takes vectors in the per-view
    camera frame into the source-camera frame. Columns of the matrix are
    the view's +X, +Y, +Z axes expressed in the source camera frame, so the
    +Z column points along the view's forward direction.
    """
    rotation = create_rotation_matrix(yaw_deg, pitch_deg, 0.0)
    return (
        (float(rotation[0, 0]), float(rotation[1, 0]), float(rotation[2, 0])),
        (float(rotation[0, 1]), float(rotation[1, 1]), float(rotation[2, 1])),
        (float(rotation[0, 2]), float(rotation[1, 2]), float(rotation[2, 2])),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Basis-dict registration
# ─────────────────────────────────────────────────────────────────────────────

def register_erp_face_entries(face_basis_dict: Dict[str, Tuple[Tuple[float, float, float], ...]]) -> None:
    """Populate ``face_basis_dict`` with entries for every ERP view in both
    presets, keyed by :func:`view_label`.

    Called once at module load by ``metashape_cameras_to_colmap.py`` so the
    shared pose-composition function ``face_world_to_camera_pose`` can resolve
    angle-keyed view labels alongside the fisheye cube-face labels.
    """
    for views in (CUBEMAP_VIEWS, REFRAME_VIEWS):
        for yaw, pitch, _fov in views:
            face_basis_dict[view_label(yaw, pitch)] = view_basis_columns(yaw, pitch)


# ─────────────────────────────────────────────────────────────────────────────
# Image reprojection
# ─────────────────────────────────────────────────────────────────────────────

def _build_remap_tables(
    erp_width: int,
    erp_height: int,
    fov_deg: float,
    yaw_deg: float,
    pitch_deg: float,
    out_size: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Precompute (map_x, map_y) lookup tables for ``cv2.remap``.

    The tables depend only on view geometry and ERP dimensions, so they are
    cached per (preset, ERP size) and reused for every source frame.
    """
    fov_rad = math.radians(fov_deg)
    focal = (out_size / 2.0) / math.tan(fov_rad / 2.0)

    u = np.arange(out_size, dtype=np.float64) - out_size / 2.0
    v = np.arange(out_size, dtype=np.float64) - out_size / 2.0
    uu, vv = np.meshgrid(u, v)

    x = uu
    y = vv  # y-down — matches COLMAP / Metashape camera convention
    z = np.full_like(uu, focal, dtype=np.float64)

    xyz = np.stack([x, y, z], axis=-1)
    rotation = create_rotation_matrix(yaw_deg, pitch_deg, 0.0)
    xyz_world = xyz @ rotation.T

    wx, wy, wz = xyz_world[..., 0], xyz_world[..., 1], xyz_world[..., 2]
    theta = np.arctan2(wx, wz)
    norm = np.linalg.norm(xyz_world, axis=-1)
    phi = np.arcsin(np.clip(wy / norm, -1.0, 1.0))

    map_x = ((theta / np.pi + 1.0) / 2.0 * erp_width).astype(np.float32) % erp_width
    map_y = np.clip(((0.5 + phi / np.pi) * erp_height).astype(np.float32), 0.0, erp_height - 1.0)
    return map_x, map_y


def reframe_erp_to_perspective(
    equirect: np.ndarray,
    fov_deg: float,
    yaw_deg: float,
    pitch_deg: float,
    out_size: int,
) -> np.ndarray:
    """Extract one perspective crop from an equirectangular image.

    Uses bilinear interpolation via ``cv2.remap`` with ``BORDER_WRAP`` so the
    seam at yaw = ±180° wraps cleanly. The output is a square ``out_size``
    × ``out_size`` image with a pinhole model: ``f = (out_size/2) / tan(fov/2)``,
    ``cx = cy = out_size/2``.
    """
    h_eq, w_eq = equirect.shape[:2]
    map_x, map_y = _build_remap_tables(w_eq, h_eq, fov_deg, yaw_deg, pitch_deg, out_size)
    return cv2.remap(equirect, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def _reframe_mask(
    mask: np.ndarray,
    map_x: np.ndarray,
    map_y: np.ndarray,
    erode_kernel_size: int = 9,
) -> np.ndarray:
    """Reproject a binary mask using precomputed tables.

    Uses ``cv2.INTER_NEAREST`` to preserve hard edges, then erodes the keep
    region by a ``erode_kernel_size`` × ``erode_kernel_size`` elliptical
    kernel to catch segmentation artifacts that bleed across the boundary
    (the LichtFeld 360 plugin's approach — see ``core/reframer.py`` for
    precedent).
    """
    reprojected = cv2.remap(mask, map_x, map_y, cv2.INTER_NEAREST, borderMode=cv2.BORDER_WRAP)
    if reprojected.ndim == 3:
        reprojected = reprojected[..., 0]
    binarised = (reprojected >= 128).astype(np.uint8) * 255
    if erode_kernel_size > 1:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (erode_kernel_size, erode_kernel_size))
        binarised = cv2.erode(binarised, kernel)
    return binarised


# ─────────────────────────────────────────────────────────────────────────────
# Pinhole intrinsics
# ─────────────────────────────────────────────────────────────────────────────

def pinhole_intrinsics(fov_deg: float, out_size: int) -> Tuple[float, float, float, float]:
    """Return (fx, fy, cx, cy) for the PINHOLE COLMAP camera model.

    All ERP views with the same (fov, out_size) share these intrinsics, so
    one PINHOLE camera record per view-slot suffices (not per source frame).
    """
    focal = out_size / (2.0 * math.tan(math.radians(fov_deg) / 2.0))
    center = out_size / 2.0
    return focal, focal, center, center


# ─────────────────────────────────────────────────────────────────────────────
# Batch processing
# ─────────────────────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff")


def _iter_images(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        return ()
    return sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def _match_mask_path(stem: str, mask_dir: Optional[Path]) -> Optional[Path]:
    """Find a mask whose stem matches the image stem (case-insensitive).

    Accepts the same extensions as images. Returns ``None`` if no mask is
    found or ``mask_dir`` is missing.
    """
    if mask_dir is None or not mask_dir.is_dir():
        return None
    stem_lower = stem.lower()
    for path in mask_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if path.stem.lower() == stem_lower:
            return path
    return None


@dataclass
class _ViewInfo:
    yaw: float
    pitch: float
    fov: float
    label: str
    dir_name: str
    images_dir: Path
    masks_dir: Path


def _prepare_views(output_dir: Path, views: Sequence[Tuple[float, float, float]]) -> List[_ViewInfo]:
    prepared = []
    for yaw, pitch, fov in views:
        dir_name = view_filename_suffix(yaw, pitch)
        view_root = output_dir / dir_name
        images_dir = view_root / "images"
        masks_dir = view_root / "masks"
        images_dir.mkdir(parents=True, exist_ok=True)
        prepared.append(_ViewInfo(
            yaw=yaw, pitch=pitch, fov=fov,
            label=view_label(yaw, pitch),
            dir_name=dir_name,
            images_dir=images_dir,
            masks_dir=masks_dir,
        ))
    return prepared


def process_equirect_sensor(
    image_dirs: Sequence[Path],
    mask_dirs: Sequence[Path],
    split_mode: str,
    split_width: int,
    output_dir: Path,
    force: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, object]:
    """Split every ERP image in ``image_dirs`` into perspective crops.

    Args:
        image_dirs: Source directories of ERP frames. Files are matched by
            stem across all directories (so each frame appears once per
            stem regardless of which input dir it came from).
        mask_dirs: Parallel to ``image_dirs``. ``mask_dirs[i]`` provides
            masks for ``image_dirs[i]``; if shorter, later dirs have no mask.
        split_mode: ``"cubemap"`` (6 views) or ``"reframe"`` (16 views).
        split_width: Output crop dimension. Each crop is split_width ×
            split_width with a square pinhole at the supplied FOV.
        output_dir: Root for the ``view_<label>/images/<stem>.png``
            directory tree.
        force: If ``True``, overwrite existing crops; otherwise skip stems
            whose first-view output is already present.
        progress_callback: Optional callable that receives status strings.

    Returns:
        Dict with keys:
            - ``views``: list of {label, yaw, pitch, fov, fx, fy, cx, cy,
              width, height, dir_name, image_count}.
            - ``processed``: count of source images processed.
            - ``skipped``: count of source images skipped (already done).
            - ``stems``: tuple of all source stems written.
            - ``split_mode``: echo of input.
            - ``split_width``: echo of input.
    """
    if split_mode not in SPLIT_MODE_VIEWS:
        raise ValueError(
            f"Unknown split_mode {split_mode!r}; expected one of "
            f"{sorted(SPLIT_MODE_VIEWS)}"
        )
    views_geom = SPLIT_MODE_VIEWS[split_mode]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    views = _prepare_views(output_dir, views_geom)

    # Pair (image, mask) per source directory, deduplicating by stem.
    by_stem: Dict[str, Tuple[Path, Optional[Path]]] = {}
    for i, img_dir in enumerate(image_dirs):
        img_dir = Path(img_dir)
        if not img_dir.is_dir():
            if progress_callback:
                progress_callback(f"WARNING: image dir {img_dir} not found, skipping")
            continue
        mask_dir = Path(mask_dirs[i]) if i < len(mask_dirs) and mask_dirs[i] else None
        for image_path in _iter_images(img_dir):
            stem = image_path.stem
            if stem in by_stem:
                if progress_callback:
                    progress_callback(
                        f"WARNING: duplicate stem {stem} across image_dirs, keeping first"
                    )
                continue
            mask_path = _match_mask_path(stem, mask_dir)
            by_stem[stem] = (image_path, mask_path)

    if not by_stem:
        if progress_callback:
            progress_callback("No ERP source images found")
        return {
            "views": [],
            "processed": 0,
            "skipped": 0,
            "stems": (),
            "split_mode": split_mode,
            "split_width": split_width,
        }

    # Read one image to get ERP dimensions, then precompute remap tables once
    # per view. Tables depend only on (erp_w, erp_h, fov, yaw, pitch, out_size)
    # so they amortise over every source frame.
    first_path = next(iter(by_stem.values()))[0]
    first_image = cv2.imread(str(first_path), cv2.IMREAD_UNCHANGED)
    if first_image is None:
        raise ValueError(f"Could not read first ERP image {first_path}")
    erp_height, erp_width = first_image.shape[:2]

    remap_tables: List[Tuple[np.ndarray, np.ndarray]] = []
    for view in views:
        remap_tables.append(
            _build_remap_tables(erp_width, erp_height, view.fov, view.yaw, view.pitch, split_width)
        )

    processed = 0
    skipped = 0
    written_stems: List[str] = []
    for stem, (image_path, mask_path) in by_stem.items():
        # Cheap skip-check: if every view's image output for this stem exists
        # and ``force`` is off, skip.
        first_view_out = views[0].images_dir / f"{stem}.png"
        if first_view_out.is_file() and not force:
            skipped += 1
            continue

        erp_image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
        if erp_image is None:
            if progress_callback:
                progress_callback(f"WARNING: could not read {image_path}, skipping")
            continue
        if erp_image.shape[:2] != (erp_height, erp_width):
            # Image dimensions changed mid-batch — rebuild tables for this image
            # only. Uncommon but possible if the user mixes resolutions.
            erp_height, erp_width = erp_image.shape[:2]
            remap_tables = [
                _build_remap_tables(erp_width, erp_height, v.fov, v.yaw, v.pitch, split_width)
                for v in views
            ]

        erp_mask = None
        if mask_path is not None:
            erp_mask = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED)
            if erp_mask is None and progress_callback:
                progress_callback(f"WARNING: could not read mask {mask_path}")

        for view, (map_x, map_y) in zip(views, remap_tables):
            crop = cv2.remap(erp_image, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
            cv2.imwrite(str(view.images_dir / f"{stem}.png"), crop)
            if erp_mask is not None:
                view.masks_dir.mkdir(parents=True, exist_ok=True)
                reprojected = _reframe_mask(erp_mask, map_x, map_y, erode_kernel_size=9)
                cv2.imwrite(str(view.masks_dir / f"{stem}.png"), reprojected)

        processed += 1
        written_stems.append(stem)
        if progress_callback and processed % 25 == 0:
            progress_callback(f"Processed {processed} ERP frames")

    # Final view metadata: includes intrinsics so the exporter can register a
    # PINHOLE camera per view-slot without re-deriving them.
    view_records: List[Dict[str, object]] = []
    for view in views:
        fx, fy, cx, cy = pinhole_intrinsics(view.fov, split_width)
        view_records.append({
            "label": view.label,
            "yaw": view.yaw,
            "pitch": view.pitch,
            "fov": view.fov,
            "fx": fx, "fy": fy, "cx": cx, "cy": cy,
            "width": split_width,
            "height": split_width,
            "dir_name": view.dir_name,
            "images_dir": str(view.images_dir),
            "masks_dir": str(view.masks_dir) if view.masks_dir.is_dir() else None,
            "image_count": sum(1 for _ in _iter_images(view.images_dir)),
        })

    return {
        "views": view_records,
        "processed": processed,
        "skipped": skipped,
        "stems": tuple(written_stems),
        "split_mode": split_mode,
        "split_width": split_width,
    }
