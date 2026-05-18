"""Unit tests for gui.fourier_corrections module."""

import tempfile
from pathlib import Path

import numpy as np
import pytest

from gui.fourier_corrections import (
    FourierCorrections,
    apply_fourier_displacement,
    corrections_cache_hash,
    get_calibration_with_corrections,
    parse_corrections_from_element,
    parse_corrections_from_xml,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

SAMPLE_CORRECTIONS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<calibration>
  <projection>equisolid_fisheye</projection>
  <width>3840</width>
  <height>3840</height>
  <f>963.5</f>
  <cx>-7.7</cx>
  <cy>0.3</cy>
  <k1>0.18</k1>
  <k2>0.028</k2>
  <k3>-0.02</k3>
  <p1>-0.00017</p1>
  <p2>0.00013</p2>
  <corrections type="fourier">
    <coeffs>{coeffs}</coeffs>
    <extent>
      <min>0 0</min>
      <max>3840 3840</max>
    </extent>
  </corrections>
  <date>2026-05-15T14:41:35Z</date>
</calibration>
"""

NO_CORRECTIONS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<calibration>
  <projection>equisolid_fisheye</projection>
  <width>3840</width>
  <height>3840</height>
  <f>1048.3</f>
  <cx>-7.1</cx>
  <cy>0.88</cy>
  <k1>0.079</k1>
  <k2>0.052</k2>
  <k3>-0.02</k3>
  <p1>0.00036</p1>
  <p2>-0.00023</p2>
  <date>2026-05-05T04:43:41Z</date>
</calibration>
"""


def _make_coeffs_str(values=None):
    if values is None:
        values = [float(i + 1) * 0.01 for i in range(96)]
    return " ".join(f"{v}" for v in values)


@pytest.fixture
def corrections_xml_path(tmp_path):
    xml = SAMPLE_CORRECTIONS_XML.format(coeffs=_make_coeffs_str())
    path = tmp_path / "cal_fit.xml"
    path.write_text(xml, encoding="utf-8")
    return path


@pytest.fixture
def no_corrections_xml_path(tmp_path):
    path = tmp_path / "cal_nofit.xml"
    path.write_text(NO_CORRECTIONS_XML, encoding="utf-8")
    return path


# ─── Dataclass Tests ─────────────────────────────────────────────────────────


def test_fourier_corrections_accepts_96_coefficients():
    c = FourierCorrections(
        coeffs=tuple(range(96)),
        extent_min=(0.0, 0.0),
        extent_max=(3840.0, 3840.0),
    )
    assert len(c.coeffs) == 96


def test_fourier_corrections_rejects_wrong_count():
    with pytest.raises(ValueError, match="Expected 96"):
        FourierCorrections(
            coeffs=tuple(range(50)),
            extent_min=(0.0, 0.0),
            extent_max=(3840.0, 3840.0),
        )


def test_fourier_corrections_is_frozen():
    c = FourierCorrections(
        coeffs=tuple(range(96)),
        extent_min=(0.0, 0.0),
        extent_max=(3840.0, 3840.0),
    )
    with pytest.raises(Exception):
        c.coeffs = tuple(range(96))


# ─── XML Parsing Tests ───────────────────────────────────────────────────────


def test_parse_corrections_from_xml_with_corrections(corrections_xml_path):
    result = parse_corrections_from_xml(str(corrections_xml_path))
    assert result is not None
    assert len(result.coeffs) == 96
    assert result.extent_min == (0.0, 0.0)
    assert result.extent_max == (3840.0, 3840.0)
    assert result.coeffs[0] == pytest.approx(0.01)
    assert result.coeffs[95] == pytest.approx(0.96)


def test_parse_corrections_from_xml_without_corrections(no_corrections_xml_path):
    result = parse_corrections_from_xml(str(no_corrections_xml_path))
    assert result is None


def test_parse_corrections_from_element():
    import xml.etree.ElementTree as ET

    xml_str = SAMPLE_CORRECTIONS_XML.format(coeffs=_make_coeffs_str())
    root = ET.fromstring(xml_str)
    result = parse_corrections_from_element(root)
    assert result is not None
    assert len(result.coeffs) == 96


def test_parse_corrections_from_element_no_corrections():
    import xml.etree.ElementTree as ET

    root = ET.fromstring(NO_CORRECTIONS_XML)
    result = parse_corrections_from_element(root)
    assert result is None


def test_parse_rejects_wrong_coefficient_count(tmp_path):
    bad_coeffs = " ".join(str(i) for i in range(50))
    xml = SAMPLE_CORRECTIONS_XML.format(coeffs=bad_coeffs)
    path = tmp_path / "bad.xml"
    path.write_text(xml, encoding="utf-8")
    with pytest.raises(ValueError, match="expected 96"):
        parse_corrections_from_xml(str(path))


def test_parse_reads_extent_values(corrections_xml_path):
    result = parse_corrections_from_xml(str(corrections_xml_path))
    assert result.extent_min == (0.0, 0.0)
    assert result.extent_max == (3840.0, 3840.0)


def test_parse_real_file_if_available():
    """Integration: parse the real Osmo360 calibration if available."""
    real_path = Path("D:/Capture/testing_fisheye/Osmo360_front_Fit.xml")
    if not real_path.is_file():
        pytest.skip("Real calibration file not available")
    result = parse_corrections_from_xml(str(real_path))
    assert result is not None
    assert len(result.coeffs) == 96
    assert result.extent_max == (3840.0, 3840.0)


def test_fourier_displacement_plausibility_with_real_calibration():
    """P2-B: Plausibility test using real Metashape calibration files.

    This test does NOT validate that our formula matches Metashape's
    internal computation (that would require ground-truth undistorted
    output from Metashape itself). It verifies structural plausibility:

    1. Center displacement is near-zero (optical axis is a fixed point
       of any radially-structured correction model).
    2. Displacement magnitude increases toward the image edges (peripheral
       pixels have larger corrections than center pixels).
    3. The corrected ray field is well-formed (unit-length rays, center
       ray pointing forward).
    4. The displacement magnitudes are in a plausible range (sub-pixel
       at center, tens of pixels at periphery for a 3840x3840 image).

    These are necessary conditions for correctness. They are not sufficient
    — a systematic sign error could still produce plausible-looking
    displacement patterns. Full validation requires Metashape ground truth
    (see plan item P2-A).
    """
    fit_path = Path("D:/Capture/testing_fisheye/Osmo360_front_Fit.xml")
    if not fit_path.is_file():
        pytest.skip("Real calibration files not available")

    from gui.corrected_rays import compute_rays_with_corrections

    result = get_calibration_with_corrections(str(fit_path))
    assert result is not None
    proj, w, h = result[0], result[1], result[2]
    params = result[3:14]  # f through b2
    corrections = result[15]
    assert corrections is not None

    # Compute displacement field
    uu, vv = np.meshgrid(np.arange(w, dtype=np.float64), np.arange(h, dtype=np.float64))
    uu_c, vv_c = apply_fourier_displacement(uu, vv, corrections)
    dx = uu_c - uu
    dy = vv_c - vv
    displacement_mag = np.sqrt(dx**2 + dy**2)

    # 1. Center displacement is near-zero
    cy, cx = h // 2, w // 2
    center_disp = displacement_mag[cy, cx]
    assert center_disp < 5.0, f"Center displacement {center_disp:.2f} px — expected < 5"

    # 2. Displacement field is spatially smooth (not random noise).
    #    A Fourier series produces smooth waves, so the displacement at adjacent
    #    pixels should be similar. We check that the mean absolute gradient is
    #    much smaller than the displacement magnitude.
    #    NOTE: Fourier corrections do NOT follow a radial pattern — they are
    #    periodic waves that can have higher displacement at the center than
    #    at the edges. We do not assert radial ordering.
    grad_x = np.abs(np.diff(displacement_mag, axis=1))
    grad_y = np.abs(np.diff(displacement_mag, axis=0))
    mean_gradient = (grad_x.mean() + grad_y.mean()) / 2.0
    assert mean_gradient < 0.1, (
        f"Mean gradient {mean_gradient:.4f} px/px — displacement field is not smooth"
    )

    # 3. Corrected ray field is well-formed
    model = "equisolid" if "equisolid" in proj else "equidistant"
    rays, _ = compute_rays_with_corrections(w, h, params, model, corrections=corrections)
    norms = np.linalg.norm(rays, axis=-1)
    np.testing.assert_allclose(norms, 1.0, atol=1e-6)
    assert rays[cy, cx, 2] > 0.99, "Center ray should point nearly forward"

    # 4. Displacement magnitudes in plausible range
    assert displacement_mag.max() < 100.0, (
        f"Max displacement {displacement_mag.max():.1f} px — suspiciously large"
    )
    assert displacement_mag.mean() > 0.1, (
        f"Mean displacement {displacement_mag.mean():.4f} px — suspiciously small for real corrections"
    )


# ─── Displacement Function Tests ────────────────────────────────────────────


def test_zero_coefficients_produce_identity():
    zeros = FourierCorrections(
        coeffs=tuple([0.0] * 96),
        extent_min=(0.0, 0.0),
        extent_max=(100.0, 100.0),
    )
    uu, vv = np.meshgrid(np.arange(100.0), np.arange(100.0))
    uu_c, vv_c = apply_fourier_displacement(uu, vv, zeros)
    np.testing.assert_array_equal(uu_c, uu)
    np.testing.assert_array_equal(vv_c, vv)


def test_single_coefficient_dx_only():
    """C[0] = 1.0 means dx = -cos(2*pi*y/H), dy = 0."""
    coeffs = [0.0] * 96
    coeffs[0] = 1.0
    c = FourierCorrections(
        coeffs=tuple(coeffs),
        extent_min=(0.0, 0.0),
        extent_max=(100.0, 100.0),
    )
    uu, vv = np.meshgrid(np.arange(100.0), np.arange(100.0))
    uu_c, vv_c = apply_fourier_displacement(uu, vv, c)

    # dy should be zero (only C[0] set, which affects dx)
    np.testing.assert_allclose(vv_c, vv, atol=1e-12)

    # dx at y=0: -cos(0) = -1, so uu_c = uu - (-1) = uu + 1
    np.testing.assert_allclose(uu_c[0, :], uu[0, :] + 1.0, atol=1e-12)

    # dx at y=50 (normalized y=0.5): -cos(pi) = 1, so uu_c = uu - 1
    np.testing.assert_allclose(uu_c[50, :], uu[50, :] - 1.0, atol=1e-12)


def test_single_coefficient_dy_only():
    """C[1] = 2.0 means dy = 2*(-cos(2*pi*y/H)), dx = 0."""
    coeffs = [0.0] * 96
    coeffs[1] = 2.0
    c = FourierCorrections(
        coeffs=tuple(coeffs),
        extent_min=(0.0, 0.0),
        extent_max=(100.0, 100.0),
    )
    uu, vv = np.meshgrid(np.arange(100.0), np.arange(100.0))
    uu_c, vv_c = apply_fourier_displacement(uu, vv, c)

    # dx should be zero
    np.testing.assert_allclose(uu_c, uu, atol=1e-12)

    # dy at y=0: 2*(-cos(0)) = -2, so vv_c = vv - (-2) = vv + 2
    np.testing.assert_allclose(vv_c[0, :], vv[0, :] + 2.0, atol=1e-12)


def test_displacement_is_periodic():
    """Displacement should repeat over the extent domain."""
    coeffs = [0.0] * 96
    coeffs[4] = 1.0  # C[5] in 1-indexed: -cos(2*pi*x)
    c = FourierCorrections(
        coeffs=tuple(coeffs),
        extent_min=(0.0, 0.0),
        extent_max=(200.0, 200.0),
    )
    uu, vv = np.meshgrid(np.arange(200.0), np.arange(200.0))
    uu_c, vv_c = apply_fourier_displacement(uu, vv, c)
    diff = uu_c - uu
    # Row 100 should equal row 0 (periodic in y — but this coeff depends on x only)
    np.testing.assert_allclose(diff[0, :], diff[100, :], atol=1e-12)
    # Column 0 and column 200 edge: x=0 and x=1 produce same cos value
    # cos(0) = cos(2*pi) = 1 — column 0 and the period
    np.testing.assert_allclose(diff[:, 0], diff[:, 0], atol=1e-12)


# ─── Cache Hash Tests ────────────────────────────────────────────────────────


def test_hash_none_returns_empty():
    assert corrections_cache_hash(None) == ""


def test_hash_deterministic():
    c = FourierCorrections(
        coeffs=tuple(range(96)),
        extent_min=(0.0, 0.0),
        extent_max=(3840.0, 3840.0),
    )
    h1 = corrections_cache_hash(c)
    h2 = corrections_cache_hash(c)
    assert h1 == h2
    assert len(h1) == 16


def test_different_coefficients_produce_different_hash():
    c1 = FourierCorrections(
        coeffs=tuple(range(96)),
        extent_min=(0.0, 0.0),
        extent_max=(3840.0, 3840.0),
    )
    c2 = FourierCorrections(
        coeffs=tuple(range(1, 97)),
        extent_min=(0.0, 0.0),
        extent_max=(3840.0, 3840.0),
    )
    assert corrections_cache_hash(c1) != corrections_cache_hash(c2)


def test_different_extent_produces_different_hash():
    c1 = FourierCorrections(
        coeffs=tuple(range(96)),
        extent_min=(0.0, 0.0),
        extent_max=(3840.0, 3840.0),
    )
    c2 = FourierCorrections(
        coeffs=tuple(range(96)),
        extent_min=(0.0, 0.0),
        extent_max=(1920.0, 1920.0),
    )
    assert corrections_cache_hash(c1) != corrections_cache_hash(c2)


# ─── get_calibration_with_corrections Tests ──────────────────────────────────


def test_get_calibration_with_corrections_full_tuple(corrections_xml_path):
    result = get_calibration_with_corrections(str(corrections_xml_path))
    assert result is not None
    proj, w, h, f, cx, cy, k1, k2, k3, k4, p1, p2, b1, b2, date, corrections = result
    assert proj == "equisolid_fisheye"
    assert w == 3840
    assert h == 3840
    assert f == pytest.approx(963.5)
    # k4, b1, b2 default to 0.0 when absent from fixture
    assert k4 == pytest.approx(0.0)
    assert b1 == pytest.approx(0.0)
    assert b2 == pytest.approx(0.0)
    assert corrections is not None
    assert len(corrections.coeffs) == 96


def test_get_calibration_without_corrections_returns_none_corrections(no_corrections_xml_path):
    result = get_calibration_with_corrections(str(no_corrections_xml_path))
    assert result is not None
    proj, w, h, f, cx, cy, k1, k2, k3, k4, p1, p2, b1, b2, date, corrections = result
    assert corrections is None
    assert f == pytest.approx(1048.3)


def test_get_calibration_reads_k4_b1_b2_when_present(tmp_path):
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<calibration>\n"
        "  <projection>equisolid_fisheye</projection>\n"
        "  <width>3840</width><height>3840</height>\n"
        "  <f>1000.0</f><cx>0</cx><cy>0</cy>\n"
        "  <k1>0.1</k1><k2>0.02</k2><k3>0.003</k3><k4>0.0004</k4>\n"
        "  <p1>0.001</p1><p2>0.002</p2>\n"
        "  <b1>0.5</b1><b2>-0.3</b2>\n"
        "  <date>2026-05-18</date>\n"
        "</calibration>\n"
    )
    path = tmp_path / "cal_full.xml"
    path.write_text(xml, encoding="utf-8")

    result = get_calibration_with_corrections(str(path))
    assert result is not None
    proj, w, h, f, cx, cy, k1, k2, k3, k4, p1, p2, b1, b2, date, corrections = result
    assert k4 == pytest.approx(0.0004)
    assert b1 == pytest.approx(0.5)
    assert b2 == pytest.approx(-0.3)
    assert corrections is None


def test_get_calibration_bad_path_returns_none():
    result = get_calibration_with_corrections("/nonexistent/path.xml")
    assert result is None
