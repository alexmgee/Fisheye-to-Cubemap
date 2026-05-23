"""Cubeface output batch provenance stamp.

Answers one question: "Were these exact files generated from this exact
processing recipe?" If yes, skip. If no or unknown, reprocess.

A _processing_stamp.json file is written to the output directory after a
successful batch. Before skipping on a rerun, the stamp is loaded and
compared against the current run's recipe. A mismatch on any field, or a
missing stamp, means the outputs are untrusted and must be regenerated.

Only parameters that affect generated file content are included. GUI state,
timestamps, user labels, and progress text are excluded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Bump when the stamp schema changes. Old stamps with a different version
# are treated as mismatches (forcing reprocessing), so stale stamps from
# a prior schema cannot accidentally validate.
STAMP_VERSION = 3

STAMP_FILENAME = "_processing_stamp.json"


def compute_calibration_digest(
    projection: str,
    width: int,
    height: int,
    params: tuple,
    corrections_hash: str,
) -> str:
    """Deterministic digest of the full calibration recipe.

    params is the 11-tuple (f, cx, cy, K1-K4, P1, P2, B1, B2).
    corrections_hash is from fourier_corrections.corrections_cache_hash
    (empty string when no corrections).
    """
    payload = (
        f"proj={projection}"
        f"|dim={width}x{height}"
        f"|params={'_'.join(f'{p:.15g}' for p in params)}"
        f"|corr={corrections_hash}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def compute_mask_input_digest(
    support_origin: str,
    mask_paths: list[Path] | None,
    lens_only_mask: Path | None,
) -> str:
    """Deterministic digest of mask/support inputs.

    Includes the support origin string, the list of mask file
    (name, size, mtime_ns) tuples, and the lens-only mask identity.
    """
    items: list = [support_origin]
    for p in sorted(mask_paths or []):
        p = Path(p)
        if p.is_file():
            stat = p.stat()
            items.append((p.name, stat.st_size, stat.st_mtime_ns))
    if lens_only_mask is not None:
        p = Path(lens_only_mask)
        if p.is_file():
            stat = p.stat()
            items.append(("__lens_only__", stat.st_size, stat.st_mtime_ns))
    data = json.dumps(items, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:20]


def build_stamp(
    calibration_digest: str,
    face_width: int,
    output_format: str,
    mask_input_digest: str,
    rig_structure: bool,
) -> dict:
    """Build a stamp dict for the current processing recipe."""
    return {
        "stamp_version": STAMP_VERSION,
        "calibration_digest": calibration_digest,
        "face_width": face_width,
        "output_format": output_format,
        "mask_input_digest": mask_input_digest,
        "rig_structure": rig_structure,
    }


def write_stamp(output_dir: Path, stamp: dict) -> Path:
    """Write the stamp to the output directory. Returns the stamp path."""
    path = Path(output_dir) / STAMP_FILENAME
    path.write_text(json.dumps(stamp, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_stamp(output_dir: Path) -> dict | None:
    """Read the stamp from the output directory. Returns None if missing or corrupt."""
    path = Path(output_dir) / STAMP_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def stamp_matches(existing: dict | None, current: dict) -> bool:
    """Return True only if the existing stamp matches the current recipe exactly.

    Returns False when:
    - existing is None (no stamp — never trust)
    - stamp_version differs (schema change — never trust)
    - any recipe field differs
    """
    if existing is None:
        return False
    if existing.get("stamp_version") != current.get("stamp_version"):
        return False
    for key in ("calibration_digest", "face_width", "output_format",
                "mask_input_digest", "rig_structure"):
        if existing.get(key) != current.get(key):
            return False
    return True


__all__ = [
    "STAMP_FILENAME",
    "STAMP_VERSION",
    "build_stamp",
    "compute_calibration_digest",
    "compute_mask_input_digest",
    "read_stamp",
    "stamp_matches",
    "write_stamp",
]
