from __future__ import annotations

"""Archive a validated SKU into the three-level catalog (spec-orchestration §7).

    work/<batch>/<sku>/current/model.glb  +  original images + reports
        ->  data/archive/<category>/<batch>/<sku>/

Copies final assets plus the version's report.json / review.json (if any) and
writes a meta.json with paths, sizes, version, credits, job history.
"""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.report import Check, ModuleReport

DEFAULT_WORK_ROOT = "data/work"
DEFAULT_ARCHIVE_ROOT = "data/archive"


@dataclass
class ArchiveResult:
    archive_dir: Path
    model: Path | None
    report: dict
    ok: bool


def _find_model(work_dir: Path) -> Path | None:
    for ext in ("*.glb", "*.obj"):
        for p in work_dir.glob(ext):
            return p
    return None


def archive_sku(
    sku_id: str,
    batch_id: str,
    work_root: Path,
    archive_root: Path,
    *,
    category: str = "general",
    source_work: Path | None = None,
) -> ArchiveResult:
    work_root = work_root.resolve()
    archive_root = archive_root.resolve()

    # Resolve source: a specific version dir, or <work_root>/<batch>/<sku>/current.
    if source_work is not None:
        src = source_work.resolve()
    else:
        cur = work_root / batch_id / sku_id / "current"
        src = cur if cur.is_dir() else work_root / batch_id / sku_id

    report = ModuleReport(module="archive", verdict="ok", input=str(src), output=str(archive_root))
    model = _find_model(src)
    if model is None:
        report.add(Check("model", False, "no model.glb/obj found in source"))
        report.finalize()
        return ArchiveResult(archive_dir=archive_root / category / batch_id / sku_id, model=None, report=report.to_dict(), ok=False)

    dest = archive_root / category / batch_id / sku_id
    images_dest = dest / "images"
    dest.mkdir(parents=True, exist_ok=True)
    images_dest.mkdir(parents=True, exist_ok=True)

    # Copy model + support files.
    copied = []
    for p in src.glob("model.*"):
        shutil.copy2(p, dest / p.name)
        copied.append(p.name)
    # copy report.json / review.json / hunyuan.log.json if present
    for name in ("report.json", "review.json", "hunyuan.log.json"):
        cand = src / name
        if cand.exists():
            shutil.copy2(cand, dest / name)

    # copy original images from incoming/<batch>/<sku>/ if present
    incoming_dir = Path("data/incoming") / batch_id / sku_id
    if incoming_dir.is_dir():
        for img in incoming_dir.glob("*.jpg"):
            shutil.copy2(img, images_dest / img.name)

    # meta.json
    meta = {
        "schema": "archive.meta.v1",
        "sku_id": sku_id,
        "batch_id": batch_id,
        "category": category,
        "source_work": str(src),
        "model": [str(dest / n) for n in copied],
        "model_bytes": [(n, (dest / n).stat().st_size) for n in copied],
        "version": (src / "hunyuan.log.json").exists() and "current" or "current",
        "archived_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report.add(Check("model", True, "model present"))
    report.add(Check("meta", True, "meta.json written"))
    report.finalize()
    report.write(dest / "archive_report.json")

    return ArchiveResult(archive_dir=dest, model=dest / model.name, report=report.to_dict(), ok=True)
