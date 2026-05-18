from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from gui import cubeface_processing as cp


def _write_png(path: Path, value: int = 127, shape: tuple[int, int] = (4, 4)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = np.full((shape[0], shape[1], 3), value, dtype=np.uint8)
    assert cv2.imwrite(str(path), image)
    return path


_CORRECTIONS_COEFFS = " ".join("0.01" for _ in range(96))

_CALIBRATION_XML_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<calibration>\n"
    "  <projection>equisolid_fisheye</projection>\n"
    "  <width>4</width>\n"
    "  <height>4</height>\n"
    "  <f>10.0</f>\n"
    "  <cx>2.0</cx>\n"
    "  <cy>2.0</cy>\n"
    "  <k1>0.0</k1>\n"
    "  <k2>0.0</k2>\n"
    "  <k3>0.0</k3>\n"
    "  <p1>0.0</p1>\n"
    "  <p2>0.0</p2>\n"
    "{corrections}"
    "  <date>2026-05-16</date>\n"
    "</calibration>\n"
)

_CORRECTIONS_BLOCK = (
    '  <corrections type="fourier">\n'
    f"    <coeffs>{_CORRECTIONS_COEFFS}</coeffs>\n"
    "    <extent><min>0 0</min><max>4 4</max></extent>\n"
    "  </corrections>\n"
)


def _write_calibration_xml(path: Path, with_corrections: bool = False) -> Path:
    """Write a minimal calibration XML matching the test's expected params."""
    path.parent.mkdir(parents=True, exist_ok=True)
    corrections = _CORRECTIONS_BLOCK if with_corrections else ""
    path.write_text(
        _CALIBRATION_XML_TEMPLATE.format(corrections=corrections),
        encoding="utf-8",
    )
    return path


def _patch_v4_heavy_ops(monkeypatch, tmp_path):
    """Patch v4's computationally expensive operations (RBF, remap, masks).

    These patches replace operations that take minutes of computation with
    immediate stubs. They do NOT patch the calibration XML reader — tests
    provide a real calibration XML file via _write_calibration_xml.
    """
    _write_calibration_xml(tmp_path / "calibration.xml")

    calls = {
        "remap_precompute": [],
        "remap_images": [],
        "remap_masks": [],
        "mask_sums": [],
        "ray_path": [],  # tracks which ray-computation path was taken
    }

    def fake_sum_thresholded_masks(mask_paths, expected_shape):
        calls["mask_sums"].append(tuple(Path(path).name for path in mask_paths))
        return np.ones(expected_shape, dtype=np.int32)

    def fake_compute_rays_uncorrected(width, height, params, maxangle, outputbonusdirectory,
                                      model, maskpixelcount=None, image_derived_support=None):
        calls["ray_path"].append("uncorrected")
        return "rays", np.full((height, width), 255, dtype=np.uint8), 52.0

    def fake_compute_rays_corrected(width, height, params, model, corrections=None):
        calls["ray_path"].append("corrected")
        return np.ones((height, width, 3), dtype=np.float64), None

    def fake_derive_useful_pixel_mask(rays, maskpixelcount=None,
                                      image_derived_support=None, maxangle=None):
        return np.full(rays.shape[:2], 255, dtype=np.uint8), np.ones(rays.shape[:2]), 52.0

    def fake_precompute(width, height, rays, useful_pixel_mask, face_width, face,
                        model, params, maxangle, outputbonusdirectory, use_cache,
                        support_origin, support_padding_px):
        calls["remap_precompute"].append(
            {
                "face": face,
                "use_cache": use_cache,
                "support_origin": support_origin,
                "support_padding_px": support_padding_px,
            }
        )
        return f"x-{face}", f"y-{face}", f"indices-{face}", f"weights-{face}"

    def fake_remap_image(sourceimage_x, sourceimage_y, indices, pixel_weights,
                         face_width, source_image_filename, destination_image_filename,
                         expected_shape=None):
        calls["remap_images"].append(
            (Path(source_image_filename).name, Path(destination_image_filename).as_posix())
        )
        Path(destination_image_filename).parent.mkdir(parents=True, exist_ok=True)
        Path(destination_image_filename).write_bytes(b"image")

    def fake_remap_mask(sourceimage_x, sourceimage_y, indices, pixel_weights,
                        face_width, source_image_filename, destination_image_filename,
                        expected_shape=None):
        calls["remap_masks"].append(
            (Path(source_image_filename).name, Path(destination_image_filename).as_posix())
        )
        Path(destination_image_filename).parent.mkdir(parents=True, exist_ok=True)
        Path(destination_image_filename).write_bytes(b"mask")

    monkeypatch.setattr(cp, "sum_thresholded_masks", fake_sum_thresholded_masks)
    monkeypatch.setattr(cp, "compute_metashape_rays_usefulpixmap", fake_compute_rays_uncorrected)
    monkeypatch.setattr(cp, "compute_rays_with_corrections", fake_compute_rays_corrected)
    monkeypatch.setattr(cp, "derive_useful_pixel_mask", fake_derive_useful_pixel_mask)
    monkeypatch.setattr(cp, "compute_image2cubeface_remapping_cached", fake_precompute)
    monkeypatch.setattr(cp, "remap_image", fake_remap_image)
    monkeypatch.setattr(cp, "remap_mask", fake_remap_mask)
    return calls


