from __future__ import annotations

"""Executable WebUI actions: build (generate), size-correction, archive.

These wrap the pipeline's real functions (process_sku / run_validate /
archive_sku) so the WebUI triggers actual work rather than mock buttons.
"""

import json
import threading
import time
from pathlib import Path

from pipeline.queue.run import process_sku, _next_version
from pipeline.validate.run import run_validate
from pipeline.archive.run import archive_sku

DEFAULT_API_ROOT = "data/api"
DEFAULT_WORK_ROOT = "data/work"
DEFAULT_ARCHIVE_ROOT = "data/archive"
DEFAULT_FIXTURE_DIR = "experiments/fixtures"

# ---- background generate tasks (single-process registry for the WebUI) ----
# key: f"{batch_id}/{sku_id}"  value: {status, version, job_id, message, started_at, finished_at}
_GENERATE_TASKS: dict[str, dict] = {}
_GENERATE_LOCK = threading.Lock()


def _gen_key(batch_id: str, sku_id: str) -> str:
    return f"{batch_id}/{sku_id}"


def _set_gen_task(key: str, **fields) -> None:
    with _GENERATE_LOCK:
        t = _GENERATE_TASKS.setdefault(key, {"status": "pending", "version": "", "job_id": "", "message": ""})
        t.update(fields)
        _GENERATE_TASKS[key] = t


def build_skus(batch_id: str, sku_ids: list[str], *, provider: str = "auto",
               api_root: Path | None = None, work_root: Path | None = None,
               fixture_dir: Path | None = None) -> dict:
    """Submit SKUs to the (mock or real) Hunyuan adapter and download results."""
    from pipeline.hunyuan.adapter import make_adapter
    api_root = api_root or Path(DEFAULT_API_ROOT)
    work_root = work_root or Path(DEFAULT_WORK_ROOT)
    fixture_dir = fixture_dir or Path(DEFAULT_FIXTURE_DIR)
    adapter = make_adapter(provider=provider, fixture_dir=fixture_dir)
    results = []
    errors = []
    for sku in sku_ids:
        try:
            res = process_sku(sku, batch_id, api_root, work_root, adapter,
                              params={"provider": provider})
            results.append({
                "sku_id": sku, "status": res.status, "version": res.version,
                "job_id": res.job_id, "files": list(res.files),
            })
        except Exception as exc:
            errors.append({"sku_id": sku, "error": str(exc)})
    ok = not errors and all(r["status"] == "done" for r in results)
    return {"ok": ok, "results": results, "errors": errors,
            "message": f"完成 {len(results)}/{len(sku_ids)}" if not errors else f"{len(errors)} 个失败"}


def _meta_path(work_root: Path, batch_id: str, sku_id: str) -> Path:
    return work_root / batch_id / sku_id / "meta.json"


