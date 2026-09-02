from __future__ import annotations

"""T4: export a captured SKU package into the ingest directory (spec-capture).

captures/<batch>/<sku>/  --capture.json, color/, depth/, camera.json
        =>  incoming/<batch>/<sku>/<sku>_<index>.jpg

Only frames listed as ok in capture.json are exported. Missing indices are
skipped (no placeholder files). 04/06 are copied too — preprocess decides to
keep them for archive but not upload them to Hunyuan.
"""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from pipeline.report import Check, ModuleReport

DEFAULT_CAPTURE_ROOT = "data/captures"
DEFAULT_INCOMING_ROOT = "data/incoming"


@dataclass
class HandoffResult:
    incoming_dir: Path
    exported: list[str]
    skipped: list[str]
    report: dict
    ok: bool


def _load_capture_json(capture_dir: Path) -> dict:
    path = capture_dir / "capture.json"
    if not path.exists():
        raise FileNotFoundError(f"capture.json not found in {capture_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def handoff_sku(
    capture_dir: Path,
    incoming_root: Path,
    *,
    sku_id: str | None = None,
    out_report: Path | None = None,
) -> HandoffResult:
    capture_dir = capture_dir.resolve()
    incoming_root = incoming_root.resolve()
    report = ModuleReport(module="handoff", verdict="ok", input=str(capture_dir), output=str(incoming_root))

    cap = _load_capture_json(capture_dir)
    sku_id = sku_id or cap.get("sku_id") or capture_dir.name
    batch_id = cap.get("batch_id") or capture_dir.parent.name

    incoming_dir = incoming_root / batch_id / sku_id
    incoming_dir.mkdir(parents=True, exist_ok=True)

    exported: list[str] = []
    skipped: list[str] = []

    frames = cap.get("frames", [])
    if not frames:
        report.add(Check("frames", False, "no captured frames in capture.json"))
        report.finalize()
        if out_report is not None:
            report.write(out_report)
        else:
            report.write(incoming_dir / "handoff_report.json")
        return HandoffResult(
            incoming_dir=incoming_dir,
            exported=[],
            skipped=[],
            report=report.to_dict(),
            ok=False,
        )

    for frame in frames:
        if not frame.get("ok"):
            skipped.append(frame.get("index", "?"))
            continue
        index = frame.get("index")
        rel = frame.get("color")
        if not index or not rel:
            skipped.append(str(index))
            continue
        src = capture_dir / rel
        if not src.exists():
            skipped.append(index)
            report.add(Check(f"file_{index}", False, f"{rel} missing on disk"))
            continue
        dest = incoming_dir / f"{sku_id}_{index}.jpg"
        shutil.copy2(src, dest)
        exported.append(index)

    report.metrics["sku_id"] = sku_id
    report.metrics["batch_id"] = batch_id
    report.metrics["exported"] = exported
    report.metrics["skipped"] = skipped
    report.metrics["source_capture"] = str(capture_dir / "capture.json")

    # handoff.json (SKU-level manifest, spec-capture section 5).
    payload = {
        "schema": "handoff.v1",
        "sku_id": sku_id,
        "batch_id": batch_id,
        "source": "capture.v1",
        "source_capture": str(capture_dir / "capture.json"),
        "exported": exported,
        "skipped": skipped,
        "status": "handed_off",
    }
    (incoming_dir / "handoff.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report.add(Check("front_present", "01" in exported, "01 exported" if "01" in exported else "01 missing"))
    report.finalize()
    if out_report is not None:
        report.write(out_report)
    else:
        report.write(incoming_dir / "handoff_report.json")

    return HandoffResult(
        incoming_dir=incoming_dir,
        exported=exported,
        skipped=skipped,
        report=report.to_dict(),
        ok=bool(exported),
    )
