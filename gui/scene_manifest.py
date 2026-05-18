"""Scene manifest data model (v2) for multi-camera COLMAP export.

This is the bridge between the GUI's sensor configuration and the exporter's
`--scene-manifest` JSON interface. Both ends read/write through these
dataclasses.

Changes from v1 (per docs/superpowers/plans/2026-05-14-colmap-export-tab-redesign.md):

- **No Body grouping.** Sensors are top-level on the manifest. Body grouping
  added GUI complexity without exporter value — the sensor label from the XML
  is identifier enough.
- **No `calibration_xml`.** Calibration is extracted from the cameras.xml
  automatically (see `gui.sensor_discovery.extract_sensor_calibration`); the
  exporter reads it directly from the XML at process time.
- **Multi-directory image/mask inputs.** `image_dirs` and `mask_dirs` are
  lists, supporting the case where one Metashape sensor's images live across
  multiple folders (front/, back/, multiple capture sessions, etc.).
- **EquirectSensor** as a separate type with `split_mode` (cubemap/reframe)
  and `split_width`.
- **ExportOptions trimmed.** `require_masks` and `projected_tracks` are
  removed; projected tracks are always built when a sparse PLY is present.
- **FisheyeSensor.routing** — optional cached routing decision (adaptive
  Path B vs cubemap Path A). Populated by the GUI's reactive routing layer
  (see Routing decision lifecycle in
  docs/superpowers/plans/Adaptive_Pinhole_Undistort_Plan.md). When `None`,
  the exporter computes the decision; when present, it trusts the cached
  values. Persisting the decision avoids re-routing at export time.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RoutingDecision:
    """Cached adaptive-routing decision for a fisheye sensor.

    Stored on FisheyeSensor.routing. Set by the GUI's routing layer (which
    keys it by a UID hash of intrinsics + calibration_type + mask_digest +
    thresholds, Meshroom-style). When persisted in the manifest, the
    exporter trusts these values rather than recomputing.

    Fields:
        processing_mode : 'single_pinhole' (adaptive Path B) or
                          'multi_pinhole'  (cubemap Path A).
        f_target        : adaptive focal length in pixels (single_pinhole only)
        w_out           : adaptive output side length in pixels (single_pinhole only)
        recommended_output_width:
                          XML/routing-derived GUI width recommendation. For
                          single_pinhole this is w_out; for multi_pinhole this
                          is the recommended cubeface face width.
        theta_max_deg   : maximum useful half-angle of the lens (informational)
        routing_uid     : the hash that produced this decision, for cache
                          invalidation when inputs change
    """
    processing_mode: str
    f_target: float | None = None
    w_out: int | None = None
    recommended_output_width: int | None = None
    theta_max_deg: float | None = None
    routing_uid: str | None = None

    def to_dict(self) -> dict:
        d: dict = {"processing_mode": self.processing_mode}
        if self.f_target is not None:
            d["f_target"] = self.f_target
        if self.w_out is not None:
            d["w_out"] = self.w_out
        if self.recommended_output_width is not None:
            d["recommended_output_width"] = self.recommended_output_width
        if self.theta_max_deg is not None:
            d["theta_max_deg"] = self.theta_max_deg
        if self.routing_uid is not None:
            d["routing_uid"] = self.routing_uid
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "RoutingDecision":
        return cls(
            processing_mode=data["processing_mode"],
            f_target=data.get("f_target"),
            w_out=data.get("w_out"),
            recommended_output_width=data.get("recommended_output_width"),
            theta_max_deg=data.get("theta_max_deg"),
            routing_uid=data.get("routing_uid"),
        )


@dataclass
class FisheyeSensor:
    """Fisheye sensor configuration.

    image_dirs / mask_dirs are lists to support sensors whose source files
    live in multiple directories (e.g. one Metashape sensor backed by
    front/ and back/ subdirectories of a 360 rig export).

    lens_only_mask is a single mask file describing the fisheye circle for
    the lens; used as a fallback when per-image masks are absent.

    multi_pinhole is the user-set processing-mode checkbox: True means
    split the fisheye into multiple pinhole images (cubefaces, Path A);
    False means use the adaptive single-pinhole undistortion (Path B).
    When `routing` is populated, the GUI initializes this from
    routing.processing_mode ("multi_pinhole" -> True). The user can
    override it; the GUI's ↻ Re-evaluate action re-runs routing and
    resets the checkbox to routing's recommendation.

    output_width is the cubeface width for multi-pinhole mode. Single-pinhole
    mode uses routing.w_out instead (computed adaptively).
    """
    sensor_id: int
    image_dirs: list[Path] = field(default_factory=list)
    mask_dirs: list[Path] = field(default_factory=list)
    lens_only_mask: Path | None = None
    multi_pinhole: bool = True
    output_width: int = 2048
    output_format: str = "jpg"
    routing: RoutingDecision | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "sensor_id": self.sensor_id,
            "image_dirs": [str(p) for p in self.image_dirs],
            "mask_dirs": [str(p) for p in self.mask_dirs],
            "multi_pinhole": self.multi_pinhole,
            "output_width": self.output_width,
            "output_format": self.output_format,
        }
        if self.lens_only_mask is not None:
            d["lens_only_mask"] = str(self.lens_only_mask)
        if self.routing is not None:
            d["routing"] = self.routing.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "FisheyeSensor":
        return cls(
            sensor_id=data["sensor_id"],
            image_dirs=[Path(p) for p in data.get("image_dirs", [])],
            mask_dirs=[Path(p) for p in data.get("mask_dirs", [])],
            lens_only_mask=Path(data["lens_only_mask"]) if data.get("lens_only_mask") else None,
            multi_pinhole=data.get("multi_pinhole", True),
            output_width=data.get("output_width", 2048),
            output_format=data.get("output_format", "jpg"),
            routing=RoutingDecision.from_dict(data["routing"]) if data.get("routing") else None,
        )


@dataclass
class FrameSensor:
    """Frame (pinhole/perspective) sensor configuration.

    Images pass through the exporter's undistortion pipeline rather than
    being reprojected to cubefaces. Masks are optional; when provided, they
    override the default validity mask the exporter generates from the
    distortion model.
    """
    sensor_id: int
    image_dirs: list[Path] = field(default_factory=list)
    mask_dirs: list[Path] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "image_dirs": [str(p) for p in self.image_dirs],
            "mask_dirs": [str(p) for p in self.mask_dirs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FrameSensor":
        return cls(
            sensor_id=data["sensor_id"],
            image_dirs=[Path(p) for p in data.get("image_dirs", [])],
            mask_dirs=[Path(p) for p in data.get("mask_dirs", [])],
        )


@dataclass
class EquirectSensor:
    """Equirectangular (stitched 360 panorama) sensor configuration.

    Images are split into multiple pinhole crops by the ERP reframing path
    (see docs/superpowers/plans/2026-05-14-colmap-export-tab-redesign.md
    Task 6). Two split modes are supported:

      - 'cubemap' (6 views, 90 deg FOV, cardinal directions including up/down)
      - 'reframe' (16 views, 90 deg FOV, two staggered horizontal rings —
                   recommended for 3DGS training quality)

    split_width is the pixel width of each per-view crop.
    """
    sensor_id: int
    image_dirs: list[Path] = field(default_factory=list)
    mask_dirs: list[Path] = field(default_factory=list)
    split_width: int = 2048
    split_mode: str = "reframe"

    def to_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "image_dirs": [str(p) for p in self.image_dirs],
            "mask_dirs": [str(p) for p in self.mask_dirs],
            "split_width": self.split_width,
            "split_mode": self.split_mode,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EquirectSensor":
        return cls(
            sensor_id=data["sensor_id"],
            image_dirs=[Path(p) for p in data.get("image_dirs", [])],
            mask_dirs=[Path(p) for p in data.get("mask_dirs", [])],
            split_width=data.get("split_width", 2048),
            split_mode=data.get("split_mode", "reframe"),
        )


@dataclass
class ExportOptions:
    """Per-export options.

    require_masks and projected_tracks have been removed in v2:
      - require_masks was rarely used; the exporter still warns on missing
        masks but does not refuse to export.
      - projected_tracks is now always on when a sparse PLY is provided.
    """
    pose_convention: str = "metashape_camera_to_world"
    force_assets: bool = False
    normalize_scene: bool = False
    keep_processing_files: bool = True

    def to_dict(self) -> dict:
        return {
            "pose_convention": self.pose_convention,
            "force_assets": self.force_assets,
            "normalize_scene": self.normalize_scene,
            "keep_processing_files": self.keep_processing_files,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExportOptions":
        return cls(
            pose_convention=data.get("pose_convention", "metashape_camera_to_world"),
            force_assets=data.get("force_assets", False),
            normalize_scene=data.get("normalize_scene", False),
            keep_processing_files=data.get("keep_processing_files", True),
        )


@dataclass
class SceneManifest:
    """Top-level manifest written by the GUI and consumed by the exporter.

    Sensors are top-level (no Body grouping in v2). Each sensor type has its
    own list; the exporter dispatches between fisheye/frame/equirect
    processing paths based on which list a sensor appears in.
    """
    cameras_xml: Path
    sparse_ply: Path
    output_dir: Path
    fisheye_sensors: list[FisheyeSensor] = field(default_factory=list)
    frame_sensors: list[FrameSensor] = field(default_factory=list)
    equirect_sensors: list[EquirectSensor] = field(default_factory=list)
    options: ExportOptions = field(default_factory=ExportOptions)

    def to_dict(self) -> dict:
        return {
            "cameras_xml": str(self.cameras_xml),
            "sparse_ply": str(self.sparse_ply),
            "output_dir": str(self.output_dir),
            "fisheye_sensors": [s.to_dict() for s in self.fisheye_sensors],
            "frame_sensors": [s.to_dict() for s in self.frame_sensors],
            "equirect_sensors": [s.to_dict() for s in self.equirect_sensors],
            "options": self.options.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SceneManifest":
        return cls(
            cameras_xml=Path(data["cameras_xml"]),
            sparse_ply=Path(data["sparse_ply"]),
            output_dir=Path(data["output_dir"]),
            fisheye_sensors=[FisheyeSensor.from_dict(s) for s in data.get("fisheye_sensors", [])],
            frame_sensors=[FrameSensor.from_dict(s) for s in data.get("frame_sensors", [])],
            equirect_sensors=[EquirectSensor.from_dict(s) for s in data.get("equirect_sensors", [])],
            options=ExportOptions.from_dict(data.get("options", {})),
        )

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> "SceneManifest":
        return cls.from_dict(json.loads(path.read_text()))
