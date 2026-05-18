import importlib.util
import inspect
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
V4_MODULE_PATH = PROJECT_ROOT / "AM_ImageAndMask_to_cubemap_v4.py"

sys.path.insert(0, str(PROJECT_ROOT))


def _load_v4_module():
    spec = importlib.util.spec_from_file_location("cubemap_v4", V4_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_v4_does_not_expose_process_sensor():
    """The original v4 script stays unrefactored; wrapper API lives elsewhere."""
    mod = _load_v4_module()
    assert not hasattr(mod, "process_sensor")


def test_cubeface_processing_signature():
    from gui.cubeface_processing import process_cubeface_sensor

    sig = inspect.signature(process_cubeface_sensor)
    param_names = list(sig.parameters.keys())
    assert param_names[:4] == [
        "calibration_xml",
        "image_dirs",
        "output_dir",
        "face_width",
    ]
    assert "mask_dirs" in param_names
    assert "lens_only_mask" in param_names
    assert "progress_callback" in param_names