def test_process_cubeface_sensor_returns_structure(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        face_width=128,
        mask_dirs=[mask_dir],
        cache_remapping=False,
    )

    assert result == {
        "processed_count": 1,
        "skipped_count": 0,
        "output_dir": str(tmp_path / "out"),
        "face_width": 128,
        "support_origin": "mask-directory",
        "maxangle_deg": 52.0,
    }
    assert len(calls["remap_precompute"]) == 5
    assert len(calls["remap_images"]) == 5
    assert len(calls["remap_masks"]) == 5


def test_process_cubeface_sensor_can_write_jpg_faces_with_png_masks(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        face_width=128,
        mask_dirs=[mask_dir],
        output_format="jpg",
        cache_remapping=False,
    )

    image_paths = [dest for _src, dest in calls["remap_images"]]
    mask_paths = [dest for _src, dest in calls["remap_masks"]]
    assert len(image_paths) == 5
    assert len(mask_paths) == 5
    assert all(path.endswith(".jpg") for path in image_paths)
    assert all(path.endswith("_mask.png") for path in mask_paths)


def test_mask_priority_both_dirs_and_lens_only(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(image_dir / "IMG_0002.png")
    _write_png(mask_dir / "IMG_0001_mask.png")
    lens_only = _write_png(tmp_path / "lens_only.png")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        mask_dirs=[mask_dir],
        lens_only_mask=lens_only,
    )

    assert result["support_origin"] == "mask-directory"
    assert calls["mask_sums"] == [("IMG_0001_mask.png",)]
    mask_sources = {source for source, _dest in calls["remap_masks"]}
    assert mask_sources == {"IMG_0001_mask.png", "lens_only.png"}


def test_mask_priority_lens_only_only(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    _write_png(image_dir / "IMG_0001.png")
    lens_only = _write_png(tmp_path / "lens_only.png")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        lens_only_mask=lens_only,
    )

    assert result["support_origin"] == "lens-only-mask"
    assert calls["mask_sums"] == [("lens_only.png",)]
    assert {source for source, _dest in calls["remap_masks"]} == {"lens_only.png"}


def test_no_masks_uses_geometric_calibration_support(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    _write_png(image_dir / "IMG_0001.png")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
    )

    assert result["support_origin"] == "geometric-calibration"
    assert calls["mask_sums"] == []
    mask_sources = {source for source, _dest in calls["remap_masks"]}
    assert mask_sources == {"fallback_mask_from_useful_pixel_mask.png"}


def test_repeated_fallback_mask_is_remapped_once_per_face(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(image_dir / "IMG_0002.png")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
    )

    assert result["processed_count"] == 2
    assert len(calls["remap_images"]) == 10
    assert len(calls["remap_masks"]) == 5


def test_skip_detection_all_exist(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    out = tmp_path / "out"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")
    for suffix in cp.FACE_FILENAME_SUFFIX.values():
        (out / "images" / "IMG_0001").mkdir(parents=True, exist_ok=True)
        (out / "images" / "IMG_0001" / f"IMG_0001{suffix}.png").write_bytes(b"done")
        (out / "masks").mkdir(parents=True, exist_ok=True)
        (out / "masks" / f"IMG_0001{suffix}_mask.png").write_bytes(b"done")

    # Run once to write the processing stamp
    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        out,
        mask_dirs=[mask_dir],
        force=False,
    )
    calls["remap_images"].clear()
    calls["remap_masks"].clear()

    # Second run should skip because stamp matches and files exist
    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        out,
        mask_dirs=[mask_dir],
        force=False,
    )

    assert result["processed_count"] == 0
    assert result["skipped_count"] == 1
    assert calls["remap_images"] == []
    assert calls["remap_masks"] == []


