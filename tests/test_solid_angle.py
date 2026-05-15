"""Tests for gui/solid_angle.py — optimal cubeface width computation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _equisolid_calibration():
    """A plausible 360-camera equisolid fisheye calibration for tests."""
    return {
        "projection": "equisolid_fisheye",
        "width": 3840,
        "height": 3840,
        "f": 1055.41,
        "cx": -5.32,
        "cy": 0.03,
        "k1": 0.063,
        "k2": 0.065,
        "k3": -0.024,
        "p1": 0.00018,
        "p2": -0.00019,
    }


def test_compute_optimal_width_equisolid_returns_reasonable_int():
    """An equisolid fisheye sensor should produce an even integer width in a sane range."""
    from gui.solid_angle import compute_optimal_width
    width = compute_optimal_width(_equisolid_calibration())
    assert isinstance(width, int)
    # Plausibility band: 360 camera lenses typically land between 1000 and 5000.
    assert 1000 < width < 5000
    # Result should be an even integer (the formula rounds to nearest even).
    assert width % 2 == 0


def test_compute_optimal_width_equidistant_returns_int():
    """Equidistant fisheye also produces an int."""
    from gui.solid_angle import compute_optimal_width
    cal = _equisolid_calibration()
    cal["projection"] = "equidistant"
    width = compute_optimal_width(cal)
    assert isinstance(width, int)
    assert 500 < width < 6000


def test_compute_optimal_width_frame_returns_none():
    """Frame sensors do not undergo reprojection — no meaningful cubeface width."""
    from gui.solid_angle import compute_optimal_width
    cal = {
        "projection": "frame",
        "width": 4000, "height": 3000,
        "f": 3000.0, "cx": 0.0, "cy": 0.0,
        "k1": 0.0, "k2": 0.0, "k3": 0.0, "p1": 0.0, "p2": 0.0,
    }
    assert compute_optimal_width(cal) is None


def test_compute_optimal_width_pinhole_returns_none():
    """'pinhole' is a synonym for frame — same outcome."""
    from gui.solid_angle import compute_optimal_width
    cal = {
        "projection": "pinhole",
        "width": 4000, "height": 3000,
        "f": 3000.0, "cx": 0.0, "cy": 0.0,
    }
    assert compute_optimal_width(cal) is None


def test_compute_optimal_width_equirectangular_returns_int():
    """Equirectangular sensors return an integer split width for cubemap projection."""
    from gui.solid_angle import compute_optimal_width
    cal = {
        "projection": "equirectangular",
        "width": 5760,
        "height": 2880,
        "f": 0.0, "cx": 0.0, "cy": 0.0,
    }
    width = compute_optimal_width(cal)
    assert isinstance(width, int)
    assert width > 0


def test_compute_optimal_width_unknown_projection_raises_or_returns_none():
    """Unknown projection types should not silently return a misleading number."""
    from gui.solid_angle import compute_optimal_width
    cal = {
        "projection": "totally_made_up",
        "width": 2048, "height": 2048, "f": 1024.0,
    }
    try:
        result = compute_optimal_width(cal)
        # If it returns instead of raising, returning None is acceptable.
        assert result is None
    except (ValueError, KeyError):
        pass
