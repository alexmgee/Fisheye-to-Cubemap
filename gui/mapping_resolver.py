"""Pure mapping-resolver logic for the Cubemap GUI COLMAP export.

Parses Metashape XML into camera runs, resolves automatic lens-to-camera
mappings with confidence levels, validates manual maps, and produces
structured diagnostics.

No GUI imports. Testable with synthetic fixtures.
"""

from __future__ import annotations

import importlib.util
import itertools
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Set,
    Sequence,
    Tuple,
)

# ── Import exporter helpers (fail-closed) ───────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXPORTER_PATH = _REPO_ROOT / "metashape_cameras_to_colmap.py"

_exporter = None
try:
    _spec = importlib.util.spec_from_file_location(
        "metashape_cameras_to_colmap", str(_EXPORTER_PATH)
    )
    if _spec is not None and _spec.loader is not None:
        _exporter = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_exporter)
except Exception:
    _exporter = None

# ── Confidence constants ────────────────────────────────────────────────

STRONG = "strong"
HEURISTIC = "heuristic"

# ── Diagnostic severity constants ───────────────────────────────────────

INFO = "info"
WARNING = "warning"
BLOCKER = "blocker"

# ── Dataclasses ─────────────────────────────────────────────────────────


@dataclass
class MappingDiagnostic:
    severity: str   # INFO, WARNING, BLOCKER
    code: str       # machine-readable
    message: str    # human-readable


@dataclass(frozen=True)
class LensJob:
    ui_label: str
    lens_label: str
    stems: FrozenSet[str]
    cal_path: Optional[str] = None


@dataclass
class CameraRun:
    ids: Tuple[int, ...]
    sensor_id: int
    sensor_label: str
    stems: FrozenSet[str]
    prefix: str
    start_label: str
    end_label: str
    group_label: Optional[str] = None
    raw_run_count: int = 1


@dataclass
class XmlRunData:
    raw_runs: List[CameraRun]
    merged_runs: List[CameraRun]
    camera_id_to_stem: Dict[int, str]
    camera_id_to_sensor: Dict[int, int]
    sensor_labels: Dict[int, str]
    sensor_calibrations: Dict[int, dict]
    equisolid_sensor_ids: Set[int]
    aligned_camera_ids: Set[int]


@dataclass
class LensAssignment:
    lens_label: str
    camera_ids: Tuple[int, ...]
    strategy: str
    confidence: str
    message: str
    detail: Optional[str] = None


@dataclass
class MappingCandidate:
    """One possible lens-to-camera assignment. For future PLY scoring."""
    spec: str
    assignments: List[LensAssignment]
    score: Optional[float] = None
    score_method: Optional[str] = None


@dataclass
class MappingResult:
    spec: str
    assignments: List[LensAssignment]
    xml_data: XmlRunData
    diagnostics: List[MappingDiagnostic] = field(default_factory=list)
    stems_are_duplicate: bool = False
    candidate_specs: List[MappingCandidate] = field(default_factory=list)

    @property
    def weakest_confidence(self) -> str:
        if not self.assignments:
            return HEURISTIC
        return (
            STRONG if all(a.confidence == STRONG for a in self.assignments)
            else HEURISTIC
        )

    @property
    def can_auto_export(self) -> bool:
        return (
            self.weakest_confidence == STRONG
            and not any(d.severity == BLOCKER for d in self.diagnostics)
        )


@dataclass
class ManualMapValidation:
    spec: str
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ── Helpers ─────────────────────────────────────────────────────────────


def _stem_key(value: str) -> str:
    """Normalize a camera label or filename to a comparable stem key."""
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    return Path(name).stem.casefold()


def format_camera_ids(ids: Sequence[int]) -> str:
    """Format a sequence of camera IDs as compact ranges."""
    values = sorted(set(int(v) for v in ids))
    if not values:
        return ""
    ranges: List[str] = []
    start = prev = values[0]
    for v in values[1:]:
        if v == prev + 1:
            prev = v
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = v
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return " ".join(ranges)


def _is_equisolid(sensor_elem, calibration_elem) -> bool:
    """Check whether a Metashape sensor element is equisolid fisheye."""
    sensor_type = str(sensor_elem.attrib.get("type", "")).lower()
    cal_type = str(
        calibration_elem.attrib.get("type", "")
        if calibration_elem is not None else ""
    ).lower()
    combined = f"{sensor_type} {cal_type}"
    return "equisolid" in combined and "fisheye" in combined


# ── Token matching ──────────────────────────────────────────────────────

_GENERIC_TOKENS = frozenset({
    "camera", "cam", "lens", "sensor", "fisheye",
    "equisolid", "calibration", "cal",
})


def tokenize_label(label: str) -> Set[str]:
    """Split a label into lowercase tokens on camelCase, alpha/digit,
    and separator boundaries. Numeric tokens are literal (01 != 1)."""
    # Split camelCase: "OsmoFront" -> "Osmo Front"
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", label)
    # Split alpha/digit: "Osmo360" -> "Osmo 360"
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    # Split on separators, then lowercase.
    parts = re.split(r"[^A-Za-z0-9]+", text.lower())
    return {p for p in parts if p}


def discriminating_tokens(labels: List[str]) -> List[Set[str]]:
    """Remove tokens shared by ALL labels. If that would leave any label
    with zero tokens, return full token sets (no reduction)."""
    token_sets = [tokenize_label(label) for label in labels]
    if len(token_sets) < 2:
        return token_sets
    common = set.intersection(*token_sets)
    reduced = [ts - common for ts in token_sets]
    if any(len(ts) == 0 for ts in reduced):
        return token_sets
    return reduced


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / max(denominator, 1)


# ── Duplicate-stem detection ────────────────────────────────────────────


def detect_stem_overlap(
    lens_jobs: Sequence[LensJob],
) -> Tuple[bool, List[MappingDiagnostic]]:
    """Detect cross-lens stem overlap. Returns (stems_are_duplicate, diagnostics).

    Two tiers:
    - >50% overlap: stems_are_duplicate=True, INFO diagnostic.
    - >5% overlap: WARNING diagnostic.
    """
    stems_are_duplicate = False
    diagnostics: List[MappingDiagnostic] = []
    for a, b in itertools.combinations(lens_jobs, 2):
        overlap = a.stems & b.stems
        if not overlap:
            continue
        ratio_a = len(overlap) / max(len(a.stems), 1)
        ratio_b = len(overlap) / max(len(b.stems), 1)
        max_ratio = max(ratio_a, ratio_b)
        samples = sorted(overlap)[:5]

        if max_ratio > 0.5:
            stems_are_duplicate = True
            diagnostics.append(MappingDiagnostic(
                severity=INFO,
                code="stems_not_discriminating",
                message=(
                    f"Source stems overlap {max_ratio:.0%} between "
                    f"{a.lens_label} and {b.lens_label}. "
                    f"Stem matching cannot verify front/back assignment. "
                    f"Sample duplicates: {samples}"
                ),
            ))
        elif max_ratio > 0.05:
            diagnostics.append(MappingDiagnostic(
                severity=WARNING,
                code="stems_overlap",
                message=(
                    f"Source stems overlap {max_ratio:.0%} between "
                    f"{a.lens_label} and {b.lens_label}. "
                    f"Sample duplicates: {samples}"
                ),
            ))
    return stems_are_duplicate, diagnostics


