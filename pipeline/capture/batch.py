from __future__ import annotations

"""Batch-level assembly: scan a capture batch and project ready SKUs to incoming.

This is the layer between T4 (single-SKU handoff) and T5 (preprocess). It reads
every <capture_root>/<batch>/<sku>/capture.json, judges each SKU against its
required views, and produces:

  incoming/<batch>/_batch_manifest.json     # whole-batch readout for queue/orchestration
  incoming/<batch>/<sku>/                   # only for SKUs marked ready (standard mode)

It does NOT call Hunyuan. It only moves files and writes manifests.
"""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.capture.handoff import handoff_sku, DEFAULT_CAPTURE_ROOT, DEFAULT_INCOMING_ROOT

DEFAULT_REQUIRED = ("01", "02", "03", "04", "05", "06", "07", "08")


@dataclass
class SkuBatchStatus:
    sku_id: str
    status: str  # ready | incomplete | no_capture
    captured_indices: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    source_capture: str = ""


@dataclass
class BatchAssembleResult:
    batch_id: str
    manifest_path: Path
    skus: list[SkuBatchStatus]
    ready: int
    incomplete: int
    report: dict


def _load_capture(capture_dir: Path) -> dict | None:
    path = capture_dir / "capture.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def assemble_batch(
    batch_id: str,
    capture_root: Path = Path(DEFAULT_CAPTURE_ROOT),
    incoming_root: Path = Path(DEFAULT_INCOMING_ROOT),
    *,
    required: tuple[str, ...] = DEFAULT_REQUIRED,
    export_ready: bool = True,
    out_report: Path | None = None,
) -> BatchAssembleResult:
    capture_root = capture_root.resolve()
    incoming_root = incoming_root.resolve()
    batch_dir = capture_root / batch_id
    manifest_dir = incoming_root / batch_id

    skus: list[SkuBatchStatus] = []
    if batch_dir.is_dir():
        for sku_dir in sorted(batch_dir.iterdir()):
            if not sku_dir.is_dir():
                continue
            cap = _load_capture(sku_dir)
            sku_id = sku_dir.name
            if cap is None:
                skus.append(SkuBatchStatus(sku_id=sku_id, status="no_capture"))
                continue
            frames = cap.get("frames", [])
            captured = [f.get("index") for f in frames if f.get("ok")]
            missing = [idx for idx in required if idx not in captured]
            status = "ready" if not missing else "incomplete"
            skus.append(
                SkuBatchStatus(
                    sku_id=sku_id,
                    status=status,
                    captured_indices=captured,
                    missing_required=missing,
                    source_capture=str(sku_dir / "capture.json"),
                )
            )

    ready_skus = [s for s in skus if s.status == "ready"]

    if export_ready:
        manifest_dir.mkdir(parents=True, exist_ok=True)
        for s in ready_skus:
            # re-run handoff to populate incoming/<batch>/<sku>/ + handoff.json
            try:
                handoff_sku(Path(s.source_capture).parent, incoming_root, sku_id=s.sku_id)
            except Exception:
                pass

    # Whole-batch manifest.
    manifest_payload = {
        "schema": "batch_manifest.v1",
        "batch_id": batch_id,
        "sku_count": len(skus),
        "ready": sum(1 for s in skus if s.status == "ready"),
        "incomplete": sum(1 for s in skus if s.status == "incomplete"),
        "no_capture": sum(1 for s in skus if s.status == "no_capture"),
        "skus": [
            {
                "sku_id": s.sku_id,
                "status": s.status,
                "captured_indices": s.captured_indices,
                "missing_required": s.missing_required,
                "source_capture": s.source_capture,
            }
            for s in skus
        ],
    }
    manifest_path = manifest_dir / "_batch_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    report = {
        "module": "batch_assemble",
        "batch_id": batch_id,
        "sku_count": len(skus),
        "ready": manifest_payload["ready"],
        "incomplete": manifest_payload["incomplete"],
        "manifest": str(manifest_path),
        "status": "ok",
    }
    if out_report is not None:
        out_report.parent.mkdir(parents=True, exist_ok=True)
        out_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return BatchAssembleResult(
        batch_id=batch_id,
        manifest_path=manifest_path,
        skus=skus,
        ready=manifest_payload["ready"],
        incomplete=manifest_payload["incomplete"],
        report=report,
    )
