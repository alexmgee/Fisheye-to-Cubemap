from __future__ import annotations

from pathlib import Path

import pytest

from gui.processing_stamp import compute_mask_input_digest
from mask_pairing import evaluate_strict_guard, resolve_image_mask_pairs


def _file(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_crossed_rows_with_unique_stems_resolve_entirely_by_name(tmp_path):
    images_a = tmp_path / "images_a"
    images_b = tmp_path / "images_b"
    masks_b = tmp_path / "masks_b"
    masks_a = tmp_path / "masks_a"
    _file(images_a / "A001.jpg")
    _file(images_a / "A002.jpg")
    _file(images_b / "B001.jpg")
    _file(masks_a / "A001_mask.png")
    _file(masks_a / "A002_mask.png")
    _file(masks_b / "B001_mask.png")

    pairs, report = resolve_image_mask_pairs(
        [images_a, images_b],
        [masks_b, masks_a],
        naming_policy="exporter",
    )

    assert [pair.mask_path.name for pair in pairs] == [
        "A001_mask.png",
        "A002_mask.png",
        "B001_mask.png",
    ]
    assert {pair.mask_source for pair in pairs} == {"per-image-mask-byname"}
    assert report.matched == report.total == 3
    assert report.resolution_counts == {
        "by-name": 3,
        "by-position": 0,
        "unmatched": 0,
    }


def test_one_shared_mask_dir_matches_duplicate_image_stems(tmp_path):
    images_a = tmp_path / "images_a"
    images_b = tmp_path / "images_b"
    masks = tmp_path / "masks"
    _file(images_a / "same.jpg")
    _file(images_b / "same.png")
    shared_mask = _file(masks / "same_mask.png")

    pairs, report = resolve_image_mask_pairs(
        [images_a, images_b], [masks], naming_policy="v4"
    )

    assert [pair.mask_path for pair in pairs] == [shared_mask, shared_mask]
    assert [pair.mask_source for pair in pairs] == [
        "per-image-mask-byname",
        "per-image-mask-byname",
    ]
    assert report.resolution_counts["by-name"] == 2


def test_image_stem_collision_uses_position_and_unmasked_dir_stays_unmatched(tmp_path):
    image_dirs = [tmp_path / f"images_{index}" for index in range(3)]
    mask_dirs = [tmp_path / f"masks_{index}" for index in range(2)]
    _file(image_dirs[0] / "collision.jpg")
    _file(image_dirs[1] / "unique.jpg")
    _file(image_dirs[2] / "collision.jpg")
    collision_mask = _file(mask_dirs[0] / "collision_mask.png")
    unique_mask = _file(mask_dirs[1] / "unique_mask.png")

    pairs, report = resolve_image_mask_pairs(
        image_dirs, mask_dirs, naming_policy="exporter"
    )
    by_image_dir = {pair.image_path.parent.name: pair for pair in pairs}

    assert by_image_dir["images_0"].mask_path == collision_mask
    assert by_image_dir["images_0"].mask_source == "per-image-mask-positional"
    assert by_image_dir["images_1"].mask_path == unique_mask
    assert by_image_dir["images_1"].mask_source == "per-image-mask-byname"
    assert by_image_dir["images_2"].mask_path is None
    assert by_image_dir["images_2"].mask_source == "fallback-pending"
    assert report.resolution_counts == {
        "by-name": 1,
        "by-position": 1,
        "unmatched": 1,
    }


def test_mask_stem_collision_is_positional_for_only_that_stem(tmp_path):
    images_a = tmp_path / "images_a"
    images_b = tmp_path / "images_b"
    masks_a = tmp_path / "masks_a"
    masks_b = tmp_path / "masks_b"
    _file(images_a / "shared.jpg")
    _file(images_a / "only_a.jpg")
    _file(images_b / "shared.jpg")
    _file(images_b / "only_b.jpg")
    shared_a = _file(masks_a / "shared_mask.png", b"a")
    shared_b = _file(masks_b / "shared_mask.png", b"b")
    only_a = _file(masks_b / "only_a_mask.png")
    only_b = _file(masks_a / "only_b_mask.png")

    pairs, report = resolve_image_mask_pairs(
        [images_a, images_b], [masks_a, masks_b], naming_policy="v4"
    )
    resolved = {(pair.image_path.parent.name, pair.image_path.stem): pair for pair in pairs}

    assert resolved[("images_a", "shared")].mask_path == shared_a
    assert resolved[("images_b", "shared")].mask_path == shared_b
    assert resolved[("images_a", "only_a")].mask_path == only_a
    assert resolved[("images_b", "only_b")].mask_path == only_b
    assert resolved[("images_a", "shared")].mask_source == "per-image-mask-positional"
    assert resolved[("images_b", "shared")].mask_source == "per-image-mask-positional"
    assert resolved[("images_a", "only_a")].mask_source == "per-image-mask-byname"
    assert resolved[("images_b", "only_b")].mask_source == "per-image-mask-byname"
    assert report.resolution_counts == {
        "by-name": 2,
        "by-position": 2,
        "unmatched": 0,
    }


def test_strict_guard_abort_partial_and_zero_candidate_cases(tmp_path):
    images = tmp_path / "images"
    masks = tmp_path / "masks"
    empty_masks = tmp_path / "empty_masks"
    _file(images / "matched.jpg")
    _file(images / "missing.jpg")
    _file(masks / "matched_mask.png")
    empty_masks.mkdir()

    _pairs, report = resolve_image_mask_pairs(
        [images], [masks], naming_policy="exporter"
    )
    with pytest.raises(SystemExit) as exc_info:
        evaluate_strict_guard(report, allow_partial=False)
    message = str(exc_info.value)
    assert "1/2 matched (1 unmatched)" in message
    assert "missing" in message
    evaluate_strict_guard(report, allow_partial=True)

    _pairs, zero_report = resolve_image_mask_pairs(
        [images], [empty_masks], naming_policy="exporter"
    )
    assert zero_report.candidate_mask_count == 0
    evaluate_strict_guard(zero_report, allow_partial=False)


def test_report_matrix_and_canonical_assignment_map(tmp_path):
    images_a = tmp_path / "images_a"
    images_b = tmp_path / "images_b"
    masks_a = tmp_path / "masks_a"
    masks_b = tmp_path / "masks_b"
    _file(images_a / "alpha.jpg")
    _file(images_a / "missing.jpg")
    _file(images_b / "beta.TIFF")
    alpha_mask = _file(masks_b / "alpha_mask.PNG", b"alpha")
    beta_mask = _file(masks_a / "beta_mask.tif", b"beta-data")
    _file(masks_a / "orphan_mask.png")

    _pairs, report = resolve_image_mask_pairs(
        [images_a, images_b], [masks_a, masks_b], naming_policy="exporter"
    )

    assert report.intersection_matrix == ((0, 1), (1, 0))
    assert [(item.total, item.matched) for item in report.per_image_dir] == [(2, 1), (1, 1)]
    assert list(report.assignment_map) == ["0/alpha.jpg", "0/missing.jpg", "1/beta.TIFF"]
    assert report.assignment_map["0/alpha.jpg"] == (
        str(alpha_mask.resolve()),
        (alpha_mask.stat().st_size, alpha_mask.stat().st_mtime_ns),
    )
    assert report.assignment_map["1/beta.TIFF"] == (
        str(beta_mask.resolve()),
        (beta_mask.stat().st_size, beta_mask.stat().st_mtime_ns),
    )
    assert report.assignment_map["0/missing.jpg"] == "FALLBACK"
    assert report.unmatched_image_stems == (("missing",), ())
    assert report.unmatched_mask_stems == (("orphan",), ())
    assert report.summary() == (
        "Mask pairing: 2/3 matched (by-name=2, by-position=0, unmatched=1)."
    )


def test_v4_and_exporter_naming_policies_round_trip(tmp_path):
    images = tmp_path / "images"
    masks = tmp_path / "masks"
    _file(images / "frame.jpg")
    mask = _file(masks / "frame.png")

    v4_pairs, _report = resolve_image_mask_pairs(
        [images], [masks], naming_policy="v4"
    )
    exporter_pairs, _report = resolve_image_mask_pairs(
        [images], [masks], naming_policy="exporter"
    )

    assert v4_pairs[0].mask_path == mask
    assert (v4_pairs[0].mask_stem_base, v4_pairs[0].mask_suffix) == ("frame", "")
    assert exporter_pairs[0].mask_path == mask
    assert (exporter_pairs[0].mask_stem_base, exporter_pairs[0].mask_suffix) == (
        "frame",
        "_mask",
    )


def test_literal_lowercase_mask_suffix_and_case_sensitive_stems(tmp_path):
    images = tmp_path / "images"
    masks = tmp_path / "masks"
    _file(images / "frame.JPG")
    _file(masks / "frame_MASK.PNG")

    pairs, report = resolve_image_mask_pairs(
        [images], [masks], naming_policy="v4"
    )

    assert pairs[0].mask_path is None
    assert report.resolution_counts["unmatched"] == 1


def test_assignment_map_preserves_both_sides_of_image_stem_collision(tmp_path):
    image_dirs = [tmp_path / f"images_{index}" for index in range(3)]
    mask_dirs = [tmp_path / f"masks_{index}" for index in range(2)]
    _file(image_dirs[0] / "collision.jpg")
    _file(image_dirs[1] / "unique.jpg")
    _file(image_dirs[2] / "collision.jpg")
    collision_mask = _file(mask_dirs[0] / "collision_mask.png")
    _file(mask_dirs[1] / "unique_mask.png")

    _pairs, report = resolve_image_mask_pairs(
        image_dirs, mask_dirs, naming_policy="exporter"
    )

    assert report.assignment_map["0/collision.jpg"] == (
        str(collision_mask.resolve()),
        (collision_mask.stat().st_size, collision_mask.stat().st_mtime_ns),
    )
    assert report.assignment_map["2/collision.jpg"] == "FALLBACK"
    assert len(report.assignment_map) == 3


def test_duplicate_mask_stem_within_one_directory_aborts(tmp_path):
    images = tmp_path / "images"
    masks = tmp_path / "masks"
    _file(images / "x.jpg")
    _file(masks / "x.png")
    _file(masks / "x_mask.png")

    with pytest.raises(SystemExit, match="duplicate mask stem"):
        resolve_image_mask_pairs([images], [masks], naming_policy="v4")


def test_extra_mask_directory_participates_in_name_lookup_and_candidate_count(tmp_path):
    images = tmp_path / "images"
    masks_0 = tmp_path / "masks_0"
    masks_1 = tmp_path / "masks_1"
    _file(images / "from_extra.jpg")
    masks_0.mkdir()
    extra_mask = _file(masks_1 / "from_extra_mask.png")

    pairs, report = resolve_image_mask_pairs(
        [images], [masks_0, masks_1], naming_policy="exporter"
    )

    assert pairs[0].mask_path == extra_mask
    assert pairs[0].mask_source == "per-image-mask-byname"
    assert report.candidate_mask_count == 1


def test_skipped_empty_image_dir_keeps_positional_mask_dir_in_global_pool(tmp_path):
    empty_images = tmp_path / "empty_images"
    images = tmp_path / "images"
    masks_0 = tmp_path / "masks_0"
    masks_1 = tmp_path / "masks_1"
    empty_images.mkdir()
    _file(images / "from_skipped_slot.jpg")
    mask = _file(masks_0 / "from_skipped_slot_mask.png")
    masks_1.mkdir()

    pairs, report = resolve_image_mask_pairs(
        [empty_images, images],
        [masks_0, masks_1],
        naming_policy="exporter",
        skip_missing_image_dirs=True,
    )

    assert pairs[0].mask_path == mask
    assert pairs[0].mask_source == "per-image-mask-byname"
    assert report.candidate_mask_count == 1
    assert report.warnings == (
        f"WARNING: image directory {empty_images} contains no supported images; skipping",
    )


def test_mask_digest_changes_only_when_collision_assignment_flips(tmp_path):
    image_dirs = [tmp_path / "images_0", tmp_path / "images_1"]
    mask_dirs = [tmp_path / "masks_0", tmp_path / "masks_1"]
    _file(image_dirs[0] / "collision.jpg")
    _file(image_dirs[1] / "collision.jpg")
    mask_0 = _file(mask_dirs[0] / "collision_mask.png", b"same")
    mask_1 = _file(mask_dirs[1] / "collision_mask.png", b"same")

    _pairs_a, report_a = resolve_image_mask_pairs(
        image_dirs, mask_dirs, naming_policy="exporter"
    )
    _pairs_b, report_b = resolve_image_mask_pairs(
        image_dirs, list(reversed(mask_dirs)), naming_policy="exporter"
    )
    support_inputs = [mask_0, mask_1]

    digest_a = compute_mask_input_digest(
        "mask-directory", support_inputs, None, report_a.assignment_map
    )
    digest_a_repeat = compute_mask_input_digest(
        "mask-directory", support_inputs, None, report_a.assignment_map
    )
    digest_b = compute_mask_input_digest(
        "mask-directory", support_inputs, None, report_b.assignment_map
    )

    assert digest_a == digest_a_repeat
    assert digest_a != digest_b