# ── XML parsing ─────────────────────────────────────────────────────────


def parse_xml_runs(xml_path: Path) -> XmlRunData:
    """Parse a Metashape cameras.xml into structured run data.

    Returns raw runs, merged runs, and per-camera lookup dicts.
    Only includes aligned equisolid fisheye cameras in runs.
    Lookups cover ALL cameras in the XML (for manual validation).
    """
    root = ET.parse(str(xml_path)).getroot()

    # ── Sensors ──
    equisolid_sensor_ids: Set[int] = set()
    sensor_labels: Dict[int, str] = {}
    sensor_calibrations: Dict[int, dict] = {}

    for sensor in root.findall(".//sensor"):
        sid = int(sensor.attrib["id"])
        slabel = sensor.attrib.get("label", "")
        sensor_labels[sid] = slabel

        calibration = sensor.find("calibration")
        if calibration is None:
            continue

        if _is_equisolid(sensor, calibration):
            equisolid_sensor_ids.add(sid)

        res = calibration.find("resolution")
        if res is None:
            res = sensor.find("resolution")
        if res is None:
            continue

        f_text = calibration.findtext("f")
        cal_dict: dict = {
            "width": int(res.attrib.get("width", 0)),
            "height": int(res.attrib.get("height", 0)),
            "f": float(f_text) if f_text else None,
            "type": (
                sensor.attrib.get("type", "") + " " +
                calibration.attrib.get("type", "")
            ).strip(),
        }
        # Distortion parameters.
        for tag in ("k1", "k2", "k3", "k4", "p1", "p2", "b1", "b2"):
            val = calibration.findtext(tag)
            if val is not None:
                cal_dict[tag] = float(val)
        sensor_calibrations[sid] = cal_dict

    # ── Camera groups ──
    group_labels_map: Dict[str, str] = {}
    for group in root.findall(".//cameras/group"):
        gid = group.attrib.get("id")
        glabel = group.attrib.get("label", "")
        if gid is not None:
            group_labels_map[gid] = glabel

    # ── Cameras ──
    camera_id_to_stem: Dict[int, str] = {}
    camera_id_to_sensor: Dict[int, int] = {}
    aligned_camera_ids: Set[int] = set()
    equisolid_cameras: List[dict] = []

    for camera in root.findall(".//camera"):
        cam_id = int(camera.attrib["id"])
        cam_sid = int(camera.attrib.get("sensor_id", "-1"))
        cam_label = camera.attrib.get("label", "")
        transform = (camera.findtext("transform") or "").split()
        aligned = len(transform) == 16
        gid = camera.attrib.get("group_id")

        stem = _stem_key(cam_label)
        camera_id_to_stem[cam_id] = stem
        camera_id_to_sensor[cam_id] = cam_sid

        if aligned:
            aligned_camera_ids.add(cam_id)

        if cam_sid in equisolid_sensor_ids and aligned:
            equisolid_cameras.append({
                "id": cam_id,
                "sensor_id": cam_sid,
                "label": cam_label,
                "stem": stem,
                "group_label": group_labels_map.get(gid) if gid else None,
            })

    equisolid_cameras.sort(key=lambda c: c["id"])

    # ── Build runs ──
    raw_runs = _build_runs(equisolid_cameras, sensor_labels)
    merged_runs = _merge_runs(raw_runs)

    return XmlRunData(
        raw_runs=raw_runs,
        merged_runs=merged_runs,
        camera_id_to_stem=camera_id_to_stem,
        camera_id_to_sensor=camera_id_to_sensor,
        sensor_labels=sensor_labels,
        sensor_calibrations=sensor_calibrations,
        equisolid_sensor_ids=equisolid_sensor_ids,
        aligned_camera_ids=aligned_camera_ids,
    )


def _split_label(label: str) -> Tuple[str, Optional[int]]:
    match = re.match(r"^(?P<prefix>.*?)(?P<number>\d+)$", label)
    if not match:
        return label, None
    return match.group("prefix"), int(match.group("number"))


def _build_runs(
    cameras: List[dict],
    sensor_labels: Dict[int, str],
) -> List[CameraRun]:
    """Split a sorted list of equisolid cameras into contiguous runs."""
    runs: List[CameraRun] = []
    current: List[dict] = []
    current_sensor_id: Optional[int] = None
    current_prefix: Optional[str] = None
    last_number: Optional[int] = None

    def flush() -> None:
        if not current:
            return
        labels = [c["label"] for c in current]
        sid = current[0]["sensor_id"]
        group_set = {c["group_label"] for c in current if c["group_label"]}
        runs.append(CameraRun(
            ids=tuple(c["id"] for c in current),
            sensor_id=sid,
            sensor_label=sensor_labels.get(sid, ""),
            stems=frozenset(c["stem"] for c in current),
            prefix=current_prefix or "",
            start_label=labels[0],
            end_label=labels[-1],
            group_label=group_set.pop() if len(group_set) == 1 else None,
        ))

    for cam in cameras:
        prefix, number = _split_label(str(cam["label"]))
        continues = (
            current
            and cam["sensor_id"] == current_sensor_id
            and prefix == current_prefix
            and number is not None
            and last_number is not None
            and number == last_number + 1
        )
        if not continues:
            flush()
            current = []
        current.append(cam)
        current_sensor_id = cam["sensor_id"]
        current_prefix = prefix
        last_number = number
    flush()
    return runs


