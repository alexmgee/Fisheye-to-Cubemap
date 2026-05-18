from __future__ import annotations

import importlib.util
import math
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "metashape_cameras_to_colmap.py"

spec = importlib.util.spec_from_file_location("metashape_cameras_to_colmap", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


def test_scene_similarity_transform_preserves_camera_projection():
    camera = {
        "model": "PINHOLE",
        "width": 1000,
        "height": 1000,
        "params": (500.0, 500.0, 500.0, 500.0),
    }
    image = {
        "qvec": (1.0, 0.0, 0.0, 0.0),
        "tvec": (0.0, 0.0, -10.0),
        "rotation": mod.colmap_qvec_to_rotation_matrix((1.0, 0.0, 0.0, 0.0)),
    }
    point = (2.0, 3.0, 20.0)
    baseline_pixel = mod._project_colmap_camera_point(point, image, camera)

    transform = mod.SceneSimilarityTransform(origin=(100.0, -50.0, 4.0), scale=0.25)
    transformed_image = transform.transform_image_record(image)
    transformed_image["rotation"] = mod.colmap_qvec_to_rotation_matrix(transformed_image["qvec"])
    transformed_pixel = mod._project_colmap_camera_point(
        transform.apply_point(point),
        transformed_image,
        camera,
    )

    assert baseline_pixel is not None
    assert transformed_pixel is not None
    assert math.isclose(baseline_pixel[0], transformed_pixel[0], rel_tol=0.0, abs_tol=1e-9)
    assert math.isclose(baseline_pixel[1], transformed_pixel[1], rel_tol=0.0, abs_tol=1e-9)


def test_scene_normalization_transform_uses_camera_p95_radius():
    metrics = {
        "camera_center_median": (10.0, 20.0, 30.0),
        "camera_radius_p95": 2.5,
    }

    transform = mod._make_scene_normalization_transform(metrics)

    assert transform.origin == (10.0, 20.0, 30.0)
    assert transform.scale == 2.0
    assert transform.apply_point((12.5, 20.0, 30.0)) == (5.0, 0.0, 0.0)
