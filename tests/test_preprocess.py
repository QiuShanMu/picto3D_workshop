from pathlib import Path

from PIL import Image

from pipeline.preprocess.run import run_preprocess
from pipeline.views import UPLOAD_SLOTS


def _write_shots(folder: Path, sku: str, indices: list[str], edge: int = 2048) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for idx in indices:
        Image.new("RGB", (edge, edge), color=(200, 200, 200)).save(folder / f"{sku}_{idx}.jpg")


def test_preprocess_maps_eight_upload_views(tmp_path: Path) -> None:
    sku = "APP-0812-001"
    incoming = tmp_path / "incoming" / sku
    _write_shots(incoming, sku, [f"{i:02d}" for i in range(1, 11)])
    out = tmp_path / "api" / sku
    report = run_preprocess(incoming, out, sku_id=sku, min_edge=2048, api_edge=1280)
    assert report.verdict == "ok"
    uploaded = [v for v in report.metrics["views"] if v["upload"]]
    skipped = [v for v in report.metrics["views"] if not v["upload"]]
    assert len(uploaded) == len(UPLOAD_SLOTS)
    assert {v["index"] for v in skipped} == {"04", "06"}
    assert (out / f"{sku}_01.jpg").is_file()
    assert not (out / f"{sku}_04.jpg").exists()


def test_preprocess_requires_front(tmp_path: Path) -> None:
    sku = "APP-0812-002"
    incoming = tmp_path / "incoming" / sku
    _write_shots(incoming, sku, ["03", "05"])
    report = run_preprocess(incoming, tmp_path / "api" / sku, sku_id=sku, min_edge=64)
    assert report.verdict == "fail"
    assert "download_error" in report.labels