def _merge_runs(runs: List[CameraRun]) -> List[CameraRun]:
    """Merge runs sharing the same (sensor_id, prefix, group_label) key.

    Only merges when the key produces multiple distinct buckets. If all runs
    share a single key, returns copies of the raw runs so the ID-gap
    partition can still split them.
    """
    if not runs:
        return []
    buckets: Dict[tuple, List[CameraRun]] = {}
    for run in runs:
        key = (run.sensor_id, run.prefix, run.group_label)
        buckets.setdefault(key, []).append(run)
    if len(buckets) < 2:
        return [CameraRun(
            ids=run.ids,
            sensor_id=run.sensor_id,
            sensor_label=run.sensor_label,
            stems=run.stems,
            prefix=run.prefix,
            start_label=run.start_label,
            end_label=run.end_label,
            group_label=run.group_label,
            raw_run_count=1,
        ) for run in runs]
    merged: List[CameraRun] = []
    for _key, group in buckets.items():
        group.sort(key=lambda r: r.ids[0])
        all_ids: List[int] = []
        all_stems: Set[str] = set()
        for run in group:
            all_ids.extend(run.ids)
            all_stems.update(run.stems)
        merged.append(CameraRun(
            ids=tuple(sorted(all_ids)),
            sensor_id=group[0].sensor_id,
            sensor_label=group[0].sensor_label,
            stems=frozenset(all_stems),
            prefix=group[0].prefix,
            start_label=group[0].start_label,
            end_label=group[-1].end_label,
            group_label=group[0].group_label,
            raw_run_count=len(group),
        ))
    merged.sort(key=lambda r: r.ids[0])
    return merged


# ── Resolver ────────────────────────────────────────────────────────────


def resolve_mapping(
    xml_data: XmlRunData,
    lens_jobs: List[LensJob],
) -> MappingResult:
    """Build a mapping proposal with confidence levels and diagnostics.

    Runtime matching priority:
      0a. Group label
      0b. Sensor ID
       1. Exact stem
       2. Subset stem (with ID-gap partition for overlaps)
       3. Count fallback
    """
    runs = xml_data.merged_runs
    if not runs:
        raise ValueError("No aligned equisolid fisheye camera runs found in the Metashape XML")
    if not lens_jobs:
        raise ValueError("No lens jobs provided")

    diagnostics: List[MappingDiagnostic] = []

    # Duplicate-stem detection (early, affects all strategies).
    stems_are_duplicate, stem_diags = detect_stem_overlap(lens_jobs)
    diagnostics.extend(stem_diags)

    assignments: Dict[str, LensAssignment] = {}
    used_run_indexes: Set[int] = set()
    remaining_jobs: List[LensJob] = list(lens_jobs)

    # ── Step 0a: Group-label matching ──
    remaining_jobs, step_assignments, step_diags = _match_by_group_label(
        runs, remaining_jobs, used_run_indexes, stems_are_duplicate,
    )
    assignments.update(step_assignments)
    diagnostics.extend(step_diags)

    # ── Step 0b: Sensor-ID partition ──
    remaining_jobs, step_assignments, step_diags = _match_by_sensor_id(
        runs, remaining_jobs, used_run_indexes, xml_data, stems_are_duplicate,
    )
    assignments.update(step_assignments)
    diagnostics.extend(step_diags)

    # ── Step 1: Exact stem match ──
    remaining_jobs, step_assignments, step_diags = _match_exact_stem(
        runs, remaining_jobs, used_run_indexes, stems_are_duplicate,
    )
    assignments.update(step_assignments)
    diagnostics.extend(step_diags)

    # ── Step 2: Subset stem match ──
    remaining_jobs, step_assignments, step_diags = _match_subset_stem(
        runs, remaining_jobs, used_run_indexes, stems_are_duplicate,
    )
    assignments.update(step_assignments)
    diagnostics.extend(step_diags)

    # ── Step 3: Count fallback ──
    remaining_jobs, step_assignments, step_diags = _match_by_count(
        runs, remaining_jobs, used_run_indexes,
    )
    assignments.update(step_assignments)
    diagnostics.extend(step_diags)

    if remaining_jobs:
        unmatched = ", ".join(j.lens_label for j in remaining_jobs)
        raise ValueError(
            f"Automatic mapping failed for: {unmatched}. "
            f"Enable the advanced manual map and choose camera id ranges."
        )

    # Build spec in original lens order.
    ordered_assignments = [assignments[j.lens_label] for j in lens_jobs]
    spec = ",".join(
        f"{a.lens_label}={format_camera_ids(a.camera_ids)}"
        for a in ordered_assignments
    )
    diagnostics.extend(
        _assignment_diagnostics(xml_data, lens_jobs, ordered_assignments)
    )

    return MappingResult(
        spec=spec,
        assignments=ordered_assignments,
        xml_data=xml_data,
        diagnostics=diagnostics,
        stems_are_duplicate=stems_are_duplicate,
    )


# ── Matching strategies ─────────────────────────────────────────────────


def _solve_candidate_groups(
    candidates_per_job: List[Tuple[LensJob, List[FrozenSet[int]]]],
    limit: int = 2,
) -> List[Dict[str, FrozenSet[int]]]:
    """Return up to ``limit`` complete disjoint assignments.

    Candidates are sets of run indexes because one lens may legitimately map
    to multiple fragmented runs.
    """
    ordered = sorted(candidates_per_job, key=lambda item: len(item[1]))
    solutions: List[Dict[str, FrozenSet[int]]] = []

    def walk(
        index: int,
        used_indexes: FrozenSet[int],
        current: Dict[str, FrozenSet[int]],
    ) -> None:
        if len(solutions) >= limit:
            return
        if index == len(ordered):
            solutions.append(dict(current))
            return
        job, candidates = ordered[index]
        for candidate in candidates:
            if not candidate or candidate & used_indexes:
                continue
            current[job.lens_label] = candidate
            walk(index + 1, used_indexes | candidate, current)
            current.pop(job.lens_label, None)

    walk(0, frozenset(), {})
    return solutions


def _add_duplicate_stem_blocker(
    diags: List[MappingDiagnostic],
    strategy_name: str,
    lens_labels: Sequence[str],
) -> None:
    labels = ", ".join(lens_labels)
    diags.append(MappingDiagnostic(
        severity=BLOCKER,
        code="duplicate_stems",
        message=(
            f"{strategy_name} matched {labels}, but source stems are "
            f"duplicated across lenses, so stem identity cannot verify assignment."
        ),
    ))


