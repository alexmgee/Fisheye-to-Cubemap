"""
Cubemap GUI V4.

Provides a CustomTkinter interface for two workflows:

1. Generate cubefaces and masks for Metashape alignment.
2. Generate a clean COLMAP scene from a Metashape alignment.

The original cubeface converter remains the source of image remapping.
COLMAP export mode treats cubefaces as internal intermediate files,
packages final images/masks/model files under the selected export root,
and uses mapping_resolver.py to validate lens-to-camera assignments.

Run:  python gui.py
Deps: customtkinter, Pillow  (pip install -r gui/requirements.txt)
"""

import customtkinter as ctk
from tkinter import filedialog
from pathlib import Path
from PIL import Image, ImageTk
import subprocess
import threading
import queue
import json
import re
import sys
import os
import time
import xml.etree.ElementTree as ET

# ── Resolve script paths and put project root on sys.path ──────────
# solid_angle.py and the adaptive routing modules depend on
# AM_ImageAndMask_to_cubemap_v4, which lives in the project root.
_THIS_DIR = Path(__file__).resolve().parent
_SCRIPT = _THIS_DIR.parent / "AM_ImageAndMask_to_cubemap_v4.py"
_SCRIPT_CORRECTED = _THIS_DIR.parent / "AM_ImageAndMask_to_cubemap_v4_corrected.py"
_EXPORTER = _THIS_DIR.parent / "metashape_cameras_to_colmap.py"
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

try:
    from gui import mapping_resolver
except ImportError:
    import mapping_resolver

# ── Settings persistence ─────────────────────────────────────────────
_PREFS_FILE = _THIS_DIR / ".cubemap_gui_v4_prefs.json"

# ── Lateral face filename mapping (matches v4 _FACE_FILENAME_SUFFIX) ─
_FACE_FILENAME_SUFFIX = {
    "+Z": "_dir_plusZ",
    "-X": "_dir_minusY",
    "+X": "_dir_plusY",
    "-Y": "_dir_minusX",
    "+Y": "_dir_plusX",
}
_SUFFIX_TO_FACE = {v: k for k, v in _FACE_FILENAME_SUFFIX.items()}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
_IMAGE_COUNT_CACHE = {}

# ── Mode constants (match CLI _resolve_support_inputs priority) ──────
MODE_MASK_DIRECTORY = "mask-directory"
MODE_LENS_ONLY_MASK = "lens-only-mask"
MODE_MANUAL_FOV = "manual-fov"
MODE_INCOMPLETE = "incomplete"

# ── Design constants ─────────────────────────────────────────────────
COLOR_BG = "#1a1a1a"
COLOR_CARD = "#2b2b2b"
COLOR_INPUT = "#343638"
COLOR_CONSOLE = "#1e1e1e"
COLOR_TEXT = "#e0e0e0"
COLOR_TEXT_DIM = "#888888"
COLOR_GREEN = "#16a34a"
COLOR_GREEN_H = "#15803d"
COLOR_RED = "#ab3434"
COLOR_BLUE = "#1976D2"
COLOR_DISABLED = "#555555"
COLOR_AMBER = "#d4a017"
FONT_LABEL = ("", 12)
FONT_HEADING = ("", 13, "bold")
FONT_CONSOLE = ("Consolas", 10)
FONT_STATUS = ("", 11)
PURPOSE_METASHAPE = "Fisheye-to-Cubemap"
PURPOSE_COLMAP = "COLMAP export"

# ── Run-state capture regexes (precompiled) ──────────────────────────
_SUPPORT_SOURCE_RE = re.compile(r"useful_pixel_mask source:\s+(.+)")
_MASK_ANGLE_RE = re.compile(r"Mask-derived maximum angle:\s+([\d.]+)\s+deg")
_MANUAL_ANGLE_RE = re.compile(r"Manual maximum angle:\s+([\d.]+)\s+deg")
_DONE_RE = re.compile(
    r"\[PROGRESS\]\s+DONE\s+\d+/\d+:\s+processed=(\d+)\s+skipped=(\d+)"
)
_FALLBACK_COUNT_RE = re.compile(r"Resolved (\d+) missing per-image mask")
_PINHOLE_START_RE = re.compile(r"set and FIXED for alignment")

# ── Progress bar weight tables ──────────────────────────────────────
# Each phase maps to (start, end) as a fraction of 0.0–1.0.
# Within a phase, current/total interpolates linearly in [start, end].

# Old path: per-lens subprocess (AM_ImageAndMask_to_cubemap_v4.py)
_LENS_PROGRESS_WEIGHTS = {
    "MASK_SUM":          (0.00, 0.10),
    "IMAGE_SAMPLE":      (0.10, 0.12),
    "RAYS":              (0.12, 0.17),
    "REMAP_PRECOMPUTE":  (0.17, 0.25),
    "REMAP_APPLY":       (0.25, 0.95),
    "DONE":              (0.95, 1.00),
}

# Old path: COLMAP scene subprocess (metashape_cameras_to_colmap.py)
_COLMAP_SCENE_PROGRESS_WEIGHTS = {
    "SCENE_EXPORT":          (0.00, 0.00),
    "BUILD_CUBEFACE_POSES":  (0.00, 0.05),
    "PACKAGE_CUBEFACES":     (0.05, 0.45),
    "PACKAGE_ADAPTIVE":      (0.45, 0.55),
    "PACKAGE_ERP":           (0.55, 0.65),
    "PACKAGE_PASSTHROUGH":   (0.65, 0.75),
    "PASSTHROUGH_UNDISTORT": (0.65, 0.75),
    "PROJECT_TRACKS":        (0.75, 0.90),
    "WRITE_COLMAP_MODEL":    (0.90, 1.00),
}

# V2 path: per-sensor local weights (within each sensor's band)
_COLMAP_EXPORT_SENSOR_LOCAL_WEIGHTS = {
    "RAYS":              (0.00, 0.07),
    "REMAP_PRECOMPUTE":  (0.07, 0.18),
    "REMAP_APPLY":       (0.18, 0.95),
    "DONE":              (0.95, 1.00),
}

# V2 path: scene-level weights (occupy 0.55–1.00 of the overall bar)
_COLMAP_EXPORT_SCENE_WEIGHTS = {
    "SCENE_EXPORT":          (0.55, 0.55),
    "BUILD_CUBEFACE_POSES":  (0.55, 0.60),
    "PACKAGE_CUBEFACES":     (0.60, 0.75),
    "PACKAGE_ADAPTIVE":      (0.75, 0.80),
    "PACKAGE_ERP":           (0.80, 0.83),
    "PACKAGE_PASSTHROUGH":   (0.83, 0.86),
    "PASSTHROUGH_UNDISTORT": (0.83, 0.86),
    "PROJECT_TRACKS":        (0.86, 0.95),
    "WRITE_COLMAP_MODEL":    (0.95, 1.00),
}

# Fraction of the bar reserved for all V2 per-sensor work
_V2_SENSOR_BAND = 0.55

# Human-readable labels for all phases
_PROGRESS_PHASE_LABELS = {
    "MASK_SUM":              "Analyzing masks",
    "IMAGE_SAMPLE":          "Sampling images",
    "RAYS":                  "Computing rays",
    "REMAP_PRECOMPUTE":      "Precomputing remap",
    "REMAP_APPLY":           "Processing image",
    "DONE":                  "Sensor complete",
    "SCENE_EXPORT":          "Building scene",
    "BUILD_CUBEFACE_POSES":  "Composing cubeface poses",
    "PACKAGE_CUBEFACES":     "Packaging cubefaces",
    "PACKAGE_ADAPTIVE":      "Packaging adaptive images",
    "PACKAGE_ERP":           "Packaging equirect views",
    "PACKAGE_PASSTHROUGH":   "Packaging frame-camera media",
    "PASSTHROUGH_UNDISTORT": "Undistorting frame-camera media",
    "PROJECT_TRACKS":        "Projecting sparse tracks",
    "WRITE_COLMAP_MODEL":    "Writing COLMAP model",
}


def _load_prefs():
    if _PREFS_FILE.exists():
        try:
            return json.loads(_PREFS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_prefs(data):
    try:
        _PREFS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def _parse_calibration_xml(path):
    """Parse calibration XML for display. Returns (info_str, has_corrections)."""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        cal = root if root.tag == "calibration" else root.find("calibration")
        if cal is None:
            return None, False
        proj = cal.findtext("projection", "?")
        w = cal.findtext("width", "?")
        h = cal.findtext("height", "?")
        f = cal.findtext("f", "?")
        corrections = cal.find("corrections")
        has_corrections = (
            corrections is not None
            and corrections.attrib.get("type", "").lower() == "fourier"
        )
        info = f"{proj}  {w}x{h}  f={f}"
        return info, has_corrections
    except Exception:
        return None, False


def _guess_lens_label(filepath):
    return Path(filepath).stem


def _count_image_files(directory):
    if not directory:
        return 0
    path = Path(directory)
    if not path.is_dir():
        return 0
    try:
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns)
        cached = _IMAGE_COUNT_CACHE.get(cache_key)
        if cached is not None:
            return cached
        count = sum(1 for f in os.listdir(path) if Path(f).suffix.lower() in _IMAGE_EXTENSIONS)
        _IMAGE_COUNT_CACHE[cache_key] = count
        return count
    except Exception:
        return 0


def _default_colmap_scene_dir(output_dir):
    out_path = Path(output_dir)
    return str(out_path)


def _normalize_colmap_export_root(path_value):
    path = Path(path_value)
    if path.name.casefold() == "colmap":
        return path.parent
    return path


def _image_stem_key(value):
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    return Path(name).stem.casefold()


def _image_stems(directory):
    root = Path(directory)
    if not root.is_dir():
        return set()
    return {
        _image_stem_key(path.name)
        for path in root.iterdir()
        if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS
    }


def _format_camera_ids(ids):
    values = sorted(set(int(value) for value in ids))
    if not values:
        return ""
    ranges = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(f"{start}-{previous}" if start != previous else str(start))
        start = previous = value
    ranges.append(f"{start}-{previous}" if start != previous else str(start))
    return " ".join(ranges)


def _is_equisolid_fisheye_sensor(sensor, calibration):
    sensor_type = str(sensor.attrib.get("type", "")).lower()
    calibration_type = str(calibration.attrib.get("type", "") if calibration is not None else "").lower()
    combined = f"{sensor_type} {calibration_type}"
    return "equisolid" in combined and "fisheye" in combined


def _metashape_equisolid_camera_runs(xml_path):
    root = ET.parse(xml_path).getroot()
    equisolid_sensor_ids = set()
    for sensor in root.findall(".//sensor"):
        calibration = sensor.find("calibration")
        if _is_equisolid_fisheye_sensor(sensor, calibration):
            equisolid_sensor_ids.add(int(sensor.attrib["id"]))

    # Parse camera group hierarchy (if present).
    group_labels = {}
    for group in root.findall(".//cameras/group"):
        gid = group.attrib.get("id")
        glabel = group.attrib.get("label", "")
        if gid is not None:
            group_labels[gid] = glabel

    cameras = []
    for camera in root.findall(".//camera"):
        sensor_id = int(camera.attrib.get("sensor_id", "-1"))
        if sensor_id not in equisolid_sensor_ids:
            continue
        transform = (camera.findtext("transform") or "").split()
        if len(transform) != 16:
            continue
        label = camera.attrib.get("label", "")
        gid = camera.attrib.get("group_id")
        cameras.append({
            "id": int(camera.attrib["id"]),
            "sensor_id": sensor_id,
            "label": label,
            "stem": _image_stem_key(label),
            "group_label": group_labels.get(gid) if gid else None,
        })

    cameras.sort(key=lambda item: item["id"])
    runs = []
    current = []
    current_sensor_id = None
    current_prefix = None
    last_number = None

    def split_label(label):
        match = re.match(r"^(?P<prefix>.*?)(?P<number>\d+)$", label)
        if not match:
            return label, None
        return match.group("prefix"), int(match.group("number"))

    def flush():
        if not current:
            return
        labels = [item["label"] for item in current]
        group_set = {item["group_label"] for item in current if item["group_label"]}
        runs.append({
            "ids": tuple(item["id"] for item in current),
            "sensor_id": current[0]["sensor_id"],
            "stems": {item["stem"] for item in current},
            "prefix": current_prefix,
            "start_label": labels[0],
            "end_label": labels[-1],
            "group_label": group_set.pop() if len(group_set) == 1 else None,
        })

    for camera in cameras:
        prefix, number = split_label(str(camera["label"]))
        continues = (
            current
            and camera["sensor_id"] == current_sensor_id
            and prefix == current_prefix
            and number is not None
            and last_number is not None
            and number == last_number + 1
        )
        if not continues:
            flush()
            current = []
        current.append(camera)
        current_sensor_id = camera["sensor_id"]
        current_prefix = prefix
        last_number = number
    flush()
    return runs


def _merge_fragmented_runs(runs):
    """Merge runs that share the same (sensor_id, prefix, group_label) key.

    Gaps from unaligned cameras cause one logical camera group to be split
    into many small runs.  This recombines them so the matcher sees one run
    per logical group.

    Only merges when the key produces multiple distinct buckets — if all runs
    share a single key, merging would collapse everything into one run and
    destroy the camera-ID gap signal that the overlap resolver needs.
    """
    if not runs:
        return runs
    buckets = {}
    for run in runs:
        key = (run["sensor_id"], run.get("prefix", ""), run.get("group_label"))
        buckets.setdefault(key, []).append(run)
    if len(buckets) < 2:
        # All runs share one key — nothing to distinguish, keep raw runs
        # so the ID-gap partition in step 2 can still split them.
        for run in runs:
            run.setdefault("raw_run_count", 1)
        return runs
    merged = []
    for _key, group in buckets.items():
        group.sort(key=lambda r: r["ids"][0])
        ids = []
        stems = set()
        for run in group:
            ids.extend(run["ids"])
            stems.update(run["stems"])
        ids = tuple(sorted(ids))
        merged.append({
            "ids": ids,
            "sensor_id": group[0]["sensor_id"],
            "stems": stems,
            "prefix": group[0].get("prefix", ""),
            "start_label": group[0]["start_label"],
            "end_label": group[-1]["end_label"],
            "group_label": group[0].get("group_label"),
            "raw_run_count": len(group),
        })
    merged.sort(key=lambda r: r["ids"][0])
    return merged


def _is_valid_positive_int(s):
    if not s or not s.strip():
        return False
    try:
        return int(s.strip()) > 0
    except ValueError:
        return False


def resolve_mode(cal_path, images_dir, masks_dir, lensonlymask, effective_fov):
    """Resolve which support source will win given the current field values.

    Priority matches the CLI's _resolve_support_inputs exactly:
      masks directory > lens-only mask > FOV

    Returns (mode, error_reason_or_None).
    """
    if not cal_path or not Path(cal_path).is_file():
        return MODE_INCOMPLETE, "Calibration XML not found"
    if not images_dir or not Path(images_dir).is_dir():
        return MODE_INCOMPLETE, "Images directory not found"

    masks_dir_valid = masks_dir and Path(masks_dir).is_dir()
    if masks_dir and not masks_dir_valid:
        return MODE_INCOMPLETE, "Masks directory does not exist"
    lensonlymask_valid = lensonlymask and Path(lensonlymask).is_file()
    if lensonlymask and not lensonlymask_valid:
        return MODE_INCOMPLETE, "Lens-only mask file not found"

    masks_dir_has_images = masks_dir_valid and _count_image_files(masks_dir) > 0
    fov_filled = _is_valid_positive_int(effective_fov)

    if masks_dir_has_images:
        return MODE_MASK_DIRECTORY, None
    if lensonlymask_valid:
        return MODE_LENS_ONLY_MASK, None
    if fov_filled:
        return MODE_MANUAL_FOV, None
    return MODE_INCOMPLETE, "Need masks directory, lens-only mask, or FOV"


def _viridis_colorize(gray_img):
    keypoints = [
        (0,   (68, 1, 84)),    (8,   (72, 35, 116)),
        (16,  (64, 67, 135)),  (24,  (52, 94, 141)),
        (32,  (41, 121, 142)), (40,  (32, 144, 140)),
        (48,  (34, 167, 132)), (56,  (68, 190, 112)),
        (64,  (121, 209, 81)), (72,  (189, 222, 38)),
        (80,  (253, 231, 36)), (255, (253, 231, 36)),
    ]
    palette = []
    for i in range(256):
        for j in range(len(keypoints) - 1):
            x0, c0 = keypoints[j]
            x1, c1 = keypoints[j + 1]
            if x0 <= i <= x1:
                t = (i - x0) / max(1, x1 - x0)
                rgb = tuple(int(c0[k] + t * (c1[k] - c0[k])) for k in range(3))
                palette.extend(rgb)
                break
        else:
            palette.extend(keypoints[-1][1])
    palette_img = Image.new("P", gray_img.size)
    palette_img.putpalette(palette)
    palette_img.putdata(list(gray_img.getdata()))
    return palette_img.convert("RGB")


