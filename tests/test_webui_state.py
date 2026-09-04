from __future__ import annotations

import json
from pathlib import Path

from pipeline.capture.webapp import CameraWorker, WebOptions
from pipeline.webui.actions import parse_size_mm, save_size_mm
from pipeline.webui.state import scan_batch, sku_detail, sku_state


def _mk(base: Path, sku: str, indices: list[str]) -> None:
    d = base / sku
    d.mkdir(parents=True)
    frames = [{"index": i, "ok": True} for i in indices]
    (d / "capture.json").write_text(json.dumps({"frames": frames}), encoding="utf-8")


def test_scan_batch_summary(tmp_path: Path) -> None:
    cap = tmp_path / "captures" / "b1"
    _mk(cap, "A", ["01", "02", "03", "04", "05", "06", "07", "08"])
    _mk(cap, "B", ["01"])
    _mk(cap, "C", [])
    st = scan_batch("b1", capture_root=cap.parent)
    assert st["total"] == 3
    assert st["ready"] == 1
    assert st["incomplete"] == 1
    assert st["no_capture"] == 1
    by_id = {s["sku_id"]: s for s in st["skus"]}
    assert by_id["A"]["status"] == "ready"
    assert by_id["B"]["status"] == "incomplete"
    assert by_id["B"]["missing_required"] == ["02", "03", "04", "05", "06", "07", "08"]
    assert by_id["C"]["status"] == "no_capture"


def test_scan_batch_orders_by_first_registration_time(tmp_path: Path) -> None:
    cap = tmp_path / "captures" / "b1"
    rows = [
        ("LATE", "2026-09-03T07:30:00+00:00"),
        ("FIRST", "2026-09-03T07:10:00+00:00"),
        ("MIDDLE", "2026-09-03T07:20:00+00:00"),
    ]
    for sku, registered_at in rows:
        _mk(cap, sku, ["01"])
        path = cap / sku / "capture.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["registered_at"] = registered_at
        path.write_text(json.dumps(payload), encoding="utf-8")

    state = scan_batch("b1", capture_root=cap.parent)
    assert [item["sku_id"] for item in state["skus"]] == ["FIRST", "MIDDLE", "LATE"]
    assert state["skus"][0]["registered_at_source"] == "registered_at"


def test_register_sku_keeps_first_timestamp(tmp_path: Path) -> None:
    root = tmp_path / "captures"
    worker = CameraWorker(WebOptions(batch_id="b1", capture_root=root))
    first = worker.register_sku("b1", "SKU-1", "扫码枪")
    second = worker.register_sku("b1", "SKU-1", "手动录入")
    assert first["ok"] is True and first["created"] is True
    assert second["created"] is False
    assert second["registered_at"] == first["registered_at"]
    payload = json.loads((root / "b1" / "SKU-1" / "capture.json").read_text(encoding="utf-8"))
    assert payload["registration_source"] == "扫码枪"


def test_sku_already_captured(tmp_path: Path) -> None:
    cap = tmp_path / "captures" / "0812"
    _mk(cap, "SHOT", ["01", "02"])
    root = cap.parent
    shot = sku_state("0812", "SHOT", capture_root=root)
    assert shot["already_captured"] is True
    assert shot["captured_count"] == 2
    fresh = sku_state("0812", "NEW", capture_root=root)
    assert fresh["already_captured"] is False
    assert fresh["status"] == "no_capture"


def test_parse_and_save_size_mm(tmp_path: Path) -> None:
    assert parse_size_mm("120,80,40") == (120.0, 80.0, 40.0)
    assert parse_size_mm([120, 80, 40]) == (120.0, 80.0, 40.0)
    assert parse_size_mm("") is None
    try:
        parse_size_mm("120,80")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    work = tmp_path / "work"
    r = save_size_mm("SKU-1", "0812", "210 × 80 × 45", work_root=work)
    assert r["ok"] is True
    assert r["size_mm"] == [210.0, 80.0, 45.0]
    meta = json.loads((work / "0812" / "SKU-1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["size_mm"] == [210.0, 80.0, 45.0]

    cap = tmp_path / "captures" / "0812" / "SKU-1"
    cap.mkdir(parents=True)
    (cap / "capture.json").write_text(json.dumps({"frames": []}), encoding="utf-8")
    d = sku_detail(
        "0812", "SKU-1",
        capture_root=tmp_path / "captures",
        incoming_root=tmp_path / "incoming",
        api_root=tmp_path / "api",
        work_root=work,
        archive_root=tmp_path / "archive",
    )
    assert d["size_mm"] == [210.0, 80.0, 45.0]


def test_save_size_mm_rejects_bad_input(tmp_path: Path) -> None:
    r = save_size_mm("SKU-1", "0812", "abc", work_root=tmp_path / "work")
    assert r["ok"] is False
    r2 = save_size_mm("", "0812", "1,2,3", work_root=tmp_path / "work")
    assert r2["ok"] is False