def _match_by_group_label(
    runs: List[CameraRun],
    jobs: List[LensJob],
    used: Set[int],
    stems_are_duplicate: bool,
) -> Tuple[List[LensJob], Dict[str, LensAssignment], List[MappingDiagnostic]]:
    """Step 0a: Match runs to lenses by camera group labels.

    All-or-nothing: accept only when every job matches a unique set of runs
    by discriminating tokens.
    """
    assignments: Dict[str, LensAssignment] = {}
    diags: List[MappingDiagnostic] = []

    has_groups = any(
        run.group_label
        for i, run in enumerate(runs)
        if i not in used
    )
    if not has_groups or len(jobs) < 2:
        return jobs, assignments, diags

    lens_labels = [j.lens_label for j in jobs]
    disc = discriminating_tokens(lens_labels)

    # Build run group labels' discriminating tokens.
    # For each job, find runs whose group label has overlapping discriminating
    # tokens with this job but NOT with other jobs.
    candidates: Dict[str, List[int]] = {}
    for ji, job in enumerate(jobs):
        my_tokens = disc[ji] - _GENERIC_TOKENS
        other_tokens: Set[str] = set()
        for oi, ts in enumerate(disc):
            if oi != ji:
                other_tokens |= (ts - _GENERIC_TOKENS)
        matched: List[int] = []
        for ri, run in enumerate(runs):
            if ri in used or not run.group_label:
                continue
            run_tokens = tokenize_label(run.group_label) - _GENERIC_TOKENS
            if my_tokens and (my_tokens & run_tokens) and not (other_tokens & run_tokens):
                matched.append(ri)
        if matched:
            candidates[job.lens_label] = matched

    # Accept only if every job matched and no run is claimed twice.
    if len(candidates) != len(jobs):
        if candidates:
            diags.append(MappingDiagnostic(
                severity=INFO,
                code="group_labels_ambiguous",
                message="Group labels found but could not be matched uniquely to all lenses.",
            ))
        return jobs, assignments, diags

    all_claimed: List[int] = []
    for idxs in candidates.values():
        all_claimed.extend(idxs)
    if len(all_claimed) != len(set(all_claimed)):
        diags.append(MappingDiagnostic(
            severity=INFO,
            code="group_labels_ambiguous",
            message="Group labels are ambiguous — runs claimed by multiple lenses.",
        ))
        return jobs, assignments, diags

    # Assign.
    for job in jobs:
        run_idxs = candidates[job.lens_label]
        ids: List[int] = []
        for ri in run_idxs:
            ids.extend(runs[ri].ids)
            used.add(ri)
        assignments[job.lens_label] = LensAssignment(
            lens_label=job.lens_label,
            camera_ids=tuple(sorted(ids)),
            strategy="group_label",
            confidence=STRONG,
            message=(
                f"matched {len(ids)} cameras from {len(run_idxs)} run(s) "
                f"by camera group label"
            ),
        )

    return [], assignments, diags


def _match_by_sensor_id(
    runs: List[CameraRun],
    jobs: List[LensJob],
    used: Set[int],
    xml_data: XmlRunData,
    stems_are_duplicate: bool,
) -> Tuple[List[LensJob], Dict[str, LensAssignment], List[MappingDiagnostic]]:
    """Step 0b: Partition runs by sensor ID, then try to confirm the
    assignment via sensor labels or unique stems."""
    assignments: Dict[str, LensAssignment] = {}
    diags: List[MappingDiagnostic] = []

    if len(jobs) < 2:
        return jobs, assignments, diags

    # Build sensor buckets from remaining runs.
    sensor_buckets: Dict[int, List[int]] = {}
    for i, run in enumerate(runs):
        if i not in used:
            sensor_buckets.setdefault(run.sensor_id, []).append(i)

    if len(sensor_buckets) != len(jobs) or len(sensor_buckets) < 2:
        return jobs, assignments, diags

    sensor_ids_sorted = sorted(sensor_buckets.keys())
    lens_labels = [j.lens_label for j in jobs]
    disc = discriminating_tokens(lens_labels)

    # Try sensor-label confirmation.
    sensor_to_job_idx: Dict[int, int] = {}
    for sid in sensor_ids_sorted:
        slabel = xml_data.sensor_labels.get(sid, "")
        if not slabel:
            continue
        slabel_tokens = tokenize_label(slabel) - _GENERIC_TOKENS
        if not slabel_tokens:
            continue
        matches = []
        for ji, ts in enumerate(disc):
            job_tokens = ts - _GENERIC_TOKENS
            if job_tokens and (job_tokens & slabel_tokens):
                matches.append(ji)
        if len(matches) == 1:
            sensor_to_job_idx[sid] = matches[0]

    # Check one-to-one.
    label_confirmed = (
        len(sensor_to_job_idx) == len(sensor_ids_sorted)
        and len(set(sensor_to_job_idx.values())) == len(jobs)
    )

    # Try unique stem confirmation (only if stems are discriminating).
    stem_confirmed = False
    stem_to_job: Dict[int, int] = {}
    if not label_confirmed and not stems_are_duplicate:
        for sid, run_idxs in sensor_buckets.items():
            bucket_stems: Set[str] = set()
            for ri in run_idxs:
                bucket_stems |= runs[ri].stems
            matching_jobs = [
                ji for ji, job in enumerate(jobs)
                if bucket_stems <= job.stems
            ]
            if len(matching_jobs) == 1:
                stem_to_job[sid] = matching_jobs[0]
        stem_confirmed = (
            len(stem_to_job) == len(sensor_ids_sorted)
            and len(set(stem_to_job.values())) == len(jobs)
        )

    if label_confirmed:
        confirmed_map = sensor_to_job_idx
        confidence = STRONG
        strategy_suffix = "sensor label"
    elif stem_confirmed:
        confirmed_map = stem_to_job
        confidence = STRONG
        strategy_suffix = "sensor ID + unique stems"
    else:
        # Unconfirmed: assign by sorted sensor ID order.
        confirmed_map = {sid: ji for ji, sid in enumerate(sensor_ids_sorted)}
        confidence = HEURISTIC
        strategy_suffix = "sensor ID (order unverified)"
        diags.append(MappingDiagnostic(
            severity=BLOCKER,
            code="sensor_order_unverified",
            message=(
                "Sensor IDs partition the cameras but lens order is unverified. "
                "Accept the proposed mapping or use manual map."
            ),
        ))

    if not stems_are_duplicate:
        diags.extend(_sensor_stem_contradiction_diagnostics(
            runs, jobs, sensor_buckets, confirmed_map,
        ))

    for sid in sensor_ids_sorted:
        ji = confirmed_map[sid]
        job = jobs[ji]
        run_idxs = sensor_buckets[sid]
        ids: List[int] = []
        for ri in run_idxs:
            ids.extend(runs[ri].ids)
            used.add(ri)
        assignments[job.lens_label] = LensAssignment(
            lens_label=job.lens_label,
            camera_ids=tuple(sorted(ids)),
            strategy=f"sensor_id_{strategy_suffix}",
            confidence=confidence,
            message=(
                f"matched {len(ids)} cameras from {len(run_idxs)} run(s) "
                f"by {strategy_suffix}"
            ),
        )

    return [], assignments, diags