def test_skip_detection_no_stamp_forces_reprocess(monkeypatch, tmp_path):
    """Existing outputs with no processing stamp must not be trusted."""
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    out = tmp_path / "out"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")
    for suffix in cp.FACE_FILENAME_SUFFIX.values():
        (out / "images" / "IMG_0001").mkdir(parents=True, exist_ok=True)
        (out / "images" / "IMG_0001" / f"IMG_0001{suffix}.png").write_bytes(b"done")
        (out / "masks").mkdir(parents=True, exist_ok=True)
        (out / "masks" / f"IMG_0001{suffix}_mask.png").write_bytes(b"done")

    # No stamp written — outputs should be regenerated
    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        out,
        mask_dirs=[mask_dir],
        force=False,
    )

    assert result["processed_count"] == 1
    assert result["skipped_count"] == 0


def test_skip_detection_force(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        mask_dirs=[mask_dir],
        force=True,
    )

    assert result["processed_count"] == 1
    assert result["skipped_count"] == 0
    assert len(calls["remap_images"]) == 5


def test_skip_detection_partial(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    out = tmp_path / "out"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")
    suffix = cp.FACE_FILENAME_SUFFIX["+Z"]
    (out / "images" / "IMG_0001").mkdir(parents=True, exist_ok=True)
    (out / "images" / "IMG_0001" / f"IMG_0001{suffix}.png").write_bytes(b"partial")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        out,
        mask_dirs=[mask_dir],
        force=False,
    )

    assert result["processed_count"] == 1
    assert result["skipped_count"] == 0
    assert len(calls["remap_images"]) == 5


def test_multiple_image_dirs_share_one_remap_precompute(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir_a = tmp_path / "images_a"
    image_dir_b = tmp_path / "images_b"
    mask_dir = tmp_path / "masks"
    _write_png(image_dir_a / "IMG_0001.png")
    _write_png(image_dir_b / "IMG_0002.png")
    _write_png(mask_dir / "IMG_0001_mask.png")
    _write_png(mask_dir / "IMG_0002_mask.png")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir_a, image_dir_b],
        tmp_path / "out",
        mask_dirs=[mask_dir],
    )

    assert result["processed_count"] == 2
    assert len(calls["remap_precompute"]) == 5
    assert len(calls["remap_images"]) == 10


def test_duplicate_image_stems_raise(monkeypatch, tmp_path):
    _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir_a = tmp_path / "images_a"
    image_dir_b = tmp_path / "images_b"
    mask_dir_a = tmp_path / "masks_a"
    mask_dir_b = tmp_path / "masks_b"
    _write_png(image_dir_a / "IMG_0001.png")
    _write_png(image_dir_b / "IMG_0001.jpg")
    _write_png(mask_dir_a / "IMG_0001_mask.png")
    _write_png(mask_dir_b / "IMG_0001_mask.png")

    with pytest.raises(SystemExit, match="duplicate image stems"):
        cp.process_cubeface_sensor(
            tmp_path / "calibration.xml",
            [image_dir_a, image_dir_b],
            tmp_path / "out",
            mask_dirs=[mask_dir_a, mask_dir_b],
        )


def test_duplicate_image_stems_can_be_disambiguated(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir_a = tmp_path / "front" / "frames"
    image_dir_b = tmp_path / "back" / "frames"
    mask_dir_a = tmp_path / "front" / "masks"
    mask_dir_b = tmp_path / "back" / "masks"
    front_image = _write_png(image_dir_a / "000001.png")
    back_image = _write_png(image_dir_b / "000001.jpg")
    _write_png(mask_dir_a / "000001_mask.png")
    _write_png(mask_dir_b / "000001_mask.png")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir_a, image_dir_b],
        tmp_path / "out",
        mask_dirs=[mask_dir_a, mask_dir_b],
        stem_overrides={
            str(front_image): "front_000001",
            str(back_image): "back_000001",
        },
    )

    assert result["processed_count"] == 2
    output_paths = {dest for _source, dest in calls["remap_images"]}
    assert any("front_000001/front_000001_dir_plusZ.png" in path for path in output_paths)
    assert any("back_000001/back_000001_dir_plusZ.png" in path for path in output_paths)


def test_output_directory_structure_and_face_names(monkeypatch, tmp_path):
    _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    out = tmp_path / "out"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        out,
        mask_dirs=[mask_dir],
    )

    for suffix in cp.FACE_FILENAME_SUFFIX.values():
        assert (out / "images" / "IMG_0001" / f"IMG_0001{suffix}.png").is_file()
        assert (out / "masks" / f"IMG_0001{suffix}_mask.png").is_file()


