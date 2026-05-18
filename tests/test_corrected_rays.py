"""Unit tests for gui.corrected_rays module."""

import numpy as np
import pytest

from gui.corrected_rays import compute_rays_with_corrections, derive_useful_pixel_mask
from gui.fourier_corrections import FourierCorrections


# Use a small image size for speed
W, H = 200, 200
# Standard equisolid calibration params: (f, cx, cy, K1-K4, P1, P2, B1, B2)
PARAMS = (100.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _zero_corrections(w=W, h=H):
    return FourierCorrections(
        coeffs=tuple([0.0] * 96),
        extent_min=(0.0, 0.0),
        extent_max=(float(w), float(h)),
    )


def _nonzero_corrections(w=W, h=H):
    coeffs = [0.0] * 96
    coeffs[0] = 5.0   # large C[1]: dx = -5*cos(2*pi*y)
    coeffs[10] = 3.0  # C[11]: dx += 3*sin(2*pi*(x+y))
    return FourierCorrections(
        coeffs=tuple(coeffs),
        extent_min=(0.0, 0.0),
        extent_max=(float(w), float(h)),
    )


# ─── compute_rays_with_corrections tests ─────────────────────────────────────


def test_none_corrections_matches_v4():
    """corrections=None should produce identical output to v4.compute_rays."""
    from AM_ImageAndMask_to_cubemap_v4 import compute_rays as v4_compute_rays

    rays_v4, _ = v4_compute_rays(W, H, PARAMS, "equisolid")
    rays_corr, _ = compute_rays_with_corrections(W, H, PARAMS, "equisolid", corrections=None)
    np.testing.assert_array_equal(rays_corr, rays_v4)


def test_zero_corrections_matches_v4():
    """All-zero corrections should produce identical output to uncorrected."""
    from AM_ImageAndMask_to_cubemap_v4 import compute_rays as v4_compute_rays

    rays_v4, _ = v4_compute_rays(W, H, PARAMS, "equisolid")
    rays_corr, _ = compute_rays_with_corrections(
        W, H, PARAMS, "equisolid", corrections=_zero_corrections()
    )
    np.testing.assert_allclose(rays_corr, rays_v4, atol=1e-12)


def test_nonzero_corrections_differ_from_v4():
    """Non-zero corrections should produce different rays."""
    from AM_ImageAndMask_to_cubemap_v4 import compute_rays as v4_compute_rays

    rays_v4, _ = v4_compute_rays(W, H, PARAMS, "equisolid")
    rays_corr, _ = compute_rays_with_corrections(
        W, H, PARAMS, "equisolid", corrections=_nonzero_corrections()
    )
    # Should not be identical
    assert not np.allclose(rays_corr, rays_v4, atol=1e-6)
    # But shape should match
    assert rays_corr.shape == rays_v4.shape


def test_equidistant_model_works():
    """Corrections should work with equidistant model too."""
    rays, _ = compute_rays_with_corrections(
        W, H, PARAMS, "equidistant", corrections=_nonzero_corrections()
    )
    assert rays.shape == (H, W, 3)
    # Center ray should still point roughly forward (+Z)
    assert rays[H // 2, W // 2, 2] > 0.9


def test_pinhole_model_works():
    """Corrections should work with pinhole model."""
    rays, _ = compute_rays_with_corrections(
        W, H, PARAMS, "pinhole", corrections=_nonzero_corrections()
    )
    assert rays.shape == (H, W, 3)


def test_unknown_model_raises():
    with pytest.raises(ValueError, match="Unknown model"):
        compute_rays_with_corrections(W, H, PARAMS, "bogus", corrections=_zero_corrections())


# ─── derive_useful_pixel_mask tests ──────────────────────────────────────────


def test_derive_mask_with_manual_maxangle():
    """Manual maxangle should produce a valid mask."""
    rays, _ = compute_rays_with_corrections(W, H, PARAMS, "equisolid", corrections=None)
    mask, omega, maxangle = derive_useful_pixel_mask(rays, maxangle=45.0)

    assert mask.shape == (H, W)
    assert mask.dtype == np.uint8
    assert maxangle == 45.0
    assert np.any(mask > 0)  # some pixels are valid
    assert omega.shape == (H, W)


def test_derive_mask_without_maxangle():
    """Without maxangle, should derive from ray field."""
    rays, _ = compute_rays_with_corrections(W, H, PARAMS, "equisolid", corrections=None)
    mask, omega, maxangle = derive_useful_pixel_mask(rays)

    assert mask.shape == (H, W)
    assert maxangle > 0
    assert np.any(mask > 0)


def test_derive_mask_with_maskpixelcount():
    """Mask-derived support should use the theta of supported pixels."""
    rays, _ = compute_rays_with_corrections(W, H, PARAMS, "equisolid", corrections=None)

    # Create a circular mask pixel count (center pixels > 0)
    yy, xx = np.mgrid[0:H, 0:W]
    dist = np.sqrt((xx - W / 2) ** 2 + (yy - H / 2) ** 2)
    maskpixelcount = (dist < 50).astype(np.uint16)

    mask, omega, maxangle = derive_useful_pixel_mask(rays, maskpixelcount=maskpixelcount)
    assert mask.shape == (H, W)
    assert maxangle > 0
    # The derived angle should be less than the full geometric extent
    _, _, full_maxangle = derive_useful_pixel_mask(rays)
    assert maxangle <= full_maxangle + 1.0  # allow small dilation tolerance