class LensPanel:
    """Per-lens fields: calibration XML, lens label, images dir, masks dir,
    lens-only mask, and optional FOV override."""

    def __init__(self, parent, name, on_change=None):
        self.name = name
        self._on_change = on_change

        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.grid_columnconfigure(0, weight=1)
        row = 0

        # Calibration XML
        row = self._add_file_picker(row, "Calibration XML", "cal_path",
                                    filetypes=[("XML files", "*.xml"), ("All", "*.*")])
        self.cal_info = ctk.CTkLabel(
            self.frame, text="", font=("Consolas", 10), text_color=COLOR_TEXT_DIM,
            wraplength=350, justify="left",
        )
        self.cal_info.grid(row=row, column=0, sticky="w", padx=(24, 12), pady=(0, 4))
        row += 1

        # Lens label
        row = self._add_entry(row, "Lens label", "lens_label")

        # Images directory
        row = self._add_dir_picker(row, "Images directory", "images_dir")

        # Masks directory (with clear button)
        row = self._add_dir_picker(row, "Masks directory (optional)", "masks_dir",
                                   with_clear=True)

        # Masks hint
        self.masks_hint_label = ctk.CTkLabel(
            self.frame, text="", font=("", 10, "italic"),
            text_color=COLOR_TEXT_DIM, wraplength=400, justify="left",
        )
        self.masks_hint_label.grid(row=row, column=0, sticky="w", padx=(24, 12), pady=(0, 4))
        row += 1

        # Lens-only mask file picker (with clear button)
        row = self._add_file_picker(row, "Lens-only mask (optional)", "lensonlymask_path",
                                    filetypes=[("Image files", "*.png;*.jpg;*.jpeg;*.tif;*.tiff"),
                                               ("All", "*.*")],
                                    with_clear=True)
        # Lens-only mask hint
        self.lensonlymask_hint_label = ctk.CTkLabel(
            self.frame, text="", font=("", 10, "italic"),
            text_color=COLOR_TEXT_DIM, wraplength=400, justify="left",
        )
        self.lensonlymask_hint_label.grid(row=row, column=0, sticky="w", padx=(24, 12), pady=(0, 0))
        row += 1

        # Per-lens FOV (optional, lowest-priority support source)
        fov_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        fov_frame.grid(row=row, column=0, sticky="w", padx=12, pady=(0, 0))
        self._fov_override_var = ctk.BooleanVar(value=False)
        self._fov_override_cb = ctk.CTkCheckBox(
            fov_frame, text="Max useful FOV:", variable=self._fov_override_var,
            font=("", 11), command=self._handle_fov_override_toggle,
        )
        self._fov_override_cb.pack(side="left")
        self._fov_override_value = ctk.StringVar(value="")
        self._fov_override_entry = ctk.CTkEntry(
            fov_frame, width=56,
            font=("Consolas", 13), justify="right",
            text_color=COLOR_TEXT_DIM, state="disabled",
        )
        self._fov_override_entry.pack(side="left", padx=(6, 0))
        # Show dim "auto" placeholder when disabled (CTkEntry hides placeholder_text when disabled)
        self._fov_override_entry.configure(state="normal")
        self._fov_override_entry.insert(0, "Auto")
        self._fov_override_entry.configure(state="disabled")
        self._fov_override_value.trace_add("write", lambda *_: self._notify_change())
        row += 1

        # Fourier corrections checkbox (auto-detected from calibration XML)
        self._corrections_var = ctk.BooleanVar(value=False)
        self._corrections_cb = ctk.CTkCheckBox(
            self.frame, text="Apply additional corrections",
            variable=self._corrections_var,
            font=("", 11), state="disabled",
        )
        self._corrections_cb.grid(row=row, column=0, sticky="w", padx=12, pady=(2, 0))
        row += 1

        # Status row: file count + mode badge inline
        status_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        status_frame.grid(row=row, column=0, sticky="ew", padx=(24, 12), pady=(12, 6))

        self.file_count_label = ctk.CTkLabel(
            status_frame, text="", font=FONT_STATUS, text_color=COLOR_TEXT_DIM,
        )
        self.file_count_label.pack(side="left")

        self.mode_badge = ctk.CTkLabel(
            status_frame, text="Mode: incomplete",
            font=("", 11, "bold"), text_color=COLOR_RED,
            fg_color="#1f1f1f", corner_radius=4,
        )
        self.mode_badge.pack(side="left", padx=(12, 0))

    def _add_file_picker(self, row, label, attr, filetypes=None, with_clear=False):
        ctk.CTkLabel(self.frame, text=label, font=FONT_LABEL).grid(
            row=row, column=0, sticky="w", padx=12, pady=(6, 0),
        )
        row += 1
        frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 4))
        frame.grid_columnconfigure(0, weight=1)

        var = ctk.StringVar()
        ctk.CTkEntry(frame, textvariable=var, font=FONT_LABEL).grid(
            row=0, column=0, sticky="ew", padx=(0, 4),
        )
        col = 1

        def browse():
            p = filedialog.askopenfilename(filetypes=filetypes or [("All", "*.*")])
            if p:
                var.set(p)

        ctk.CTkButton(frame, text="...", width=36, command=browse).grid(row=0, column=col)
        col += 1

        if with_clear:
            ctk.CTkButton(frame, text="X", width=28, fg_color=COLOR_RED,
                          command=lambda: var.set("")).grid(row=0, column=col)

        setattr(self, attr, var)
        var.trace_add("write", lambda *_: self._notify_change())
        row += 1
        return row

    def _add_dir_picker(self, row, label, attr, with_clear=False):
        ctk.CTkLabel(self.frame, text=label, font=FONT_LABEL).grid(
            row=row, column=0, sticky="w", padx=12, pady=(6, 0),
        )
        row += 1
        frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 4))
        frame.grid_columnconfigure(0, weight=1)

        var = ctk.StringVar()
        ctk.CTkEntry(frame, textvariable=var, font=FONT_LABEL).grid(
            row=0, column=0, sticky="ew", padx=(0, 4),
        )
        col = 1

        def browse():
            p = filedialog.askdirectory()
            if p:
                var.set(p)

        ctk.CTkButton(frame, text="...", width=36, command=browse).grid(row=0, column=col)
        col += 1

        if with_clear:
            ctk.CTkButton(frame, text="X", width=28, fg_color=COLOR_RED,
                          command=lambda: var.set("")).grid(row=0, column=col)

        setattr(self, attr, var)
        var.trace_add("write", lambda *_: self._notify_change())
        row += 1
        return row

    def _add_entry(self, row, label, attr):
        ctk.CTkLabel(self.frame, text=label, font=FONT_LABEL).grid(
            row=row, column=0, sticky="w", padx=12, pady=(6, 0),
        )
        row += 1
        var = ctk.StringVar()
        ctk.CTkEntry(self.frame, textvariable=var, font=FONT_LABEL).grid(
            row=row, column=0, sticky="ew", padx=12, pady=(0, 4),
        )
        setattr(self, attr, var)
        row += 1
        return row

    def _notify_change(self):
        # Auto-fill cal info and lens label when calibration XML changes
        path = self.cal_path.get()
        if path and Path(path).is_file():
            info, has_corrections = _parse_calibration_xml(path)
            cal_text = info or "Could not parse XML"
            if has_corrections:
                cal_text += "  [Fourier corrections detected]"
            self.cal_info.configure(text=cal_text)
            self.lens_label.set(_guess_lens_label(path))
            # Update corrections checkbox state
            self._corrections_var.set(has_corrections)
            self._corrections_cb.configure(
                state="normal" if has_corrections else "disabled"
            )
        else:
            self.cal_info.configure(text="")
            self._corrections_var.set(False)
            self._corrections_cb.configure(state="disabled")
        if self._on_change:
            self._on_change()

    def _handle_fov_override_toggle(self):
        if self._fov_override_var.get():
            self._fov_override_entry.configure(state="normal")
            # Clear the "auto" placeholder and bind the real variable
            self._fov_override_entry.delete(0, "end")
            self._fov_override_entry.configure(
                textvariable=self._fov_override_value,
                text_color=("black", "white"),
            )
        else:
            # Unbind variable, show dim "auto"
            self._fov_override_entry.configure(textvariable="", text_color=COLOR_TEXT_DIM)
            self._fov_override_entry.delete(0, "end")
            self._fov_override_entry.insert(0, "Auto")
            self._fov_override_entry.configure(state="disabled")
        self._notify_change()

    def get_effective_fov(self, shared_fov):
        if self._fov_override_var.get():
            v = self._fov_override_value.get().strip()
            if v:
                return v
        return shared_fov

    def get_mode(self, shared_fov):
        effective_fov = self.get_effective_fov(shared_fov)
        mode, reason = resolve_mode(
            self.cal_path.get(),
            self.images_dir.get(),
            self.masks_dir.get(),
            self.lensonlymask_path.get(),
            effective_fov,
        )
        return mode, reason

    def update_ui(self, shared_fov):
        mode, reason = self.get_mode(shared_fov)
        effective_fov = self.get_effective_fov(shared_fov)
        masks_dir = self.masks_dir.get().strip()
        lensonlymask = self.lensonlymask_path.get().strip()
        fov_filled = _is_valid_positive_int(effective_fov)
        masks_filled = masks_dir and Path(masks_dir).is_dir() and _count_image_files(masks_dir) > 0
        lensmask_filled = lensonlymask and Path(lensonlymask).is_file()

        # Badge
        if mode == MODE_MASK_DIRECTORY:
            extra = ""
            if fov_filled and lensmask_filled:
                extra = " (FOV + lens mask ignored)"
            elif fov_filled:
                extra = " (FOV ignored — masks take priority)"
            elif lensmask_filled:
                extra = " (lens mask ignored)"
            self.mode_badge.configure(
                text=f"Support: mask directory{extra}", text_color=COLOR_GREEN,
            )
        elif mode == MODE_LENS_ONLY_MASK:
            extra = " (FOV ignored)" if fov_filled else ""
            self.mode_badge.configure(
                text=f"Support: lens-only mask{extra}", text_color=COLOR_BLUE,
            )
        elif mode == MODE_MANUAL_FOV:
            self.mode_badge.configure(
                text="Support: manual FOV", text_color=COLOR_BLUE,
            )
        else:
            self.mode_badge.configure(
                text=f"Incomplete: {reason}", text_color=COLOR_RED,
            )

        # Masks hint
        if mode == MODE_MASK_DIRECTORY:
            self.masks_hint_label.configure(text="")
        elif mode == MODE_LENS_ONLY_MASK:
            self.masks_hint_label.configure(
                text="No masks directory — using lens-only mask for support."
            )
        elif mode == MODE_MANUAL_FOV:
            self.masks_hint_label.configure(
                text="No masks — using FOV for support."
            )
        else:
            self.masks_hint_label.configure(
                text="Provide a masks directory, lens-only mask, or set FOV below."
            )

        # Lens-only mask hint
        if mode == MODE_LENS_ONLY_MASK:
            self.lensonlymask_hint_label.configure(
                text="Using this mask for support. All per-image masks will be generated from it."
            )
        elif mode == MODE_MASK_DIRECTORY:
            self.lensonlymask_hint_label.configure(
                text="Not used — masks directory takes priority." if lensmask_filled else ""
            )
        elif mode == MODE_MANUAL_FOV:
            self.lensonlymask_hint_label.configure(text="")
        else:
            self.lensonlymask_hint_label.configure(text="")

        # File count
        ni = _count_image_files(self.images_dir.get())
        if ni == 0:
            self.file_count_label.configure(text="", text_color=COLOR_TEXT_DIM)
        elif mode == MODE_MASK_DIRECTORY:
            nm = _count_image_files(masks_dir)
            if fov_filled:
                self.file_count_label.configure(
                    text=f"{ni} images, {nm} masks (FOV ignored — masks take priority)",
                    text_color=COLOR_GREEN,
                )
            elif ni == nm:
                self.file_count_label.configure(
                    text=f"{ni} images, {nm} masks", text_color=COLOR_GREEN,
                )
            else:
                self.file_count_label.configure(
                    text=f"{ni} images, {nm} masks (partial — missing use fallback)",
                    text_color=COLOR_BLUE,
                )
        elif mode == MODE_LENS_ONLY_MASK:
            self.file_count_label.configure(
                text=f"{ni} images, lens-only mask", text_color=COLOR_BLUE,
            )
        elif mode == MODE_MANUAL_FOV:
            self.file_count_label.configure(
                text=f"{ni} images, manual FOV", text_color=COLOR_BLUE,
            )
        else:
            self.file_count_label.configure(
                text=f"{ni} images", text_color=COLOR_TEXT_DIM,
            )

    def validate(self, shared_fov):
        errors = []
        if not self.cal_path.get() or not Path(self.cal_path.get()).is_file():
            errors.append(f"{self.name}: Calibration XML not found")
        if not self.lens_label.get().strip():
            errors.append(f"{self.name}: Lens label is empty")
        if not self.images_dir.get() or not Path(self.images_dir.get()).is_dir():
            errors.append(f"{self.name}: Images directory not found")

        masks = self.masks_dir.get().strip()
        if masks and not Path(masks).is_dir():
            errors.append(f"{self.name}: Masks directory does not exist")

        lensmask = self.lensonlymask_path.get().strip()
        if lensmask and not Path(lensmask).is_file():
            errors.append(f"{self.name}: Lens-only mask file not found")

        # Per-lens FOV override validation
        if self._fov_override_var.get():
            v = self._fov_override_value.get().strip()
            if v and not _is_valid_positive_int(v):
                errors.append(f"{self.name}: Per-lens FOV override must be a positive integer")

        # At least one support source
        effective_fov = self.get_effective_fov(shared_fov)
        mode, _ = resolve_mode(
            self.cal_path.get(), self.images_dir.get(),
            masks, lensmask, effective_fov,
        )
        if mode == MODE_INCOMPLETE:
            errors.append(f"{self.name}: Provide masks directory, lens-only mask, or FOV")

        return errors

    def get_values(self):
        return {
            "cal_path": self.cal_path.get(),
            "lens_label": self.lens_label.get(),
            "images_dir": self.images_dir.get(),
            "masks_dir": self.masks_dir.get(),
            "lensonlymask_path": self.lensonlymask_path.get(),
            "fov_override_enabled": self._fov_override_var.get(),
            "fov_override_value": self._fov_override_value.get(),
        }

    def set_values(self, data):
        for key in ("cal_path", "lens_label", "images_dir", "masks_dir", "lensonlymask_path"):
            if key in data:
                getattr(self, key).set(data[key])
        if "fov_override_enabled" in data:
            self._fov_override_var.set(data["fov_override_enabled"])
            self._handle_fov_override_toggle()
        if "fov_override_value" in data:
            self._fov_override_value.set(data["fov_override_value"])


class MediaSetRow:
    """One explicit additional media set for frame-camera images."""

    def __init__(self, parent, index, on_change=None, on_remove=None, data=None):
        self.index = index
        self._on_change = on_change
        self._on_remove = on_remove

        self.frame = ctk.CTkFrame(parent, fg_color=COLOR_INPUT, corner_radius=6)
        self.frame.grid_columnconfigure(0, weight=1)

        self.name = ctk.StringVar(value=f"Media {index + 1}")
        self.image_root = ctk.StringVar(value="")
        self.mask_root = ctk.StringVar(value="")

        row = 0
        header = ctk.CTkFrame(self.frame, fg_color="transparent")
        header.grid(row=row, column=0, sticky="ew", padx=8, pady=(8, 2))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text=f"Frame Camera Set {index + 1}", font=FONT_LABEL).grid(
            row=0, column=0, sticky="w",
        )
        ctk.CTkButton(
            header, text="X", width=28, fg_color=COLOR_RED,
            command=lambda: self._on_remove(self) if self._on_remove else None,
        ).grid(row=0, column=2, sticky="e")
        row += 1

        row = self._add_entry(row, "Name", self.name)
        row = self._add_dir_picker(row, "Image folder", self.image_root)
        row = self._add_dir_picker(row, "Mask folder", self.mask_root, optional=True)

        if data:
            self.set_values(data)

        for var in (self.name, self.image_root, self.mask_root):
            var.trace_add("write", lambda *_: self._notify_change())

    def _notify_change(self):
        if self._on_change:
            self._on_change()

    def _add_entry(self, row, label, var):
        ctk.CTkLabel(self.frame, text=label, font=("", 10), text_color=COLOR_TEXT_DIM).grid(
            row=row, column=0, sticky="w", padx=8, pady=(4, 0),
        )
        row += 1
        ctk.CTkEntry(self.frame, textvariable=var, font=FONT_LABEL).grid(
            row=row, column=0, sticky="ew", padx=8, pady=(0, 2),
        )
        return row + 1

    def _add_dir_picker(self, row, label, var, optional=False):
        ctk.CTkLabel(self.frame, text=label, font=("", 10), text_color=COLOR_TEXT_DIM).grid(
            row=row, column=0, sticky="w", padx=8, pady=(4, 0),
        )
        row += 1
        frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=8, pady=(0, 6))
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(frame, textvariable=var, font=FONT_LABEL).grid(
            row=0, column=0, sticky="ew", padx=(0, 4),
        )

        def browse():
            p = filedialog.askdirectory()
            if p:
                var.set(p)

        ctk.CTkButton(frame, text="...", width=36, command=browse).grid(row=0, column=1)
        if optional:
            ctk.CTkButton(
                frame, text="X", width=28, fg_color=COLOR_RED,
                command=lambda: var.set(""),
            ).grid(row=0, column=2, padx=(4, 0))
        return row + 1

    def is_blank(self):
        name = self.name.get().strip()
        default_name = f"Media {self.index + 1}"
        return (
            not self.image_root.get().strip()
            and not self.mask_root.get().strip()
            and (not name or name == default_name)
        )

    def get_values(self):
        return {
            "name": self.name.get(),
            "image_root": self.image_root.get(),
            "mask_root": self.mask_root.get(),
        }

    def set_values(self, data):
        self.name.set(data.get("name", self.name.get()))
        self.image_root.set(data.get("image_root", ""))
        self.mask_root.set(data.get("mask_root", ""))

    def manifest_entry(self):
        entry = {
            "name": self.name.get().strip(),
            "image_root": self.image_root.get().strip(),
        }
        mask_root = self.mask_root.get().strip()
        if mask_root:
            entry["mask_root"] = mask_root
        return entry

    def validate(self, require_masks=False):
        errors = []
        values = self.get_values()
        if self.is_blank():
            return errors
        if not values["name"].strip():
            errors.append(f"Media set {self.index + 1}: name is empty")
        if not values["image_root"].strip() or not Path(values["image_root"]).is_dir():
            errors.append(f"Media set {self.index + 1}: image folder not found")
        mask_root = values["mask_root"].strip()
        if require_masks and not mask_root:
            errors.append(f"Media set {self.index + 1}: mask folder is required")
        if mask_root and not Path(mask_root).is_dir():
            errors.append(f"Media set {self.index + 1}: mask folder not found")
        return errors


