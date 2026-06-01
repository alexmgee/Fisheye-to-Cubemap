from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .core import Calibration, CalibrationSourceGeometry


def load_metashape_calibration(path: str | Path) -> Calibration:
    path = Path(path)
    schema = {
        "projection": str,
        "date": str,
        "width": int,
        "height": int,
        "f": float,
        "cx": float,
        "cy": float,
        "k1": float,
        "k2": float,
        "k3": float,
        "p1": float,
        "p2": float,
    }

    tree = ET.parse(path)
    root = tree.getroot()
    calibration_block = root if root.tag == "calibration" else root.find("calibration")
    if calibration_block is None:
        raise ValueError(f"No <calibration> block found in {path}.")

    data = {}
    ignored = []
    for element in calibration_block:
        tag = element.tag
        if tag not in schema:
            ignored.append(tag)
            continue
        try:
            data[tag] = schema[tag](element.text)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Could not parse Metashape calibration field {tag}={element.text!r}."
            ) from exc

    missing = [field for field in schema if field not in data]
    if missing:
        raise ValueError(
            f"Metashape calibration {path} is missing required field(s): "
            + ", ".join(missing)
        )

    projection = data["projection"]
    if projection == "equidistant_fisheye":
        model = "equidistant"
    elif projection == "equisolid_fisheye":
        model = "equisolid"
    else:
        raise ValueError(f'Projection "{projection}" is not supported.')

    width = data["width"]
    height = data["height"]
    params_tuple = (
        data["f"],
        data["cx"],
        data["cy"],
        data["k1"],
        data["k2"],
        data["k3"],
        0.0,
        data["p1"],
        data["p2"],
        0.0,
        0.0,
    )

    warnings = (
        "Metashape calibration uses Metashape's lens-model semantics. Do not "
        "substitute similarly named coefficients from other tools.",
    )

    return Calibration(
        provider="metashape",
        model=model,
        width=width,
        height=height,
        params={
            "projection": projection,
            "f": data["f"],
            "cx": data["cx"],
            "cy": data["cy"],
            "k1": data["k1"],
            "k2": data["k2"],
            "k3": data["k3"],
            "k4": 0.0,
            "p1": data["p1"],
            "p2": data["p2"],
            "b1": 0.0,
            "b2": 0.0,
            "params_tuple": params_tuple,
            "date": data["date"],
        },
        source_path=path,
        source_geometry=CalibrationSourceGeometry(
            image_width=width,
            image_height=height,
            pixel_center_convention="metashape_export_width_height_half_plus_cx_cy",
            source_image_state="original_distorted",
        ),
        warnings=warnings,
        ignored_fields=tuple(ignored),
        raw=data,
    )
