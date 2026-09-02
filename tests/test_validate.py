from pathlib import Path

import trimesh

from pipeline.validate.run import run_validate


def _box_glb(path: Path, extents=(100.0, 50.0, 30.0)) -> Path:
    mesh = trimesh.creation.box(extents=extents)
    mesh.export(path)
    return path


def test_validate_ok_box(tmp_path: Path) -> None:
    model = _box_glb(tmp_path / "ok.glb")
    report = run_validate(model, size_mm=(100, 50, 30), out_path=tmp_path / "report.json")
    assert report.verdict == "ok"
    assert report.labels == []
    assert (tmp_path / "report.json").is_file()


def test_validate_scale_warn(tmp_path: Path) -> None:
    model = _box_glb(tmp_path / "scaled.glb", extents=(100.0, 50.0, 30.0))
    report = run_validate(model, size_mm=(100, 50, 10), out_path=tmp_path / "report.json")
    assert report.verdict == "warn"
    assert "scale_review" in report.labels


def test_validate_missing_file(tmp_path: Path) -> None:
    report = run_validate(tmp_path / "nope.glb", out_path=tmp_path / "report.json")
    assert report.verdict == "fail"
    assert "download_error" in report.labels