def _load_sku_meta(work_root: Path, batch_id: str, sku_id: str) -> dict:
    p = _meta_path(work_root, batch_id, sku_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_sku_meta(work_root: Path, batch_id: str, sku_id: str, meta: dict) -> None:
    p = _meta_path(work_root, batch_id, sku_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_validate(sku: str, batch_id: str, work_root: Path, version_dir: Path,
                    size_mm) -> None:
    """After a successful download, auto-validate the model into report.json.

    Uses the SKU's stored size_mm if present (or a supplied fallback); if no
    size is known we run a geometry-only validation (scale check skipped) and
    flag it so it can be reviewed / corrected later.
    """
    model = version_dir / "model.glb"
    if not model.exists():
        return
    if size_mm:
        try:
            if isinstance(size_mm, (list, tuple)):
                mm = tuple(float(x) for x in size_mm)
            else:
                parts = str(size_mm).replace("×", ",").replace("x", ",").replace("X", ",").split(",")
                mm = tuple(float(x) for x in parts[:3])
            if len(mm) != 3:
                mm = None
        except Exception:
            mm = None
    else:
        mm = None
    run_validate(model, size_mm=mm, out_path=version_dir / "report.json")


def _run_one_generate(key: str, batch_id: str, sku: str, provider: str,
                      api_root: Path, work_root: Path, fixture_dir: Path,
                      size_mm) -> None:
    """Background thread body: submit -> poll -> download -> validate."""
    from pipeline.hunyuan.adapter import make_adapter
    _set_gen_task(key, status="running", message="提交混元中…", started_at=time.time())
    try:
        adapter = make_adapter(provider=provider, fixture_dir=fixture_dir)
        res = process_sku(sku, batch_id, api_root, work_root, adapter,
                          params={"provider": provider})
        _set_gen_task(key, status=res.status, version=res.version, job_id=res.job_id,
                      message="")
        if res.status == "done":
            # auto-validate the freshly downloaded model
            vdir = work_root / batch_id / sku / res.version
            _apply_validate(sku, batch_id, work_root, vdir, size_mm)
            _set_gen_task(key, status="done", version=res.version, job_id=res.job_id,
                          message="模型已下载并完成自动校验", finished_at=time.time())
        else:
            _set_gen_task(key, status=res.status, version=res.version, job_id=res.job_id,
                          message=f"生成失败（{res.status}）：{res.error or ''}",
                          finished_at=time.time())
    except Exception as exc:
        _set_gen_task(key, status="fail", message=f"生成异常：{exc}",
                      finished_at=time.time())


def start_generate_task(batch_id: str, sku_ids: list[str], *, provider: str = "auto",
                        size_mm=None, api_root: Path | None = None,
                        work_root: Path | None = None,
                        fixture_dir: Path | None = None) -> dict:
    """Start one background thread to generate the SKUs (submit/poll/download).

    Returns immediately. Poll get_generate_status()/gen_queue_state() to follow.
    ``size_mm`` (W,H,D) is stored on the SKU meta so auto-validate can use it.
    """
    api_root = api_root or Path(DEFAULT_API_ROOT)
    work_root = work_root or Path(DEFAULT_WORK_ROOT)
    fixture_dir = fixture_dir or Path(DEFAULT_FIXTURE_DIR)

    # persist size_mm (maybe empty -> clear) on the SKU meta for later validate
    for sku in sku_ids:
        meta = _load_sku_meta(work_root, batch_id, sku)
        meta["size_mm"] = size_mm or meta.get("size_mm")
        _save_sku_meta(work_root, batch_id, sku, meta)

    launched = []
    for sku in sku_ids:
        key = _gen_key(batch_id, sku)
        with _GENERATE_LOCK:
            cur = _GENERATE_TASKS.get(key, {})
            if cur.get("status") in ("running", "pending"):
                launched.append({"sku_id": sku, "status": "already_queue", "message": "任务已在运行"})
                continue
        _set_gen_task(key, status="pending", version="", job_id="",
                      message="排队中…", started_at=time.time())
        t = threading.Thread(
            target=_run_one_generate,
            args=(key, batch_id, sku, provider, api_root, work_root, fixture_dir, size_mm),
            daemon=True,
        )
        t.start()
        launched.append({"sku_id": sku, "status": "launched", "message": "已受理，后台生成中"})
    return {"ok": True, "launched": launched, "message": f"已受理 {len(launched)} 个 SKU 后台生成"}


def get_generate_status(batch_id: str) -> dict:
    """Snapshot of all generate tasks for this batch (for the WebUI poll)."""
    with _GENERATE_LOCK:
        out = {}
        for key, t in _GENERATE_TASKS.items():
            b, sku = key.split("/", 1)
            if b == batch_id:
                out[sku] = dict(t)
    return {"batch_id": batch_id, "tasks": out}


def size_correct(sku_id: str, batch_id: str, version: str, size_mm,
                 *, work_root: Path | None = None) -> dict:
    """Scale a model to a target mm size and write it as the next version.

    ``size_mm`` may be a "W,H,D" string or a list/tuple of three numbers.
    """
    import numpy as np
    try:
        import trimesh
    except Exception as exc:
        return {"ok": False, "error": f"trimesh 不可用: {exc}"}
    try:
        if isinstance(size_mm, (list, tuple)):
            parts = [str(x) for x in size_mm]
        else:
            parts = str(size_mm).replace("×", ",").replace("x", ",").replace("X", ",").split(",")
        target = tuple(float(x) for x in parts[:3])
        if len(target) != 3:
            raise ValueError("需要 3 个数值")
    except Exception as exc:
        return {"ok": False, "error": f"尺寸解析失败: {exc}"}

    work_root = work_root or Path(DEFAULT_WORK_ROOT)
    src_dir = work_root / batch_id / sku_id / version
    model_path = src_dir / "model.glb"
    if not model_path.exists():
        return {"ok": False, "error": f"模型不存在: {model_path}"}
    try:
        loaded = trimesh.load(model_path, force="mesh")
        # bounding box extents are in scene units; Hunyuan GLB is meters.
        bbox = np.array(loaded.bounding_box.extents, dtype=float)
        if bbox.max() <= 0:
            raise ValueError("包围盒非法")
        # target is mm; convert current extents to mm, then scale proportionally
        bbox_mm = bbox * 1000.0
        scale_factor = max(target) / bbox_mm.max()
        loaded.apply_scale(scale_factor)
        new_dir, new_version = _next_version(work_root, batch_id, sku_id)
        new_dir.mkdir(parents=True, exist_ok=True)
        out = new_dir / "model.glb"
        loaded.export(out)
        real_bbox_mm = [round(x, 2) for x in np.array(loaded.bounding_box.extents, dtype=float) * 1000.0]
    except Exception as exc:
        return {"ok": False, "error": f"矫正失败: {exc}"}

    # re-validate at the corrected size
    rep = run_validate(out, size_mm=target, out_path=new_dir / "report.json")
    return {
        "ok": True,
        "new_version": new_version,
        "size_mm": [round(x, 3) for x in target],
        "model": str(out),
        "real_bbox_mm": real_bbox_mm,
        "validate_verdict": rep.verdict,
    }


def list_archives(archive_root: Path | None = None) -> dict:
    archive_root = archive_root or Path(DEFAULT_ARCHIVE_ROOT)
    items = []
    for cat in sorted(archive_root.iterdir()) if archive_root.is_dir() else []:
        if not cat.is_dir():
            continue
        for batchdir in cat.iterdir() if cat.is_dir() else []:
            if not batchdir.is_dir():
                continue
            for skudir in batchdir.iterdir() if batchdir.is_dir() else []:
                meta = skudir / "meta.json"
                if meta.exists():
                    try:
                        m = json.loads(meta.read_text(encoding="utf-8"))
                    except Exception:
                        m = {}
                    model = skudir / "model.glb"
                    items.append({
                        "category": cat.name,
                        "batch_id": batchdir.name,
                        "sku_id": skudir.name,
                        "size": model.stat().st_size if model.exists() else 0,
                    })
    items.sort(key=lambda i: (i["category"], i["batch_id"], i["sku_id"]))
    return {"total": len(items), "models": sum(1 for i in items if i["size"] > 0), "items": items}


def archive_sku_web(
    sku_id: str, batch_id: str, version: str = "",
    *, category: str = "general",
    work_root: Path | None = None, archive_root: Path | None = None,
) -> dict:
    """Archive one SKU's (validated) version into the three-level catalog.

    ``version`` may be a specific ''vN'' or left empty to prefer the latest
    version directory. Returns an archive result dict for the WebUI.
    """
    work_root = work_root or Path(DEFAULT_WORK_ROOT)
    archive_root = archive_root or Path(DEFAULT_ARCHIVE_ROOT)
    sku_work = work_root / batch_id / sku_id
    if not sku_work.is_dir():
        return {"ok": False, "error": f"work dir not found: {sku_work}"}

    # resolve source version dir
    if version:
        src = sku_work / version
        if not src.is_dir():
            return {"ok": False, "error": f"version not found: {src}"}
    else:
        vdirs = sorted(sku_work.glob("v*"), key=lambda p: int(p.name[1:])) if sku_work.is_dir() else []
        if not vdirs:
            cur = sku_work / "current"
            src = cur if cur.is_dir() else sku_work
        else:
            src = vdirs[-1]
        if not any(src.glob("model.*")):
            return {"ok": False, "error": f"no model in {src}"}

    try:
        res = archive_sku(sku_id, batch_id, work_root, archive_root, category=category, source_work=src)
    except Exception as exc:
        return {"ok": False, "error": f"archive failed: {exc}"}
    return {
        "ok": res.ok,
        "archive_dir": str(res.archive_dir),
        "model": str(res.model) if res.model else "",
        "report": res.report,
        "message": "已归档" if res.ok else "归档失败",
    }


def rerun_sku(
    batch_id: str, sku_id: str,
    *, provider: str = "auto",
    api_root: Path | None = None, work_root: Path | None = None,
    fixture_dir: Path | None = None,
) -> dict:
    """Re-generate one SKU (new version vN) — used for texture/geometry retry.

    Now asynchronous: launches a background generate task (submit/poll/download
    + auto-validate) and returns immediately; poll get_generate_status() to follow.
    """
    return start_generate_task(batch_id, [sku_id], provider=provider,
                               api_root=api_root, work_root=work_root,
                               fixture_dir=fixture_dir)