class CubemapGUI(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title(_SCRIPT.stem)
        self.geometry("1100x900")
        self.minsize(950, 900)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._proc = None
        self._reader_thread = None
        self._log_queue = queue.Queue()
        self._is_running = False
        self._run_queue = []
        self._current_run_label = ""
        self._progress_hwm = 0.0
        self._progress_v2_sensor_index = 0
        self._progress_v2_sensor_count = 0
        self._prefs = _load_prefs()
        self._run_state = {}
        self._media_set_rows = []
        self._routing_lock = threading.Lock()
        self._routing_job_seq = 0
        self._restoring_prefs = False
        self._pending_colmap_manifest_restore = None

        self._build_ui()
        self._restore_prefs()
        self._refresh_purpose_ui(scroll=False)
        self._update_all_modes()
        self._poll_log()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)  # purpose toggle row
        self.grid_rowconfigure(1, weight=1)  # panels row
        self.grid_propagate(False)

        # ── Purpose toggle at very top ──────────────────────────────────
        purpose_bar = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=8, height=44)
        purpose_bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        purpose_bar.grid_propagate(False)
        purpose_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(purpose_bar, text="Purpose:", font=FONT_LABEL).grid(
            row=0, column=0, sticky="w", padx=(12, 8), pady=8,
        )
        self._purpose_var = ctk.StringVar(value=PURPOSE_METASHAPE)
        self._purpose_seg = ctk.CTkSegmentedButton(
            purpose_bar,
            values=[PURPOSE_METASHAPE, PURPOSE_COLMAP],
            variable=self._purpose_var,
            command=lambda *_: self._on_purpose_changed(),
            dynamic_resizing=False,
            width=280,
        )
        self._purpose_seg.grid(row=0, column=1, sticky="w", pady=8)

        # ── Panels container with draggable divider ─────────────────────
        panels = ctk.CTkFrame(self, fg_color="transparent")
        panels.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        panels.grid_rowconfigure(0, weight=1)
        # 1:1 default; both columns share the available width equally
        panels.grid_columnconfigure(0, weight=1, minsize=380, uniform="panel")
        panels.grid_columnconfigure(1, weight=0)              # grip
        panels.grid_columnconfigure(2, weight=1, minsize=380, uniform="panel")
        self._panels = panels

        # Left panel
        left_outer = ctk.CTkFrame(panels, fg_color=COLOR_CARD, corner_radius=8)
        left_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 0))
        left_outer.grid_rowconfigure(0, weight=1)
        left_outer.grid_columnconfigure(0, weight=1)
        self._left_outer = left_outer

        self._left_scroll = ctk.CTkScrollableFrame(left_outer, fg_color=COLOR_CARD, corner_radius=0)
        self._left_scroll.grid(row=0, column=0, sticky="nsew")

        # Metashape mode content (existing _build_left)
        self._metashape_left_frame = ctk.CTkFrame(self._left_scroll, fg_color="transparent")
        self._metashape_left_frame.pack(fill="both", expand=True)
        self._build_left(self._metashape_left_frame)

        # COLMAP mode content
        self._colmap_left_frame = ctk.CTkFrame(self._left_scroll, fg_color="transparent")
        # Hidden by default — shown when purpose is COLMAP
        self._build_colmap_left(self._colmap_left_frame)

        # Draggable grip divider
        grip = ctk.CTkFrame(panels, fg_color=COLOR_TEXT_DIM, width=8, corner_radius=4)
        grip.grid(row=0, column=1, sticky="ns", padx=2, pady=16)
        grip.configure(cursor="sb_h_double_arrow")
        self._grip = grip
        self._grip_dragging = False
        grip.bind("<Button-1>", self._grip_start)
        grip.bind("<B1-Motion>", self._grip_drag)

        # Right panel
        right = ctk.CTkFrame(panels, fg_color=COLOR_CARD, corner_radius=8)
        right.grid(row=0, column=2, sticky="nsew", padx=(0, 0))
        self._build_right(right)

    def _grip_start(self, event):
        self._grip_dragging = True
        self._grip_start_x = event.x_root
        self._grip_start_left_width = self._left_outer.winfo_width()

    def _grip_drag(self, event):
        if not self._grip_dragging:
            return
        dx = event.x_root - self._grip_start_x
        new_left = max(380, self._grip_start_left_width + dx)
        total = self._panels.winfo_width() - 12  # grip + padding
        new_right = total - new_left
        if new_right < 300:
            return
        # Update weights to reflect new proportions
        self._panels.grid_columnconfigure(0, weight=new_left, uniform="panel")
        self._panels.grid_columnconfigure(2, weight=new_right, uniform="panel")

    def _add_colmap_file_picker(self, parent, row, label, attr, filetypes=None, on_change=None):
        """File picker styled for the COLMAP panel — label with colon, placeholder text."""
        label_widget = ctk.CTkLabel(parent, text=label, font=FONT_LABEL)
        label_widget.grid(row=row, column=0, sticky="w", padx=12, pady=(4, 0))
        row += 1
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 2))
        frame.grid_columnconfigure(0, weight=1)

        var = ctk.StringVar()
        ctk.CTkEntry(frame, textvariable=var, font=FONT_LABEL,
                     placeholder_text="(not set)").grid(
            row=0, column=0, sticky="ew", padx=(0, 4))

        def browse():
            p = filedialog.askopenfilename(filetypes=filetypes or [("All", "*.*")])
            if p:
                var.set(p)
                if on_change:
                    on_change()

        ctk.CTkButton(frame, text="...", width=36, command=browse).grid(row=0, column=1)
        setattr(self, attr, var)
        setattr(self, f"{attr}_label", label_widget)
        setattr(self, f"{attr}_frame", frame)
        if on_change:
            var.trace_add("write", lambda *_: on_change())
        row += 1
        return row

    def _add_colmap_dir_picker(self, parent, row, label, attr, on_change=None):
        """Dir picker styled for the COLMAP panel — label with colon, placeholder text."""
        label_widget = ctk.CTkLabel(parent, text=label, font=FONT_LABEL)
        label_widget.grid(row=row, column=0, sticky="w", padx=12, pady=(4, 0))
        row += 1
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 2))
        frame.grid_columnconfigure(0, weight=1)

        var = ctk.StringVar()
        ctk.CTkEntry(frame, textvariable=var, font=FONT_LABEL,
                     placeholder_text="(not set)").grid(
            row=0, column=0, sticky="ew", padx=(0, 4))

        def browse():
            p = filedialog.askdirectory()
            if p:
                var.set(p)
                if on_change:
                    on_change()

        ctk.CTkButton(frame, text="...", width=36, command=browse).grid(row=0, column=1)
        setattr(self, attr, var)
        setattr(self, f"{attr}_label", label_widget)
        setattr(self, f"{attr}_frame", frame)
        if on_change:
            var.trace_add("write", lambda *_: on_change())
        row += 1
        return row

    def _build_colmap_left(self, parent):
        """Build the COLMAP export left panel (v2 — top-level sensors, no Body grouping)."""
        parent.grid_columnconfigure(0, weight=1)
        row = 0

        ctk.CTkLabel(parent, text="COLMAP Export", font=FONT_HEADING).grid(
            row=row, column=0, sticky="w", padx=12, pady=(8, 2))
        row += 1

        row = self._add_colmap_file_picker(parent, row, "Metashape XML  .xml",
                                           "_colmap_cameras_xml",
                                           filetypes=[("XML", "*.xml")],
                                           on_change=self._on_colmap_xml_changed)

        row = self._add_colmap_file_picker(parent, row, "Sparse Pointcloud  .ply",
                                           "_colmap_sparse_ply",
                                           filetypes=[("PLY", "*.ply")])

        row = self._add_colmap_dir_picker(parent, row, "COLMAP Output  dir",
                                          "_colmap_output_dir")

        self._colmap_discovery_status = ctk.CTkLabel(
            parent, text="", font=FONT_LABEL, text_color=COLOR_TEXT_DIM)
        self._colmap_discovery_status.grid(row=row, column=0, sticky="w", padx=12, pady=(2, 0))
        row += 1

        # Empty state — visible until sensors are discovered
        self._colmap_empty_state = ctk.CTkFrame(parent, fg_color="transparent")
        self._colmap_empty_state.grid(row=row, column=0, sticky="ew", padx=12, pady=(8, 4))
        ctk.CTkLabel(
            self._colmap_empty_state,
            text="Load a Metashape XML file to discover sensors",
            font=("", 16),
            text_color=COLOR_TEXT_DIM,
        ).pack(anchor="center", pady=(4, 2))
        ctk.CTkLabel(
            self._colmap_empty_state,
            text="Fisheye, frame, and equirectangular sensors are auto-detected\nfrom the Metashape alignment.",
            font=("", 12),
            text_color="#666666",
            justify="center",
        ).pack(anchor="center", pady=(0, 4))
        ctk.CTkButton(
            self._colmap_empty_state,
            text="Load Sensors",
            width=140,
            command=self._on_colmap_xml_changed,
        ).pack(anchor="center", pady=(2, 6))
        row += 1

        self._colmap_sensors_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self._colmap_sensors_frame.grid(row=row, column=0, sticky="ew", padx=0)
        self._colmap_sensors_frame.grid_columnconfigure(0, weight=1)
        row += 1

        # ── Options ──────────────────────────────────────────────────
        ctk.CTkFrame(parent, fg_color=COLOR_TEXT_DIM, height=1).grid(
            row=row, column=0, sticky="ew", padx=12, pady=(6, 2))
        row += 1

        ctk.CTkLabel(parent, text="Options", font=FONT_HEADING).grid(
            row=row, column=0, sticky="w", padx=12, pady=(2, 2))
        row += 1

        options_frame = ctk.CTkFrame(parent, fg_color="transparent")
        options_frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 4))
        options_frame.grid_columnconfigure(1, weight=1)

        opt_row = 0
        ctk.CTkLabel(options_frame, text="Pose:", font=FONT_LABEL).grid(
            row=opt_row, column=0, sticky="w", padx=(0, 6), pady=2)
        self._colmap_pose_convention_var = ctk.StringVar(value="metashape_camera_to_world")
        ctk.CTkOptionMenu(
            options_frame,
            values=["metashape_camera_to_world", "metashape_world_to_camera"],
            variable=self._colmap_pose_convention_var,
            width=240,
        ).grid(row=opt_row, column=1, sticky="w", pady=2)
        opt_row += 1

        self._colmap_force_assets_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(options_frame, text="Force assets",
                        variable=self._colmap_force_assets_var,
                        font=FONT_LABEL).grid(row=opt_row, column=0, sticky="w", padx=(0, 16), pady=(6, 3))
        self._colmap_projected_tracks_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(options_frame, text="Projected tracks",
                        variable=self._colmap_projected_tracks_var,
                        font=FONT_LABEL).grid(row=opt_row, column=1, sticky="w", padx=(16, 0), pady=(6, 3))
        opt_row += 1

        self._colmap_normalize_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(options_frame, text="Normalize scene",
                        variable=self._colmap_normalize_var,
                        font=FONT_LABEL).grid(row=opt_row, column=0, sticky="w", padx=(0, 16), pady=(3, 6))
        self._colmap_keep_processing_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(options_frame, text="Keep processing files",
                        variable=self._colmap_keep_processing_var,
                        font=FONT_LABEL).grid(row=opt_row, column=1, sticky="w", padx=(16, 0), pady=(3, 6))
        opt_row += 1
        row += 1

        # ── Export / Cancel ──────────────────────────────────────────
        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(4, 8))
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        self._colmap_export_btn = ctk.CTkButton(
            btn_frame, text="Export", font=("", 14, "bold"),
            fg_color=COLOR_GREEN, hover_color=COLOR_GREEN_H,
            command=self._on_colmap_export, height=38, state="disabled",
        )
        self._colmap_export_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self._colmap_cancel_btn = ctk.CTkButton(
            btn_frame, text="Cancel", font=("", 14, "bold"),
            fg_color=COLOR_RED, hover_color="#7a0000",
            command=self._on_cancel, height=38, state="disabled",
        )
        self._colmap_cancel_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        row += 1

        # Internal state — three sensor-type stores, no body grouping
        self._colmap_discovered = None
        self._colmap_fisheye_cards = {}
        self._colmap_frame_cards = {}
        self._colmap_equirect_cards = {}

    def _on_colmap_xml_changed(self):
        """Re-run sensor discovery (v2 — three categories, no body grouping)."""
        if getattr(self, "_restoring_prefs", False):
            status = getattr(self, "_colmap_discovery_status", None)
            if status is not None:
                status.configure(
                    text="Saved XML restored; click Load Sensors to discover.",
                    text_color=COLOR_TEXT_DIM,
                )
            return
        try:
            from gui.sensor_discovery import discover_sensors, recommended_equirect_width
        except ImportError:
            from sensor_discovery import discover_sensors, recommended_equirect_width

        xml_path = getattr(self, "_colmap_cameras_xml", None)
        if xml_path is None:
            return
        path_str = xml_path.get().strip()
        if not path_str:
            return

        from pathlib import Path
        xml = Path(path_str)
        if not xml.is_file():
            self._colmap_discovery_status.configure(
                text="File not found", text_color=COLOR_RED)
            return

        result = discover_sensors(xml)
        if "error" in result:
            self._colmap_discovery_status.configure(
                text=f"Error: {result['error']}", text_color=COLOR_RED)
            self._colmap_export_btn.configure(state="disabled")
            return

        fisheye_sensors = list(result["equisolid"]) + list(result["equidistant"])
        frame_sensors = list(result["frame"])
        equirect_sensors = list(result["equirectangular"])
        n_fis = len(fisheye_sensors)
        n_fr = len(frame_sensors)
        n_eq = len(equirect_sensors)

        self._colmap_discovery_status.configure(
            text=f"{n_fis} fisheye, {n_fr} frame, {n_eq} equirect sensors detected",
            text_color=COLOR_TEXT_DIM)
        self._colmap_discovered = result

        empty = getattr(self, "_colmap_empty_state", None)
        if empty is not None:
            empty.grid_remove()

        for widget in self._colmap_sensors_frame.winfo_children():
            widget.destroy()
        self._colmap_fisheye_cards = {}
        self._colmap_frame_cards = {}
        self._colmap_equirect_cards = {}

        def _fisheye_initial_width(_cal):
            # Full optimal-width calculation builds dense ray fields and can
            # freeze the GUI on high-resolution fisheye sensors. Use a stable
            # editable default here; the background routing worker fills the
            # XML-informed recommended width when its decision is ready.
            return 2048

        def _equirect_initial_width(cal):
            return recommended_equirect_width(cal, "reframe")

        card_row = 0
        if n_fis > 0:
            ctk.CTkLabel(self._colmap_sensors_frame, text="Fisheye Sensors",
                         font=FONT_HEADING).grid(
                row=card_row, column=0, sticky="w", padx=12, pady=(8, 4))
            card_row += 1
            equisolid_ids = {s["sensor_id"] for s in result["equisolid"]}
            for sensor in fisheye_sensors:
                stype = "equisolid_fisheye" if sensor["sensor_id"] in equisolid_ids else "equidistant_fisheye"
                card_row = self._build_fisheye_sensor_card(
                    self._colmap_sensors_frame, card_row, sensor, stype,
                    _fisheye_initial_width(sensor.get("calibration")))

        if n_fr > 0:
            ctk.CTkLabel(self._colmap_sensors_frame, text="Frame Sensors",
                         font=FONT_HEADING).grid(
                row=card_row, column=0, sticky="w", padx=12, pady=(12, 4))
            card_row += 1
            for sensor in frame_sensors:
                card_row = self._build_frame_sensor_card(
                    self._colmap_sensors_frame, card_row, sensor)

        if n_eq > 0:
            ctk.CTkLabel(self._colmap_sensors_frame, text="Equirectangular Sensors",
                         font=FONT_HEADING).grid(
                row=card_row, column=0, sticky="w", padx=12, pady=(12, 4))
            card_row += 1
            for sensor in equirect_sensors:
                card_row = self._build_equirect_sensor_card(
                    self._colmap_sensors_frame, card_row, sensor,
                    _equirect_initial_width(sensor.get("calibration")))

        if n_fis == 0 and n_fr == 0 and n_eq == 0:
            ctk.CTkLabel(self._colmap_sensors_frame,
                         text="No fisheye, frame, or equirectangular sensors found in cameras.xml.",
                         font=FONT_LABEL, text_color=COLOR_TEXT_DIM).grid(
                row=card_row, column=0, sticky="w", padx=12, pady=(8, 4))
            card_row += 1

        pending_restore = getattr(self, "_pending_colmap_manifest_restore", None)
        if pending_restore and pending_restore.get("cameras_xml") == path_str:
            self._pending_colmap_manifest_restore = None
            self._apply_colmap_card_prefs(pending_restore)

        self._colmap_check_export_ready()

    # ── Multi-directory section helpers ──────────────────────────────

    def _build_dir_section(self, parent, row, label_text, card_state, kind, sensor_id, labels):
        """Build a multi-directory picker section ('Image directories:' or 'Mask directories:').

        kind is 'img' or 'mask'. card_state carries the row state and matching context.
        """
        section = ctk.CTkFrame(parent, fg_color="transparent")
        section.grid(row=row, column=0, sticky="ew", padx=8, pady=(2, 0))
        section.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(section, text=label_text, font=("", 11),
                     text_color=COLOR_TEXT, anchor="w").grid(
            row=0, column=0, sticky="w", pady=(2, 1))

        rows_container = ctk.CTkFrame(section, fg_color="transparent")
        rows_container.grid(row=1, column=0, sticky="ew")
        rows_container.grid_columnconfigure(0, weight=1)

        card_state[f"{kind}_rows_container"] = rows_container
        card_state[f"{kind}_dirs"] = []
        card_state[f"{kind}_row_frames"] = []
        card_state[f"{kind}_labels"] = labels
        self._add_dir_row(card_state, kind, sensor_id, is_first=True)
        return row + 1

    def _add_dir_row(self, card_state, kind, sensor_id, is_first=False):
        """Append a new directory-picker row to the kind's container."""
        container = card_state[f"{kind}_rows_container"]
        dirs_list = card_state[f"{kind}_dirs"]
        frames_list = card_state[f"{kind}_row_frames"]

        var = ctk.StringVar()
        idx = len(dirs_list)
        row_frame = ctk.CTkFrame(container, fg_color="transparent")
        row_frame.grid(row=idx, column=0, sticky="ew", pady=1)
        row_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkEntry(row_frame, textvariable=var, font=("", 11), height=26).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))

        def browse():
            p = filedialog.askdirectory()
            if p:
                var.set(p)

        ctk.CTkButton(row_frame, text="...", width=26, height=26,
                      fg_color=COLOR_BLUE, command=browse).grid(row=0, column=1, padx=(0, 4))

        if is_first:
            ctk.CTkButton(row_frame, text="+", width=26, height=26,
                          fg_color=COLOR_GREEN, hover_color=COLOR_GREEN_H,
                          command=lambda: self._add_dir_row(card_state, kind, sensor_id)
                          ).grid(row=0, column=2)
        else:
            ctk.CTkButton(row_frame, text="x", width=26, height=26,
                          fg_color=COLOR_RED, hover_color="#7a0000",
                          command=lambda i=idx: self._remove_dir_row(card_state, kind, i, sensor_id)
                          ).grid(row=0, column=2)

        dirs_list.append(var)
        frames_list.append(row_frame)

        def on_dir_changed(*_):
            self._update_match_count(card_state)
            if "multi_pinhole_var" in card_state:
                self._schedule_fisheye_routing(sensor_id, reset_checkbox=False)

        var.trace_add("write", on_dir_changed)
        self._update_match_count(card_state)

    def _remove_dir_row(self, card_state, kind, idx, sensor_id):
        """Remove a directory-picker row."""
        dirs_list = card_state[f"{kind}_dirs"]
        frames_list = card_state[f"{kind}_row_frames"]
        if idx < 0 or idx >= len(frames_list):
            return
        frames_list[idx].destroy()
        del dirs_list[idx]
        del frames_list[idx]
        # Re-grid remaining rows to keep their row indices contiguous
        for new_idx, frame in enumerate(frames_list):
            frame.grid_configure(row=new_idx)
        self._update_match_count(card_state)
        if "multi_pinhole_var" in card_state:
            self._schedule_fisheye_routing(sensor_id, reset_checkbox=False)

    def _update_match_count(self, card_state):
        """Recompute the match status label for a fisheye/frame/equirect card."""
        try:
            from gui.sensor_discovery import match_sensor_images_multi
        except ImportError:
            from sensor_discovery import match_sensor_images_multi
        from pathlib import Path

        match_label = card_state.get("match_label")
        if match_label is None:
            return
        labels = card_state.get("img_labels") or card_state.get("sensor_record", {}).get("camera_labels", [])
        if not labels:
            labels = [str(cid) for cid in card_state.get("sensor_record", {}).get("camera_ids", [])]
        img_paths = [Path(v.get().strip()) for v in card_state.get("img_dirs", []) if v.get().strip()]
        if not img_paths:
            match_label.configure(text="No image directories set", text_color=COLOR_AMBER)
            self._colmap_check_export_ready()
            return
        valid = [p for p in img_paths if p.is_dir()]
        if not valid:
            match_label.configure(text="Image directories not found", text_color=COLOR_AMBER)
            self._colmap_check_export_ready()
            return
        result = match_sensor_images_multi(valid, labels)
        total = result["total"]
        matched = result["matched"]
        if total == 0:
            match_label.configure(text="No camera labels to match against", text_color=COLOR_AMBER)
        elif matched == total:
            match_label.configure(text=f"{matched}/{total} images matched",
                                  text_color=COLOR_GREEN)
        else:
            match_label.configure(
                text=f"{matched}/{total} images matched — {total - matched} missing",
                text_color=COLOR_AMBER)
        self._colmap_check_export_ready()

    def _on_lens_only_toggled(self, sensor_id):
        """Enable/disable the lens-only mask path entry based on checkbox state."""
        card = self._colmap_fisheye_cards.get(sensor_id)
        if not card:
            return
        enabled = card["lens_only_enabled_var"].get()
        state = "normal" if enabled else "disabled"
        entry = card.get("lens_only_entry")
        btn = card.get("lens_only_btn")
        if entry:
            entry.configure(state=state)
        if btn:
            btn.configure(state=state)
        self._colmap_check_export_ready()
        self._schedule_fisheye_routing(sensor_id, reset_checkbox=False)

    def _fisheye_card_mask_dirs(self, card):
        """Return existing mask directories for routing recompute."""
        return [
            Path(v.get().strip())
            for v in card.get("mask_dirs", [])
            if v.get().strip() and Path(v.get().strip()).is_dir()
        ]

    def _fisheye_card_lens_only_mask(self, card):
        """Return the enabled lens-only mask path for routing recompute."""
        if not card.get("lens_only_enabled_var") or not card["lens_only_enabled_var"].get():
            return None
        value = card.get("lens_only_path_var").get().strip()
        if not value:
            return None
        path = Path(value)
        return path if path.is_file() else None

    def _format_theta_label(self, decision):
        """Small inline theta label text for a routing decision."""
        if decision is None or decision.theta_max_deg is None:
            return "(theta_max=?)"
        return f"(theta_max={decision.theta_max_deg:.0f}°)"

    def _routing_recommends_multi(self, decision):
        return decision is not None and decision.processing_mode == "multi_pinhole"

    def _apply_routing_to_card(self, card, decision, reset_checkbox=False):
        """Store routing, update theta display, and optionally reset the checkbox."""
        card["routing"] = decision
        card["routing_error"] = None
        theta_label = card.get("theta_label")
        if theta_label is not None:
            theta_label.configure(
                text=self._format_theta_label(decision),
                text_color="#666666",
            )

        should_reset = reset_checkbox or not card.get("multi_pinhole_user_overridden")
        if should_reset:
            card["_suppress_multi_trace"] = True
            try:
                card["multi_pinhole_var"].set(self._routing_recommends_multi(decision))
            finally:
                card["_suppress_multi_trace"] = False
            if reset_checkbox:
                card["multi_pinhole_user_overridden"] = False
        recommended_width = getattr(decision, "recommended_output_width", None)
        if (
            recommended_width
            and (
                not card.get("width_user_overridden")
                or card["width_var"].get().strip() in ("", "0")
            )
        ):
            card["_suppress_width_trace"] = True
            try:
                card["width_var"].set(str(int(recommended_width)))
            finally:
                card["_suppress_width_trace"] = False

    def _on_fisheye_width_changed(self, sensor_id):
        card = self._colmap_fisheye_cards.get(sensor_id)
        if not card or card.get("_suppress_width_trace"):
            return
        value = card["width_var"].get().strip()
        card["width_user_overridden"] = value not in ("", "0")

    def _recommended_equirect_width_for_card(self, card):
        try:
            from gui.sensor_discovery import recommended_equirect_width
        except ImportError:
            from sensor_discovery import recommended_equirect_width
        sensor = card.get("sensor_record", {})
        return recommended_equirect_width(
            sensor.get("calibration"),
            card["split_mode_var"].get(),
        )

    def _apply_equirect_auto_width(self, card):
        value = card["split_width_var"].get().strip()
        if card.get("split_width_user_overridden") and value not in ("", "0"):
            return
        recommended = self._recommended_equirect_width_for_card(card)
        card["_suppress_split_width_trace"] = True
        try:
            card["split_width_var"].set(str(int(recommended)))
        finally:
            card["_suppress_split_width_trace"] = False
        card["split_width_user_overridden"] = False

    def _normalize_equirect_width_entry(self, sensor_id):
        card = self._colmap_equirect_cards.get(sensor_id)
        if not card:
            return
        if card["split_width_var"].get().strip() in ("", "0"):
            self._apply_equirect_auto_width(card)

    def _schedule_equirect_auto_width(self, sensor_id):
        card = self._colmap_equirect_cards.get(sensor_id)
        if not card or card.get("_split_width_auto_after_id") is not None:
            return

        def _apply_when_idle():
            card["_split_width_auto_after_id"] = None
            if card["split_width_var"].get().strip() == "0":
                self._apply_equirect_auto_width(card)

        card["_split_width_auto_after_id"] = self.after_idle(_apply_when_idle)

    def _on_equirect_width_changed(self, sensor_id):
        card = self._colmap_equirect_cards.get(sensor_id)
        if not card or card.get("_suppress_split_width_trace"):
            return
        value = card["split_width_var"].get().strip()
        card["split_width_user_overridden"] = value not in ("", "0")
        if value == "0":
            self._schedule_equirect_auto_width(sensor_id)

    def _on_equirect_split_mode_changed(self, sensor_id, _value=None):
        card = self._colmap_equirect_cards.get(sensor_id)
        if not card:
            return
        self._apply_equirect_auto_width(card)

    def _set_fisheye_routing_pending(self, card):
        card["routing_pending"] = True
        theta_label = card.get("theta_label")
        if theta_label is not None:
            theta_label.configure(text="(routing...)", text_color=COLOR_TEXT_DIM)

    def _schedule_fisheye_routing(
        self,
        sensor_id,
        *,
        reset_checkbox=False,
        clear_cache=False,
        delay_ms=350,
    ):
        """Debounce and then compute routing away from the Tk event loop."""
        card = self._colmap_fisheye_cards.get(sensor_id)
        if not card:
            return None
        pending_after = card.get("routing_after_id")
        if pending_after is not None:
            try:
                self.after_cancel(pending_after)
            except Exception:
                pass
            card["routing_after_id"] = None
        self._set_fisheye_routing_pending(card)
        self._colmap_check_export_ready()
        card["routing_after_id"] = self.after(
            delay_ms,
            lambda s=sensor_id, r=reset_checkbox, c=clear_cache: (
                self._start_fisheye_routing_worker(
                    s,
                    reset_checkbox=r,
                    clear_cache=c,
                )
            ),
        )
        return None

    def _start_fisheye_routing_worker(self, sensor_id, *, reset_checkbox=False, clear_cache=False):
        """Start one background routing job and ignore stale completions."""
        card = self._colmap_fisheye_cards.get(sensor_id)
        if not card:
            return None
        card["routing_after_id"] = None
        self._routing_job_seq += 1
        job_id = self._routing_job_seq
        card["routing_job_id"] = job_id

        sensor_record = dict(card["sensor_record"])
        mask_dirs = self._fisheye_card_mask_dirs(card)
        lens_only_mask = self._fisheye_card_lens_only_mask(card)

        def worker():
            decision = None
            error = None
            try:
                with self._routing_lock:
                    try:
                        from gui.routing import clear_cache as _clear_cache, get_routing
                    except ImportError:
                        from routing import clear_cache as _clear_cache, get_routing
                    if clear_cache:
                        _clear_cache()
                    decision = get_routing(
                        sensor_record,
                        mask_dirs=mask_dirs,
                        lens_only_mask=lens_only_mask,
                    )
            except (Exception, SystemExit) as exc:
                error = exc
            try:
                self.after(
                    0,
                    lambda d=decision, e=error: self._finish_fisheye_routing(
                        sensor_id,
                        job_id,
                        d,
                        e,
                        reset_checkbox=reset_checkbox,
                    ),
                )
            except Exception:
                pass

        thread = threading.Thread(
            target=worker,
            name=f"fisheye-routing-{sensor_id}",
            daemon=True,
        )
        thread.start()
        return None

    def _finish_fisheye_routing(self, sensor_id, job_id, decision, error, *, reset_checkbox=False):
        card = self._colmap_fisheye_cards.get(sensor_id)
        if not card or card.get("routing_job_id") != job_id:
            return None
        card["routing_pending"] = False
        if error is not None:
            card["routing"] = None
            card["routing_error"] = str(error)
            theta_label = card.get("theta_label")
            if theta_label is not None:
                theta_label.configure(text="(theta_max=?)", text_color=COLOR_AMBER)
            self._colmap_check_export_ready()
            return None

        self._apply_routing_to_card(card, decision, reset_checkbox=reset_checkbox)
        self._colmap_check_export_ready()
        return decision

    def _refresh_fisheye_routing(self, sensor_id, *, reset_checkbox=False, clear_cache=False):
        """Compatibility wrapper: schedule routing without blocking the GUI."""
        return self._schedule_fisheye_routing(
            sensor_id,
            reset_checkbox=reset_checkbox,
            clear_cache=clear_cache,
            delay_ms=0,
        )

    def _on_multi_pinhole_changed(self, sensor_id):
        """Track user overrides and warn for risky single-pinhole overrides."""
        card = self._colmap_fisheye_cards.get(sensor_id)
        if not card or card.get("_suppress_multi_trace"):
            return
        card["multi_pinhole_user_overridden"] = True
        decision = card.get("routing")
        if (
            decision is not None
            and decision.processing_mode == "multi_pinhole"
            and not card["multi_pinhole_var"].get()
        ):
            from tkinter import messagebox
            messagebox.showwarning(
                "Routing override",
                "This sensor is routed to multi-pinhole. Forcing single-pinhole "
                "will produce a single flat image covering the center of the "
                "lens, with edges cropped. By default, the output width "
                "matches a 90° field of view at the lens center's pixel "
                "density. To use a different size, enter a value in the "
                "Width field.",
            )
        self._colmap_check_export_ready()

    def _on_reevaluate_routing(self, sensor_id):
        """Re-run routing for a sensor and reset multi_pinhole to recommendation."""
        self._refresh_fisheye_routing(
            sensor_id,
            reset_checkbox=True,
            clear_cache=True,
        )

    # ── Sensor cards ────────────────────────────────────────────────

    def _build_fisheye_sensor_card(self, parent, row, sensor, stype, optimal_width):
        """Build a v2 fisheye sensor card per the Pencil design.

        Layout (per gui/design_planning.pen frame `qhLDi`):
            Sensor metadata header (with ↻ Re-evaluate top-right)
            Image directories: rows with [...] + [+]/[x]
            Mask directories:  rows with [...] + [+]/[x]
            match-status label
            [cb] Multi-pinhole (theta_max=…°)  Width: [____]  (0=auto)
            [cb] Lens-only mask  [path]  [...]
        """
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=6,
                            border_width=1, border_color="#444444")
        card.grid(row=row, column=0, sticky="ew", padx=8, pady=(4, 4))
        card.grid_columnconfigure(0, weight=1)

        sid = sensor["sensor_id"]
        labels = sensor.get("camera_labels") or [str(c) for c in sensor.get("camera_ids", [])]

        card_state = {
            "sensor_id": sid,
            "sensor_record": sensor,
            "sensor_type": stype,
            "img_labels": labels,
            "multi_pinhole_var": ctk.BooleanVar(value=True),
            "width_var": ctk.StringVar(value=str(optimal_width)),
            "output_format_var": ctk.StringVar(value="jpg"),
            "lens_only_enabled_var": ctk.BooleanVar(value=False),
            "lens_only_path_var": ctk.StringVar(),
            "routing": None,
            "routing_error": None,
            "routing_pending": False,
            "multi_pinhole_user_overridden": False,
            "width_user_overridden": False,
            "_suppress_multi_trace": False,
            "_suppress_width_trace": False,
        }
        self._colmap_fisheye_cards[sid] = card_state

        card_row = 0

        # Header: meta on left, ↻ Re-evaluate on right
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=card_row, column=0, sticky="ew", padx=8, pady=(6, 2))
        header.grid_columnconfigure(0, weight=1)
        meta = (f"Sensor {sid} — {sensor.get('label', '')} — {stype} — "
                f"{sensor['camera_count']} cameras")
        ctk.CTkLabel(header, text=meta, font=("", 11),
                     text_color=COLOR_TEXT_DIM).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            header, text="↻ Re-evaluate", font=("", 11),
            fg_color="transparent", hover_color="#3a3a3a",
            text_color=COLOR_BLUE, width=100, height=20,
            command=lambda s=sid: self._on_reevaluate_routing(s),
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))
        card_row += 1

        # Fourier corrections indicator
        calibration = sensor.get("calibration") or {}
        has_corrections = "corrections" in calibration
        if has_corrections:
            corr_count = len(calibration["corrections"].coeffs)
            corr_text = f"Fourier corrections: {corr_count} coefficients"
            corr_color = COLOR_GREEN
        else:
            corr_text = "Fourier corrections: not present"
            corr_color = COLOR_TEXT_DIM
        ctk.CTkLabel(card, text=corr_text, font=("", 10),
                     text_color=corr_color).grid(
            row=card_row, column=0, sticky="w", padx=12, pady=(0, 2))
        card_row += 1

        # Image and mask directory sections
        card_row = self._build_dir_section(card, card_row, "Image directories:",
                                           card_state, "img", sid, labels)
        card_row = self._build_dir_section(card, card_row, "Mask directories:",
                                           card_state, "mask", sid, labels)

        # Match status label
        match_label = ctk.CTkLabel(card, text="", font=("", 11), text_color=COLOR_AMBER)
        match_label.grid(row=card_row, column=0, sticky="w", padx=8, pady=(2, 0))
        card_state["match_label"] = match_label
        card_row += 1

        # Mode row: [cb] Multi-pinhole (theta_max=…°)  Width: [_]  (0=auto)
        mode_row = ctk.CTkFrame(card, fg_color="transparent")
        mode_row.grid(row=card_row, column=0, sticky="ew", padx=8, pady=(6, 2))
        ctk.CTkCheckBox(
            mode_row, text="Multi-pinhole", variable=card_state["multi_pinhole_var"],
            font=("", 11), checkbox_width=14, checkbox_height=14,
        ).grid(row=0, column=0, sticky="w")
        # Theta info — Phase B item 9 populates from routing
        theta_label = ctk.CTkLabel(mode_row, text="(theta_max=…°)", font=("", 9),
                                   text_color="#666666")
        theta_label.grid(row=0, column=1, sticky="w", padx=(6, 12))
        card_state["theta_label"] = theta_label
        ctk.CTkLabel(mode_row, text="Width:", font=("", 11),
                     text_color=COLOR_TEXT).grid(row=0, column=2, sticky="w", padx=(0, 4))
        ctk.CTkEntry(mode_row, textvariable=card_state["width_var"], width=60,
                     font=("Consolas", 11), height=24).grid(row=0, column=3, sticky="w", padx=(0, 6))
        ctk.CTkLabel(mode_row, text="(0=auto)", font=("", 9),
                     text_color="#666666").grid(row=0, column=4, sticky="w")
        ctk.CTkLabel(mode_row, text="Format:", font=("", 11),
                     text_color=COLOR_TEXT).grid(row=0, column=5, sticky="w", padx=(12, 4))
        ctk.CTkOptionMenu(
            mode_row,
            values=["jpg", "png", "tiff"],
            variable=card_state["output_format_var"],
            width=70,
        ).grid(row=0, column=6, sticky="w")
        card_row += 1

        # Lens-only mask row
        lens_row = ctk.CTkFrame(card, fg_color="transparent")
        lens_row.grid(row=card_row, column=0, sticky="ew", padx=8, pady=(2, 8))
        lens_row.grid_columnconfigure(2, weight=1)
        ctk.CTkCheckBox(
            lens_row, text="Lens-only mask", variable=card_state["lens_only_enabled_var"],
            font=("", 11), checkbox_width=14, checkbox_height=14,
            command=lambda s=sid: self._on_lens_only_toggled(s),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        lens_entry = ctk.CTkEntry(lens_row, textvariable=card_state["lens_only_path_var"],
                                  font=("", 11), state="disabled", height=24)
        lens_entry.grid(row=0, column=2, sticky="ew", padx=(0, 4))
        card_state["lens_only_entry"] = lens_entry

        def browse_lens_only():
            p = filedialog.askopenfilename(
                filetypes=[("Image", "*.png *.jpg *.jpeg *.tif *.tiff"), ("All", "*.*")])
            if p:
                card_state["lens_only_path_var"].set(p)
                self._colmap_check_export_ready()

        lens_btn = ctk.CTkButton(lens_row, text="...", width=26, height=24,
                                 fg_color=COLOR_BLUE,
                                 command=browse_lens_only, state="disabled")
        lens_btn.grid(row=0, column=3, sticky="e")
        card_state["lens_only_btn"] = lens_btn
        card_row += 1

        card_state["multi_pinhole_var"].trace_add(
            "write", lambda *_args, s=sid: self._on_multi_pinhole_changed(s))
        card_state["width_var"].trace_add(
            "write", lambda *_args, s=sid: self._on_fisheye_width_changed(s))
        card_state["lens_only_path_var"].trace_add(
            "write", lambda *_args, s=sid: (
                self._colmap_check_export_ready(),
                self._schedule_fisheye_routing(s, reset_checkbox=False),
            ))

        self._schedule_fisheye_routing(sid, reset_checkbox=True)

        return row + 1

    def _build_frame_sensor_card(self, parent, row, sensor):
        """v2 frame sensor card: multi-dir images, multi-dir masks, match status."""
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=6,
                            border_width=1, border_color="#444444")
        card.grid(row=row, column=0, sticky="ew", padx=8, pady=(4, 4))
        card.grid_columnconfigure(0, weight=1)

        sid = sensor["sensor_id"]
        labels = sensor.get("camera_labels", [])
        card_state = {
            "sensor_id": sid,
            "sensor_record": sensor,
            "img_labels": labels,
        }
        self._colmap_frame_cards[sid] = card_state

        card_row = 0
        meta = (f"Sensor {sid} — {sensor.get('label', '')} — frame — "
                f"{sensor['camera_count']} cameras")
        ctk.CTkLabel(card, text=meta, font=("", 11),
                     text_color=COLOR_TEXT_DIM).grid(
            row=card_row, column=0, sticky="w", padx=8, pady=(6, 2))
        card_row += 1

        card_row = self._build_dir_section(card, card_row, "Image directories:",
                                           card_state, "img", sid, labels)
        card_row = self._build_dir_section(card, card_row, "Mask directories:",
                                           card_state, "mask", sid, labels)

        match_label = ctk.CTkLabel(card, text="", font=("", 11), text_color=COLOR_AMBER)
        match_label.grid(row=card_row, column=0, sticky="w", padx=8, pady=(2, 8))
        card_state["match_label"] = match_label
        card_row += 1

        return row + 1

    def _build_equirect_sensor_card(self, parent, row, sensor, optimal_width):
        """v2 equirectangular sensor card: multi-dir, split mode dropdown, split width."""
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=6,
                            border_width=1, border_color="#444444")
        card.grid(row=row, column=0, sticky="ew", padx=8, pady=(4, 4))
        card.grid_columnconfigure(0, weight=1)

        sid = sensor["sensor_id"]
        labels = sensor.get("camera_labels") or [str(c) for c in sensor.get("camera_ids", [])]
        card_state = {
            "sensor_id": sid,
            "sensor_record": sensor,
            "img_labels": labels,
            "split_mode_var": ctk.StringVar(value="reframe"),
            "split_width_var": ctk.StringVar(value=str(optimal_width)),
            "split_width_user_overridden": False,
            "_suppress_split_width_trace": False,
            "_split_width_auto_after_id": None,
        }
        self._colmap_equirect_cards[sid] = card_state

        card_row = 0
        meta = (f"Sensor {sid} — {sensor.get('label', '')} — equirectangular — "
                f"{sensor['camera_count']} cameras")
        ctk.CTkLabel(card, text=meta, font=("", 11),
                     text_color=COLOR_TEXT_DIM).grid(
            row=card_row, column=0, sticky="w", padx=8, pady=(6, 2))
        card_row += 1

        card_row = self._build_dir_section(card, card_row, "Image directories:",
                                           card_state, "img", sid, labels)
        card_row = self._build_dir_section(card, card_row, "Mask directories:",
                                           card_state, "mask", sid, labels)

        match_label = ctk.CTkLabel(card, text="", font=("", 11), text_color=COLOR_AMBER)
        match_label.grid(row=card_row, column=0, sticky="w", padx=8, pady=(2, 0))
        card_state["match_label"] = match_label
        card_row += 1

        # Split mode + width row
        split_row = ctk.CTkFrame(card, fg_color="transparent")
        split_row.grid(row=card_row, column=0, sticky="ew", padx=8, pady=(6, 8))
        ctk.CTkLabel(split_row, text="Split mode:", font=("", 11),
                     text_color=COLOR_TEXT).grid(row=0, column=0, sticky="w", padx=(0, 4))
        ctk.CTkOptionMenu(
            split_row, values=["reframe", "cubemap"],
            variable=card_state["split_mode_var"], width=110,
            command=lambda value, s=sid: self._on_equirect_split_mode_changed(s, value),
        ).grid(row=0, column=1, sticky="w", padx=(0, 12))
        ctk.CTkLabel(split_row, text="Width:", font=("", 11),
                     text_color=COLOR_TEXT).grid(row=0, column=2, sticky="w", padx=(0, 4))
        split_width_entry = ctk.CTkEntry(
            split_row,
            textvariable=card_state["split_width_var"],
            width=60,
            font=("Consolas", 11),
            height=24,
        )
        split_width_entry.grid(row=0, column=3, sticky="w", padx=(0, 6))

        def _commit_width(_event, s=sid):
            self._normalize_equirect_width_entry(s)
            return "break"

        split_width_entry.bind("<Return>", _commit_width)
        split_width_entry.bind(
            "<FocusOut>",
            lambda _event, s=sid: self._normalize_equirect_width_entry(s),
        )
        ctk.CTkLabel(split_row, text="(0=auto)", font=("", 9),
                     text_color="#666666").grid(row=0, column=4, sticky="w")
        card_state["split_width_var"].trace_add(
            "write", lambda *_args, s=sid: self._on_equirect_width_changed(s))
        card_row += 1

        return row + 1

    def _colmap_check_export_ready(self):
        """Enable Export when at least one sensor card has image directories set."""
        ready = False
        blocked_by_routing = False
        for card_state in self._colmap_fisheye_cards.values():
            if any(v.get().strip() for v in card_state.get("img_dirs", [])):
                ready = True
                if card_state.get("routing_pending") or card_state.get("routing") is None:
                    blocked_by_routing = True
        for card_state in (list(self._colmap_frame_cards.values())
                           + list(self._colmap_equirect_cards.values())):
            if any(v.get().strip() for v in card_state.get("img_dirs", [])):
                ready = True
        btn = getattr(self, "_colmap_export_btn", None)
        if btn:
            btn.configure(state="normal" if ready and not blocked_by_routing else "disabled")

    def _on_colmap_export(self):
        """Build a v2 SceneManifest and launch the exporter subprocess."""
        from pathlib import Path
        try:
            from gui.scene_manifest import (
                SceneManifest, FisheyeSensor, FrameSensor, EquirectSensor, ExportOptions,
            )
        except ImportError:
            from scene_manifest import (
                SceneManifest, FisheyeSensor, FrameSensor, EquirectSensor, ExportOptions,
            )
        import tempfile

        def _paths(card_state, kind):
            return [Path(v.get().strip()) for v in card_state.get(f"{kind}_dirs", [])
                    if v.get().strip()]

        def _auto_width_int(var):
            s = (var.get() or "").strip()
            if s in ("", "0"):
                return 0
            return int(s) if s.isdigit() else 0

        cameras_xml = Path(self._colmap_cameras_xml.get().strip())
        sparse_ply_str = self._colmap_sparse_ply.get().strip()
        sparse_ply = Path(sparse_ply_str) if sparse_ply_str else Path(".")
        output_dir = (Path(self._colmap_output_dir.get().strip())
                      if self._colmap_output_dir.get().strip() else Path("."))

        fisheye_sensors = []
        for sid, card in self._colmap_fisheye_cards.items():
            img_paths = _paths(card, "img")
            if not img_paths:
                continue
            sensor = FisheyeSensor(
                sensor_id=sid,
                image_dirs=img_paths,
                mask_dirs=_paths(card, "mask"),
                multi_pinhole=card["multi_pinhole_var"].get(),
                output_width=_auto_width_int(card["width_var"]),
                output_format=card["output_format_var"].get(),
                routing=card.get("routing"),
            )
            if card["lens_only_enabled_var"].get():
                lens_only = card["lens_only_path_var"].get().strip()
                if lens_only:
                    sensor.lens_only_mask = Path(lens_only)
            fisheye_sensors.append(sensor)

        frame_sensors = []
        for sid, card in self._colmap_frame_cards.items():
            img_paths = _paths(card, "img")
            if not img_paths:
                continue
            frame_sensors.append(FrameSensor(
                sensor_id=sid,
                image_dirs=img_paths,
                mask_dirs=_paths(card, "mask"),
            ))

        equirect_sensors = []
        for sid, card in self._colmap_equirect_cards.items():
            img_paths = _paths(card, "img")
            if not img_paths:
                continue
            equirect_sensors.append(EquirectSensor(
                sensor_id=sid,
                image_dirs=img_paths,
                mask_dirs=_paths(card, "mask"),
                split_width=_auto_width_int(card["split_width_var"]),
                split_mode=card["split_mode_var"].get(),
            ))

        manifest = SceneManifest(
            cameras_xml=cameras_xml,
            sparse_ply=sparse_ply,
            output_dir=output_dir,
            fisheye_sensors=fisheye_sensors,
            frame_sensors=frame_sensors,
            equirect_sensors=equirect_sensors,
            options=ExportOptions(
                pose_convention=self._colmap_pose_convention_var.get(),
                force_assets=self._colmap_force_assets_var.get(),
                normalize_scene=self._colmap_normalize_var.get(),
                keep_processing_files=self._colmap_keep_processing_var.get(),
                projected_tracks=self._colmap_projected_tracks_var.get(),
            ),
        )

        manifest_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix="scene_manifest_")
        manifest.save(Path(manifest_file.name))
        manifest_file.close()

        cmd = [
            sys.executable,
            str(_EXPORTER),
            f"--scene-manifest={manifest_file.name}",
            "--progress",
        ]
        if self._colmap_normalize_var.get():
            cmd.append("--normalize-scene")

        self._save_current_prefs()
        self._clear_console()
        self._progress.set(0)
        self._is_running = True
        self._colmap_export_btn.configure(state="disabled")
        self._colmap_cancel_btn.configure(state="normal")
        self._progress_v2_sensor_count = sum(
            1 for s in fisheye_sensors if s.multi_pinhole and s.image_dirs
        )
        self._run_queue = [("COLMAP Export", cmd)]
        self._run_next()

    def _build_left(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        row = 0

        ctk.CTkLabel(parent, text="Lens Configuration", font=FONT_HEADING).grid(
            row=row, column=0, sticky="w", padx=12, pady=(8, 4),
        )
        row += 1

        tab_header = ctk.CTkFrame(parent, fg_color="transparent")
        tab_header.grid(row=row, column=0, sticky="ew", padx=8, pady=(8, 0))
        tab_header.grid_columnconfigure(1, weight=1)

        self._lens_seg = ctk.CTkSegmentedButton(
            tab_header, values=["Lens A", "Lens B"],
            font=("", 13, "bold"), command=self._on_tab_selected,
            dynamic_resizing=False, width=260,
        )
        self._lens_seg.set("Lens A")
        self._lens_seg.grid(row=0, column=0, sticky="w")

        self._dual_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab_header, text="Dual-lens Mode", variable=self._dual_var,
            font=FONT_LABEL, command=self._on_dual_toggled,
        ).grid(row=0, column=1, sticky="e", padx=(12, 0))
        row += 1

        self._lens_container = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=6)
        self._lens_container.grid(row=row, column=0, sticky="ew", padx=8, pady=(4, 0))
        self._lens_container.grid_columnconfigure(0, weight=1)
        row += 1

        self._lens_a = LensPanel(self._lens_container, "Lens A",
                                 on_change=self._on_lens_panel_changed)
        self._lens_b = LensPanel(self._lens_container, "Lens B",
                                 on_change=self._on_lens_panel_changed)

        self._active_lens_tab = "Lens A"
        self._lens_a.frame.grid(row=0, column=0, sticky="nsew")
        self._lens_b.frame.grid(row=0, column=0, sticky="nsew")
        self._lens_b.frame.grid_remove()
        self._set_lens_b_enabled(False)

        # ── Shared settings ──────────────────────────────────────────
        sep = ctk.CTkFrame(parent, fg_color=COLOR_TEXT_DIM, height=1)
        sep.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 8))
        row += 1

        ctk.CTkLabel(parent, text="Shared Settings", font=FONT_HEADING).grid(
            row=row, column=0, sticky="w", padx=12, pady=(0, 4),
        )
        row += 1

        # Shared FOV removed — FOV is per-lens only. Hidden StringVar
        # so get_effective_fov's fallback path still works.
        self._fov = ctk.StringVar(value="")
        row += 0  # purpose toggle is now in the top bar (_build_ui)

        # Output directory (meaning changes with the selected output purpose).
        row = self._add_dir_picker(parent, row, "Cubeface output folder", "_output_dir")

        # Face width + output format on one row
        fw_fmt_frame = ctk.CTkFrame(parent, fg_color="transparent")
        fw_fmt_frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(18, 4))

        ctk.CTkLabel(fw_fmt_frame, text="Face width", font=FONT_LABEL).pack(
            side="left")
        self._facewidth = ctk.StringVar(value="2100")
        ctk.CTkEntry(
            fw_fmt_frame, textvariable=self._facewidth, width=60,
            font=("Consolas", 13), justify="left",
        ).pack(side="left", padx=(4, 16))

        ctk.CTkLabel(fw_fmt_frame, text="Format:", font=FONT_LABEL).pack(side="left")
        self._format_var = ctk.StringVar(value="png")
        for fmt in ("png", "tiff", "jpg"):
            ctk.CTkRadioButton(
                fw_fmt_frame, text=fmt, variable=self._format_var, value=fmt,
                font=FONT_LABEL, width=50,
            ).pack(side="left", padx=(4, 0))
        row += 1

        self._cache_var = ctk.BooleanVar(value=True)

        checkbox_frame = ctk.CTkFrame(parent, fg_color="transparent")
        checkbox_frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(20, 0))
        self._shared_checkbox_frame = checkbox_frame
        row += 1

        self._force_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            checkbox_frame, text="Force reprocess", variable=self._force_var,
            font=FONT_LABEL,
        ).pack(side="left", padx=(12, 0))

        self._skip_cubefaces_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            checkbox_frame, text="Skip cubeface generation", variable=self._skip_cubefaces_var,
            font=FONT_LABEL,
        ).pack(side="left", padx=(12, 0))

        structure_frame = ctk.CTkFrame(parent, fg_color="transparent")
        structure_frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(8, 10))
        self._structure_frame = structure_frame

        self._structure_var = ctk.StringVar(value="station")
        ctk.CTkRadioButton(
            structure_frame, text="Station", variable=self._structure_var,
            value="station", font=FONT_LABEL, width=80,
        ).pack(side="left", padx=(12, 0))

        ctk.CTkRadioButton(
            structure_frame, text="Rig", variable=self._structure_var,
            value="rig", font=FONT_LABEL, width=55,
        ).pack(side="left")
        row += 1

        sep = ctk.CTkFrame(parent, fg_color=COLOR_TEXT_DIM, height=1)
        sep.grid(row=row, column=0, sticky="ew", padx=12, pady=(8, 8))
        row += 1

        row = self._build_colmap_export_section(parent, row)

        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(8, 12))
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        self._run_btn = ctk.CTkButton(
            btn_frame, text="Run", font=("", 14, "bold"),
            fg_color=COLOR_GREEN, hover_color=COLOR_GREEN_H,
            command=self._on_run, height=38,
        )
        self._run_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self._cancel_btn = ctk.CTkButton(
            btn_frame, text="Cancel", font=("", 14, "bold"),
            fg_color=COLOR_RED, hover_color="#7a0000",
            command=self._on_cancel, height=38, state="disabled",
        )
        self._cancel_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def _build_right(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)
        parent.grid_propagate(False)

        self._phase_label = ctk.CTkLabel(
            parent, text="Ready", font=FONT_HEADING, text_color=COLOR_TEXT_DIM,
            anchor="w",
        )
        self._phase_label.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))

        self._progress = ctk.CTkProgressBar(parent, height=12)
        self._progress.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        self._progress.set(0)

        self._console = ctk.CTkTextbox(
            parent, font=FONT_CONSOLE, fg_color=COLOR_CONSOLE,
            text_color=COLOR_TEXT, state="disabled", wrap="word",
        )
        self._console.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 4))

        self._preview_frame = ctk.CTkFrame(parent, fg_color="transparent", height=200)
        self._preview_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=(4, 8))
        self._preview_frame.grid_propagate(False)

        selector_row = ctk.CTkFrame(self._preview_frame, fg_color="transparent")
        selector_row.pack(anchor="w", padx=4, pady=(0, 4), fill="x")
        ctk.CTkLabel(
            selector_row, text="Preview view:", font=FONT_LABEL,
            text_color=COLOR_TEXT_DIM,
        ).pack(side="left", padx=(0, 8))
        self._preview_view_var = ctk.StringVar(value="Cube faces")
        self._preview_view_dropdown = ctk.CTkOptionMenu(
            selector_row,
            values=["Cube faces", "Useful pixel mask", "Mask coverage",
                    "Fallback mask", "Run summary", "COLMAP scene"],
            variable=self._preview_view_var, command=lambda *_: self._show_preview(),
            width=180,
        )
        self._preview_view_dropdown.pack(side="left")
        self._preview_lens_var = ctk.StringVar(value="Lens A")
        self._preview_lens_dropdown = ctk.CTkOptionMenu(
            selector_row, values=["Lens A", "Lens B"],
            variable=self._preview_lens_var,
            command=lambda *_: self._show_preview(), width=100,
        )
        self._preview_lens_dropdown.pack(side="left", padx=(8, 0))
        self._preview_lens_dropdown.pack_forget()

        self._preview_label = ctk.CTkLabel(
            self._preview_frame, text="", font=FONT_STATUS, text_color=COLOR_TEXT_DIM,
        )
        self._preview_label.pack(anchor="w", padx=4)

        self._preview_content = ctk.CTkFrame(self._preview_frame, fg_color="transparent")
        self._preview_content.pack(fill="x", padx=4, pady=4)
        self._thumb_images = []

    def _build_colmap_export_section(self, parent, row):
        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        body.grid_columnconfigure(0, weight=1)
        self._colmap_section_body = body
        body_row = 0

        body_row = self._add_file_picker(
            body, body_row, "Metashape cameras.xml", "_metashape_xml",
            filetypes=[("XML files", "*.xml"), ("All", "*.*")],
            on_change=self._on_mapping_input_change,
        )
        body_row = self._add_file_picker(
            body, body_row, "Metashape sparse cloud .ply", "_metashape_ply",
            filetypes=[("PLY files", "*.ply"), ("All", "*.*")],
        )
        body_row = self._add_dir_picker(body, body_row, "COLMAP output folder", "_colmap_scene_dir")

        ctk.CTkLabel(
            body,
            text="Advanced: manual lens-to-camera map (optional)",
            font=FONT_LABEL,
            text_color=COLOR_TEXT_DIM,
        ).grid(row=body_row, column=0, sticky="w", padx=12, pady=(6, 0))
        body_row += 1

        self._manual_lens_map_frame = ctk.CTkFrame(body, fg_color="transparent")
        self._manual_lens_map_frame.grid(row=body_row, column=0, sticky="ew", padx=12, pady=(0, 4))
        self._manual_lens_map_frame.grid_columnconfigure(0, weight=1)
        self._last_proposed_spec = None
        self._last_proposed_signature = None
        self._generated_map_value = None
        self._generated_map_signature = None
        self._generated_map_stale = False
        self._setting_generated_map = False
        self._lens_camera_map = ctk.StringVar(value="")
        self._lens_camera_map.trace_add("write", lambda *_: self._on_manual_map_changed())
        ctk.CTkEntry(
            self._manual_lens_map_frame,
            textvariable=self._lens_camera_map,
            font=FONT_LABEL,
            placeholder_text="Osmo360_back=37-73,Osmo360_front=74-110",
        ).grid(
            row=0, column=0, sticky="ew",
        )
        body_row += 1

        mapping_check = ctk.CTkFrame(body, fg_color="transparent")
        mapping_check.grid(row=body_row, column=0, sticky="ew", padx=12, pady=(0, 4))
        mapping_check.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(
            mapping_check,
            text="Check Mapping",
            width=130,
            command=self._check_lens_mapping,
        ).grid(row=0, column=0, sticky="w")
        self._use_proposed_btn = ctk.CTkButton(
            mapping_check,
            text="Use Proposed Map",
            width=140,
            command=self._use_proposed_map,
            state="disabled",
            fg_color=COLOR_DISABLED,
        )
        self._use_proposed_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))
        body_row += 1

        options = ctk.CTkFrame(body, fg_color="transparent")
        options.grid(row=body_row, column=0, sticky="ew", padx=12, pady=(6, 8))
        options.grid_columnconfigure(1, weight=1)
        self._pose_convention_var = ctk.StringVar(value="metashape_camera_to_world")
        ctk.CTkLabel(options, text="Pose", font=FONT_LABEL).grid(
            row=0, column=0, sticky="w", padx=(0, 6),
        )
        ctk.CTkOptionMenu(
            options,
            values=["metashape_camera_to_world", "metashape_world_to_camera"],
            variable=self._pose_convention_var,
            width=220,
        ).grid(row=0, column=1, sticky="w")
        self._require_masks_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            options, text="Require masks", variable=self._require_masks_var,
            font=FONT_LABEL,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self._projected_tracks_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            options, text="Projected tracks", variable=self._projected_tracks_var,
            font=FONT_LABEL,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._force_scene_assets_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            options, text="Force assets", variable=self._force_scene_assets_var,
            font=FONT_LABEL,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._normalize_scene_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            options, text="Normalize scene scale", variable=self._normalize_scene_var,
            font=FONT_LABEL,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._keep_processing_files_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            options, text="Keep processing files after successful export",
            variable=self._keep_processing_files_var,
            font=FONT_LABEL,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        body_row += 1

        media_header = ctk.CTkFrame(body, fg_color="transparent")
        media_header.grid(row=body_row, column=0, sticky="ew", padx=12, pady=(6, 4))
        media_header.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            media_header, text="Add Frame Camera Media Set",
            command=self._add_media_set_row,
        ).grid(row=0, column=0, sticky="ew")
        body_row += 1

        self._media_sets_frame = ctk.CTkFrame(body, fg_color="transparent")
        self._media_sets_frame.grid(row=body_row, column=0, sticky="ew", padx=12, pady=(0, 4))
        self._media_sets_frame.grid_columnconfigure(0, weight=1)
        body_row += 1

        self._output_dir.trace_add("write", lambda *_: self._maybe_default_colmap_scene_dir())
        body.grid_remove()
        row += 1
        return row

    def _is_colmap_purpose(self):
        return getattr(self, "_purpose_var", None) is not None and self._purpose_var.get() == PURPOSE_COLMAP

    def _on_purpose_changed(self):
        self._refresh_purpose_ui()
        self._on_mapping_input_change()

    def _refresh_purpose_ui(self, scroll=True):
        colmap_mode = self._is_colmap_purpose()

        # Show/hide left panel content frames
        metashape_frame = getattr(self, "_metashape_left_frame", None)
        colmap_frame = getattr(self, "_colmap_left_frame", None)
        if metashape_frame is not None and colmap_frame is not None:
            if colmap_mode:
                metashape_frame.pack_forget()
                colmap_frame.pack(fill="both", expand=True)
            else:
                colmap_frame.pack_forget()
                metashape_frame.pack(fill="both", expand=True)

        output_label = getattr(self, "_output_dir_label", None)
        if output_label is not None:
            output_label.configure(
                text="COLMAP output folder" if colmap_mode else "Cubeface output folder"
            )

        structure_frame = getattr(self, "_structure_frame", None)
        if structure_frame is not None:
            if colmap_mode:
                structure_frame.grid_remove()
            else:
                structure_frame.grid()

        body = getattr(self, "_colmap_section_body", None)
        if body is not None:
            if colmap_mode:
                body.grid()
            else:
                body.grid_remove()

        # The shared output folder is the COLMAP export root in COLMAP mode,
        # so the old per-section folder picker stays internal and hidden.
        colmap_dir_label = getattr(self, "_colmap_scene_dir_label", None)
        colmap_dir_frame = getattr(self, "_colmap_scene_dir_frame", None)
        for widget in (colmap_dir_label, colmap_dir_frame):
            if widget is not None:
                widget.grid_remove()

    # ── Shared widget builders ───────────────────────────────────────

    def _add_file_picker(self, parent, row, label, attr, filetypes=None, on_change=None):
        label_widget = ctk.CTkLabel(parent, text=label, font=FONT_LABEL)
        label_widget.grid(
            row=row, column=0, sticky="w", padx=12, pady=(6, 0),
        )
        row += 1
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 4))
        frame.grid_columnconfigure(0, weight=1)

        var = ctk.StringVar()
        ctk.CTkEntry(frame, textvariable=var, font=FONT_LABEL).grid(
            row=0, column=0, sticky="ew", padx=(0, 4),
        )

        def browse():
            p = filedialog.askopenfilename(filetypes=filetypes or [("All", "*.*")])
            if p:
                var.set(p)
                if on_change:
                    on_change()

        ctk.CTkButton(frame, text="...", width=36, command=browse).grid(row=0, column=1)
        setattr(self, attr, var)
        setattr(self, f"{attr}_label", label_widget)
        setattr(self, f"{attr}_frame", frame)
        if on_change:
            var.trace_add("write", lambda *_: on_change())
        row += 1
        return row

    def _add_dir_picker(self, parent, row, label, attr, on_change=None):
        label_widget = ctk.CTkLabel(parent, text=label, font=FONT_LABEL)
        label_widget.grid(
            row=row, column=0, sticky="w", padx=12, pady=(6, 0),
        )
        row += 1
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 4))
        frame.grid_columnconfigure(0, weight=1)

        var = ctk.StringVar()
        ctk.CTkEntry(frame, textvariable=var, font=FONT_LABEL).grid(
            row=0, column=0, sticky="ew", padx=(0, 4),
        )

        def browse():
            p = filedialog.askdirectory()
            if p:
                var.set(p)
                if on_change:
                    on_change()

        ctk.CTkButton(frame, text="...", width=36, command=browse).grid(row=0, column=1)
        setattr(self, attr, var)
        setattr(self, f"{attr}_label", label_widget)
        setattr(self, f"{attr}_frame", frame)
        if on_change:
            var.trace_add("write", lambda *_: on_change())
        row += 1
        return row

    def _maybe_default_colmap_scene_dir(self):
        scene_var = getattr(self, "_colmap_scene_dir", None)
        if scene_var is None:
            return
        output_dir = self._output_dir.get().strip()
        if not output_dir:
            return
        if self._is_colmap_purpose():
            scene_var.set(_default_colmap_scene_dir(output_dir))
            return
        if scene_var.get().strip():
            return
        scene_var.set(_default_colmap_scene_dir(output_dir))

    def _add_media_set_row(self, data=None):
        index = len(self._media_set_rows)
        row = MediaSetRow(
            self._media_sets_frame,
            index,
            on_change=None,
            on_remove=self._remove_media_set_row,
            data=data,
        )
        self._media_set_rows.append(row)
        self._layout_media_set_rows()
        return row

    def _remove_media_set_row(self, row):
        if row not in self._media_set_rows:
            return
        row.frame.destroy()
        self._media_set_rows.remove(row)
        self._layout_media_set_rows()

    def _layout_media_set_rows(self):
        for index, row in enumerate(self._media_set_rows):
            row.index = index
            row.frame.grid(row=index, column=0, sticky="ew", pady=(0, 6))

    def _active_media_sets(self):
        return [
            row.manifest_entry()
            for row in self._media_set_rows
            if not row.is_blank()
        ]

    def _colmap_export_requested(self):
        return (
            self._is_colmap_purpose()
            and bool(self._metashape_xml.get().strip())
            and bool(self._metashape_ply.get().strip())
        )

    def _colmap_export_touched(self):
        if not self._is_colmap_purpose():
            return False
        return True

    def _active_lens_jobs(self):
        jobs = [("Lens A", self._lens_a)]
        if self._dual_var.get():
            jobs.append(("Lens B", self._lens_b))
        return jobs

    def _active_lens_labels(self):
        labels = []
        for ui_label, panel in self._active_lens_jobs():
            lens_label = panel.lens_label.get().strip()
            if not lens_label:
                raise ValueError(f"{ui_label}: lens label is empty")
            labels.append(lens_label)
        return labels

    def _build_lens_jobs(self):
        """Build LensJob list from active lens panels."""
        jobs = []
        for ui_label, panel in self._active_lens_jobs():
            lens_label = panel.lens_label.get().strip()
            img_dir = panel.images_dir.get().strip()
            stems = _image_stems(img_dir) if img_dir else set()
            cal_path = panel.cal_path.get().strip() or None
            if not lens_label:
                raise ValueError(f"{ui_label}: lens label is empty")
            if not stems:
                raise ValueError(f"{ui_label}: no source fisheye images were found")
            jobs.append(mapping_resolver.LensJob(
                ui_label=ui_label,
                lens_label=lens_label,
                stems=frozenset(stems),
                cal_path=cal_path,
            ))
        return jobs

    def _resolve_auto_mapping(self):
        """Run the mapping resolver. Returns MappingResult."""
        xml_path = self._metashape_xml.get().strip()
        if not xml_path or not Path(xml_path).is_file():
            raise ValueError("Metashape cameras.xml is required for automatic fisheye pose mapping")
        xml_data = mapping_resolver.parse_xml_runs(Path(xml_path))
        jobs = self._build_lens_jobs()
        return mapping_resolver.resolve_mapping(xml_data, jobs)

    def _compute_input_signature(self):
        """Hash of all inputs that affect mapping."""
        import hashlib
        parts = [
            self._metashape_xml.get().strip(),
            str(self._dual_var.get()),
        ]
        for _ui_label, panel in self._active_lens_jobs():
            parts.append(panel.lens_label.get().strip())
            parts.append(panel.images_dir.get().strip())
            parts.append(panel.cal_path.get().strip())
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def _clear_generated_map_tracking(self):
        self._generated_map_value = None
        self._generated_map_signature = None
        self._generated_map_stale = False

    def _refresh_generated_map_staleness(self):
        generated_value = getattr(self, "_generated_map_value", None)
        generated_signature = getattr(self, "_generated_map_signature", None)
        if not generated_value or not generated_signature:
            self._generated_map_stale = False
            return False
        if self._lens_camera_map.get().strip() != generated_value:
            self._clear_generated_map_tracking()
            return False
        self._generated_map_stale = self._compute_input_signature() != generated_signature
        return self._generated_map_stale

    def _generated_map_stale_message(self):
        if not self._refresh_generated_map_staleness():
            return None
        return (
            "Manual lens-camera map was copied from an older generated proposal. "
            "Re-run Check Mapping and accept the current proposal, or edit the map manually."
        )

    def _raise_if_generated_map_stale(self):
        message = self._generated_map_stale_message()
        if message:
            raise ValueError(message)

    def _on_manual_map_changed(self):
        if getattr(self, "_setting_generated_map", False):
            return
        if getattr(self, "_generated_map_value", None):
            manual = self._lens_camera_map.get().strip()
            if manual != self._generated_map_value:
                self._clear_generated_map_tracking()
            else:
                self._refresh_generated_map_staleness()

    def _on_mapping_input_change(self):
        self._refresh_generated_map_staleness()
        last_sig = getattr(self, "_last_proposed_signature", None)
        if last_sig and last_sig != self._compute_input_signature():
            self._last_proposed_spec = None
            self._last_proposed_signature = None
            btn = getattr(self, "_use_proposed_btn", None)
            if btn is not None:
                btn.configure(state="disabled", fg_color=COLOR_DISABLED)

    def _on_lens_panel_changed(self):
        if getattr(self, "_restoring_prefs", False):
            return
        self._update_all_modes()
        self._on_mapping_input_change()

    def _use_proposed_map(self):
        """Copy the last proposed spec into the manual map field."""
        if self._last_proposed_spec:
            signature = self._compute_input_signature()
            self._setting_generated_map = True
            try:
                self._lens_camera_map.set(self._last_proposed_spec)
            finally:
                self._setting_generated_map = False
            self._generated_map_value = self._last_proposed_spec
            self._generated_map_signature = signature
            self._generated_map_stale = False
            self._append_console(
                "\nCopied proposed mapping into manual field.\n",
                color=COLOR_GREEN,
            )

    def _auto_lens_camera_map_LEGACY(self):
        """LEGACY — retained temporarily for reference. Will be removed."""
        xml_path = self._metashape_xml.get().strip()
        if not xml_path or not Path(xml_path).is_file():
            raise ValueError("Metashape cameras.xml is required for automatic fisheye pose mapping")

        raw_runs = _metashape_equisolid_camera_runs(Path(xml_path))
        if not raw_runs:
            raise ValueError("No aligned equisolid fisheye camera runs were found in the Metashape XML")

        # Merge fragmented runs that share the same sensor_id and label prefix.
        # Gaps from unaligned cameras split what's logically one camera group
        # into many small runs — merge them back together.
        runs = _merge_fragmented_runs(raw_runs)

        lens_jobs = []
        for ui_label, panel in self._active_lens_jobs():
            lens_label = panel.lens_label.get().strip()
            stems = _image_stems(panel.images_dir.get().strip())
            if not lens_label:
                raise ValueError(f"{ui_label}: lens label is empty")
            if not stems:
                raise ValueError(f"{ui_label}: no source fisheye images were found")
            lens_jobs.append({
                "ui_label": ui_label,
                "lens_label": lens_label,
                "stems": stems,
            })

        assignments = {}
        status_parts = []
        used_run_indexes = set()
        deferred = []

        # Step 0a: group-label match — if XML cameras have group labels,
        # match them against lens labels (case-insensitive substring).
        has_groups = any(run.get("group_label") for run in runs)
        if has_groups:
            still_unmatched = []
            for job in lens_jobs:
                label_lower = job["lens_label"].casefold()
                matched_indexes = [
                    i for i, run in enumerate(runs)
                    if i not in used_run_indexes
                    and run.get("group_label")
                    and (label_lower in run["group_label"].casefold()
                         or run["group_label"].casefold() in label_lower)
                ]
                if matched_indexes:
                    ids = []
                    for i in matched_indexes:
                        ids.extend(runs[i]["ids"])
                        used_run_indexes.add(i)
                    assignments[job["lens_label"]] = tuple(sorted(ids))
                    status_parts.append(
                        f"{job['lens_label']}: matched {len(ids)} XML cameras "
                        f"from {len(matched_indexes)} run(s) by camera group label"
                    )
                else:
                    still_unmatched.append(job)
            lens_jobs_remaining = still_unmatched
        else:
            lens_jobs_remaining = list(lens_jobs)

        # Step 0b: sensor-ID partition — if runs use multiple sensor IDs
        # and there are exactly as many distinct sensors as remaining lenses,
        # partition by sensor.
        if lens_jobs_remaining:
            remaining_sensors = {}
            for i, run in enumerate(runs):
                if i in used_run_indexes:
                    continue
                remaining_sensors.setdefault(run["sensor_id"], []).append(i)
            if len(remaining_sensors) == len(lens_jobs_remaining) and len(remaining_sensors) > 1:
                for job, (_sid, run_indexes) in zip(
                    lens_jobs_remaining,
                    sorted(remaining_sensors.items()),
                ):
                    ids = []
                    for i in run_indexes:
                        ids.extend(runs[i]["ids"])
                        used_run_indexes.add(i)
                    assignments[job["lens_label"]] = tuple(sorted(ids))
                    status_parts.append(
                        f"{job['lens_label']}: matched {len(ids)} XML cameras "
                        f"from {len(run_indexes)} run(s) by sensor ID"
                    )
                lens_jobs_remaining = []

        deferred = lens_jobs_remaining

        # Step 1: exact stem match against a single run
        if deferred:
            still_deferred = []
            for job in deferred:
                candidates = [
                    (index, run)
                    for index, run in enumerate(runs)
                    if index not in used_run_indexes
                    and len(run["ids"]) == len(job["stems"])
                    and job["stems"] == run["stems"]
                ]
                if len(candidates) == 1:
                    index, run = candidates[0]
                    assignments[job["lens_label"]] = run["ids"]
                    used_run_indexes.add(index)
                    status_parts.append(
                        f"{job['lens_label']}: matched {len(run['ids'])} XML cameras by filename"
                    )
                else:
                    still_deferred.append(job)
            deferred = still_deferred

        # Step 2: subset stem match — combine all runs whose stems are
        # contained in the source stems (handles unaligned frames and
        # multiple video sequences per lens)
        if deferred:
            job_matches = []
            for job in deferred:
                matching = []
                for index, run in enumerate(runs):
                    if index in used_run_indexes:
                        continue
                    if run["stems"] <= job["stems"]:
                        matching.append(index)
                job_matches.append((job, matching))

            # Check for overlap: do multiple jobs claim the same runs?
            all_matched = set()
            overlap = False
            for _job, indexes in job_matches:
                if all_matched & set(indexes):
                    overlap = True
                    break
                all_matched.update(indexes)

            if overlap and len(deferred) > 1:
                # Partition overlapping runs by camera-ID contiguity.
                contested = set()
                for _job, indexes in job_matches:
                    contested.update(indexes)
                sorted_contested = sorted(contested, key=lambda i: runs[i]["ids"][0])
                if len(sorted_contested) >= len(deferred):
                    gaps = []
                    for pos in range(1, len(sorted_contested)):
                        prev_last = runs[sorted_contested[pos - 1]]["ids"][-1]
                        curr_first = runs[sorted_contested[pos]]["ids"][0]
                        gaps.append((curr_first - prev_last, pos))
                    gaps.sort(reverse=True)
                    split_positions = sorted(pos for _gap, pos in gaps[: len(deferred) - 1])
                    groups = []
                    prev = 0
                    for sp in split_positions:
                        groups.append(sorted_contested[prev:sp])
                        prev = sp
                    groups.append(sorted_contested[prev:])

                    if len(groups) == len(deferred):
                        for job, group in zip(deferred, groups):
                            ids = []
                            for idx in group:
                                ids.extend(runs[idx]["ids"])
                                used_run_indexes.add(idx)
                            assignments[job["lens_label"]] = tuple(sorted(ids))
                            status_parts.append(
                                f"{job['lens_label']}: matched {len(ids)} XML cameras "
                                f"from {len(group)} run(s) by stem subset + ID partition"
                            )
                        deferred = []

            if deferred:
                still_deferred = []
                for job, indexes in job_matches:
                    matching_ids = []
                    for index in indexes:
                        if index in used_run_indexes:
                            continue
                        matching_ids.extend(runs[index]["ids"])
                    if matching_ids:
                        for index in indexes:
                            used_run_indexes.add(index)
                        assignments[job["lens_label"]] = tuple(sorted(matching_ids))
                        status_parts.append(
                            f"{job['lens_label']}: matched {len(matching_ids)} XML cameras "
                            f"from {len(indexes)} run(s) by stem subset"
                        )
                    else:
                        still_deferred.append(job)
                deferred = still_deferred

        # Step 3: count-based fallback for remaining unmatched lenses
        if deferred:
            remaining_runs = [
                (index, run)
                for index, run in enumerate(runs)
                if index not in used_run_indexes
            ]
            count_candidates = [
                (job, [(index, run) for index, run in remaining_runs if len(run["ids"]) == len(job["stems"])])
                for job in deferred
            ]
            exact_count_fit = (
                len(remaining_runs) == len(deferred)
                and all(len(candidates) == len(deferred) for _job, candidates in count_candidates)
                and sorted(len(run["ids"]) for _index, run in remaining_runs)
                == sorted(len(job["stems"]) for job in deferred)
            )
            if not exact_count_fit:
                details = []
                for job, candidates in count_candidates:
                    details.append(f"{job['lens_label']} matched {len(candidates)} possible XML runs")
                raise ValueError(
                    "Automatic fisheye pose mapping is ambiguous: "
                    + "; ".join(details)
                    + ". Enable the advanced manual map and choose camera id ranges."
                )

            ordered_runs = sorted(remaining_runs, key=lambda item: item[1]["ids"][0])
            for job, (_index, run) in zip(deferred, ordered_runs):
                if len(run["ids"]) != len(job["stems"]):
                    raise ValueError(
                        f"Automatic fisheye pose mapping failed for {job['lens_label']}: "
                        f"{len(job['stems'])} source images but XML run has {len(run['ids'])} cameras"
                    )
                assignments[job["lens_label"]] = run["ids"]
                status_parts.append(
                    f"{job['lens_label']}: matched {len(run['ids'])} XML cameras by Lens A/B order"
                )

        spec = ",".join(
            f"{job['lens_label']}={_format_camera_ids(assignments[job['lens_label']])}"
            for job in lens_jobs
        )
        return spec, status_parts, runs

    def _resolve_lens_camera_map(self):
        manual = self._lens_camera_map.get().strip()
        if manual:
            xml_path = self._metashape_xml.get().strip()
            if not xml_path or not Path(xml_path).is_file():
                raise ValueError(
                    "Metashape cameras.xml is required to validate manual lens-camera map"
                )
            self._raise_if_generated_map_stale()
            validation = mapping_resolver.validate_manual_map(
                manual, Path(xml_path), self._active_lens_labels(),
            )
            if not validation.valid:
                raise ValueError(
                    "Manual lens-camera map is invalid: "
                    + "; ".join(validation.errors)
                )
            return validation.spec
        result = self._resolve_auto_mapping()
        if not result.can_auto_export:
            raise ValueError(
                "Automatic mapping is heuristic (front/back assignment unverified). "
                "Use Check Mapping, then click 'Use Proposed Map' to accept."
            )
        return result.spec

    def _colmap_support_output_dir(self):
        export_root = self._colmap_export_root_dir()
        if export_root is None:
            return None
        return export_root / "processing"

    def _colmap_reports_output_dir(self):
        export_root = self._colmap_export_root_dir()
        if export_root is None:
            return None
        return export_root / "reports"

    def _colmap_export_root_dir(self):
        if self._is_colmap_purpose():
            output_dir = self._output_dir.get().strip()
            if not output_dir:
                return None
            return _normalize_colmap_export_root(output_dir)
        scene_value = self._colmap_scene_dir.get().strip()
        if scene_value:
            return _normalize_colmap_export_root(scene_value)
        output_dir = self._output_dir.get().strip()
        if not output_dir:
            return None
        return _normalize_colmap_export_root(_default_colmap_scene_dir(output_dir))

    def _colmap_scene_output_dir(self):
        export_root = self._colmap_export_root_dir()
        if export_root is None:
            return None
        return export_root / "colmap"

    def _cubeface_work_output_dir(self):
        output_dir = self._output_dir.get().strip()
        if not output_dir:
            return None
        if self._is_colmap_purpose():
            export_root = self._colmap_export_root_dir()
            if export_root is not None:
                return export_root / "processing" / "tmp" / "cubefaces"
        return Path(output_dir)

    def _check_lens_mapping(self):
        self._clear_console()
        self._append_console("=== Check Mapping ===\n\n")
        self._last_proposed_spec = None
        self._last_proposed_signature = None
        self._use_proposed_btn.configure(state="disabled", fg_color=COLOR_DISABLED)

        manual = self._lens_camera_map.get().strip()
        xml_path = self._metashape_xml.get().strip()

        # ── Manual map path ──
        if manual:
            self._append_console(f"Manual map: {manual}\n\n")
            stale_message = self._generated_map_stale_message()
            if stale_message:
                self._append_console(f"  ERROR: {stale_message}\n", color=COLOR_RED)
            if not xml_path or not Path(xml_path).is_file():
                self._append_console("Cannot validate: no Metashape XML.\n", color=COLOR_RED)
                self._append_console(">> Manual mapping not validated\n", color=COLOR_AMBER)
                return
            try:
                lens_labels = self._active_lens_labels()
                validation = mapping_resolver.validate_manual_map(
                    manual, Path(xml_path), lens_labels,
                )
            except Exception as exc:
                self._append_console(f"  ERROR: {exc}\n", color=COLOR_RED)
                self._append_console("\n>> Manual mapping has errors\n", color=COLOR_RED)
                return
            if validation.errors:
                for err in validation.errors:
                    self._append_console(f"  ERROR: {err}\n", color=COLOR_RED)
            if validation.warnings:
                for warn in validation.warnings:
                    self._append_console(f"  WARNING: {warn}\n", color=COLOR_AMBER)
            if stale_message:
                self._append_console("\n>> Manual mapping has errors\n", color=COLOR_RED)
            elif validation.valid and not validation.warnings:
                self._append_console("\n>> Manual mapping accepted\n", color=COLOR_GREEN)
            elif validation.valid:
                self._append_console("\n>> Manual mapping accepted — review warnings\n", color=COLOR_AMBER)
            else:
                self._append_console("\n>> Manual mapping has errors\n", color=COLOR_RED)
            return

        # ── Automatic mapping path ──
        try:
            xml_data = mapping_resolver.parse_xml_runs(Path(xml_path))
        except Exception as exc:
            self._append_console(f"Failed to parse XML: {exc}\n", color=COLOR_RED)
            self._append_console(">> Mapping failed\n", color=COLOR_RED)
            return

        # Show run summary.
        raw = xml_data.raw_runs
        merged = xml_data.merged_runs
        self._append_console(f"Metashape XML: {xml_path}\n")
        self._append_console(
            f"Raw camera runs: {len(raw)}  |  "
            f"After merging fragments: {len(merged)}\n"
        )
        for i, run in enumerate(merged):
            frag = f" ({run.raw_run_count} fragments)" if run.raw_run_count > 1 else ""
            group = f"  group={run.group_label}" if run.group_label else ""
            slabel = f"  sensor_label={run.sensor_label!r}" if run.sensor_label and run.sensor_label != "unknown" else ""
            self._append_console(
                f"  run {i}: sensor={run.sensor_id}{slabel}, "
                f"cameras={len(run.ids)} "
                f"(ids {run.ids[0]}-{run.ids[-1]}){frag}{group}\n"
                f"         labels: {run.start_label} ... {run.end_label}\n"
            )
        if merged:
            total = sum(len(r.ids) for r in merged)
            all_ids = [cid for r in merged for cid in r.ids]
            self._append_console(
                f"\nTotal aligned equisolid cameras: {total} "
                f"(ids {min(all_ids)}-{max(all_ids)})\n"
            )
        self._append_console("\n")

        # Show lens source info.
        for ui_label, panel in self._active_lens_jobs():
            lens_label = panel.lens_label.get().strip()
            img_dir = panel.images_dir.get().strip()
            stems = _image_stems(img_dir) if img_dir else set()
            self._append_console(f"{ui_label} ({lens_label}):\n")
            self._append_console(f"  images dir: {img_dir}\n")
            self._append_console(f"  source stems: {len(stems)}\n")
            if stems:
                self._append_console(f"  sample stems: {sorted(stems)[:3]}\n")
            self._append_console("\n")

        # Resolve.
        try:
            result = self._resolve_auto_mapping()
        except Exception as exc:
            self._append_console(f"FAILED: {exc}\n\n", color=COLOR_RED)
            self._append_console(">> Mapping failed — check settings above\n", color=COLOR_RED)
            return

        # Show matching strategy per lens.
        self._append_console("Matching strategy:\n")
        for a in result.assignments:
            conf_tag = f"[{a.confidence}]"
            self._append_console(f"  {a.lens_label}: {a.message} {conf_tag}\n")
            if a.detail:
                self._append_console(f"    {a.detail}\n")
        self._append_console("\n")

        # Show result spec.
        items = [s.strip() for s in result.spec.split(",") if s.strip()]
        detail = "\n".join(f"  {item}" for item in items)
        self._append_console(f"Result (automatic):\n{detail}\n\n")

        # Per-lens stem validation using per-camera-ID lookup.
        id_to_stem = result.xml_data.camera_id_to_stem
        for a in result.assignments:
            job = next((j for j in result.xml_data.merged_runs), None)
            # Find the matching lens job for source stems.
            panel_stems = set()
            for _ui_label, panel in self._active_lens_jobs():
                if panel.lens_label.get().strip() == a.lens_label:
                    panel_stems = _image_stems(panel.images_dir.get().strip())
                    break
            assigned_stems = {id_to_stem[cid] for cid in a.camera_ids if cid in id_to_stem}
            matched = panel_stems & assigned_stems
            pct = len(matched) / max(len(panel_stems), 1) * 100
            unmatched = len(panel_stems) - len(matched)
            line = (
                f"  {a.lens_label}: {len(panel_stems)} source, "
                f"{len(a.camera_ids)} aligned, "
                f"{len(matched)} matched ({pct:.0f}%)"
            )
            if unmatched > 0:
                line += f", {unmatched} unmatched"
            self._append_console(line + "\n")

        # Show diagnostics.
        if result.diagnostics:
            self._append_console("\nDiagnostics:\n")
            for d in result.diagnostics:
                color = {
                    mapping_resolver.INFO: COLOR_TEXT_DIM,
                    mapping_resolver.WARNING: COLOR_AMBER,
                    mapping_resolver.BLOCKER: COLOR_RED,
                }.get(d.severity, COLOR_TEXT)
                self._append_console(f"  [{d.severity}] {d.message}\n", color=color)

        # Final status.
        has_blockers = any(d.severity == mapping_resolver.BLOCKER for d in result.diagnostics)
        has_warnings = any(d.severity == mapping_resolver.WARNING for d in result.diagnostics)

        self._append_console("\n")
        if result.can_auto_export and not has_warnings:
            self._append_console(">> Mapping OK — ready to export\n", color=COLOR_GREEN)
        elif result.can_auto_export and has_warnings:
            self._append_console(
                ">> Mapping OK with warnings — review before export\n",
                color=COLOR_AMBER,
            )
        else:
            self._last_proposed_spec = result.spec
            self._last_proposed_signature = self._compute_input_signature()
            self._use_proposed_btn.configure(state="normal", fg_color=COLOR_BLUE)
            self._append_console(
                ">> Mapping proposed — accept before export\n",
                color=COLOR_AMBER,
            )
            self._append_console(
                "   Click [Use Proposed Map] to copy into the manual field.\n",
            )

    # ── Dual-lens toggle ─────────────────────────────────────────────

    def _on_tab_selected(self, value):
        if value == "Lens B" and not self._dual_var.get():
            self._lens_seg.set("Lens A")
            return
        if value == "Lens A":
            self._lens_b.frame.grid_remove()
            self._lens_a.frame.grid()
        else:
            self._lens_a.frame.grid_remove()
            self._lens_b.frame.grid()
        self._active_lens_tab = value

    def _on_dual_toggled(self):
        self._set_lens_b_enabled(self._dual_var.get())
        if getattr(self, "_restoring_prefs", False):
            return
        self._update_all_modes()
        self._on_mapping_input_change()
        if self._dual_var.get():
            self._preview_lens_dropdown.pack(side="left", padx=(8, 0))
        else:
            self._preview_lens_dropdown.pack_forget()
            self._preview_lens_var.set("Lens A")

    def _set_lens_b_enabled(self, enabled):
        if enabled:
            self._lens_seg.configure(state="normal")
        else:
            self._lens_seg.set("Lens A")
            self._on_tab_selected("Lens A")

    # ── Mode resolution and hint refresh ─────────────────────────────

    def _update_all_modes(self):
        fov = self._fov.get()
        self._lens_a.update_ui(fov)
        if self._dual_var.get():
            self._lens_b.update_ui(fov)

    # ── Command building ─────────────────────────────────────────────

    def _build_cmd_for_lens(self, lens_panel):
        """Build the CLI command, passing every non-empty support flag."""
        effective_fov = lens_panel.get_effective_fov(self._fov.get())
        output_dir = self._cubeface_work_output_dir() or Path(self._output_dir.get())
        use_corrections = lens_panel._corrections_var.get()
        script = _SCRIPT_CORRECTED if use_corrections else _SCRIPT
        cmd = [
            sys.executable, str(script),
            "--amlenscal", lens_panel.cal_path.get(),
            "--lenslabel", lens_panel.lens_label.get(),
            "--directoryfisheyeimages", lens_panel.images_dir.get(),
            "--facewidth", str(self._facewidth.get()),
            "--outputdir", str(output_dir),
            "--outputformat", self._format_var.get(),
        ]
        # Pass every non-empty support flag — CLI resolves priority
        masks = lens_panel.masks_dir.get().strip()
        if masks:
            cmd += ["--directoryfisheyemasks", masks]
        lensmask = lens_panel.lensonlymask_path.get().strip()
        if lensmask:
            cmd += ["--lensonlymask", lensmask]
        if _is_valid_positive_int(effective_fov):
            cmd += ["--maxusefulfov", effective_fov.strip()]
        if self._is_colmap_purpose() or self._structure_var.get() == "rig":
            cmd.append("--rigstructure")
        if self._force_var.get():
            cmd.append("--force")
        return cmd

    def _write_passthrough_manifest(self):
        media_sets = self._active_media_sets()
        if not media_sets:
            return None
        manifest_dir = self._colmap_support_output_dir()
        if manifest_dir is None:
            return None
        manifest_dir = manifest_dir / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "passthrough_media_manifest.json"
        manifest_path.write_text(
            json.dumps({"media_sets": media_sets}, indent=2),
            encoding="utf-8",
        )
        return manifest_path

    def _build_cmd_for_colmap_export(self):
        manifest_path = self._write_passthrough_manifest()
        lens_camera_map = self._resolve_lens_camera_map()
        cubeface_root = self._cubeface_work_output_dir() or Path(self._output_dir.get())
        support_dir = self._colmap_support_output_dir()
        reports_dir = self._colmap_reports_output_dir()
        output_scene = self._colmap_scene_output_dir()
        cmd = [
            sys.executable, str(_EXPORTER),
            "--metashape-cameras", self._metashape_xml.get(),
            "--metashape-points", self._metashape_ply.get(),
            "--cubeface-root", str(cubeface_root),
            "--lens-camera-map", lens_camera_map,
            "--pose-convention", self._pose_convention_var.get(),
            "--output-scene", str(output_scene),
            "--support-output-dir", str(support_dir),
            "--reports-output-dir", str(reports_dir),
            "--undistort-passthrough", "auto",
            "--passthrough-output-format", "jpg",
            "--strict-pinhole",
            "--package-assets",
            "--progress",
        ]
        if manifest_path is not None:
            cmd += ["--passthrough-media-manifest", str(manifest_path)]
        if self._require_masks_var.get():
            cmd.append("--require-masks")
        if self._projected_tracks_var.get():
            cmd.append("--projected-tracks")
        if self._normalize_scene_var.get():
            cmd.append("--normalize-scene")
        if self._force_scene_assets_var.get():
            cmd.append("--force-assets")
        if not self._keep_processing_files_var.get():
            cmd.append("--clean-processing-files")
        return cmd

    def _validate(self):
        errors = []
        active_panels = [("Lens A", self._lens_a)]
        if self._dual_var.get():
            active_panels.append(("Lens B", self._lens_b))

        for label, panel in active_panels:
            errors.extend(panel.validate(shared_fov=self._fov.get()))

        if not self._output_dir.get().strip():
            errors.append("Output directory is empty")
        if self._colmap_export_touched():
            if not _EXPORTER.is_file():
                errors.append(f"COLMAP exporter not found: {_EXPORTER}")
            if not self._metashape_xml.get().strip() or not Path(self._metashape_xml.get()).is_file():
                errors.append("Metashape cameras.xml not found")
            if not self._metashape_ply.get().strip() or not Path(self._metashape_ply.get()).is_file():
                errors.append("Metashape sparse cloud .ply not found")
            if not self._colmap_scene_dir.get().strip():
                errors.append("COLMAP output folder is empty")
            if self._colmap_export_requested():
                try:
                    self._resolve_lens_camera_map()
                except Exception as exc:
                    errors.append(str(exc))
            for row in self._media_set_rows:
                errors.extend(row.validate(require_masks=self._require_masks_var.get()))
            try:
                export_root = self._colmap_export_root_dir()
                scene_output = self._colmap_scene_output_dir()
                export_root_resolved = export_root.resolve() if export_root is not None else None
                scene_dir = scene_output.resolve() if scene_output is not None else None
                work_dir = self._cubeface_work_output_dir()
                work_dir_resolved = work_dir.resolve() if work_dir is not None else None
                if scene_dir is None or export_root_resolved is None:
                    errors.append("COLMAP output folder is empty")
                elif export_root_resolved == scene_dir:
                    errors.append(
                        "Final COLMAP scene folder must differ from the selected export root"
                    )
                if work_dir_resolved is not None and scene_dir is not None and work_dir_resolved == scene_dir:
                    errors.append(
                        "Final COLMAP scene folder must differ from the cubeface working folder"
                    )
                for media_set in self._active_media_sets():
                    image_root = Path(media_set["image_root"]).resolve()
                    if image_root == scene_dir:
                        errors.append("Final COLMAP scene folder must differ from media image folders")
                    mask_root = media_set.get("mask_root")
                    if mask_root and Path(mask_root).resolve() == scene_dir:
                        errors.append("Final COLMAP scene folder must differ from media mask folders")
            except OSError:
                pass
        return errors

    # ── Run / Cancel ──────────────────────────────────────────────────

    def _on_run(self):
        errors = self._validate()
        if errors:
            self._append_console("Validation errors:\n" + "\n".join(f"  - {e}" for e in errors) + "\n")
            return

        self._save_current_prefs()
        self._clear_console()
        self._clear_preview()
        self._progress.set(0)
        self._is_running = True
        self._run_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")

        self._run_queue = []
        if not self._skip_cubefaces_var.get():
            self._run_queue.append(("Lens A", self._build_cmd_for_lens(self._lens_a)))
            if self._dual_var.get():
                self._run_queue.append(("Lens B", self._build_cmd_for_lens(self._lens_b)))
        if self._colmap_export_requested():
            self._run_queue.append(("COLMAP Scene", self._build_cmd_for_colmap_export()))

        self._run_next()

    def _run_next(self):
        if not self._run_queue:
            self._phase_label.configure(text="Done", text_color=COLOR_GREEN)
            self._progress.set(1.0)
            self._show_preview()
            self._finish_run()
            return

        label, cmd = self._run_queue.pop(0)
        self._current_run_label = label
        self._progress_hwm = 0.0
        self._progress_v2_sensor_index = 0
        remaining = len(self._run_queue)
        suffix = f" (then {remaining} more)" if remaining > 0 else ""
        self._phase_label.configure(
            text=f"Starting {label}{suffix}...", text_color=COLOR_TEXT,
        )
        self._progress.set(0)
        self._append_console(f"\n{'='*60}\n  {label}\n{'='*60}\n")
        self._append_console("$ " + " ".join(cmd) + "\n\n")

        if label in ("Lens A", "Lens B"):
            lens_panel = self._lens_a if label == "Lens A" else self._lens_b
            lens_label = lens_panel.lens_label.get()
            mode, _ = lens_panel.get_mode(self._fov.get())
            self._run_state[lens_label] = {
                "mode": mode,
                "support_source": None,
                "effective_angle": None,
                "angle_source": None,
                "processed": 0,
                "skipped": 0,
                "fallback_mask_count": 0,
                "pinhole_params": [],
                "_capturing_pinhole": False,
                "wall_clock_s": None,
                "started_at": time.perf_counter(),
                "bonusdata_dir": (
                    (self._cubeface_work_output_dir() or Path(self._output_dir.get()))
                    / lens_label / "bonusdata"
                ),
            }
            self._current_lens_label_for_state = lens_label
        else:
            self._current_lens_label_for_state = None

        def run_subprocess():
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    cwd=str(_SCRIPT.parent),
                )
                for line in self._proc.stdout:
                    self._log_queue.put(line)
                self._proc.wait()
                self._log_queue.put(("__EXIT__", self._proc.returncode))
            except Exception as e:
                self._log_queue.put(f"ERROR: {e}\n")
                self._log_queue.put(("__EXIT__", 1))

        self._reader_thread = threading.Thread(target=run_subprocess, daemon=True)
        self._reader_thread.start()

    def _on_cancel(self):
        self._run_queue.clear()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._append_console("\n--- Cancelled by user ---\n")
            self._phase_label.configure(text="Cancelled", text_color=COLOR_RED)
        self._finish_run()

    def _finish_run(self):
        self._is_running = False
        self._run_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self._proc = None
        # Re-enable COLMAP export button if it exists
        colmap_btn = getattr(self, "_colmap_export_btn", None)
        if colmap_btn is not None:
            self._colmap_check_export_ready()
        colmap_cancel = getattr(self, "_colmap_cancel_btn", None)
        if colmap_cancel is not None:
            colmap_cancel.configure(state="disabled")

    # ── Log polling ───────────────────────────────────────────────────

    def _poll_log(self):
        try:
            while True:
                item = self._log_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__EXIT__":
                    code = item[1]
                    self._finalize_lens_state()
                    if code == 0:
                        if self._run_queue:
                            self._append_console(
                                f"\n{self._current_run_label} completed successfully.\n"
                            )
                            self._run_next()
                        else:
                            self._phase_label.configure(text="Done", text_color=COLOR_GREEN)
                            self._progress.set(1.0)
                            self._show_preview()
                            self._finish_run()
                    else:
                        self._run_queue.clear()
                        self._phase_label.configure(
                            text=f"{self._current_run_label} exited with code {code}",
                            text_color=COLOR_RED,
                        )
                        self._finish_run()
                else:
                    self._append_console(item)
                    self._parse_progress(item)
                    self._capture_run_state(item)
        except queue.Empty:
            pass
        self.after(50, self._poll_log)

    def _parse_progress(self, line):
        m = re.match(r"^\[PROGRESS\]\s+(\S+)\s+(\d+)/(\d+)(?::\s*(.*))?", line)
        if not m:
            return
        phase, current, total = m.group(1), int(m.group(2)), int(m.group(3))
        message = (m.group(4) or "").strip()
        ratio = min(max(current / max(total, 1), 0.0), 1.0)
        label = self._current_run_label
        prefix = (
            f"{label}: "
            if self._dual_var.get() or label in ("COLMAP Scene", "COLMAP Export")
            else ""
        )

        # ── Compute weighted bar position ────────────────────────────
        value = None
        if label == "COLMAP Export":
            # V2 path: per-sensor phases use local weights scaled into
            # the sensor's band; scene phases use absolute weights.
            sensor_band = _V2_SENSOR_BAND if self._progress_v2_sensor_count > 0 else 0.0
            if phase in _COLMAP_EXPORT_SENSOR_LOCAL_WEIGHTS and self._progress_v2_sensor_count > 0:
                local_start, local_end = _COLMAP_EXPORT_SENSOR_LOCAL_WEIGHTS[phase]
                sensor_span = sensor_band / self._progress_v2_sensor_count
                base = self._progress_v2_sensor_index * sensor_span
                value = base + (local_start + (local_end - local_start) * ratio) * sensor_span
            elif phase in _COLMAP_EXPORT_SCENE_WEIGHTS:
                # Scale scene weights into [sensor_band, 1.0]
                raw_start, raw_end = _COLMAP_EXPORT_SCENE_WEIGHTS[phase]
                scene_range = 1.0 - _V2_SENSOR_BAND  # 0.45
                start = sensor_band + (raw_start - _V2_SENSOR_BAND) / scene_range * (1.0 - sensor_band)
                end = sensor_band + (raw_end - _V2_SENSOR_BAND) / scene_range * (1.0 - sensor_band)
                value = start + (end - start) * ratio
        elif label == "COLMAP Scene":
            if phase in _COLMAP_SCENE_PROGRESS_WEIGHTS:
                start, end = _COLMAP_SCENE_PROGRESS_WEIGHTS[phase]
                value = start + (end - start) * ratio
        else:
            # Old lens path (Lens A / Lens B)
            if phase in _LENS_PROGRESS_WEIGHTS:
                start, end = _LENS_PROGRESS_WEIGHTS[phase]
                value = start + (end - start) * ratio

        # ── Apply high-water mark and set bar ────────────────────────
        if phase == "RAYS" and current < total:
            # Indeterminate spinner while rays are being computed
            self._phase_label.configure(
                text=f"{prefix}Computing rays: {message}", text_color=COLOR_BLUE,
            )
            self._progress.configure(mode="indeterminate")
            self._progress.start()
            return

        self._progress.stop()
        self._progress.configure(mode="determinate")
        if value is not None:
            value = max(value, self._progress_hwm)
            self._progress_hwm = value
            self._progress.set(value)
        else:
            # Unknown phase: keep bar at high-water mark
            self._progress.set(self._progress_hwm)

        # ── Advance V2 sensor index on DONE ──────────────────────────
        if phase == "DONE" and label == "COLMAP Export" and current >= total:
            self._progress_v2_sensor_index += 1

        # ── Update phase label ───────────────────────────────────────
        phase_text = _PROGRESS_PHASE_LABELS.get(phase, phase)
        if phase == "DONE":
            self._phase_label.configure(
                text=f"{prefix}Sensor complete", text_color=COLOR_GREEN,
            )
        elif current >= total:
            self._phase_label.configure(
                text=f"{prefix}{phase_text} {current}/{total}: {message}",
                text_color=COLOR_GREEN,
            )
        else:
            self._phase_label.configure(
                text=f"{prefix}{phase_text} {current}/{total}: {message}",
                text_color=COLOR_BLUE,
            )

    # ── Run-state capture from log lines ─────────────────────────────

    def _capture_run_state(self, line):
        label = getattr(self, "_current_lens_label_for_state", None)
        if not label or label not in self._run_state:
            return
        state = self._run_state[label]

        m = _SUPPORT_SOURCE_RE.search(line)
        if m:
            state["support_source"] = m.group(1).strip()
            return
        m = _MASK_ANGLE_RE.search(line)
        if m:
            state["effective_angle"] = float(m.group(1))
            state["angle_source"] = "mask-derived"
            return
        m = _MANUAL_ANGLE_RE.search(line)
        if m:
            state["effective_angle"] = float(m.group(1))
            state["angle_source"] = "manual"
            return
        m = _DONE_RE.search(line)
        if m:
            state["processed"] = int(m.group(1))
            state["skipped"] = int(m.group(2))
            return
        m = _FALLBACK_COUNT_RE.search(line)
        if m:
            state["fallback_mask_count"] = int(m.group(1))
            return
        # Capture pinhole params: once the "FIXED for alignment" line is seen,
        # grab every subsequent logger.info line until the run ends.
        if state.get("_capturing_pinhole"):
            # Extract the message part after the logging prefix
            parts = line.split(": ", 2)
            if len(parts) >= 3:
                state["pinhole_params"].append(parts[2].strip())
            elif line.strip():
                state["pinhole_params"].append(line.strip())
            return
        if _PINHOLE_START_RE.search(line):
            state["_capturing_pinhole"] = True

    def _finalize_lens_state(self):
        label = getattr(self, "_current_lens_label_for_state", None)
        if not label or label not in self._run_state:
            return
        state = self._run_state[label]
        if state.get("started_at") is not None:
            state["wall_clock_s"] = time.perf_counter() - state["started_at"]
        self._write_run_report(label, state)

    def _write_run_report(self, lens_label, state):
        """Write a run summary text file next to the generated cubefaces."""
        work_dir = self._cubeface_work_output_dir()
        if work_dir is None:
            return
        report_dir = work_dir / lens_label
        if not report_dir.is_dir():
            return

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            f"Cubemap Splitter V4 — Run Report",
            f"Generated: {timestamp}",
            f"Lens: {lens_label}",
            "",
        ]

        source = state.get("support_source") or state.get("mode", "?")
        lines.append(f"Support source: {source}")

        angle = state.get("effective_angle")
        angle_src = state.get("angle_source")
        if angle is not None:
            angle_label = "Mask-derived" if angle_src == "mask-derived" else "Manual"
            lines.append(f"{angle_label} maximum angle: {angle:.4f} deg")

        lines.append("")
        lines.append(f"Processed: {state.get('processed', 0)}")
        lines.append(f"Skipped: {state.get('skipped', 0)}")
        fallback = state.get("fallback_mask_count", 0)
        if fallback > 0:
            lines.append(f"Fallback masks used: {fallback}")
        wall = state.get("wall_clock_s")
        if wall is not None:
            lines.append(f"Wall clock: {wall:.1f}s")

        pinhole = state.get("pinhole_params", [])
        if pinhole:
            lines.append("")
            lines.append("Output pinhole camera parameters (set and lock in Metashape):")
            for p in pinhole:
                lines.append(f"  {p}")

        lines.append("")

        report_path = report_dir / "run_report.txt"
        try:
            report_path.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass

    # ── Console ───────────────────────────────────────────────────────

    def _append_console(self, text, color=None):
        self._console.configure(state="normal")
        try:
            if color:
                tag = f"color_{color}"
                try:
                    self._console._textbox.tag_configure(tag, foreground=color)
                    self._console._textbox.insert("end", text, tag)
                except Exception:
                    self._console.insert("end", text)
            else:
                self._console.insert("end", text)
            self._console.see("end")
        finally:
            self._console.configure(state="disabled")

    def _clear_console(self):
        self._console.configure(state="normal")
        self._console.delete("1.0", "end")
        self._console.configure(state="disabled")

    # ── Preview ──────────────────────────────────────────────────────

    def _clear_preview(self):
        for w in self._preview_content.winfo_children():
            w.destroy()
        self._thumb_images.clear()
        self._preview_label.configure(text="")

    def _active_lens_for_preview(self):
        if self._dual_var.get() and self._preview_lens_var.get() == "Lens B":
            return self._lens_b
        return self._lens_a

    def _show_preview(self):
        self._clear_preview()
        view = self._preview_view_var.get()
        if view == "Cube faces":
            self._render_cube_faces()
        elif view == "Useful pixel mask":
            self._render_single_image("useful_pixel_mask.png", "Useful pixel mask")
        elif view == "Mask coverage":
            self._render_mask_coverage()
        elif view == "Fallback mask":
            self._render_single_image(
                "fallback_mask_from_useful_pixel_mask.png", "Fallback mask",
                missing_msg="No fallback mask was generated \u2014 all images had matching masks.",
            )
        elif view == "Run summary":
            self._render_run_summary()
        elif view == "COLMAP scene":
            self._render_colmap_scene_summary()

    def _render_cube_faces(self):
        out_dir = Path(self._output_dir.get()) if self._output_dir.get() else None
        lens = self._active_lens_for_preview()
        label = lens.lens_label.get()
        if not out_dir or not out_dir.is_dir() or not label:
            self._preview_label.configure(text="No output yet \u2014 run the script first.")
            return

        fmt = self._format_var.get()
        ext = {"png": ".png", "tiff": ".tif", "jpg": ".jpg"}.get(fmt, ".png")
        img_dir = out_dir / label / "images"

        face_files = []
        if img_dir.is_dir():
            subdirs = sorted(d for d in img_dir.iterdir() if d.is_dir())
            first_sub = subdirs[0] if subdirs else None
            for face_tag in ("+Z", "-X", "+X", "-Y", "+Y"):
                file_suffix = _FACE_FILENAME_SUFFIX[face_tag].lstrip("_")
                fp = None
                if first_sub is not None:
                    candidates = sorted(first_sub.glob(f"*_{file_suffix}{ext}"))
                    if candidates:
                        fp = candidates[0]
                if fp is None:
                    face_dir = img_dir / file_suffix
                    if face_dir.is_dir():
                        candidates = sorted(face_dir.glob(f"*_{file_suffix}{ext}"))
                        if candidates:
                            fp = candidates[0]
                if fp is not None:
                    face_files.append((face_tag, file_suffix, fp))

        if not face_files:
            self._preview_label.configure(text=f"({label}) No output cube faces found.")
            return

        self._preview_label.configure(
            text=f"Cube faces ({label}): {face_files[0][2].parent.name}",
        )
        thumb_size = 120
        for face_tag, file_suffix, fp in face_files:
            try:
                img = Image.open(fp)
                img.thumbnail((thumb_size, thumb_size))
                ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                self._thumb_images.append(ctk_img)
                cell = ctk.CTkFrame(self._preview_content, fg_color="transparent")
                cell.pack(side="left", padx=6)
                ctk.CTkLabel(cell, image=ctk_img, text="").pack()
                ctk.CTkLabel(
                    cell, text=f"{file_suffix}\n({face_tag} face)",
                    font=("Consolas", 9), text_color=COLOR_TEXT_DIM,
                    justify="center",
                ).pack(pady=(2, 0))
            except Exception:
                pass

    def _render_single_image(self, filename, title, missing_msg=None):
        out_dir = Path(self._output_dir.get()) if self._output_dir.get() else None
        lens = self._active_lens_for_preview()
        label = lens.lens_label.get()
        if not out_dir or not out_dir.is_dir() or not label:
            self._preview_label.configure(text="No output yet \u2014 run the script first.")
            return
        img_path = out_dir / label / "bonusdata" / filename
        if not img_path.is_file():
            msg = missing_msg or f"({label}) {filename} not yet written."
            self._preview_label.configure(text=msg)
            return
        self._preview_label.configure(text=f"{title} ({label}): {img_path.name}")
        try:
            img = Image.open(img_path)
            img.thumbnail((280, 280))
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
            self._thumb_images.append(ctk_img)
            ctk.CTkLabel(self._preview_content, image=ctk_img, text="").pack(padx=4, pady=4)
        except Exception as e:
            self._preview_label.configure(text=f"Could not load image: {e}")

    def _render_mask_coverage(self):
        out_dir = Path(self._output_dir.get()) if self._output_dir.get() else None
        lens = self._active_lens_for_preview()
        label = lens.lens_label.get()
        if not out_dir or not out_dir.is_dir() or not label:
            self._preview_label.configure(text="No output yet \u2014 run the script first.")
            return
        tif_path = out_dir / label / "bonusdata" / "validpixelcountimage_frommasks_16bit.tif"
        if not tif_path.is_file():
            self._preview_label.configure(
                text="Mask coverage is only written when support comes from masks.",
            )
            return
        self._preview_label.configure(
            text=f"Mask coverage ({label}): {tif_path.name}  (viridis colormap)",
        )
        try:
            img = Image.open(tif_path)
            arr = list(img.getdata())
            if not arr:
                self._preview_label.configure(text=f"({label}) Mask coverage TIFF is empty.")
                return
            sorted_vals = sorted(arr)
            p99 = sorted_vals[int(len(sorted_vals) * 0.99)]
            if p99 <= 0:
                p99 = max(sorted_vals) or 1
            scaled = [min(255, int(v * 255 / p99)) for v in arr]
            gray = Image.new("L", img.size)
            gray.putdata(scaled)
            colored = _viridis_colorize(gray)
            colored.thumbnail((280, 280))
            ctk_img = ctk.CTkImage(
                light_image=colored, dark_image=colored, size=colored.size,
            )
            self._thumb_images.append(ctk_img)
            ctk.CTkLabel(self._preview_content, image=ctk_img, text="").pack(padx=4, pady=4)
        except Exception as e:
            self._preview_label.configure(text=f"Could not render TIFF: {e}")

    def _render_run_summary(self):
        lens = self._active_lens_for_preview()
        label = lens.lens_label.get()
        state = self._run_state.get(label)
        if not state:
            self._preview_label.configure(
                text=f"({label}) No run summary yet \u2014 run the script first.",
            )
            return
        self._preview_label.configure(text=f"Run summary ({label})")
        card = ctk.CTkFrame(self._preview_content, fg_color=COLOR_CONSOLE, corner_radius=6)
        card.pack(fill="x", padx=4, pady=4)

        source = state.get("support_source") or state.get("mode", "?")
        angle = state.get("effective_angle")
        angle_src = state.get("angle_source")
        processed = state.get("processed", 0)
        skipped = state.get("skipped", 0)
        fallback = state.get("fallback_mask_count", 0)
        wall = state.get("wall_clock_s")
        bonus = state.get("bonusdata_dir")

        ctk.CTkLabel(
            card, text=f"Support source: {source}", font=("", 12, "bold"),
            text_color=COLOR_TEXT,
        ).pack(anchor="w", padx=10, pady=(8, 2))

        if angle is not None:
            angle_label = "Mask-derived" if angle_src == "mask-derived" else "Manual"
            color = COLOR_GREEN if angle_src == "mask-derived" else COLOR_BLUE
            ctk.CTkLabel(
                card, text=f"{angle_label} angle: {angle:.2f}\u00b0",
                font=("Consolas", 18, "bold"), text_color=color,
            ).pack(anchor="w", padx=10, pady=(0, 4))

        counts = f"Processed: {processed}    Skipped: {skipped}"
        if fallback > 0:
            counts += f"    Fallback masks: {fallback}"
        if wall is not None:
            counts += f"    Wall clock: {wall:.1f}s"
        ctk.CTkLabel(
            card, text=counts, font=("Consolas", 11), text_color=COLOR_TEXT_DIM,
        ).pack(anchor="w", padx=10, pady=(0, 4))

        if bonus and Path(bonus).is_dir():
            ctk.CTkLabel(
                card, text=f"Bonus data: {bonus}",
                font=("Consolas", 10), text_color=COLOR_TEXT_DIM,
                wraplength=520, justify="left",
            ).pack(anchor="w", padx=10, pady=(0, 8))

    def _render_colmap_scene_summary(self):
        scene_dir = self._colmap_scene_output_dir()
        if not scene_dir or not scene_dir.is_dir():
            self._preview_label.configure(text="No COLMAP scene output yet.")
            return
        sparse_dir = scene_dir / "sparse" / "0"
        reports_dir = self._colmap_reports_output_dir()
        support_dir = self._colmap_support_output_dir()
        validation_path = (reports_dir / "validation_report.txt") if reports_dir is not None else None
        if validation_path is None or not validation_path.is_file():
            validation_path = (support_dir / "validation_report.txt") if support_dir is not None else None
        if validation_path is None or not validation_path.is_file():
            validation_path = scene_dir / "validation_report.txt"
        report_path = (reports_dir / "conversion_report.txt") if reports_dir is not None else None
        if report_path is None or not report_path.is_file():
            report_path = sparse_dir / "conversion_report.txt"

        self._preview_label.configure(text=f"COLMAP scene: {scene_dir}")
        card = ctk.CTkFrame(self._preview_content, fg_color=COLOR_CONSOLE, corner_radius=6)
        card.pack(fill="x", padx=4, pady=4)

        lines = [f"images/: {(scene_dir / 'images').is_dir()}"]
        lines.append(f"masks/: {(scene_dir / 'masks').is_dir()}")
        lines.append(f"sparse/0/: {sparse_dir.is_dir()}")

        scale_path = None
        if support_dir is not None:
            scale_path = support_dir / "manifests" / "scene_scale_diagnostics.json"
        if scale_path is not None and scale_path.is_file():
            try:
                scale_data = json.loads(scale_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                scale_data = {}
            normalization = scale_data.get("normalization", {})
            applied = bool(normalization.get("applied")) if isinstance(normalization, dict) else False
            metrics = scale_data.get("normalized" if applied else "original", {})
            warnings = scale_data.get("warnings", [])
            if not isinstance(metrics, dict):
                metrics = {}
            if not isinstance(warnings, list):
                warnings = []
            lines.append("")
            lines.append("scene_scale_diagnostics.json")
            lines.append(f"normalization: {'applied' if applied else 'not applied'}")
            lines.append(f"camera_radius_p95: {metrics.get('camera_radius_p95')}")
            lines.append(f"point/camera_radius_ratio: {metrics.get('point_to_camera_radius_ratio')}")
            lines.append(f"combined_bounds_diagonal: {metrics.get('combined_bounds_diagonal')}")
            if warnings:
                lines.append(f"scale_warnings: {len(warnings)}")
                for warning in warnings[:3]:
                    if isinstance(warning, dict):
                        lines.append(f"- {warning.get('code')}: {warning.get('message')}")

        for path in (validation_path, report_path):
            if path is not None and path.is_file():
                try:
                    report_lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    report_lines = []
                lines.append("")
                lines.append(path.name)
                lines.extend(report_lines[:12])

        ctk.CTkLabel(
            card,
            text="\n".join(lines),
            font=("Consolas", 10),
            text_color=COLOR_TEXT,
            justify="left",
            wraplength=520,
        ).pack(anchor="w", padx=10, pady=8)

    # ── Prefs persistence ─────────────────────────────────────────────

    def _build_colmap_prefs_dict(self):
        """Build a serializable dict of current COLMAP panel state for prefs (v2)."""
        xml_var = getattr(self, "_colmap_cameras_xml", None)
        if xml_var is None or not xml_var.get().strip():
            return None
        ply_var = getattr(self, "_colmap_sparse_ply", None)
        out_var = getattr(self, "_colmap_output_dir", None)

        def _paths(card_state, kind):
            return [v.get() for v in card_state.get(f"{kind}_dirs", []) if v.get().strip()]

        def _auto_width_int(var):
            s = (var.get() or "").strip()
            if s in ("", "0"):
                return 0
            return int(s) if s.isdigit() else 0

        fisheye_sensors = []
        for sid, card in getattr(self, "_colmap_fisheye_cards", {}).items():
            entry = {
                "sensor_id": sid,
                "image_dirs": _paths(card, "img"),
                "mask_dirs": _paths(card, "mask"),
                "multi_pinhole": bool(card["multi_pinhole_var"].get()),
                "output_width": _auto_width_int(card["width_var"]),
                "output_format": card["output_format_var"].get(),
            }
            if card["lens_only_enabled_var"].get() and card["lens_only_path_var"].get().strip():
                entry["lens_only_mask"] = card["lens_only_path_var"].get()
            routing = card.get("routing")
            if routing is not None:
                entry["routing"] = routing.to_dict()
            fisheye_sensors.append(entry)

        frame_sensors = []
        for sid, card in getattr(self, "_colmap_frame_cards", {}).items():
            frame_sensors.append({
                "sensor_id": sid,
                "image_dirs": _paths(card, "img"),
                "mask_dirs": _paths(card, "mask"),
            })

        equirect_sensors = []
        for sid, card in getattr(self, "_colmap_equirect_cards", {}).items():
            equirect_sensors.append({
                "sensor_id": sid,
                "image_dirs": _paths(card, "img"),
                "mask_dirs": _paths(card, "mask"),
                "split_width": _auto_width_int(card["split_width_var"]),
                "split_mode": card["split_mode_var"].get(),
                "split_width_user_overridden": bool(card.get("split_width_user_overridden")),
            })

        return {
            "cameras_xml": xml_var.get(),
            "sparse_ply": ply_var.get() if ply_var else "",
            "output_dir": out_var.get() if out_var else "",
            "fisheye_sensors": fisheye_sensors,
            "frame_sensors": frame_sensors,
            "equirect_sensors": equirect_sensors,
            "options": {
                "pose_convention": self._colmap_pose_convention_var.get(),
                "normalize_scene": self._colmap_normalize_var.get(),
                "force_assets": self._colmap_force_assets_var.get(),
                "keep_processing_files": self._colmap_keep_processing_var.get(),
                "projected_tracks": self._colmap_projected_tracks_var.get(),
            },
        }

    def _restore_colmap_prefs(self, manifest_dict):
        """Restore COLMAP panel state from a saved v2 manifest dict."""
        from pathlib import Path
        xml_path = manifest_dict.get("cameras_xml", "")
        if not (xml_path and Path(xml_path).is_file()):
            return
        xml_var = getattr(self, "_colmap_cameras_xml", None)
        if xml_var:
            xml_var.set(xml_path)
        ply_var = getattr(self, "_colmap_sparse_ply", None)
        if ply_var and manifest_dict.get("sparse_ply"):
            ply_var.set(manifest_dict["sparse_ply"])
        out_var = getattr(self, "_colmap_output_dir", None)
        if out_var and manifest_dict.get("output_dir"):
            out_var.set(manifest_dict["output_dir"])

        if getattr(self, "_restoring_prefs", False):
            self._pending_colmap_manifest_restore = manifest_dict
            return

        # Discovery rebuilds the cards based on the XML
        self._on_colmap_xml_changed()
        self._apply_colmap_card_prefs(manifest_dict)

    def _apply_colmap_card_prefs(self, manifest_dict):
        """Apply saved COLMAP sensor-card fields after cards exist."""
        def _populate_dirs(card, kind, paths):
            for i, p in enumerate(paths):
                if i >= len(card[f"{kind}_dirs"]):
                    self._add_dir_row(card, kind, card["sensor_id"])
                card[f"{kind}_dirs"][i].set(p)

        for s in manifest_dict.get("fisheye_sensors", []):
            card = self._colmap_fisheye_cards.get(s["sensor_id"])
            if not card:
                continue
            _populate_dirs(card, "img", s.get("image_dirs", []))
            _populate_dirs(card, "mask", s.get("mask_dirs", []))
            if "multi_pinhole" in s:
                card["_suppress_multi_trace"] = True
                try:
                    card["multi_pinhole_var"].set(bool(s["multi_pinhole"]))
                finally:
                    card["_suppress_multi_trace"] = False
                routing = card.get("routing")
                if routing is not None:
                    card["multi_pinhole_user_overridden"] = (
                        bool(s["multi_pinhole"]) != self._routing_recommends_multi(routing)
                    )
            if "output_width" in s:
                card["_suppress_width_trace"] = True
                try:
                    card["width_var"].set(str(s["output_width"]))
                finally:
                    card["_suppress_width_trace"] = False
                card["width_user_overridden"] = True
            if "output_format" in s:
                card["output_format_var"].set(s.get("output_format") or "jpg")
            lens_only = s.get("lens_only_mask")
            if lens_only:
                card["lens_only_enabled_var"].set(True)
                card["lens_only_path_var"].set(lens_only)
                self._on_lens_only_toggled(s["sensor_id"])

        for s in manifest_dict.get("frame_sensors", []):
            card = self._colmap_frame_cards.get(s["sensor_id"])
            if not card:
                continue
            _populate_dirs(card, "img", s.get("image_dirs", []))
            _populate_dirs(card, "mask", s.get("mask_dirs", []))

        for s in manifest_dict.get("equirect_sensors", []):
            card = self._colmap_equirect_cards.get(s["sensor_id"])
            if not card:
                continue
            _populate_dirs(card, "img", s.get("image_dirs", []))
            _populate_dirs(card, "mask", s.get("mask_dirs", []))
            if "split_mode" in s:
                card["split_mode_var"].set(s["split_mode"])
            if "split_width" in s:
                saved_width = s.get("split_width")
                try:
                    saved_width_int = int(saved_width)
                except (TypeError, ValueError):
                    saved_width_int = 0
                saved_override = bool(s.get("split_width_user_overridden"))
                if saved_width_int > 0 and (saved_override or saved_width_int != 2048):
                    card["_suppress_split_width_trace"] = True
                    try:
                        card["split_width_var"].set(str(saved_width_int))
                    finally:
                        card["_suppress_split_width_trace"] = False
                    card["split_width_user_overridden"] = saved_override
                else:
                    self._apply_equirect_auto_width(card)
            else:
                self._apply_equirect_auto_width(card)

        opts = manifest_dict.get("options", {})
        if "pose_convention" in opts:
            self._colmap_pose_convention_var.set(opts["pose_convention"])
        if "normalize_scene" in opts:
            self._colmap_normalize_var.set(opts["normalize_scene"])
        if "force_assets" in opts:
            self._colmap_force_assets_var.set(opts["force_assets"])
        if "keep_processing_files" in opts:
            self._colmap_keep_processing_var.set(opts["keep_processing_files"])
        if "projected_tracks" in opts:
            self._colmap_projected_tracks_var.set(opts["projected_tracks"])

    def _save_current_prefs(self):
        data = {
            "lens_a": self._lens_a.get_values(),
            "lens_b": self._lens_b.get_values(),
            "dual_mode": self._dual_var.get(),
            "output_purpose": self._purpose_var.get(),
            "facewidth": self._facewidth.get(),
            "output_dir": self._output_dir.get(),
            "output_format": self._format_var.get(),
            "structure": self._structure_var.get(),
            "force": self._force_var.get(),
            "preview_view": self._preview_view_var.get(),
            "preview_lens": self._preview_lens_var.get(),
            "metashape_xml": self._metashape_xml.get(),
            "metashape_ply": self._metashape_ply.get(),
            "colmap_scene_dir": self._colmap_scene_dir.get(),
            "manual_lens_camera_map": self._lens_camera_map.get(),
            "pose_default_version": 2,
            "pose_convention": self._pose_convention_var.get(),
            "require_masks_default_version": 2,
            "require_masks": self._require_masks_var.get(),
            "projected_tracks": self._projected_tracks_var.get(),
            "normalize_scene": self._normalize_scene_var.get(),
            "force_scene_assets": self._force_scene_assets_var.get(),
            "keep_processing_files": self._keep_processing_files_var.get(),
            "passthrough_media_sets": [
                row.get_values()
                for row in self._media_set_rows
                if not row.is_blank()
            ],
        }
        # COLMAP manifest persistence
        colmap_manifest = self._build_colmap_prefs_dict()
        if colmap_manifest:
            data["colmap_last_manifest"] = colmap_manifest
        _save_prefs(data)

    def _restore_prefs(self):
        p = self._prefs
        if not p:
            return
        self._restoring_prefs = True
        try:
            if "lens_a" in p:
                self._lens_a.set_values(p["lens_a"])
            if "lens_b" in p:
                self._lens_b.set_values(p["lens_b"])
            if "dual_mode" in p:
                self._dual_var.set(p["dual_mode"])
                self._on_dual_toggled()
            if "output_purpose" in p:
                purpose = p["output_purpose"]
                if purpose in (PURPOSE_METASHAPE, PURPOSE_COLMAP):
                    self._purpose_var.set(purpose)
            if "facewidth" in p:
                self._facewidth.set(p["facewidth"])
            if "output_dir" in p:
                self._output_dir.set(p["output_dir"])
            if "output_format" in p:
                self._format_var.set(p["output_format"])
            if "structure" in p:
                self._structure_var.set(p["structure"])
            if "force" in p:
                self._force_var.set(p["force"])
            if "preview_view" in p:
                self._preview_view_var.set(p["preview_view"])
            if "preview_lens" in p:
                self._preview_lens_var.set(p["preview_lens"])
            if "metashape_xml" in p:
                self._metashape_xml.set(p["metashape_xml"])
            if "metashape_ply" in p:
                self._metashape_ply.set(p["metashape_ply"])
            if "colmap_scene_dir" in p:
                self._colmap_scene_dir.set(p["colmap_scene_dir"])
            manual_map = p.get("manual_lens_camera_map", p.get("lens_camera_map", ""))
            if manual_map:
                self._lens_camera_map.set(manual_map)
            if "pose_convention" in p:
                pose = p["pose_convention"]
                if pose == "auto":
                    pose = "metashape_camera_to_world"
                self._pose_convention_var.set(pose)
            if "require_masks" in p and p.get("require_masks_default_version") == 2:
                self._require_masks_var.set(p["require_masks"])
            if "projected_tracks" in p:
                self._projected_tracks_var.set(p["projected_tracks"])
            if "normalize_scene" in p:
                self._normalize_scene_var.set(p["normalize_scene"])
            if "force_scene_assets" in p:
                self._force_scene_assets_var.set(p["force_scene_assets"])
            if "keep_processing_files" in p:
                self._keep_processing_files_var.set(p["keep_processing_files"])
            for media_set in p.get("passthrough_media_sets", []):
                self._add_media_set_row(media_set)
            if "colmap_last_manifest" in p:
                self._restore_colmap_prefs(p["colmap_last_manifest"])
        finally:
            self._restoring_prefs = False
        self._refresh_purpose_ui(scroll=False)
        self._update_all_modes()


if __name__ == "__main__":
    app = CubemapGUI()
    app.mainloop()
