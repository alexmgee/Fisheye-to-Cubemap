"""
Cubemap GUI V4 — standalone wrapper for AM_ImageAndMask_to_cubemap_v4.py.

Provides file/directory pickers for all CLI arguments, a live console
showing subprocess stdout, a progress bar driven by [PROGRESS] lines,
and preview of output artifacts (cube faces, useful pixel mask, mask
coverage, fallback mask, run summary).

Supports single-lens and dual-lens (360) workflows. In dual-lens mode,
the Lens B tab is enabled and both lenses are processed sequentially
with a single Run click.

Support-source priority matches the CLI exactly:
  mask directory > lens-only mask > manual FOV
The GUI passes every non-empty support flag to the CLI and communicates
which source will take priority via badges and hints.

Run:  python cubemap_gui.py
Deps: customtkinter, Pillow  (pip install customtkinter Pillow)
"""

import customtkinter as ctk
import tkinter as tk
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

# ── Resolve the v4 script path ───────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_SCRIPT = _THIS_DIR.parent / "AM_ImageAndMask_to_cubemap_v4.py"

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
FONT_LABEL = ("", 12)
FONT_HEADING = ("", 13, "bold")
FONT_CONSOLE = ("Consolas", 10)
FONT_STATUS = ("", 11)

# ── Run-state capture regexes (precompiled) ──────────────────────────
_SUPPORT_SOURCE_RE = re.compile(r"useful_pixel_mask source:\s+(.+)")
_MASK_ANGLE_RE = re.compile(r"Mask-derived maximum angle:\s+([\d.]+)\s+deg")
_MANUAL_ANGLE_RE = re.compile(r"Manual maximum angle:\s+([\d.]+)\s+deg")
_DONE_RE = re.compile(
    r"\[PROGRESS\]\s+DONE\s+\d+/\d+:\s+processed=(\d+)\s+skipped=(\d+)"
)
_FALLBACK_COUNT_RE = re.compile(r"Resolved (\d+) missing per-image mask")
_PINHOLE_START_RE = re.compile(r"set and FIXED for alignment")


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
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        cal = root if root.tag == "calibration" else root.find("calibration")
        if cal is None:
            return None
        proj = cal.findtext("projection", "?")
        w = cal.findtext("width", "?")
        h = cal.findtext("height", "?")
        f = cal.findtext("f", "?")
        return f"{proj}  {w}x{h}  f={f}"
    except Exception:
        return None


def _guess_lens_label(filepath):
    return Path(filepath).stem


def _count_image_files(directory):
    exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
    if not directory or not Path(directory).is_dir():
        return 0
    try:
        return sum(1 for f in os.listdir(directory) if Path(f).suffix.lower() in exts)
    except Exception:
        return 0


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
            fov_frame, textvariable=self._fov_override_value, width=80,
            font=("Consolas", 13), justify="left",
        )
        # Hidden by default — shown when checkbox is checked
        self._fov_override_entry_packed = False
        self._fov_override_value.trace_add("write", lambda *_: self._notify_change())
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
            info = _parse_calibration_xml(path)
            self.cal_info.configure(text=info or "Could not parse XML")
            self.lens_label.set(_guess_lens_label(path))
        else:
            self.cal_info.configure(text="")
        if self._on_change:
            self._on_change()

    def _handle_fov_override_toggle(self):
        if self._fov_override_var.get():
            if not self._fov_override_entry_packed:
                self._fov_override_entry.pack(side="left", padx=(6, 0))
                self._fov_override_entry_packed = True
        else:
            if self._fov_override_entry_packed:
                self._fov_override_entry.pack_forget()
                self._fov_override_entry_packed = False
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


