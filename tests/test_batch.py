from __future__ import annotations

import json
from pathlib import Path

from pipeline.capture.batch import assemble_batch


def _make_capture(base: Path, sku_id: str, indices: list[str]) -> None:
    d = base / sku_id
    d.mkdir(parents=True)
    frames = [{"index": i, "ok": True} for i in indices]
    (d / "capture.json").write_text(json.dumps({"sku_id": sku_id, "frames": frames}), encoding="utf-8")


def test_assemble_batch_flags_ready_and_incomplete(tmp_path: Path) -> None:
    cap = tmp_path / "captures" / "b1"
    _make_capture(cap, "A", ["01", "02", "03", "04", "05", "06", "07", "08"])
    _make_capture(cap, "B", ["01", "02"])
    _make_capture(cap, "C", ["01"])

    res = assemble_batch("b1", cap.parent, tmp_path / "incoming", export_ready=False)
    by_id = {s.sku_id: s.status for s in res.skus}
    assert by_id["A"] == "ready"
    assert by_id["B"] == "incomplete"
    assert by_id["C"] == "incomplete"
    assert res.ready == 1
    # manifest written
    m = tmp_path / "incoming" / "b1" / "_batch_manifest.json"
    assert m.exists()
    data = json.loads(m.read_text(encoding="utf-8"))
    assert data["ready"] == 1
    assert data["incomplete"] == 2
