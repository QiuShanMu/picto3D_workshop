from __future__ import annotations

import json
from pathlib import Path

from pipeline.webui.actions import delete_capture_frame


def _write(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_delete_one_capture_frame_and_quarantine_files(tmp_path: Path) -> None:
    capture = tmp_path / "captures"
    incoming = tmp_path / "incoming"
    api = tmp_path / "api"
    work = tmp_path / "work"
    trash = tmp_path / "trash"
    sku_dir = capture / "b1" / "SKU-1"
    cap_path = sku_dir / "capture.json"
    _write(sku_dir / "color/01.jpg")
    _write(sku_dir / "color/02.jpg")
    _write(sku_dir / "depth/02.png")
    cap_path.write_text(
        json.dumps(
            {
                "registered_at": "2026-09-03T08:00:00+00:00",
                "frames": [
                    {"index": "01", "ok": True, "color": "color/01.jpg"},
                    {
                        "index": "02",
                        "ok": True,
                        "color": "color/02.jpg",
                        "depth": "depth/02.png",
                    },
                ],
                "session_metrics": {},
            }
        ),
        encoding="utf-8",
    )
    _write(incoming / "b1/SKU-1/SKU-1_02.jpg")
    _write(incoming / "b1/SKU-1/handoff.json")
    _write(incoming / "b1/SKU-1/handoff_report.json")
    _write(api / "b1/SKU-1/SKU-1_02.jpg")
    _write(api / "b1/SKU-1/report.json")
    _write(work / "b1/SKU-1/v1/model.glb")

    result = delete_capture_frame(
        "SKU-1",
        "b1",
        "02",
        capture_root=capture,
        incoming_root=incoming,
        api_root=api,
        work_root=work,
        trash_root=trash,
    )

    assert result["ok"] is True
    assert result["captured_indices"] == ["01"]
    assert result["warning"]
    assert (sku_dir / "color/01.jpg").exists()
    assert not (sku_dir / "color/02.jpg").exists()
    assert not (sku_dir / "depth/02.png").exists()
    assert not (incoming / "b1/SKU-1/SKU-1_02.jpg").exists()
    assert not (api / "b1/SKU-1/SKU-1_02.jpg").exists()
    assert (work / "b1/SKU-1/v1/model.glb").exists()
    quarantined = Path(result["trash_dir"])
    assert (quarantined / "capture/color/02.jpg").exists()
    assert (quarantined / "capture/depth/02.png").exists()
    assert (quarantined / "incoming/SKU-1_02.jpg").exists()
    assert (quarantined / "api/SKU-1_02.jpg").exists()

    updated = json.loads(cap_path.read_text(encoding="utf-8"))
    assert updated["registered_at"] == "2026-09-03T08:00:00+00:00"
    assert [frame["index"] for frame in updated["frames"]] == ["01"]
    assert updated["session_metrics"]["captured_indices"] == ["01"]
    assert "02" in updated["session_metrics"]["missing_required"]


def test_delete_capture_frame_rejects_unknown_slot(tmp_path: Path) -> None:
    result = delete_capture_frame(
        "SKU-1",
        "b1",
        "99",
        capture_root=tmp_path / "captures",
        trash_root=tmp_path / "trash",
    )
    assert result["ok"] is False
    assert not (tmp_path / "trash").exists()
