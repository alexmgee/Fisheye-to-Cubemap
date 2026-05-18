"""Tests for gui/routing.py adaptive fisheye routing cache."""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


BASE_CALIBRATION = {
    "projection": "equidistant",
    "width": 64,
    "height": 64,
    "f": 32.0,
    "cx": 0.0,
    "cy": 0.0,
    "k1": 0.0,
    "k2": 0.0,
    "k3": 0.0,
    "k4": 0.0,
    "p1": 0.0,
    "p2": 0.0,
    "b1": 0.0,
    "b2": 0.0,
}


def _sensor_record(projection="equidistant"):
    calibration = dict(BASE_CALIBRATION)
    calibration["projection"] = projection
    return {
        "sensor_id": 7,
        "label": "cosmetic-name-not-hashed",
        "prefix": "IMG_",
        "camera_count": 12,
        "camera_ids": [1, 2, 3],
        "camera_labels": ["IMG_0001", "IMG_0002"],
        "calibration": calibration,
    }


def _patch_fast_characteristics(monkeypatch, theta_max_deg=52.0):
    import gui.routing as routing

    calls = {"extract": 0}

    def fake_load_useful_pixel_mask(*args, **kwargs):
        return None

    def fake_extract_lens_characteristics(calibration, useful_pixel_mask=None):
        calls["extract"] += 1
        return {
            "theta_max_deg": theta_max_deg,
            "center_solid_angle": 1e-6,
            "calibration_type": calibration["projection"],
        }

    monkeypatch.setattr(routing, "_load_analysis_useful_pixel_mask", fake_load_useful_pixel_mask)
    monkeypatch.setattr(routing, "extract_lens_characteristics", fake_extract_lens_characteristics)
    routing.clear_cache()
    return routing, calls


def test_routing_uid_is_stable_across_python_hash_seeds():
    """UID hashing must not depend on PYTHONHASHSEED."""
    repo = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(repo)!r}); "
        "from gui.routing import compute_routing_uid; "
        f"cal = {BASE_CALIBRATION!r}; "
        "print(compute_routing_uid(cal, 'mask-digest', (55.0, 3.0, 6000)))"
    )
    env_one = os.environ.copy()
    env_two = os.environ.copy()
    env_one["PYTHONHASHSEED"] = "1"
    env_two["PYTHONHASHSEED"] = "987"

    uid_one = subprocess.check_output(
        [sys.executable, "-c", code],
        text=True,
        env=env_one,
    ).strip()
    uid_two = subprocess.check_output(
        [sys.executable, "-c", code],
        text=True,
        env=env_two,
    ).strip()

    assert uid_one == uid_two
    assert len(uid_one) == 64
    int(uid_one, 16)


def test_mask_digest_is_deterministic_and_changes_with_mask_metadata(tmp_path):
    from gui.routing import compute_mask_digest

    mask_dir = tmp_path / "masks"
    mask_dir.mkdir()
    (mask_dir / "b.png").write_bytes(b"bbb")
    (mask_dir / "a.png").write_bytes(b"a")

    digest_one = compute_mask_digest([mask_dir], None)
    digest_two = compute_mask_digest([mask_dir], None)
    assert digest_one == digest_two
    assert compute_mask_digest(None, None) == compute_mask_digest([], None)

    (mask_dir / "c.png").write_bytes(b"cc")
    digest_three = compute_mask_digest([mask_dir], None)
    assert digest_three != digest_one

    lens_mask = tmp_path / "lens.png"
    lens_mask.write_bytes(b"lens")
    digest_four = compute_mask_digest([mask_dir], lens_mask)
    assert digest_four != digest_three


def test_get_routing_uses_cache_and_invalidates_on_mask_digest_change(monkeypatch, tmp_path):
    routing, calls = _patch_fast_characteristics(monkeypatch)

    first = routing.get_routing(_sensor_record())
    second = routing.get_routing(_sensor_record())

    assert first is second
    assert calls["extract"] == 1

    mask_dir = tmp_path / "masks"
    mask_dir.mkdir()
    (mask_dir / "mask.png").write_bytes(b"not-an-image-but-load-is-patched")

    third = routing.get_routing(_sensor_record(), mask_dirs=[mask_dir])
    assert third is not first
    assert third.routing_uid != first.routing_uid
    assert calls["extract"] == 2


def test_projection_aware_stretch_changes_boundary_decision(monkeypatch):
    routing, _calls = _patch_fast_characteristics(monkeypatch, theta_max_deg=54.5)

    equidistant = routing.get_routing(_sensor_record("equidistant"))
    equisolid = routing.get_routing(_sensor_record("equisolid_fisheye"))

    assert equidistant.processing_mode == "single_pinhole"
    assert equidistant.f_target is not None
    assert equidistant.w_out is not None
    assert equidistant.recommended_output_width == equidistant.w_out
    assert equisolid.processing_mode == "multi_pinhole"
    assert equisolid.f_target is None
    assert equisolid.w_out is None
    assert equisolid.recommended_output_width == 2000


def test_lens_only_mask_changes_uid_and_invalidates_cache(monkeypatch, tmp_path):
    routing, calls = _patch_fast_characteristics(monkeypatch)

    first = routing.get_routing(_sensor_record())

    lens_mask = tmp_path / "lens.png"
    lens_mask.write_bytes(b"not-an-image-but-load-is-patched")
    second = routing.get_routing(_sensor_record(), lens_only_mask=lens_mask)

    assert second is not first
    assert second.routing_uid != first.routing_uid
    assert calls["extract"] == 2


def test_corrections_change_routing_uid():
    """P1-E: Adding Fourier corrections to the same calibration must produce
    a different routing UID."""
    from gui.routing import compute_routing_uid, compute_mask_digest
    from gui.fourier_corrections import FourierCorrections

    cal_without = dict(BASE_CALIBRATION)
    cal_with = dict(BASE_CALIBRATION)
    cal_with["corrections"] = FourierCorrections(
        coeffs=tuple(0.01 * i for i in range(96)),
        extent_min=(0.0, 0.0),
        extent_max=(64.0, 64.0),
    )

    mask_digest = compute_mask_digest(None, None)
    thresholds = (55.0, 3.0, 6000)

    uid_without = compute_routing_uid(cal_without, mask_digest, thresholds)
    uid_with = compute_routing_uid(cal_with, mask_digest, thresholds)

    assert uid_without != uid_with