def test_empty_mask_dir_can_fall_back_to_lens_only(monkeypatch, tmp_path):
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    mask_dir.mkdir()
    _write_png(image_dir / "IMG_0001.png")
    lens_only = _write_png(tmp_path / "lens_only.png")

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        mask_dirs=[mask_dir],
        lens_only_mask=lens_only,
    )

    assert result["support_origin"] == "lens-only-mask"
    assert calls["mask_sums"] == [("lens_only.png",)]


# ── Processing stamp tests ──────────────────────────────────────────────────


def test_stamp_written_after_processing(monkeypatch, tmp_path):
    """A processing stamp is written to the output dir after a successful batch."""
    _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    out = tmp_path / "out"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        out,
        mask_dirs=[mask_dir],
    )

    stamp_path = out / "_processing_stamp.json"
    assert stamp_path.is_file()
    import json
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    assert "stamp_version" in stamp
    assert "calibration_digest" in stamp
    assert "face_width" in stamp
    assert "output_format" in stamp
    assert "mask_input_digest" in stamp


def test_stamp_invalidation_on_face_width_change(monkeypatch, tmp_path):
    """Changing face width must invalidate the prior output batch."""
    _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    out = tmp_path / "out"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml", [image_dir], out,
        face_width=128, mask_dirs=[mask_dir],
    )
    first_stamp = (out / "_processing_stamp.json").read_text()

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml", [image_dir], out,
        face_width=256, mask_dirs=[mask_dir],
    )
    second_stamp = (out / "_processing_stamp.json").read_text()

    assert first_stamp != second_stamp
    assert result["processed_count"] == 1


def test_stamp_invalidation_on_corrections_change(monkeypatch, tmp_path):
    """Changing Fourier corrections must invalidate the prior output batch."""
    _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    out = tmp_path / "out"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml", [image_dir], out,
        face_width=128, mask_dirs=[mask_dir],
    )
    first_stamp = (out / "_processing_stamp.json").read_text()

    coeffs = " ".join("0.01" for _ in range(96))
    (tmp_path / "calibration.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<calibration>\n"
        "  <projection>equisolid_fisheye</projection>\n"
        "  <width>4</width><height>4</height>\n"
        "  <f>10.0</f><cx>2.0</cx><cy>2.0</cy>\n"
        "  <k1>0.0</k1><k2>0.0</k2><k3>0.0</k3>\n"
        "  <p1>0.0</p1><p2>0.0</p2>\n"
        f'  <corrections type="fourier">\n'
        f"    <coeffs>{coeffs}</coeffs>\n"
        f"    <extent><min>0 0</min><max>4 4</max></extent>\n"
        f"  </corrections>\n"
        "  <date>2026-05-18</date>\n"
        "</calibration>\n",
        encoding="utf-8",
    )

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml", [image_dir], out,
        face_width=128, mask_dirs=[mask_dir],
    )
    second_stamp = (out / "_processing_stamp.json").read_text()

    assert first_stamp != second_stamp
    assert result["processed_count"] == 1


def test_stamp_invalidation_on_output_format_change(monkeypatch, tmp_path):
    """Changing output format must invalidate the prior output batch."""
    _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    out = tmp_path / "out"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml", [image_dir], out,
        face_width=128, mask_dirs=[mask_dir], output_format="png",
    )
    first_stamp = (out / "_processing_stamp.json").read_text()

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml", [image_dir], out,
        face_width=128, mask_dirs=[mask_dir], output_format="jpg",
    )
    second_stamp = (out / "_processing_stamp.json").read_text()

    assert first_stamp != second_stamp
    assert result["processed_count"] == 1


def test_stamp_matching_run_skips(monkeypatch, tmp_path):
    """An unchanged rerun with matching stamp and complete files may skip."""
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    out = tmp_path / "out"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml", [image_dir], out,
        face_width=128, mask_dirs=[mask_dir],
    )
    calls["remap_images"].clear()
    calls["remap_masks"].clear()

    result = cp.process_cubeface_sensor(
        tmp_path / "calibration.xml", [image_dir], out,
        face_width=128, mask_dirs=[mask_dir],
    )

    assert result["processed_count"] == 0
    assert result["skipped_count"] == 1
    assert calls["remap_images"] == []


# ── Corrections integration tests ────────────────────────────────────────────


