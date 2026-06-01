from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CalibrationSourceGeometry:
    image_width: int
    image_height: int
    pixel_center_convention: str
    source_image_state: str = "original_distorted"
    rotation_applied_deg: float = 0.0
    crop_rect: tuple[int, int, int, int] | None = None
    scale_from_calibration: float = 1.0
    lens_side: str = "unknown"
    warnings: tuple[str, ...] = ()

    def validate_known(self) -> None:
        if self.source_image_state == "unknown":
            raise ValueError(
                "Calibration source image state is unknown. Refusing to run "
                "without explicit experimental support."
            )


@dataclass(frozen=True)
class Calibration:
    provider: str
    model: str
    width: int
    height: int
    params: dict[str, Any]
    source_path: Path | None
    source_geometry: CalibrationSourceGeometry
    warnings: tuple[str, ...] = ()
    ignored_fields: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)
    rays: Any | None = field(default=None, repr=False, compare=False)

    def validate_dimensions(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(
                f"Calibration dimensions must be positive, got {self.width}x{self.height}."
            )
        if self.source_geometry.image_width != self.width:
            raise ValueError(
                "Calibration source geometry width does not match calibration width: "
                f"{self.source_geometry.image_width} != {self.width}."
            )
        if self.source_geometry.image_height != self.height:
            raise ValueError(
                "Calibration source geometry height does not match calibration height: "
                f"{self.source_geometry.image_height} != {self.height}."
            )
        self.source_geometry.validate_known()

    def cache_fingerprint(self) -> str:
        payload = {
            "provider": self.provider,
            "model": self.model,
            "width": self.width,
            "height": self.height,
            "params": self.params,
            "source_geometry": asdict(self.source_geometry),
        }
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.md5(encoded).hexdigest()

    def summary_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "width": self.width,
            "height": self.height,
            "source_path": str(self.source_path) if self.source_path else None,
            "source_path_sha256": _sha256_file(self.source_path),
            "params": self.params,
            "source_geometry": asdict(self.source_geometry),
            "warnings": list(self.warnings),
            "ignored_fields": list(self.ignored_fields),
            "raw": self.raw,
            "cache_fingerprint": self.cache_fingerprint(),
            "has_embedded_rays": self.rays is not None,
        }


def _sha256_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_calibration(
    path: str | Path,
    provider: str = "auto",
    *,
    camera_id: int | None = None,
) -> Calibration:
    calibration_path = Path(path)
    provider = (provider or "auto").lower().replace("_", "-")

    if provider == "auto":
        suffix = calibration_path.suffix.lower()
        if suffix == ".npz":
            provider = "raymap"
        elif suffix == ".xml":
            provider = "metashape"
        else:
            raise ValueError(
                f"Cannot auto-detect calibration provider for {calibration_path}. "
                "Pass --calibration-provider explicitly."
            )

    if provider == "metashape":
        from .metashape import load_metashape_calibration

        cal = load_metashape_calibration(calibration_path)
    elif provider == "raymap":
        from .raymap import load_raymap_calibration

        cal = load_raymap_calibration(calibration_path)
    elif provider in {"opencv", "opencv-fisheye"}:
        from .opencv import load_opencv_fisheye_calibration

        cal = load_opencv_fisheye_calibration(calibration_path)
    elif provider == "colmap":
        from .colmap import load_colmap_calibration

        cal = load_colmap_calibration(calibration_path, camera_id=camera_id)
    elif provider in {"realityscan-xmp", "metadata"}:
        raise NotImplementedError(
            f"Calibration provider '{provider}' is planned but not implemented yet. "
            "Use metashape or raymap in this implementation stage."
        )
    else:
        raise ValueError(f"Unknown calibration provider: {provider}")

    cal.validate_dimensions()
    return cal


def write_provider_summary(calibration: Calibration, output_path: str | Path, extra: dict[str, Any] | None = None) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = calibration.summary_dict()
    if extra:
        summary["run"] = extra
    output_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    return output_path
