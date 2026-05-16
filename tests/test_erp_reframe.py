"""Math-level unit tests for gui/erp_reframe.py.

These tests catch typos and shape errors before integration testing on real
ERP datasets. They do NOT catch coordinate convention bugs (axis flips,
pose composition order, etc.) — those require running the exporter on a
real dataset and inspecting the COLMAP output. See
``docs/superpowers/plans/2026-05-14-colmap-export-tab-redesign.md`` Task 6
Layer B for the integration-test checklist.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gui.erp_reframe import (  # noqa: E402
    CUBEMAP_VIEWS,
    REFRAME_VIEWS,
    SPLIT_MODE_VIEWS,
    create_rotation_matrix,
    pinhole_intrinsics,
    process_equirect_sensor,
    reframe_erp_to_perspective,
    register_erp_face_entries,
    view_basis_columns,
    view_filename_suffix,
    view_label,
)


# ─────────────────────────────────────────────────────────────────────────────
# Preset structure
# ─────────────────────────────────────────────────────────────────────────────

def test_cubemap_has_six_views():
    assert len(CUBEMAP_VIEWS) == 6


def test_reframe_has_sixteen_views():
    assert len(REFRAME_VIEWS) == 16


def test_reframe_two_rings_eight_views_each():
    """8 views per ring, ±35° pitch, no zenith/nadir."""
    pitches = sorted({pitch for _yaw, pitch, _fov in REFRAME_VIEWS})
    assert pitches == [-35.0, 35.0]
    for pitch in pitches:
        count = sum(1 for _yaw, p, _fov in REFRAME_VIEWS if p == pitch)
        assert count == 8


def test_reframe_yaw_stagger_is_22_5_degrees():
    """Lower ring at integer multiples of 45°; upper ring offset by 22.5°."""
    lower = sorted(yaw for yaw, pitch, _fov in REFRAME_VIEWS if pitch == -35.0)
    upper = sorted(yaw for yaw, pitch, _fov in REFRAME_VIEWS if pitch == 35.0)
    for yaw in lower:
        assert yaw % 45.0 == 0.0, f"lower ring yaw {yaw} not multiple of 45"
    for yaw in upper:
        assert (yaw - 22.5) % 45.0 == 0.0, f"upper ring yaw {yaw} not 22.5+45k"


def test_all_views_have_90_degree_fov():
    for views in (CUBEMAP_VIEWS, REFRAME_VIEWS):
        for _yaw, _pitch, fov in views:
            assert fov == 90.0


# ─────────────────────────────────────────────────────────────────────────────
# Label formatting
# ─────────────────────────────────────────────────────────────────────────────

def test_view_label_zero_zero():
    assert view_label(0.0, 0.0) == "yaw000.0_pitch+00"


def test_view_label_half_degree_yaw():
    assert view_label(22.5, -35.0) == "yaw022.5_pitch-35"


def test_view_label_negative_yaw():
    assert view_label(-157.5, 35.0) == "yaw-157.5_pitch+35"


def test_view_label_zenith():
    assert view_label(0.0, 90.0) == "yaw000.0_pitch+90"


def test_view_filename_suffix_has_view_prefix():
    assert view_filename_suffix(0.0, 0.0) == "view_yaw000.0_pitch+00"


def test_all_preset_labels_are_unique():
    """No two views in either preset should collide on the label key —
    that would corrupt the FACE_BASIS_SOURCE_FROM_FACE dict."""
    for views in (CUBEMAP_VIEWS, REFRAME_VIEWS):
        labels = [view_label(y, p) for y, p, _f in views]
        assert len(labels) == len(set(labels))


# ─────────────────────────────────────────────────────────────────────────────
# Rotation math
# ─────────────────────────────────────────────────────────────────────────────

def test_identity_rotation():
    """(yaw=0, pitch=0) -> identity matrix."""
    rotation = create_rotation_matrix(0.0, 0.0)
    np.testing.assert_allclose(rotation, np.eye(3), atol=1e-12)


def test_basis_columns_format():
    """view_basis_columns returns a 3-tuple of 3-tuples of floats."""
    basis = view_basis_columns(0.0, 0.0)
    assert len(basis) == 3
    for col in basis:
        assert len(col) == 3
        for value in col:
            assert isinstance(value, float)


def test_basis_at_origin_is_identity():
    basis = view_basis_columns(0.0, 0.0)
    # +X column = (1, 0, 0), +Y column = (0, 1, 0), +Z column = (0, 0, 1)
    assert basis[0] == (1.0, 0.0, 0.0)
    assert basis[1] == (0.0, 1.0, 0.0)
    assert basis[2] == (0.0, 0.0, 1.0)


def test_yaw_90_rotates_forward_to_x_axis():
    """At yaw=90°, the view's +Z (forward) should point along source's +X.

    This is the same convention the existing fisheye '+X' face uses
    (basis = ((0,0,-1),(0,1,0),(1,0,0))) — the +Z column ((1,0,0)) is
    source's +X axis.
    """
    basis = view_basis_columns(90.0, 0.0)
    forward = np.array(basis[2])  # +Z column = view's forward in source frame
    np.testing.assert_allclose(forward, [1.0, 0.0, 0.0], atol=1e-12)


def test_yaw_180_rotates_forward_to_negative_z():
    basis = view_basis_columns(180.0, 0.0)
    forward = np.array(basis[2])
    np.testing.assert_allclose(forward, [0.0, 0.0, -1.0], atol=1e-12)


def test_yaw_90_with_pitch_does_not_gimbal_lock():
    """Regression: an earlier construction using Rz @ Rx @ Ry collapsed the
    pitch component to zero at yaw=±90 (forward → world X axis, pitch around
    world X has no effect → reframe lower ring yaw=90, pitch=-35 became
    indistinguishable from yaw=90, pitch=0).

    With the spherical-forward construction, the pitch component must remain
    non-zero at every yaw. For the reframe medium preset's yaw=90, pitch=-35
    view, the forward direction should be approximately
    (cos(35°), sin(35°), 0) = (0.819, 0.574, 0) in Y-down — looking right and
    35° below horizon.
    """
    basis_left  = view_basis_columns(-90.0, -35.0)
    basis_right = view_basis_columns(+90.0, -35.0)
    fwd_left  = np.array(basis_left[2])
    fwd_right = np.array(basis_right[2])
    # X has the expected magnitude (cos(35) * sin(±90))
    assert abs(fwd_right[0] - math.cos(math.radians(35))) < 1e-6
    assert abs(fwd_left[0] + math.cos(math.radians(35))) < 1e-6
    # Y has the expected pitch-down component (sin(35) in Y-down)
    assert abs(fwd_right[1] - math.sin(math.radians(35))) < 1e-6
    assert abs(fwd_left[1]  - math.sin(math.radians(35))) < 1e-6
    # Z is approximately 0 (purely lateral)
    assert abs(fwd_right[2]) < 1e-6
    assert abs(fwd_left[2])  < 1e-6


def test_pitch_inverse_does_not_flip_y_sign_at_extreme_yaw():
    """Regression: under the previous Rz @ Rx @ Ry construction, the Y
    component of forward flipped sign past yaw=±90 for non-zero pitch
    (e.g. yaw=135, pitch=-35 gave Y=-0.41 instead of +0.41 in Y-down).
    With the spherical construction, Y carries the same sign for a given
    pitch regardless of yaw — pitch=-35 always means looking *down* in
    Y-down convention.
    """
    for yaw in (-135.0, -90.0, -45.0, 0.0, 45.0, 90.0, 135.0, 180.0):
        basis = view_basis_columns(yaw, -35.0)
        forward = np.array(basis[2])
        # Y component should be positive (looking down) for every yaw at pitch=-35
        assert forward[1] > 0.5, f"yaw={yaw}, pitch=-35 should have +Y (down) component, got {forward[1]:.3f}"
    for yaw in (-157.5, -112.5, -67.5, -22.5, 22.5, 67.5, 112.5, 157.5):
        basis = view_basis_columns(yaw, +35.0)
        forward = np.array(basis[2])
        # Y component should be negative (looking up) for every yaw at pitch=+35
        assert forward[1] < -0.5, f"yaw={yaw}, pitch=+35 should have -Y (up) component, got {forward[1]:.3f}"


# ─────────────────────────────────────────────────────────────────────────────
# Image reprojection
# ─────────────────────────────────────────────────────────────────────────────

def _make_erp_with_quadrants():
    """Build a 100x200 ERP with each yaw quadrant a distinct colour.

    Equirect pixel layout (width=200, height=100):
      - column 0..50  -> theta in (-pi, -pi/2) -> back-left
      - column 50..100 -> theta in (-pi/2, 0) -> front-left
      - column 100..150 -> theta in (0, pi/2) -> front-right
      - column 150..200 -> theta in (pi/2, pi) -> back-right
    """
    erp = np.zeros((100, 200, 3), dtype=np.uint8)
    erp[:, 0:50] = (50, 50, 50)        # back-left (gray)
    erp[:, 50:100] = (0, 0, 255)       # front-left (BGR red)
    erp[:, 100:150] = (0, 0, 255)      # front-right (BGR red) — same half
    erp[:, 150:200] = (50, 50, 50)     # back-right (gray)
    # Now make front (yaw≈0) clearly red and back (yaw≈±180) clearly blue
    erp[:, 50:150] = (0, 0, 255)       # front half = red
    # leave back halves as configured above
    erp[:, 0:50] = (255, 0, 0)         # back-left = blue
    erp[:, 150:200] = (255, 0, 0)      # back-right = blue
    return erp


def test_yaw_zero_samples_front_hemisphere():
    """yaw=0 looks at the centre of the ERP (front of sphere = red half).

    If this fails with blue, my yaw=0 is actually looking backward — a sign
    or coordinate convention bug.
    """
    erp = _make_erp_with_quadrants()
    crop = reframe_erp_to_perspective(erp, fov_deg=60, yaw_deg=0, pitch_deg=0, out_size=32)
    center = crop[16, 16]  # BGR
    # Red dominant: B<R and G<R
    assert center[2] > center[0], (
        f"yaw=0 centre pixel not red-dominant (BGR={tuple(int(v) for v in center)})"
    )


def test_yaw_180_samples_back_hemisphere():
    """yaw=180 looks at the opposite side of the sphere (back = blue half)."""
    erp = _make_erp_with_quadrants()
    crop = reframe_erp_to_perspective(erp, fov_deg=60, yaw_deg=180, pitch_deg=0, out_size=32)
    center = crop[16, 16]
    # Blue dominant: R<B and G<B
    assert center[0] > center[2], (
        f"yaw=180 centre pixel not blue-dominant (BGR={tuple(int(v) for v in center)})"
    )


def test_output_size():
    erp = _make_erp_with_quadrants()
    crop = reframe_erp_to_perspective(erp, fov_deg=90, yaw_deg=0, pitch_deg=0, out_size=64)
    assert crop.shape == (64, 64, 3)


# ─────────────────────────────────────────────────────────────────────────────
# Pinhole intrinsics
# ─────────────────────────────────────────────────────────────────────────────

def test_pinhole_intrinsics_90_fov_1920():
    """The medium-preset doc specifies fx=fy=960 and cx=cy=960 for 90°@1920px."""
    fx, fy, cx, cy = pinhole_intrinsics(90.0, 1920)
    assert fx == pytest.approx(960.0, abs=1e-9)
    assert fy == pytest.approx(960.0, abs=1e-9)
    assert cx == 960.0
    assert cy == 960.0


def test_pinhole_intrinsics_square_centre():
    """cx/cy always sit at out_size/2 regardless of FOV."""
    for size in (256, 512, 1024, 2048):
        _fx, _fy, cx, cy = pinhole_intrinsics(60.0, size)
        assert cx == size / 2.0
        assert cy == size / 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Face-dict registration
# ─────────────────────────────────────────────────────────────────────────────

def test_register_erp_face_entries_populates_dict():
    target = {}
    register_erp_face_entries(target)
    assert len(target) == 22  # 6 cubemap + 16 reframe
    # Every entry is a 3-tuple of 3-tuples
    for label, basis in target.items():
        assert label.startswith("yaw")
        assert len(basis) == 3
        for col in basis:
            assert len(col) == 3


def test_register_erp_face_entries_idempotent():
    """Registering twice into the same dict is a no-op for entries."""
    target = {}
    register_erp_face_entries(target)
    register_erp_face_entries(target)
    assert len(target) == 22


def test_register_erp_face_entries_into_existing_dict_preserves_existing():
    target = {"+Z": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))}
    register_erp_face_entries(target)
    assert target["+Z"] == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    assert len(target) == 23


def test_exporter_module_registers_entries():
    """The exporter's FACE_BASIS_SOURCE_FROM_FACE dict gets ERP entries
    populated at module load."""
    import metashape_cameras_to_colmap as m
    # cubemap entries
    assert "yaw000.0_pitch+00" in m.FACE_BASIS_SOURCE_FROM_FACE  # front
    assert "yaw000.0_pitch+90" in m.FACE_BASIS_SOURCE_FROM_FACE  # zenith
    # reframe entries — half-degree yaws live on the upper ring only
    assert "yaw022.5_pitch+35" in m.FACE_BASIS_SOURCE_FROM_FACE
    assert "yaw-157.5_pitch+35" in m.FACE_BASIS_SOURCE_FROM_FACE
    # the 5 fisheye cube faces survived the rename + ERP registration
    for face in ("+Z", "-X", "+X", "-Y", "+Y"):
        assert face in m.FACE_BASIS_SOURCE_FROM_FACE


# ─────────────────────────────────────────────────────────────────────────────
# Batch processing (integration-lite — exercises the full disk pipeline
# without needing a real ERP dataset)
# ─────────────────────────────────────────────────────────────────────────────

def test_process_equirect_sensor_cubemap_writes_six_view_dirs(tmp_path):
    import cv2
    # Synthetic 4:2 ERP image
    erp = _make_erp_with_quadrants()
    image_dir = tmp_path / "input"
    image_dir.mkdir()
    cv2.imwrite(str(image_dir / "frame_001.jpg"), erp)
    cv2.imwrite(str(image_dir / "frame_002.jpg"), erp)

    output_dir = tmp_path / "output"
    result = process_equirect_sensor(
        image_dirs=[image_dir],
        mask_dirs=[],
        split_mode="cubemap",
        split_width=64,
        output_dir=output_dir,
    )
    assert result["split_mode"] == "cubemap"
    assert len(result["views"]) == 6
    assert result["processed"] == 2
    # Every view dir should exist with 2 image files
    for view in result["views"]:
        images_dir = Path(view["images_dir"])
        assert images_dir.is_dir(), f"Missing view dir: {images_dir}"
        files = list(images_dir.iterdir())
        assert len(files) == 2, f"Expected 2 frames in {images_dir}, got {len(files)}"


def test_process_equirect_sensor_reframe_writes_sixteen_view_dirs(tmp_path):
    import cv2
    erp = _make_erp_with_quadrants()
    image_dir = tmp_path / "input"
    image_dir.mkdir()
    cv2.imwrite(str(image_dir / "DJI_0001.jpg"), erp)

    output_dir = tmp_path / "output"
    result = process_equirect_sensor(
        image_dirs=[image_dir],
        mask_dirs=[],
        split_mode="reframe",
        split_width=32,
        output_dir=output_dir,
    )
    assert result["split_mode"] == "reframe"
    assert len(result["views"]) == 16
    assert result["processed"] == 1


def test_process_equirect_sensor_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError, match="Unknown split_mode"):
        process_equirect_sensor(
            image_dirs=[tmp_path],
            mask_dirs=[],
            split_mode="bogus",
            split_width=64,
            output_dir=tmp_path / "out",
        )


def test_process_equirect_sensor_skips_already_processed(tmp_path):
    """Second call with the same inputs should skip frames whose output
    already exists (force=False)."""
    import cv2
    erp = _make_erp_with_quadrants()
    image_dir = tmp_path / "input"
    image_dir.mkdir()
    cv2.imwrite(str(image_dir / "frame_001.jpg"), erp)

    output_dir = tmp_path / "output"
    first = process_equirect_sensor(
        image_dirs=[image_dir], mask_dirs=[],
        split_mode="cubemap", split_width=32, output_dir=output_dir,
    )
    second = process_equirect_sensor(
        image_dirs=[image_dir], mask_dirs=[],
        split_mode="cubemap", split_width=32, output_dir=output_dir,
    )
    assert first["processed"] == 1
    assert first["skipped"] == 0
    assert second["processed"] == 0
    assert second["skipped"] == 1


def test_process_equirect_sensor_with_masks(tmp_path):
    """When mask_dirs is parallel to image_dirs, masks get reprojected too."""
    import cv2
    erp = _make_erp_with_quadrants()
    mask = np.full(erp.shape[:2], 255, dtype=np.uint8)
    mask[:, :50] = 0  # left strip = excluded
    image_dir = tmp_path / "input"
    mask_dir = tmp_path / "masks"
    image_dir.mkdir()
    mask_dir.mkdir()
    cv2.imwrite(str(image_dir / "frame_001.jpg"), erp)
    cv2.imwrite(str(mask_dir / "frame_001.png"), mask)

    output_dir = tmp_path / "output"
    result = process_equirect_sensor(
        image_dirs=[image_dir],
        mask_dirs=[mask_dir],
        split_mode="cubemap",
        split_width=32,
        output_dir=output_dir,
    )
    assert result["processed"] == 1
    # At least one view should have a mask written
    views_with_masks = [
        v for v in result["views"]
        if v["masks_dir"] and Path(v["masks_dir"]).is_dir()
        and any(Path(v["masks_dir"]).iterdir())
    ]
    assert views_with_masks, "No mask outputs were written"
