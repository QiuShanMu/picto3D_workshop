from __future__ import annotations

import json
from pathlib import Path

from pipeline.webui.state import scan_batch


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
