from __future__ import annotations

"""Local generation queue: submit -> poll -> download, bounded concurrency.

One SKU is 'processed' end-to-end here (spec-architecture: B. 混元生成落盘 is a
single unit — submit + poll + download must not be split). Reads an api/ folder,
calls the Hunyuan adapter, and writes the model into a versioned work dir.

    api/<batch>/<sku>/  ->  work/<batch>/<sku>/v<N>/model.glb + hunyuan.log

This module is adapter-agnostic: it takes a BaseHunyuan (Mock or Tencent).
Queue logic (inflight <= N) is a thin wrapper over process_sku.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.hunyuan.adapter import BaseHunyuan, make_adapter
from pipeline.views import SLOT_BY_INDEX

DEFAULT_API_ROOT = "data/api"
DEFAULT_WORK_ROOT = "data/work"
DEFAULT_INFLIGHT = 3
POLL_INTERVAL = 5.0


@dataclass
class ProcessResult:
    sku_id: str
    job_id: str
    work_dir: Path
    version: str
    status: str            # done | fail | no_images
    files: dict = field(default_factory=dict)
    error: str = ""
    credits: int = 0


def _load_api_images(api_dir: Path) -> dict[str, str]:
    """Read api/<sku>/ directory: return {view_type: local_path} for upload.

    Uses the preprocess report's view mapping; falls back to filename parse.
    """
    images: dict[str, str] = {}
    report = api_dir / "report.json"
    if report.exists():
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            for v in data.get("metrics", {}).get("views", []):
                if not v.get("upload") or not v.get("api_file"):
                    continue
                field_name = v.get("hunyuan") or "front"
                # 'ImageUrl' is the single front view -> normalize to 'front'
                key = "front" if field_name == "ImageUrl" else field_name
                images[key] = str(api_dir / v["api_file"])
        except Exception:
            pass
    if images:
        return images
    # fallback: map by index
    for idx, slot in SLOT_BY_INDEX.items():
        if slot.hunyuan_field is None:
            continue
        for f in api_dir.glob(f"*_{idx}.*"):
            images[slot.hunyuan_field] = str(f)
    return images


def _next_version(work_root: Path, batch_id: str, sku_id: str) -> tuple[Path, str]:
    base = work_root / batch_id / sku_id
    n = 1
    while (base / f"v{n}").exists():
        n += 1
    return base / f"v{n}", f"v{n}"


def process_sku(
    sku_id: str,
    batch_id: str,
    api_root: Path,
    work_root: Path,
    adapter: BaseHunyuan,
    *,
    params: dict | None = None,
    poll_interval: float = POLL_INTERVAL,
    poll_timeout: float = 3600.0,
) -> ProcessResult:
    params = params or {}
    api_dir = api_root / batch_id / sku_id
    work_dir, version = _next_version(work_root, batch_id, sku_id)
    images = _load_api_images(api_dir)
    # Normalize keys: prefer hunyuan field names; 'front' = ImageUrl.
    if not images:
        return ProcessResult(sku_id=sku_id, job_id="", work_dir=work_dir, version=version, status="no_images")

    provider = params.get("provider", "auto")
    job_id = adapter.submit(images, params)

    # Poll until done/fail/timeout.
    status = "run"
    started = time.time()
    files: dict[str, list[str]] = {}
    credits = 0
    error = ""
    while time.time() - started < poll_timeout:
        res = adapter.poll(job_id)
        status = res.status
        files = res.files
        credits = res.credits
        error = res.error
        if status in ("done", "fail"):
            break
        time.sleep(poll_interval)

    # Download into version dir.
    local_files: dict[str, list[str]] = {}
    if status == "done" and files:
        local_files = adapter.download(files, work_dir)

    # Write a hunyuan.log for traceability.
    work_dir.mkdir(parents=True, exist_ok=True)
    log = {
        "schema": "hunyuan.log.v1",
        "sku_id": sku_id,
        "batch_id": batch_id,
        "job_id": job_id,
        "version": version,
        "provider": params.get("provider", "auto"),
        "status": status,
        "images_used": images,
        "files_remote": files,
        "files_local": local_files,
        "credits": credits,
        "error": error,
        "polled_at": time.time(),
    }
    (work_dir / "hunyuan.log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    return ProcessResult(
        sku_id=sku_id,
        job_id=job_id,
        work_dir=work_dir,
        version=version,
        status="done" if status == "done" and local_files else ("fail" if status == "fail" else "error"),
        files=local_files,
        error=error,
        credits=credits,
    )


def run_queue(
    batch_id: str,
    sku_ids: list[str],
    api_root: Path,
    work_root: Path,
    *,
    inflight: int = DEFAULT_INFLIGHT,
    provider: str = "auto",
    fixture_dir=None,
    poll_interval: float = POLL_INTERVAL,
) -> list[ProcessResult]:
    """Process a batch of SKUs. In the real provider the adapter enforces the
    concurrency limit (account default inflight); here it is a simple loop with
    a note that genuine parallel submission is the adapter's job."""
    adapter = make_adapter(provider=provider, fixture_dir=fixture_dir)
    results: list[ProcessResult] = []
    for sku_id in sku_ids:
        res = process_sku(
            sku_id, batch_id, api_root, work_root, adapter,
            params={"provider": provider},
            poll_interval=poll_interval,
        )
        results.append(res)
    return results