def _sensor_stem_contradiction_diagnostics(
    runs: List[CameraRun],
    jobs: List[LensJob],
    sensor_buckets: Dict[int, List[int]],
    confirmed_map: Dict[int, int],
) -> List[MappingDiagnostic]:
    diags: List[MappingDiagnostic] = []
    if len(jobs) < 2:
        return diags

    for sid, assigned_job_idx in confirmed_map.items():
        bucket_stems: Set[str] = set()
        for ri in sensor_buckets.get(sid, []):
            bucket_stems |= runs[ri].stems
        if not bucket_stems:
            continue

        assigned_job = jobs[assigned_job_idx]
        assigned_rate = _ratio(
            len(bucket_stems & assigned_job.stems),
            len(bucket_stems),
        )
        other_scores = [
            (
                _ratio(len(bucket_stems & job.stems), len(bucket_stems)),
                job,
            )
            for ji, job in enumerate(jobs)
            if ji != assigned_job_idx
        ]
        if not other_scores:
            continue
        best_other_rate, best_other_job = max(
            other_scores, key=lambda item: item[0]
        )
        if best_other_rate >= 0.8 and assigned_rate < 0.5:
            severity = BLOCKER
        elif best_other_rate >= assigned_rate + 0.2:
            severity = WARNING
        else:
            continue
        diags.append(MappingDiagnostic(
            severity=severity,
            code="sensor_stem_contradiction",
            message=(
                f"Sensor {sid} was assigned to {assigned_job.lens_label}, "
                f"but its camera labels match {best_other_job.lens_label} "
                f"better ({best_other_rate:.0%} vs {assigned_rate:.0%})."
            ),
        ))
    return diags


def _match_exact_stem(
    runs: List[CameraRun],
    jobs: List[LensJob],
    used: Set[int],
    stems_are_duplicate: bool,
) -> Tuple[List[LensJob], Dict[str, LensAssignment], List[MappingDiagnostic]]:
    """Step 1: Exact stem match — global candidate solving."""
    assignments: Dict[str, LensAssignment] = {}
    diags: List[MappingDiagnostic] = []

    if not jobs:
        return jobs, assignments, diags

    # Build candidates for all jobs before assigning.
    candidates_per_job: List[Tuple[LensJob, List[FrozenSet[int]]]] = []
    for job in jobs:
        matched = [
            frozenset({i}) for i, run in enumerate(runs)
            if i not in used
            and run.stems == job.stems
            and len(run.ids) == len(job.stems)
        ]
        candidates_per_job.append((job, matched))

    if all(candidates for _, candidates in candidates_per_job):
        solutions = _solve_candidate_groups(candidates_per_job)
    else:
        solutions = []

    if len(solutions) == 1:
        solution = solutions[0]
        confidence = HEURISTIC if stems_are_duplicate else STRONG
        assigned_labels: List[str] = []
        for job in jobs:
            ri = next(iter(solution[job.lens_label]))
            run = runs[ri]
            used.add(ri)
            assigned_labels.append(job.lens_label)
            assignments[job.lens_label] = LensAssignment(
                lens_label=job.lens_label,
                camera_ids=run.ids,
                strategy="exact_stem",
                confidence=confidence,
                message=f"matched {len(run.ids)} cameras by exact filename",
            )
        if stems_are_duplicate:
            _add_duplicate_stem_blocker(diags, "Exact stem matching", assigned_labels)
        return [], assignments, diags

    if len(solutions) > 1:
        return jobs, assignments, diags

    # Some jobs have 0 or >1 candidates — defer them.
    remaining = [job for job, matched in candidates_per_job if not matched or len(matched) != 1]
    # Assign jobs with exactly one unique candidate that isn't claimed by others.
    claimed_counts: Dict[FrozenSet[int], int] = {}
    for _, matched in candidates_per_job:
        for candidate in matched:
            claimed_counts[candidate] = claimed_counts.get(candidate, 0) + 1
    assigned_labels = []
    for job, matched in candidates_per_job:
        if len(matched) == 1 and claimed_counts[matched[0]] == 1 and job not in remaining:
            ri = next(iter(matched[0]))
            run = runs[ri]
            used.add(ri)
            confidence = HEURISTIC if stems_are_duplicate else STRONG
            assigned_labels.append(job.lens_label)
            assignments[job.lens_label] = LensAssignment(
                lens_label=job.lens_label,
                camera_ids=run.ids,
                strategy="exact_stem",
                confidence=confidence,
                message=f"matched {len(run.ids)} cameras by exact filename",
            )

    if stems_are_duplicate and assigned_labels:
        _add_duplicate_stem_blocker(diags, "Exact stem matching", assigned_labels)

    return remaining, assignments, diags


def _match_subset_stem(
    runs: List[CameraRun],
    jobs: List[LensJob],
    used: Set[int],
    stems_are_duplicate: bool,
) -> Tuple[List[LensJob], Dict[str, LensAssignment], List[MappingDiagnostic]]:
    """Step 2: Subset stem match — global candidate solving.

    Each job's candidate is the set of run indexes whose stems are subsets
    of the job's source stems. If candidate sets are disjoint, assign.
    If they overlap, delegate to ID-gap partition.
    """
    assignments: Dict[str, LensAssignment] = {}
    diags: List[MappingDiagnostic] = []

    if not jobs:
        return jobs, assignments, diags

    # Build candidate sets: for each job, the set of run indexes
    # whose stems are subsets of this job's stems.
    candidates_per_job: List[Tuple[LensJob, FrozenSet[int]]] = []
    for job in jobs:
        matched = frozenset(
            i for i, run in enumerate(runs)
            if i not in used and run.stems <= job.stems
        )
        candidates_per_job.append((job, matched))

    # Check for disjoint candidate sets.
    all_matched_sets = [m for _, m in candidates_per_job if m]
    overlap = False
    seen_runs: Set[int] = set()
    for m in all_matched_sets:
        if seen_runs & m:
            overlap = True
            break
        seen_runs |= m

    if not overlap:
        # Disjoint: assign each job its candidate set.
        remaining = []
        for job, matched in candidates_per_job:
            if not matched:
                remaining.append(job)
                continue
            ids: List[int] = []
            for ri in sorted(matched):
                ids.extend(runs[ri].ids)
                used.add(ri)
            confidence = HEURISTIC if stems_are_duplicate else STRONG
            assignments[job.lens_label] = LensAssignment(
                lens_label=job.lens_label,
                camera_ids=tuple(sorted(ids)),
                strategy="subset_stem",
                confidence=confidence,
                message=(
                    f"matched {len(ids)} cameras from {len(matched)} run(s) "
                    f"by stem subset"
                ),
            )
            if stems_are_duplicate:
                diags.append(MappingDiagnostic(
                    severity=BLOCKER,
                    code="duplicate_stems",
                    message=(
                        f"Subset stem match for {job.lens_label} used but "
                        f"stems are duplicated across lenses."
                    ),
                ))
        return remaining, assignments, diags

    # Overlap: delegate to ID-gap partition (implemented in slice 6).
    remaining, gap_assignments, gap_diags = _resolve_overlap_by_id_gap(
        runs, jobs, candidates_per_job, used, stems_are_duplicate,
    )
    assignments.update(gap_assignments)
    diags.extend(gap_diags)
    return remaining, assignments, diags