class CubemapGUI(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title(_SCRIPT.stem)
        self.geometry("1200x900")
        self.minsize(950, 900)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._proc = None
        self._reader_thread = None
        self._log_queue = queue.Queue()
        self._is_running = False
        self._run_queue = []
        self._current_run_label = ""
        self._prefs = _load_prefs()
        self._run_state = {}

        self._build_ui()
        self._restore_prefs()
        self._update_all_modes()
        self._poll_log()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1, minsize=420, uniform="pane")
        self.grid_columnconfigure(1, weight=2, minsize=450, uniform="pane")
        self.grid_rowconfigure(0, weight=1)
        # Prevent content from negotiating column widths during a run.
        self.grid_propagate(False)

        left_outer = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=8)
        left_outer.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        left_outer.grid_rowconfigure(0, weight=1)
        left_outer.grid_columnconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(left_outer, fg_color=COLOR_CARD, corner_radius=0)
        left.grid(row=0, column=0, sticky="nsew")
        self._build_left(left)

        right = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        self._build_right(right)

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
                                 on_change=self._update_all_modes)
        self._lens_b = LensPanel(self._lens_container, "Lens B",
                                 on_change=self._update_all_modes)

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

        # Output directory (top of shared settings)
        row = self._add_dir_picker(parent, row, "Output directory", "_output_dir")

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

        options_frame = ctk.CTkFrame(parent, fg_color="transparent")
        options_frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(20, 10))

        self._force_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            options_frame, text="Force reprocess", variable=self._force_var,
            font=FONT_LABEL,
        ).pack(side="left", padx=(12, 0))

        structure_group = ctk.CTkFrame(options_frame, fg_color="transparent")
        structure_group.pack(side="right", padx=(0, 12))

        self._structure_var = ctk.StringVar(value="station")
        ctk.CTkRadioButton(
            structure_group, text="Station", variable=self._structure_var,
            value="station", font=FONT_LABEL, width=80,
        ).pack(side="left")

        ctk.CTkRadioButton(
            structure_group, text="Rig", variable=self._structure_var,
            value="rig", font=FONT_LABEL, width=55,
        ).pack(side="left")
        row += 1

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
                    "Fallback mask", "Run summary"],
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

    # ── Shared widget builders ───────────────────────────────────────

    def _add_dir_picker(self, parent, row, label, attr, on_change=None):
        ctk.CTkLabel(parent, text=label, font=FONT_LABEL).grid(
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
        if on_change:
            var.trace_add("write", lambda *_: on_change())
        row += 1
        return row

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
        self._update_all_modes()
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
        cmd = [
            sys.executable, str(_SCRIPT),
            "--amlenscal", lens_panel.cal_path.get(),
            "--lenslabel", lens_panel.lens_label.get(),
            "--directoryfisheyeimages", lens_panel.images_dir.get(),
            "--facewidth", str(self._facewidth.get()),
            "--outputdir", self._output_dir.get(),
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
        if self._structure_var.get() == "rig":
            cmd.append("--rigstructure")
        if self._force_var.get():
            cmd.append("--force")
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

        self._run_queue = [("Lens A", self._build_cmd_for_lens(self._lens_a))]
        if self._dual_var.get():
            self._run_queue.append(("Lens B", self._build_cmd_for_lens(self._lens_b)))

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
        remaining = len(self._run_queue)
        suffix = f" (then {remaining} more)" if remaining > 0 else ""
        self._phase_label.configure(
            text=f"Starting {label}{suffix}...", text_color=COLOR_TEXT,
        )
        self._progress.set(0)
        self._append_console(f"\n{'='*60}\n  {label}\n{'='*60}\n")
        self._append_console("$ " + " ".join(cmd) + "\n\n")

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
                Path(self._output_dir.get()) / lens_label / "bonusdata"
            ),
        }
        self._current_lens_label_for_state = lens_label

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
        m = re.match(r"^\[PROGRESS\]\s+(\S+)\s+(\d+)/(\d+):\s+(.*)", line)
        if not m:
            return
        phase, current, total = m.group(1), int(m.group(2)), int(m.group(3))
        message = m.group(4).strip()
        prefix = f"{self._current_run_label}: " if self._dual_var.get() else ""

        if phase == "MASK_SUM":
            self._progress.stop()
            self._progress.configure(mode="determinate")
            self._progress.set(current / max(total, 1))
            self._phase_label.configure(
                text=f"{prefix}Analyzing masks {current}/{total}: {message}",
                text_color=COLOR_BLUE,
            )
        elif phase == "RAYS":
            self._phase_label.configure(
                text=f"{prefix}Computing rays: {message}", text_color=COLOR_BLUE,
            )
            self._progress.configure(mode="indeterminate")
            self._progress.start()
        elif phase == "REMAP_PRECOMPUTE":
            self._progress.stop()
            self._progress.configure(mode="determinate")
            self._progress.set(current / max(total, 1))
            self._phase_label.configure(
                text=f"{prefix}Precomputing remap {current}/{total}: {message}",
                text_color=COLOR_BLUE,
            )
        elif phase == "REMAP_APPLY":
            self._progress.configure(mode="determinate")
            self._progress.set(current / max(total, 1))
            self._phase_label.configure(
                text=f"{prefix}Processing image {current}/{total}: {message}",
                text_color=COLOR_TEXT,
            )
        elif phase == "DONE":
            self._progress.stop()
            self._progress.configure(mode="determinate")
            self._progress.set(1.0)
            self._phase_label.configure(
                text=f"{prefix}Lens complete", text_color=COLOR_GREEN,
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
        """Write a run summary text file to the output directory."""
        out_dir = self._output_dir.get().strip()
        if not out_dir:
            return
        report_dir = Path(out_dir) / lens_label
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

    def _append_console(self, text):
        self._console.configure(state="normal")
        self._console.insert("end", text)
        self._console.see("end")
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

    # ── Prefs persistence ─────────────────────────────────────────────

    def _save_current_prefs(self):
        data = {
            "lens_a": self._lens_a.get_values(),
            "lens_b": self._lens_b.get_values(),
            "dual_mode": self._dual_var.get(),
            "facewidth": self._facewidth.get(),
            "output_dir": self._output_dir.get(),
            "output_format": self._format_var.get(),
            "structure": self._structure_var.get(),
            "force": self._force_var.get(),
            "preview_view": self._preview_view_var.get(),
            "preview_lens": self._preview_lens_var.get(),
        }
        _save_prefs(data)

    def _restore_prefs(self):
        p = self._prefs
        if not p:
            return
        if "lens_a" in p:
            self._lens_a.set_values(p["lens_a"])
        if "lens_b" in p:
            self._lens_b.set_values(p["lens_b"])
        if "dual_mode" in p:
            self._dual_var.set(p["dual_mode"])
            self._on_dual_toggled()
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
        self._update_all_modes()


if __name__ == "__main__":
    app = CubemapGUI()
    app.mainloop()
