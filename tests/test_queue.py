from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.queue.run import process_sku, _load_api_images
from pipeline.hunyuan.adapter import MockHunyuan


def test_load_api_images_maps_front_view(tmp_path: Path) -> None:
    api = tmp_path / "b" / "sku"
    api.mkdir(parents=True)
    (api / "APP_01.jpg").write_bytes(b"x")
    report = {
        "metrics": {
            "views": [
                {"index": "01", "hunyuan": "ImageUrl", "upload": True, "api_file": "APP_01.jpg"},
                {"index": "02", "hunyuan": "MultiView01", "upload": True, "api_file": "none.jpg"},
            ]
        }
    }
    (api / "report.json").write_text(json.dumps(report), encoding="utf-8")
    imgs = _load_api_images(api)
    assert imgs.get("front") is not None
    assert "MultiView01" in imgs


def test_process_sku_mock_done(tmp_path: Path) -> None:
    api = tmp_path / "api"
    work = tmp_path / "work"
    sku_dir = api / "b" / "sku"
    sku_dir.mkdir(parents=True)
    (sku_dir / "front.jpg").write_bytes(b"x")
    (sku_dir / "report.json").write_text(
        json.dumps({"metrics": {"views": [{"index": "01", "hunyuan": "ImageUrl", "upload": True, "api_file": "front.jpg"}]}}),
        encoding="utf-8",
    )
    ad = MockHunyuan(poll_seconds=0.1)
    res = process_sku("sku", "b", api, work, ad)
    assert res.status == "done"
    assert (res.work_dir / "hunyuan.log.json").exists()