def _resolve_overlap_by_id_gap(
    runs: List[CameraRun],
    jobs: List[LensJob],
    candidates_per_job: List[Tuple[LensJob, FrozenSet[int]]],
    used: Set[int],
    stems_are_duplicate: bool,
) -> Tuple[List[LensJob], Dict[str, LensAssignment], List[MappingDiagnostic]]:
    """Resolve overlapping subset-stem candidates by ID-gap partition."""
    # Collect all contested run indexes.
    contested: Set[int] = set()
    for _, matched in candidates_per_job:
        contested |= matched
    sorted_contested = sorted(contested, key=lambda i: runs[i].ids[0])

    if len(sorted_contested) < len(jobs):
        return list(j for j, _ in candidates_per_job), {}, []

    # Find gaps between consecutive runs.
    gaps: List[Tuple[int, int]] = []  # (gap_size, position)
    for pos in range(1, len(sorted_contested)):
        prev_last = runs[sorted_contested[pos - 1]].ids[-1]
        curr_first = runs[sorted_contested[pos]].ids[0]
        gaps.append((curr_first - prev_last, pos))
    gaps.sort(reverse=True)

    n_splits = len(jobs) - 1
    if len(gaps) < n_splits:
        return list(j for j, _ in candidates_per_job), {}, []

    # Quality checks.
    diags: List[MappingDiagnostic] = []
    selected = gaps[:n_splits]
    unselected = gaps[n_splits:]

    if unselected:
        smallest_selected = selected[-1][0]
        largest_unselected = unselected[0][0]
        if smallest_selected == largest_unselected:
            diags.append(MappingDiagnostic(
                severity=WARNING,
                code="gap_tie",
                message="Exact tie at ID-gap selection boundary.",
            ))
        elif smallest_selected < largest_unselected * 2:
            diags.append(MappingDiagnostic(
                severity=WARNING,
                code="gap_tie",
                message=(
                    f"ID-gap split is weak: smallest selected gap "
                    f"({smallest_selected}) is close to largest unselected "
                    f"gap ({largest_unselected})."
                ),
            ))

    # Split into groups.
    split_positions = sorted(pos for _, pos in selected)
    groups: List[List[int]] = []
    prev = 0
    for sp in split_positions:
        groups.append(sorted_contested[prev:sp])
        prev = sp
    groups.append(sorted_contested[prev:])

    if len(groups) != len(jobs):
        return list(j for j, _ in candidates_per_job), {}, diags

    selected_by_position = sorted(selected, key=lambda item: item[1])
    selected_gap_parts = []
    for gap_size, pos in selected_by_position:
        left = runs[sorted_contested[pos - 1]].ids[-1]
        right = runs[sorted_contested[pos]].ids[0]
        selected_gap_parts.append(f"{gap_size} between ids {left} and {right}")
    unselected_gap_parts = []
    for gap_size, pos in unselected[:3]:
        left = runs[sorted_contested[pos - 1]].ids[-1]
        right = runs[sorted_contested[pos]].ids[0]
        unselected_gap_parts.append(f"{gap_size} between ids {left} and {right}")

    assignments: Dict[str, LensAssignment] = {}
    group_detail_parts = []
    for job, group in zip(jobs, groups):
        ids: List[int] = []
        for ri in group:
            ids.extend(runs[ri].ids)
            used.add(ri)
        cam_ids = tuple(sorted(ids))

        # Group count vs source count check.
        ratio = len(cam_ids) / max(len(job.stems), 1)
        if ratio < 0.3 or ratio > 1.5:
            diags.append(MappingDiagnostic(
                severity=WARNING,
                code="group_count_mismatch",
                message=(
                    f"{job.lens_label}: {len(cam_ids)} assigned cameras vs "
                    f"{len(job.stems)} source images ({ratio:.0%})."
                ),
            ))

        group_detail_parts.append(f"{job.lens_label}={format_camera_ids(cam_ids)}")
        assignments[job.lens_label] = LensAssignment(
            lens_label=job.lens_label,
            camera_ids=cam_ids,
            strategy="id_gap_partition",
            confidence=HEURISTIC,
            message=(
                f"matched {len(cam_ids)} cameras from {len(group)} run(s) "
                f"by stem subset + ID-gap partition"
            ),
            detail="",
        )

    gap_detail = "selected gaps: " + "; ".join(selected_gap_parts)
    if unselected_gap_parts:
        gap_detail += "; largest unselected gaps: " + "; ".join(unselected_gap_parts)
    gap_detail += "; groups: " + ", ".join(group_detail_parts)
    for assignment in assignments.values():
        assignment.detail = gap_detail

    diags.append(MappingDiagnostic(
        severity=BLOCKER,
        code="id_gap_order_unverified",
        message=(
            f"ID-gap partition: {gap_detail}. "
            f"Front/back assignment is unverified."
        ),
    ))

    return [], assignments, diags


def _match_by_count(
    runs: List[CameraRun],
    jobs: List[LensJob],
    used: Set[int],
) -> Tuple[List[LensJob], Dict[str, LensAssignment], List[MappingDiagnostic]]:
    """Step 3: Count-based fallback with solution enumeration.
    Always heuristic. Always blocked for auto-export."""
    assignments: Dict[str, LensAssignment] = {}
    diags: List[MappingDiagnostic] = []

    if not jobs:
        return jobs, assignments, diags

    remaining_runs = [
        (i, run) for i, run in enumerate(runs)
        if i not in used
    ]

    # Build candidate list per job: runs with matching count.
    candidates_per_job = [
        (job, [(i, run) for i, run in remaining_runs if len(run.ids) == len(job.stems)])
        for job in jobs
    ]

    # Enumerate perfect matchings, stop at 2.
    solutions = list(itertools.islice(
        _enumerate_count_matchings(candidates_per_job), 2
    ))

    if len(solutions) == 0:
        # No count match — cannot resolve.
        details = [
            f"{job.lens_label} matched {len(matches)} run(s) by count"
            for job, matches in candidates_per_job
        ]
        raise ValueError(
            "Automatic fisheye pose mapping is ambiguous: "
            + "; ".join(details)
            + ". Enable the advanced manual map and choose camera id ranges."
        )

    if len(solutions) == 1:
        solution = solutions[0]
        message_suffix = "unique count (count is not identity proof)"
        diags.append(MappingDiagnostic(
            severity=BLOCKER,
            code="count_order_unverified",
            message="Count-based matching is not identity proof.",
        ))
    else:
        # Multiple solutions — assign by camera-ID order as arbitrary proposal.
        ordered = sorted(remaining_runs, key=lambda item: item[1].ids[0])
        solution = {}
        for job in jobs:
            for i, run in ordered:
                if len(run.ids) == len(job.stems) and i not in solution.values():
                    solution[job.lens_label] = i
                    break
        message_suffix = "count (2+ possible assignments, order is arbitrary)"
        diags.append(MappingDiagnostic(
            severity=BLOCKER,
            code="count_multi_solution",
            message=(
                "Count-based matching found 2+ possible assignments. "
                "Order is arbitrary."
            ),
        ))

    for job in jobs:
        ri = solution.get(job.lens_label)
        if ri is None:
            continue
        run = runs[ri]
        used.add(ri)
        assignments[job.lens_label] = LensAssignment(
            lens_label=job.lens_label,
            camera_ids=run.ids,
            strategy="count_fallback",
            confidence=HEURISTIC,
            message=f"matched {len(run.ids)} cameras by {message_suffix}",
        )

    resolved_labels = set(assignments.keys())
    remaining = [j for j in jobs if j.lens_label not in resolved_labels]
    return remaining, assignments, diags


