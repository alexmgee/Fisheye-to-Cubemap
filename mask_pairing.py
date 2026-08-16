"""Resolve per-image masks across one or more image and mask directories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from AM_ImageAndMask_to_cubemap_v4 import (
    _SUPPORTED_IMAGE_EXTS,
    _filtered_image_files,
    split_mask_string,
)


MaskSource = Literal[
    "per-image-mask-byname",
    "per-image-mask-positional",
    "fallback-pending",
]
AssignmentValue = tuple[str, tuple[int, int]] | Literal["FALLBACK"]


@dataclass(frozen=True)
class ResolvedPair:
    image_path: Path
    mask_path: Path | None
    mask_stem_base: str
    mask_suffix: str
    mask_source: MaskSource


@dataclass(frozen=True)
class ImageDirectoryMatchCount:
    image_dir: Path
    total: int
    matched: int

    @property
    def unmatched(self) -> int:
        return self.total - self.matched


@dataclass(frozen=True)
class MaskPairingReport:
    per_image_dir: tuple[ImageDirectoryMatchCount, ...]
    image_dirs: tuple[Path, ...]
    mask_dirs: tuple[Path, ...]
    intersection_matrix: tuple[tuple[int, ...], ...]
    resolution_counts: dict[str, int]
    assignment_map: dict[str, AssignmentValue]
    unmatched_image_stems: tuple[tuple[str, ...], ...]
    unmatched_mask_stems: tuple[tuple[str, ...], ...]
    candidate_mask_count: int
    warnings: tuple[str, ...]

    @property
    def total(self) -> int:
        return sum(count.total for count in self.per_image_dir)

    @property
    def matched(self) -> int:
        return sum(count.matched for count in self.per_image_dir)

    def summary(self) -> str:
        counts = self.resolution_counts
        return (
            f"Mask pairing: {self.matched}/{self.total} matched "
            f"(by-name={counts['by-name']}, "
            f"by-position={counts['by-position']}, "
            f"unmatched={counts['unmatched']})."
        )

    def format_abort_message(self) -> str:
        lines = [
            "Error: strict mask pairing failed; unmatched images were found.",
            self.summary(),
            "Per-image-directory counts:",
        ]
        for count in self.per_image_dir:
            lines.append(
                f"  {count.image_dir}: {count.matched}/{count.total} matched "
                f"({count.unmatched} unmatched)"
            )

        lines.append("Image-directory x mask-directory stem intersections:")
        if self.mask_dirs:
            header = "  image_dir"
            for index, mask_dir in enumerate(self.mask_dirs):
                header += f" | mask[{index}]={mask_dir}"
            lines.append(header)
            for index, row in enumerate(self.intersection_matrix):
                lines.append(
                    f"  image[{index}]={self.image_dirs[index]} | "
                    + " | ".join(str(value) for value in row)
                )
        else:
            lines.append("  (no mask directories)")

        lines.append("Sample unmatched image stems (up to 3 per directory):")
        for index, stems in enumerate(self.unmatched_image_stems):
            sample = ", ".join(stems[:3]) if stems else "(none)"
            lines.append(f"  image[{index}]={self.image_dirs[index]}: {sample}")

        lines.append("Sample unmatched mask stems (up to 3 per directory):")
        for index, stems in enumerate(self.unmatched_mask_stems):
            sample = ", ".join(stems[:3]) if stems else "(none)"
            lines.append(f"  mask[{index}]={self.mask_dirs[index]}: {sample}")
        return "\n".join(lines)


def _image_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise SystemExit(f"Error: image directory does not exist: {directory}")
    images = _filtered_image_files(directory)
    if not images:
        raise SystemExit(
            f"Error: no supported image files found in {directory}. "
            f"Supported extensions: {_SUPPORTED_IMAGE_EXTS}."
        )
    return images


def _mask_lookup(directory: Path) -> tuple[dict[str, Path], list[Path]]:
    if not directory.is_dir():
        raise SystemExit(f"Error: mask directory does not exist: {directory}")
    lookup: dict[str, Path] = {}
    masks = _filtered_image_files(directory)
    for mask_path in masks:
        mask_stem_base, _mask_suffix = split_mask_string(mask_path.stem)
        if mask_stem_base in lookup:
            raise SystemExit(
                "Error: duplicate mask stem after stripping '_mask'. "
                f"Stem '{mask_stem_base}' appears in both "
                f"{lookup[mask_stem_base]} and {mask_path}."
            )
        lookup[mask_stem_base] = mask_path
    return lookup, masks


def _assignment_value(mask_path: Path | None) -> AssignmentValue:
    if mask_path is None:
        return "FALLBACK"
    stat = mask_path.stat()
    return str(mask_path.resolve()), (stat.st_size, stat.st_mtime_ns)


def resolve_image_mask_pairs(
    image_dirs: Sequence[Path],
    mask_dirs: Sequence[Path] | None,
    *,
    naming_policy: Literal["v4", "exporter"],
    lens_only_mask: Path | None = None,
    skip_missing_image_dirs: bool = False,
) -> tuple[list[ResolvedPair], MaskPairingReport]:
    """Resolve masks using shared, name-based, then per-stem positional rules."""
    if naming_policy not in ("v4", "exporter"):
        raise ValueError("naming_policy must be 'v4' or 'exporter'")

    resolved_image_dirs = tuple(Path(directory) for directory in image_dirs)
    resolved_mask_dirs = tuple(Path(directory) for directory in (mask_dirs or ()))
    warnings = []
    images_by_dir_list = []
    for directory in resolved_image_dirs:
        if not skip_missing_image_dirs:
            images_by_dir_list.append(_image_files(directory))
            continue
        if not directory.is_dir():
            warnings.append(
                f"WARNING: image directory {directory} does not exist; skipping"
            )
            images_by_dir_list.append([])
            continue
        images = _filtered_image_files(directory)
        if not images:
            warnings.append(
                f"WARNING: image directory {directory} contains no supported images; skipping"
            )
        images_by_dir_list.append(images)
    images_by_dir = tuple(images_by_dir_list)
    mask_data = tuple(_mask_lookup(directory) for directory in resolved_mask_dirs)
    masks_by_dir = tuple(data[0] for data in mask_data)
    mask_paths_by_dir = tuple(data[1] for data in mask_data)

    if lens_only_mask is not None:
        lens_only_mask = Path(lens_only_mask)
        if not lens_only_mask.is_file():
            raise SystemExit(f"Error: lens-only mask file does not exist: {lens_only_mask}")
        if lens_only_mask.suffix.lower() not in _SUPPORTED_IMAGE_EXTS:
            raise SystemExit(
                f"Error: unsupported lens-only mask file: {lens_only_mask}. "
                f"Supported extensions: {_SUPPORTED_IMAGE_EXTS}."
            )

    image_stem_dirs: dict[str, set[int]] = {}
    for dir_index, image_paths in enumerate(images_by_dir):
        for image_path in image_paths:
            image_stem_dirs.setdefault(image_path.stem, set()).add(dir_index)

    mask_stem_dirs: dict[str, set[int]] = {}
    for dir_index, lookup in enumerate(masks_by_dir):
        for stem in lookup:
            mask_stem_dirs.setdefault(stem, set()).add(dir_index)

    matrix = tuple(
        tuple(
            len({path.stem for path in image_paths} & set(mask_lookup))
            for mask_lookup in masks_by_dir
        )
        for image_paths in images_by_dir
    )

    pairs_with_dirs: list[tuple[int, ResolvedPair]] = []
    mode_counts = {"by-name": 0, "by-position": 0, "unmatched": 0}
    unmatched_by_dir: list[list[str]] = [[] for _ in resolved_image_dirs]

    for dir_index, image_paths in enumerate(images_by_dir):
        for image_path in image_paths:
            image_stem = image_path.stem
            mask_path: Path | None = None
            source: MaskSource = "fallback-pending"

            if len(masks_by_dir) == 1:
                mask_path = masks_by_dir[0].get(image_stem)
                if mask_path is not None:
                    source = "per-image-mask-byname"
                    mode_counts["by-name"] += 1
            else:
                hit_dirs = mask_stem_dirs.get(image_stem, set())
                if not hit_dirs:
                    pass
                elif len(image_stem_dirs[image_stem]) == 1 and len(hit_dirs) == 1:
                    mask_path = masks_by_dir[next(iter(hit_dirs))][image_stem]
                    source = "per-image-mask-byname"
                    mode_counts["by-name"] += 1
                else:
                    positional_lookup = (
                        masks_by_dir[dir_index]
                        if dir_index < len(masks_by_dir)
                        else {}
                    )
                    mask_path = positional_lookup.get(image_stem)
                    if mask_path is not None:
                        source = "per-image-mask-positional"
                        mode_counts["by-position"] += 1

            if mask_path is None:
                mode_counts["unmatched"] += 1
                unmatched_by_dir[dir_index].append(image_stem)

            if naming_policy == "v4" and mask_path is not None:
                mask_stem_base, mask_suffix = split_mask_string(mask_path.stem)
            else:
                mask_stem_base, mask_suffix = image_stem, "_mask"

            pairs_with_dirs.append(
                (
                    dir_index,
                    ResolvedPair(
                        image_path=image_path,
                        mask_path=mask_path,
                        mask_stem_base=mask_stem_base,
                        mask_suffix=mask_suffix,
                        mask_source=source,
                    ),
                )
            )

    counts = []
    for dir_index, image_paths in enumerate(images_by_dir):
        unmatched = len(unmatched_by_dir[dir_index])
        counts.append(
            ImageDirectoryMatchCount(
                image_dir=resolved_image_dirs[dir_index],
                total=len(image_paths),
                matched=len(image_paths) - unmatched,
            )
        )

    assigned_mask_paths = {
        pair.mask_path
        for _dir_index, pair in pairs_with_dirs
        if pair.mask_path is not None
    }
    unmatched_masks = tuple(
        tuple(
            sorted(
                stem
                for stem, mask_path in lookup.items()
                if mask_path not in assigned_mask_paths
            )
        )
        for lookup in masks_by_dir
    )
    sorted_pairs = sorted(
        pairs_with_dirs,
        key=lambda item: (item[1].image_path.stem, item[0], str(item[1].image_path)),
    )
    assignment_map = {
        f"{dir_index}/{pair.image_path.name}": _assignment_value(pair.mask_path)
        for dir_index, pair in sorted_pairs
    }
    assignment_map = dict(sorted(assignment_map.items()))

    report = MaskPairingReport(
        per_image_dir=tuple(counts),
        image_dirs=resolved_image_dirs,
        mask_dirs=resolved_mask_dirs,
        intersection_matrix=matrix,
        resolution_counts=mode_counts,
        assignment_map=assignment_map,
        unmatched_image_stems=tuple(
            tuple(sorted(stems)) for stems in unmatched_by_dir
        ),
        unmatched_mask_stems=unmatched_masks,
        candidate_mask_count=sum(len(paths) for paths in mask_paths_by_dir),
        warnings=tuple(warnings),
    )
    return [pair for _dir_index, pair in sorted_pairs], report


def evaluate_strict_guard(report: MaskPairingReport, allow_partial: bool) -> None:
    """Abort strict runs when candidate masks exist but any image is unmatched."""
    if (
        report.candidate_mask_count > 0
        and not allow_partial
        and any(count.unmatched for count in report.per_image_dir)
    ):
        raise SystemExit(report.format_abort_message())


__all__ = [
    "ImageDirectoryMatchCount",
    "MaskPairingReport",
    "ResolvedPair",
    "evaluate_strict_guard",
    "resolve_image_mask_pairs",
]
