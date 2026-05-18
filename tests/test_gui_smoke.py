import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def test_gui_imports_without_error():
    """gui.py can be imported without crashing (no display required)."""
    import ast
    source = (Path(__file__).resolve().parents[1] / "gui" / "gui.py").read_text()
    ast.parse(source)  # raises SyntaxError if broken
    import gui.gui  # noqa: F401


def test_parse_calibration_xml_detects_corrections(tmp_path):
    """P3-B: _parse_calibration_xml returns has_corrections correctly."""
    from gui.gui import _parse_calibration_xml

    # Without corrections
    plain = tmp_path / "plain.xml"
    plain.write_text(
        '<?xml version="1.0"?>\n<calibration>'
        "<projection>equisolid_fisheye</projection>"
        "<width>3840</width><height>3840</height><f>1000</f>"
        "</calibration>",
        encoding="utf-8",
    )
    info, has = _parse_calibration_xml(str(plain))
    assert info is not None
    assert has is False

    # With corrections
    coeffs = " ".join("0.01" for _ in range(96))
    corrected = tmp_path / "corrected.xml"
    corrected.write_text(
        '<?xml version="1.0"?>\n<calibration>'
        "<projection>equisolid_fisheye</projection>"
        "<width>3840</width><height>3840</height><f>963</f>"
        f'<corrections type="fourier"><coeffs>{coeffs}</coeffs>'
        "<extent><min>0 0</min><max>3840 3840</max></extent>"
        "</corrections></calibration>",
        encoding="utf-8",
    )
    info2, has2 = _parse_calibration_xml(str(corrected))
    assert info2 is not None
    assert has2 is True


def test_script_corrected_path_constant_exists():
    """P3-C: gui.py defines _SCRIPT_CORRECTED pointing to the wrapper."""
    from gui.gui import _SCRIPT_CORRECTED
    assert _SCRIPT_CORRECTED.name == "AM_ImageAndMask_to_cubemap_v4_corrected.py"
    assert _SCRIPT_CORRECTED.is_file()