def _enumerate_count_matchings(
    candidates_per_job: List[Tuple[LensJob, List[Tuple[int, CameraRun]]]],
    depth: int = 0,
    used_indexes: FrozenSet[int] = frozenset(),
):
    """Yield all valid 1:1 count-based assignments. Caller stops at 2."""
    if depth == len(candidates_per_job):
        yield {}
        return
    job, matches = candidates_per_job[depth]
    for i, run in matches:
        if i in used_indexes:
            continue
        for rest in _enumerate_count_matchings(
            candidates_per_job, depth + 1, used_indexes | {i}
        ):
            result = dict(rest)
            result[job.lens_label] = i
            yield result


# ── Assignment diagnostics ───────────────────────────────────────────────


def _assignment_diagnostics(
    xml_data: XmlRunData,
    lens_jobs: Sequence[LensJob],
    assignments: Sequence[LensAssignment],
) -> List[MappingDiagnostic]:
    diagnostics: List[MappingDiagnostic] = []
    jobs_by_label = {job.lens_label: job for job in lens_jobs}
    for assignment in assignments:
        job = jobs_by_label.get(assignment.lens_label)
        if job is None:
            continue
        diagnostics.extend(_match_rate_diagnostics(xml_data, job, assignment))
        diagnostics.extend(_sensor_assignment_diagnostics(xml_data, job, assignment))
        diagnostics.extend(_calibration_diagnostics(xml_data, job, assignment))
    return diagnostics


def _match_rate_diagnostics(
    xml_data: XmlRunData,
    job: LensJob,
    assignment: LensAssignment,
) -> List[MappingDiagnostic]:
    assigned_stems = {
        xml_data.camera_id_to_stem[cid]
        for cid in assignment.camera_ids
        if cid in xml_data.camera_id_to_stem
    }
    matched = job.stems & assigned_stems
    rate = _ratio(len(matched), len(job.stems))
    if rate < 0.5:
        severity = BLOCKER
    elif rate < 0.8:
        severity = WARNING
    else:
        return []
    return [MappingDiagnostic(
        severity=severity,
        code="low_match_rate",
        message=(
            f"{job.lens_label}: only {len(matched)}/{len(job.stems)} "
            f"source stems matched assigned Metashape cameras ({rate:.0%})."
        ),
    )]


def _assigned_sensor_ids(
    xml_data: XmlRunData,
    assignment: LensAssignment,
) -> Set[int]:
    return {
        xml_data.camera_id_to_sensor[cid]
        for cid in assignment.camera_ids
        if cid in xml_data.camera_id_to_sensor
    }


def _sensor_assignment_diagnostics(
    xml_data: XmlRunData,
    job: LensJob,
    assignment: LensAssignment,
) -> List[MappingDiagnostic]:
    sensor_ids = _assigned_sensor_ids(xml_data, assignment)
    if len(sensor_ids) <= 1:
        return []

    cals = [
        xml_data.sensor_calibrations[sid]
        for sid in sensor_ids
        if sid in xml_data.sensor_calibrations
    ]
    dims = {
        (cal.get("width"), cal.get("height"))
        for cal in cals
        if cal.get("width") and cal.get("height")
    }
    dims_match = len(dims) <= 1
    focals = [cal.get("f") for cal in cals if cal.get("f") is not None]
    focal_spread = (
        (max(focals) - min(focals)) / max(max(focals), 1e-6)
        if len(focals) >= 2 else 0.0
    )
    severity = BLOCKER if (not dims_match or focal_spread > 0.05) else WARNING
    return [MappingDiagnostic(
        severity=severity,
        code="multi_sensor_assignment",
        message=(
            f"{job.lens_label}: assigned cameras use multiple sensor IDs "
            f"{sorted(sensor_ids)}"
            + (" with materially different calibrations" if severity == BLOCKER else "")
        ),
    )]


