"""Metashape Fourier additional corrections — parsing and application.

When Metashape's 'Fit additional corrections' is enabled during Optimize
Camera Alignment, it estimates 96 Fourier coefficients that define a 2D
periodic pixel displacement beyond what Brown's distortion model captures.

This module:
  - Parses the corrections from standalone calibration XML or cameras.xml
  - Applies the 96-term Fourier displacement to pixel coordinate arrays
  - Provides a deterministic hash for cache invalidation

The formula is a truncated 2D Fourier series over normalized image
coordinates [0, 1] x [0, 1], producing a displacement vector (dx, dy)
in pixel units. Three frequency octaves are used (frequencies 1, 2, 3).

Reference:
  docs/plans/2026-05-18-fourier-corrections-plan.md
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FourierCorrections:
    """96-coefficient 2D Fourier pixel displacement from Metashape.

    The coefficients define a truncated Fourier series over normalized
    image coordinates that produces a per-pixel displacement (dx, dy).
    Applied in pixel space before Brown's undistortion.

    Fields:
        coeffs     : 96 floats, C[1] through C[96] in the formula (0-indexed here)
        extent_min : (x_min, y_min) pixel origin of the normalization domain
        extent_max : (x_max, y_max) pixel extent of the normalization domain
    """

    coeffs: tuple[float, ...]
    extent_min: tuple[float, float]
    extent_max: tuple[float, float]

    def __post_init__(self):
        if len(self.coeffs) != 96:
            raise ValueError(
                f"Expected 96 Fourier coefficients, got {len(self.coeffs)}"
            )


# ─── XML Parsing ────────────────────────────────────────────────────────────


def parse_corrections_from_xml(file_path: str) -> FourierCorrections | None:
    """Parse corrections from a standalone Metashape calibration XML.

    Expects a file with <calibration> as root (or containing a <calibration>
    child). Returns None when no <corrections type="fourier"> block is present.
    """
    tree = ET.parse(file_path)
    root = tree.getroot()
    cal = root if root.tag == "calibration" else root.find("calibration")
    if cal is None:
        return None
    return _parse_corrections_element(cal)


def get_calibration_with_corrections(file_path: str):
    """Parse a standalone Metashape calibration XML in a single pass.

    Returns both the standard calibration parameters AND corrections (if
    present) from one XML read. This replaces calling get_metashape_calibration_data
    and parse_corrections_from_xml separately on the same file.

    Returns:
        tuple of (projection, width, height, f, cx, cy, k1, k2, k3, k4,
                  p1, p2, b1, b2, date, corrections)
        where corrections is FourierCorrections or None, and k4/b1/b2
        default to 0.0 when absent from the XML (backwards-compatible
        with calibration files that only contain the original v4 parameter set).

        Returns None on parse failure (missing calibration block, missing
        required fields, type conversion errors).
    """
    try:
        tree = ET.parse(file_path)
    except (ET.ParseError, OSError):
        return None
    root = tree.getroot()
    cal = root if root.tag == "calibration" else root.find("calibration")
    if cal is None:
        return None

    def _text(tag, default="0"):
        elem = cal.find(tag)
        if elem is not None and elem.text:
            return elem.text.strip()
        return default

    try:
        projection = _text("projection", "unknown")
        width = int(_text("width"))
        height = int(_text("height"))
        f = float(_text("f"))
        cx = float(_text("cx"))
        cy = float(_text("cy"))
        k1 = float(_text("k1"))
        k2 = float(_text("k2"))
        k3 = float(_text("k3"))
        k4 = float(_text("k4"))
        p1 = float(_text("p1"))
        p2 = float(_text("p2"))
        b1 = float(_text("b1"))
        b2 = float(_text("b2"))
        date = _text("date", "unknown")
    except (ValueError, TypeError):
        return None

    corrections = _parse_corrections_element(cal)

    return (projection, width, height, f, cx, cy, k1, k2, k3, k4, p1, p2, b1, b2, date, corrections)


def parse_corrections_from_element(calibration_element) -> FourierCorrections | None:
    """Parse corrections from a <calibration> XML element (cameras.xml sensor block)."""
    return _parse_corrections_element(calibration_element)


def _parse_corrections_element(calibration_element) -> FourierCorrections | None:
    """Internal: extract FourierCorrections from a <calibration> element."""
    corrections = calibration_element.find("corrections")
    if corrections is None:
        return None
    if corrections.attrib.get("type", "").lower() != "fourier":
        return None

    coeffs_text = corrections.findtext("coeffs", "").strip()
    if not coeffs_text:
        return None
    coeffs = tuple(float(x) for x in coeffs_text.split())
    if len(coeffs) != 96:
        raise ValueError(
            f"Fourier corrections block has {len(coeffs)} coefficients, expected 96"
        )

    extent = corrections.find("extent")
    if extent is None:
        raise ValueError("Fourier corrections block missing <extent>")

    min_vals = extent.findtext("min", "0 0").split()
    max_vals = extent.findtext("max", "").split()
    if len(max_vals) != 2:
        raise ValueError(
            f"Fourier corrections <extent><max> has {len(max_vals)} values, expected 2"
        )

    extent_min = (float(min_vals[0]), float(min_vals[1]))
    extent_max = (float(max_vals[0]), float(max_vals[1]))

    return FourierCorrections(
        coeffs=coeffs,
        extent_min=extent_min,
        extent_max=extent_max,
    )


# ─── Fourier Displacement ───────────────────────────────────────────────────


def apply_fourier_displacement(
    uu: np.ndarray,
    vv: np.ndarray,
    corrections: FourierCorrections,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply Fourier displacement to pixel coordinates.

    Takes pixel-space meshgrids (uu, vv) and returns corrected
    meshgrids (uu_corrected, vv_corrected) with the Fourier
    displacement subtracted.

    The formula gives the forward displacement that Metashape ADDS
    during projection (camera coords -> pixel). Our pixel-to-ray
    pipeline inverts this by SUBTRACTING the displacement.
    """
    C = corrections.coeffs
    TWO_PI = 2.0 * np.pi

    # Normalize to [0, 1] using the extent
    x_range = corrections.extent_max[0] - corrections.extent_min[0]
    y_range = corrections.extent_max[1] - corrections.extent_min[1]
    x = (uu.astype(np.float64) - corrections.extent_min[0]) / x_range
    y = (vv.astype(np.float64) - corrections.extent_min[1]) / y_range

    # Precompute trig arguments for reuse
    tpx = TWO_PI * x
    tpy = TWO_PI * y

    dx = np.zeros_like(uu, dtype=np.float64)
    dy = np.zeros_like(vv, dtype=np.float64)

    # ── Octave 1: C[0]–C[15] (0-indexed) ──
    # Cosine terms
    dx += C[0] * (-np.cos(tpy))
    dy += C[1] * (-np.cos(tpy))
    dx += C[2] * (np.cos(tpx + tpy))
    dy += C[3] * (np.cos(tpx + tpy))
    dx += C[4] * (-np.cos(tpx))
    dy += C[5] * (-np.cos(tpx))
    dx += C[6] * (np.cos(tpx - tpy))
    dy += C[7] * (np.cos(tpx - tpy))
    # Sine terms
    dx += C[8] * (-np.sin(tpy))
    dy += C[9] * (-np.sin(tpy))
    dx += C[10] * (np.sin(tpx + tpy))
    dy += C[11] * (np.sin(tpx + tpy))
    dx += C[12] * (-np.sin(tpx))
    dy += C[13] * (-np.sin(tpx))
    dx += C[14] * (np.sin(tpx - tpy))
    dy += C[15] * (np.sin(tpx - tpy))

    # ── Octave 2: C[16]–C[47] ──
    tp2x = 2.0 * tpx
    tp2y = 2.0 * tpy
    # Cosine terms
    dx += C[16] * (np.cos(tp2y))
    dy += C[17] * (np.cos(tp2y))
    dx += C[18] * (-np.cos(tpx + tp2y))
    dy += C[19] * (-np.cos(tpx + tp2y))
    dx += C[20] * (np.cos(tp2x + tp2y))
    dy += C[21] * (np.cos(tp2x + tp2y))
    dx += C[22] * (-np.cos(tp2x + tpy))
    dy += C[23] * (-np.cos(tp2x + tpy))
    dx += C[24] * (np.cos(tp2x))
    dy += C[25] * (np.cos(tp2x))
    dx += C[26] * (-np.cos(tp2x - tpy))
    dy += C[27] * (-np.cos(tp2x - tpy))
    dx += C[28] * (np.cos(tp2x - tp2y))
    dy += C[29] * (np.cos(tp2x - tp2y))
    dx += C[30] * (-np.cos(tpx - tp2y))
    dy += C[31] * (-np.cos(tpx - tp2y))
    # Sine terms
    dx += C[32] * (np.sin(tp2y))
    dy += C[33] * (np.sin(tp2y))
    dx += C[34] * (-np.sin(tpx + tp2y))
    dy += C[35] * (-np.sin(tpx + tp2y))
    dx += C[36] * (np.sin(tp2x + tp2y))
    dy += C[37] * (np.sin(tp2x + tp2y))
    dx += C[38] * (-np.sin(tp2x + tpy))
    dy += C[39] * (-np.sin(tp2x + tpy))
    dx += C[40] * (np.sin(tp2x))
    dy += C[41] * (np.sin(tp2x))
    dx += C[42] * (-np.sin(tp2x - tpy))
    dy += C[43] * (-np.sin(tp2x - tpy))
    dx += C[44] * (np.sin(tp2x - tp2y))
    dy += C[45] * (np.sin(tp2x - tp2y))
    dx += C[46] * (-np.sin(tpx - tp2y))
    dy += C[47] * (-np.sin(tpx - tp2y))

    # ── Octave 3: C[48]–C[95] ──
    tp3x = 3.0 * tpx
    tp3y = 3.0 * tpy
    # Cosine terms
    dx += C[48] * (-np.cos(tp3y))
    dy += C[49] * (-np.cos(tp3y))
    dx += C[50] * (np.cos(tpx + tp3y))
    dy += C[51] * (np.cos(tpx + tp3y))
    dx += C[52] * (-np.cos(tp2x + tp3y))
    dy += C[53] * (-np.cos(tp2x + tp3y))
    dx += C[54] * (np.cos(tp3x + tp3y))
    dy += C[55] * (np.cos(tp3x + tp3y))
    dx += C[56] * (-np.cos(tp3x + tp2y))
    dy += C[57] * (-np.cos(tp3x + tp2y))
    dx += C[58] * (np.cos(tp3x + tpy))
    dy += C[59] * (np.cos(tp3x + tpy))
    dx += C[60] * (-np.cos(tp3x))
    dy += C[61] * (-np.cos(tp3x))
    dx += C[62] * (np.cos(tp3x - tpy))
    dy += C[63] * (np.cos(tp3x - tpy))
    dx += C[64] * (-np.cos(tp3x - tp2y))
    dy += C[65] * (-np.cos(tp3x - tp2y))
    dx += C[66] * (np.cos(tp3x - tp3y))
    dy += C[67] * (np.cos(tp3x - tp3y))
    dx += C[68] * (-np.cos(tp2x - tp3y))
    dy += C[69] * (-np.cos(tp2x - tp3y))
    dx += C[70] * (np.cos(tpx - tp3y))
    dy += C[71] * (np.cos(tpx - tp3y))
    # Sine terms
    dx += C[72] * (-np.sin(tp3y))
    dy += C[73] * (-np.sin(tp3y))
    dx += C[74] * (np.sin(tpx + tp3y))
    dy += C[75] * (np.sin(tpx + tp3y))
    dx += C[76] * (-np.sin(tp2x + tp3y))
    dy += C[77] * (-np.sin(tp2x + tp3y))
    dx += C[78] * (np.sin(tp3x + tp3y))
    dy += C[79] * (np.sin(tp3x + tp3y))
    dx += C[80] * (-np.sin(tp3x + tp2y))
    dy += C[81] * (-np.sin(tp3x + tp2y))
    dx += C[82] * (np.sin(tp3x + tpy))
    dy += C[83] * (np.sin(tp3x + tpy))
    dx += C[84] * (-np.sin(tp3x))
    dy += C[85] * (-np.sin(tp3x))
    dx += C[86] * (np.sin(tp3x - tpy))
    dy += C[87] * (np.sin(tp3x - tpy))
    dx += C[88] * (-np.sin(tp3x - tp2y))
    dy += C[89] * (-np.sin(tp3x - tp2y))
    dx += C[90] * (np.sin(tp3x - tp3y))
    dy += C[91] * (np.sin(tp3x - tp3y))
    dx += C[92] * (-np.sin(tp2x - tp3y))
    dy += C[93] * (-np.sin(tp2x - tp3y))
    dx += C[94] * (np.sin(tpx - tp3y))
    dy += C[95] * (np.sin(tpx - tp3y))

    # Invert: subtract the forward displacement to undo it
    return uu - dx, vv - dy


# ─── Cache Hash ──────────────────────────────────────────────────────────────


def corrections_cache_hash(corrections: FourierCorrections | None) -> str:
    """Deterministic hash of corrections for cache invalidation.

    Returns empty string when corrections is None (uncorrected path).
    Includes extent values so different normalization domains produce
    different hashes.
    """
    if corrections is None:
        return ""
    payload = corrections.coeffs + corrections.extent_min + corrections.extent_max
    data = "|".join(f"{v:.15g}" for v in payload).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


__all__ = [
    "FourierCorrections",
    "apply_fourier_displacement",
    "corrections_cache_hash",
    "get_calibration_with_corrections",
    "parse_corrections_from_element",
    "parse_corrections_from_xml",
]