def test_corrections_bearing_xml_takes_corrected_path(monkeypatch, tmp_path):
    """P1-F: A calibration XML with corrections must use the corrected ray path,
    not compute_metashape_rays_usefulpixmap."""
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    _write_calibration_xml(tmp_path / "calibration.xml", with_corrections=True)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        mask_dirs=[mask_dir],
    )

    assert calls["ray_path"] == ["corrected"]


def test_no_corrections_xml_takes_uncorrected_path(monkeypatch, tmp_path):
    """Counterpart to P1-F: standard XML must use the uncorrected path."""
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        mask_dirs=[mask_dir],
    )

    assert calls["ray_path"] == ["uncorrected"]


def test_corrections_effective_support_origin_includes_fourier_hash(monkeypatch, tmp_path):
    """P1-G: When corrections are present, effective_support_origin passed to
    remap precompute must contain '+fourier_'."""
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    _write_calibration_xml(tmp_path / "calibration.xml", with_corrections=True)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        mask_dirs=[mask_dir],
    )

    support_origins = [c["support_origin"] for c in calls["remap_precompute"]]
    assert len(support_origins) == 5
    for origin in support_origins:
        assert "+fourier_" in origin


def test_no_corrections_support_origin_has_no_fourier_hash(monkeypatch, tmp_path):
    """Counterpart to P1-G: standard XML support_origin must not contain '+fourier_'."""
    calls = _patch_v4_heavy_ops(monkeypatch, tmp_path)
    image_dir = tmp_path / "images"
    mask_dir = tmp_path / "masks"
    _write_png(image_dir / "IMG_0001.png")
    _write_png(mask_dir / "IMG_0001_mask.png")

    cp.process_cubeface_sensor(
        tmp_path / "calibration.xml",
        [image_dir],
        tmp_path / "out",
        mask_dirs=[mask_dir],
    )

    support_origins = [c["support_origin"] for c in calls["remap_precompute"]]
    for origin in support_origins:
        assert "+fourier_" not in origin


# ── Exporter XML preservation tests ──────────────────────────────────────────


def test_write_sensor_calibration_xml_preserves_corrections(tmp_path):
    """P1-H: _write_sensor_calibration_xml must preserve <corrections> when present."""
    import xml.etree.ElementTree as ET
    import metashape_cameras_to_colmap as mod

    coeffs = " ".join("0.5" for _ in range(96))
    sensor = ET.fromstring(
        '<sensor id="0" label="test" type="equisolid_fisheye">'
        '<resolution width="3840" height="3840"/>'
        '<calibration type="equisolid_fisheye">'
        '<f>963.5</f><cx>-7.7</cx><cy>0.3</cy>'
        '<k1>0.18</k1><k2>0.028</k2><k3>-0.02</k3>'
        '<p1>-0.00017</p1><p2>0.00013</p2>'
        f'<corrections type="fourier">'
        f'<coeffs>{coeffs}</coeffs>'
        f'<extent><min>0 0</min><max>3840 3840</max></extent>'
        f'</corrections>'
        '</calibration>'
        '</sensor>'
    )

    path = tmp_path / "cal.xml"
    mod._write_sensor_calibration_xml(sensor, path)

    root = ET.parse(str(path)).getroot()
    corrections = root.find("corrections")
    assert corrections is not None
    assert corrections.attrib.get("type") == "fourier"
    parsed_coeffs = corrections.findtext("coeffs", "").strip().split()
    assert len(parsed_coeffs) == 96
    extent = corrections.find("extent")
    assert extent is not None
    assert extent.findtext("max") == "3840 3840"


def test_write_sensor_calibration_xml_omits_corrections_when_absent(tmp_path):
    """P1-I: _write_sensor_calibration_xml must not include <corrections> when
    the source sensor has none."""
    import xml.etree.ElementTree as ET
    import metashape_cameras_to_colmap as mod

    sensor = ET.fromstring(
        '<sensor id="0" label="test" type="equisolid_fisheye">'
        '<resolution width="3840" height="3840"/>'
        '<calibration type="equisolid_fisheye">'
        '<f>1048.3</f><cx>-7.1</cx><cy>0.88</cy>'
        '<k1>0.079</k1><k2>0.052</k2><k3>-0.02</k3>'
        '<p1>0.00036</p1><p2>-0.00023</p2>'
        '</calibration>'
        '</sensor>'
    )

    path = tmp_path / "cal.xml"
    mod._write_sensor_calibration_xml(sensor, path)

    root = ET.parse(str(path)).getroot()
    assert root.find("corrections") is None