def _parse_lens_calibration(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    cal_path = Path(path)
    if not cal_path.is_file():
        return None
    try:
        root = ET.parse(str(cal_path)).getroot()
    except Exception:
        return None
    cal = root if root.tag == "calibration" else root.find("calibration")
    if cal is None:
        return None

    res = cal.find("resolution")
    width = height = None
    if res is not None:
        width = res.attrib.get("width")
        height = res.attrib.get("height")
    width = width or cal.findtext("width")
    height = height or cal.findtext("height")
    f_text = cal.findtext("f")
    try:
        parsed = {
            "width": int(width) if width else None,
            "height": int(height) if height else None,
            "f": float(f_text) if f_text else None,
            "type": cal.findtext("projection") or cal.attrib.get("type", ""),
        }
    except (TypeError, ValueError):
        return None
    for tag in ("k1", "k2", "k3", "k4", "p1", "p2", "b1", "b2"):
        val = cal.findtext(tag)
        if val is not None:
            try:
                parsed[tag] = float(val)
            except ValueError:
                pass
    return parsed


def _relative_focal_delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return abs(a - b) / max(abs(a), abs(b), 1e-6)


def _calibration_diagnostics(
    xml_data: XmlRunData,
    job: LensJob,
    assignment: LensAssignment,
) -> List[MappingDiagnostic]:
    lens_cal = _parse_lens_calibration(job.cal_path)
    if lens_cal is None:
        return []

    sensor_ids = _assigned_sensor_ids(xml_data, assignment)
    if not sensor_ids:
        return []

    diagnostics: List[MappingDiagnostic] = []
    for sid in sorted(sensor_ids):
        sensor_cal = xml_data.sensor_calibrations.get(sid)
        if not sensor_cal:
            continue

        lens_dims = (lens_cal.get("width"), lens_cal.get("height"))
        sensor_dims = (sensor_cal.get("width"), sensor_cal.get("height"))
        if all(lens_dims) and all(sensor_dims) and lens_dims != sensor_dims:
            diagnostics.append(MappingDiagnostic(
                severity=BLOCKER,
                code="calibration_dim_mismatch",
                message=(
                    f"{job.lens_label}: lens calibration dimensions "
                    f"{lens_dims[0]}x{lens_dims[1]} do not match assigned "
                    f"sensor {sid} dimensions {sensor_dims[0]}x{sensor_dims[1]}."
                ),
            ))

        focal_delta = _relative_focal_delta(lens_cal.get("f"), sensor_cal.get("f"))
        if focal_delta is not None and focal_delta > 0.10:
            diagnostics.append(MappingDiagnostic(
                severity=BLOCKER,
                code="calibration_focal_mismatch",
                message=(
                    f"{job.lens_label}: lens calibration focal differs from "
                    f"assigned sensor {sid} by {focal_delta:.0%}."
                ),
            ))

    candidate_cals = {
        sid: xml_data.sensor_calibrations[sid]
        for sid in xml_data.equisolid_sensor_ids
        if sid in xml_data.sensor_calibrations
    }
    if len(candidate_cals) < 2:
        return diagnostics

    focal_values = [
        cal.get("f") for cal in candidate_cals.values()
        if cal.get("f") is not None
    ]
    dims_values = {
        (cal.get("width"), cal.get("height"))
        for cal in candidate_cals.values()
    }
    if (
        len(focal_values) == len(candidate_cals)
        and len(dims_values) == 1
        and _relative_focal_delta(max(focal_values), min(focal_values)) is not None
        and _relative_focal_delta(max(focal_values), min(focal_values)) <= 0.01
    ):
        diagnostics.append(MappingDiagnostic(
            severity=INFO,
            code="calibration_not_discriminating",
            message=(
                f"{job.lens_label}: equisolid sensors have nearly identical "
                f"calibrations, so calibration cannot identify this lens."
            ),
        ))
        return diagnostics

    lens_f = lens_cal.get("f")
    if lens_f is None:
        return diagnostics

    scored = []
    for sid, cal in candidate_cals.items():
        delta = _relative_focal_delta(lens_f, cal.get("f"))
        if delta is None:
            continue
        dim_penalty = 0.0
        if (
            lens_cal.get("width")
            and lens_cal.get("height")
            and (lens_cal.get("width"), lens_cal.get("height")) != (cal.get("width"), cal.get("height"))
        ):
            dim_penalty = 1.0
        scored.append((delta + dim_penalty, delta, sid))
    if len(scored) < 2:
        return diagnostics

    scored.sort()
    best_score, _best_delta, best_sid = scored[0]
    second_score = scored[1][0]
    assigned_single = next(iter(sensor_ids)) if len(sensor_ids) == 1 else None
    if assigned_single == best_sid and second_score - best_score > 0.05:
        diagnostics.append(MappingDiagnostic(
            severity=INFO,
            code="calibration_confirms",
            message=(
                f"{job.lens_label}: calibration best matches assigned "
                f"sensor {best_sid}."
            ),
        ))
    elif assigned_single is not None and assigned_single != best_sid:
        diagnostics.append(MappingDiagnostic(
            severity=WARNING,
            code="calibration_mismatch",
            message=(
                f"{job.lens_label}: calibration is closer to sensor {best_sid} "
                f"than assigned sensor {assigned_single}."
            ),
        ))
    return diagnostics


# ── Manual map validation ───────────────────────────────────────────────


def validate_manual_map(
    spec: str,
    xml_path: Path,
    active_lens_labels: List[str],
) -> ManualMapValidation:
    """Validate a user-entered manual lens-camera map.

    Checks: syntax, required labels, extra labels, cross-lens duplicate IDs,
    camera ID existence, equisolid sensor type, aligned transforms.
    """
    if _exporter is None:
        return ManualMapValidation(
            spec=spec, valid=False,
            errors=["Cannot validate: exporter module failed to load. "
                    "Check that metashape_cameras_to_colmap.py exists."],
        )

    errors: List[str] = []
    warnings: List[str] = []

    # 1. Parse spec.
    try:
        parsed = _exporter.parse_lens_camera_map(spec)
    except _exporter.ValidationError as exc:
        return ManualMapValidation(spec=spec, valid=False, errors=[str(exc)])

    # 2. Required labels present, no extras.
    for label in active_lens_labels:
        if label not in parsed:
            errors.append(f"Missing lens label: {label}")
    for label in parsed:
        if label not in active_lens_labels:
            errors.append(
                f"Extra lens label in map: {label} "
                f"(active lenses: {', '.join(active_lens_labels)})"
            )

    # 3. Cross-lens duplicate camera IDs.
    seen: Dict[int, str] = {}
    for label, ids in parsed.items():
        for cid in ids:
            if cid in seen:
                errors.append(
                    f"Camera ID {cid} assigned to both "
                    f"{seen[cid]} and {label}"
                )
            seen[cid] = label

    # 4. Camera IDs exist in XML + domain checks.
    try:
        document = _exporter.parse_metashape_cameras_xml(xml_path)
    except Exception as exc:
        errors.append(f"Failed to parse Metashape XML: {exc}")
        return ManualMapValidation(
            spec=spec, valid=False, errors=errors, warnings=warnings
        )

    all_cameras = document["cameras"]
    sensors = document["sensors"]
    equisolid_sids = set()
    for sid, s in sensors.items():
        combined = f"{s.get('sensor_type', '')} {s.get('calibration_type', '')}".lower()
        if "equisolid" in combined and "fisheye" in combined:
            equisolid_sids.add(sid)

    for label, ids in parsed.items():
        non_existent = [cid for cid in ids if cid not in all_cameras]
        if non_existent:
            errors.append(
                f"Lens {label}: camera IDs not found in XML: "
                f"{non_existent[:5]}{'...' if len(non_existent) > 5 else ''}"
            )

        non_equisolid = [
            cid for cid in ids
            if cid in all_cameras
            and all_cameras[cid]["sensor_id"] not in equisolid_sids
        ]
        if non_equisolid:
            errors.append(
                f"Lens {label}: {len(non_equisolid)} camera(s) are not "
                f"equisolid fisheye: {non_equisolid[:5]}"
            )

        unaligned = [
            cid for cid in ids
            if cid in all_cameras
            and len(all_cameras[cid].get("transform", ())) != 16
        ]
        if unaligned:
            errors.append(
                f"Lens {label}: {len(unaligned)} camera(s) are unaligned "
                f"(no transform): {unaligned[:5]}"
            )

    return ManualMapValidation(
        spec=spec, valid=len(errors) == 0,
        errors=errors, warnings=warnings,
    )
