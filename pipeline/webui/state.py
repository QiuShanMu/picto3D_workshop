from __future__ import annotations

"""Read-only aggregation of capture / manifest state for the WebUI board.

Turns filesystem state into a JSON-friendly batch summary:
  total / ready / incomplete / no_capture + per-SKU captured_indices,
  missing_required, required views, and downstream presence (api/work/archive).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from pipeline.capture.batch import DEFAULT_REQUIRED
from pipeline.views import SLOTS as VIEW_SLOTS, SLOT_BY_INDEX

DEFAULT_CAPTURE_ROOT = "data/captures"
DEFAULT_INCOMING_ROOT = "data/incoming"
DEFAULT_API_ROOT = "data/api"
DEFAULT_WORK_ROOT = "data/work"
DEFAULT_ARCHIVE_ROOT = "data/archive"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _capture_for(sku_dir: Path) -> dict | None:
    return _load_json(sku_dir / "capture.json")


def read_manifest(batch_id: str, incoming_root: Path = Path(DEFAULT_INCOMING_ROOT)) -> dict | None:
    return _load_json(incoming_root / batch_id / "_batch_manifest.json")


def scan_batch(
    batch_id: str,
    *,
    capture_root: Path = Path(DEFAULT_CAPTURE_ROOT),
    required: tuple[str, ...] = DEFAULT_REQUIRED,
) -> dict:
    """Aggregate the whole batch: summarize each SKU capture status + downstream."""
    batch_dir = capture_root / batch_id
    skus: list[dict] = []
    total = 0
    ready = 0
    incomplete = 0
    no_capture = 0

    if batch_dir.is_dir():
        for sku_dir in sorted(batch_dir.iterdir()):
            if not sku_dir.is_dir():
                continue
            sku_id = sku_dir.name
            cap = _capture_for(sku_dir)
            total += 1
            if cap is None:
                no_capture += 1
                skus.append(_sku_entry(sku_id, batch_id, "no_capture", [], list(required), sku_dir, cap))
                continue
            frames = [f for f in cap.get("frames", []) if f.get("ok")]
            captured = [f.get("index") for f in frames]
            if not captured:
                no_capture += 1
                skus.append(_sku_entry(sku_id, batch_id, "no_capture", [], list(required), sku_dir, cap))
                continue
            missing = [i for i in required if i not in captured]
            status = "ready" if not missing else "incomplete"
            if status == "ready":
                ready += 1
            else:
                incomplete += 1
            skus.append(_sku_entry(sku_id, batch_id, status, captured, missing, sku_dir, cap))

    # Oldest registration first. SKU is the deterministic tie-breaker, so the
    # board does not jump around during its five-second refresh.
    skus.sort(key=lambda item: (item.pop("_registered_sort"), item["sku_id"]))

    return {
        "batch_id": batch_id,
        "total": total,
        "ready": ready,
        "incomplete": incomplete,
        "no_capture": no_capture,
        "skus": skus,
    }


def _parse_timestamp(raw) -> tuple[str, float] | None:
    if not raw:
        return None
    text = str(raw)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return text, dt.timestamp()
    except (TypeError, ValueError):
        return None


def _registration_time(cap: dict | None, sku_dir: Path) -> tuple[str, float, str]:
    """Resolve first-entry time, including fallbacks for historical captures."""
    cap = cap or {}
    candidates = [
        (cap.get("registered_at"), "registered_at"),
        (((cap.get("barcode") or {}).get("captured_at")), "barcode.captured_at"),
        (cap.get("started_at"), "capture.started_at"),
    ]
    frame_times = [
        parsed
        for parsed in (_parse_timestamp(f.get("captured_at")) for f in cap.get("frames", []))
        if parsed is not None
    ]
    if frame_times:
        first_frame = min(frame_times, key=lambda item: item[1])
        candidates.append((first_frame[0], "frame.captured_at"))
    for raw, source in candidates:
        parsed = _parse_timestamp(raw)
        if parsed is not None:
            return parsed[0], parsed[1], source
    try:
        timestamp = sku_dir.stat().st_mtime
    except OSError:
        timestamp = 0.0
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(), timestamp, "filesystem.mtime"


def _sku_entry(
    sku_id: str,
    batch_id: str,
    status: str,
    captured: list[str],
    missing: list[str],
    sku_dir: Path,
    cap: dict | None = None,
) -> dict:
    registered_at, registered_sort, registered_source = _registration_time(cap, sku_dir)
    return {
        "sku_id": sku_id,
        "batch_id": batch_id,
        "registered_at": registered_at,
        "registered_at_source": registered_source,
        "_registered_sort": registered_sort,
        "status": status,
        "captured_indices": captured,
        "missing_required": missing,
        "required": list(DEFAULT_REQUIRED),
        "source_capture": str(sku_dir / "capture.json"),
        "pipeline": _downstream(sku_id, batch_id),
    }


def _downstream(sku_id: str, batch_id: str) -> dict:
    """Report presence of downstream artifacts for the pipeline status column."""
    api_dir = Path(DEFAULT_API_ROOT) / batch_id / sku_id
    work_dir = Path(DEFAULT_WORK_ROOT) / batch_id / sku_id
    arc_dir = Path(DEFAULT_ARCHIVE_ROOT)
    # find any archive category containing this sku
    archived = [str(p) for p in arc_dir.glob(f"*/{batch_id}/{sku_id}/meta.json")]
    vdirs = sorted(work_dir.glob("v*"), key=lambda p: int(p.name[1:])) if work_dir.is_dir() else []
    latest_version = vdirs[-1] if vdirs else work_dir
    # any generated model in any version
    models = list(work_dir.glob("v*/model.glb")) if work_dir.is_dir() else []
    model = latest_version / "model.glb" if latest_version != work_dir else None
    return {
        "api": (api_dir / "report.json").exists(),
        "work": bool(models) or (model is not None and model.exists()),
        "archived": bool(archived),
        "latest_version": latest_version.name if latest_version != work_dir else "",
        "model_present": bool(models) or (model is not None and model.exists()),
    }


def sku_state(batch_id: str, sku_id: str, *, capture_root: Path = Path(DEFAULT_CAPTURE_ROOT)) -> dict:
    cap = _load_json(capture_root / batch_id / sku_id / "capture.json")
    if cap is None:
        return {
            "sku_id": sku_id, "batch_id": batch_id, "status": "no_capture",
            "already_captured": False, "captured_count": 0,
            "captured_indices": [], "missing_required": list(DEFAULT_REQUIRED),
            "frames": [],
        }
    frames = [f for f in cap.get("frames", []) if f.get("ok")]
    captured = [f.get("index") for f in frames]
    missing = [i for i in DEFAULT_REQUIRED if i not in captured]
    status = "ready" if not missing else "incomplete"
    return {
        "sku_id": sku_id,
        "batch_id": batch_id,
        "status": status,
        "already_captured": bool(captured),
        "captured_count": len(captured),
        "captured_indices": captured,
        "missing_required": missing,
        "frames": frames,
        "session_metrics": cap.get("session_metrics"),
    }


def gen_queue_state(
    batch_id: str,
    *,
    api_root: Path = Path(DEFAULT_API_ROOT),
    work_root: Path = Path(DEFAULT_WORK_ROOT),
    archive_root: Path = Path(DEFAULT_ARCHIVE_ROOT),
) -> dict:
    """Generate-workbench state: ready SKUs + their build versions/status."""
    manifest = read_manifest(batch_id)
    skus: list[dict] = []
    sources = []
    if manifest:
        sources = [s["sku_id"] for s in manifest.get("skus", []) if s.get("status") == "ready"]
    else:
        api_dir = api_root / batch_id
        if api_dir.is_dir():
            sources = [d.name for d in api_dir.iterdir() if d.is_dir()]
    # merge in-progress background generate tasks (polled for the board)
    from pipeline.webui import actions as _actions
    task_snapshot = _actions.get_generate_status(batch_id).get("tasks", {})
    for sku in sorted(set(sources)):
        w = work_root / batch_id / sku
        vdirs = sorted(w.glob("v*"), key=lambda p: int(p.name[1:])) if w.is_dir() else []
        versions = []
        for vd in vdirs:
            log = _load_json(vd / "hunyuan.log.json")
            rep = _load_json(vd / "report.json")
            versions.append({
                "version": vd.name,
                "has_model": (vd / "model.glb").exists(),
                "status": (log or {}).get("status", "?"),
                "job_id": (log or {}).get("job_id", ""),
                "validate_verdict": (rep or {}).get("verdict"),
                "real_bytes": (vd / "model.glb").stat().st_size if (vd / "model.glb").exists() else 0,
            })
        archived = bool(list(archive_root.glob(f"*/{batch_id}/{sku}/meta.json")))
        skus.append({
            "sku_id": sku,
            "batch_id": batch_id,
            "buildable": (api_root / batch_id / sku / "report.json").exists(),
            "versions": versions,
            "latest_version": versions[-1]["version"] if versions else "",
            "archived": archived,
            "model_present": any(v["has_model"] for v in versions),
            "task": task_snapshot.get(sku),
        })
    return {"batch_id": batch_id, "skus": skus}


def validate_workbench(
    batch_id: str,
    *,
    work_root: Path = Path(DEFAULT_WORK_ROOT),
    archive_root: Path = Path(DEFAULT_ARCHIVE_ROOT),
) -> dict:
    """Validate / size-correct workbench: all built models + their validate reports."""
    w = work_root / batch_id
    items: list[dict] = []
    if w.is_dir():
        for sku_dir in sorted(w.iterdir()):
            if not sku_dir.is_dir():
                continue
            sku = sku_dir.name
            vdirs = sorted(sku_dir.glob("v*"), key=lambda p: int(p.name[1:]))
            for vd in vdirs:
                model = vd / "model.glb"
                rep = _load_json(vd / "report.json")
                checks = (rep or {}).get("checks", [])
                metrics = (rep or {}).get("metrics", {})
                items.append({
                    "sku_id": sku,
                    "batch_id": batch_id,
                    "version": vd.name,
                    "model": str(model),
                    "model_exists": model.exists(),
                    "size_bytes": model.stat().st_size if model.exists() else 0,
                    "verdict": (rep or {}).get("verdict", "unvalidated"),
                    "checks": checks,
                    "metrics": metrics,
                    "faces": metrics.get("faces"),
                    "manifold_ratio": metrics.get("manifold_ratio"),
                    "bbox": metrics.get("bbox"),
                    "size_mm": metrics.get("size_mm"),
                    "scale_deviation": metrics.get("scale_deviation"),
                    "archived": bool(list(archive_root.glob(f"*/{batch_id}/{sku}/meta.json"))),
                })
    items.sort(key=lambda i: (i["sku_id"], int(i["version"][1:]) if i["version"].startswith("v") else 0))
    return {"batch_id": batch_id, "items": items}


def sku_detail(
    batch_id: str,
    sku_id: str,
    *,
    capture_root: Path = Path(DEFAULT_CAPTURE_ROOT),
    incoming_root: Path = Path(DEFAULT_INCOMING_ROOT),
    api_root: Path = Path(DEFAULT_API_ROOT),
    work_root: Path = Path(DEFAULT_WORK_ROOT),
    archive_root: Path = Path(DEFAULT_ARCHIVE_ROOT),
) -> dict:
    """Aggregate one SKU's full detail: views (with source images) + stage state.

    ``views`` lists every view slot (01–10) with captured/pending status, the
    degree & pose, and a ``url`` to the raw capture image when present. The
    capture image is served over a separate Flask route /api/capture-img.
    """
    cap = _load_json(capture_root / batch_id / sku_id / "capture.json")
    frames = [f for f in (cap or {}).get("frames", []) if f.get("ok")]
    captured = [f.get("index") for f in frames]
    by_index = {f.get("index"): f for f in frames}

    # collect view info from the canonical view table
    views = []
    for slot in VIEW_SLOTS:
        idx = slot.index
        is_captured = idx in captured
        fr = by_index.get(idx)
        # capture image url, e.g. /api/capture-img/<batch>/<sku>/<frame color path>
        url = ""
        color_rel = (fr or {}).get("color", "")
        if color_rel:
            url = f"/api/capture-img/{batch_id}/{sku_id}/{color_rel}"
        views.append({
            "index": idx,
            "degrees": slot.degrees,
            "pose": slot.pose_name,
            "hunyuan": slot.hunyuan_field,
            "required": slot.required_for_submit,
            "captured": is_captured,
            "url": url,
        })

    rep = _load_json(capture_root / batch_id / sku_id / "report.json")
    missing = [i for i in DEFAULT_REQUIRED if i not in captured]
    status = "ready" if not missing else ("incomplete" if captured else "no_capture")

    # downstream presence
    api_dir = api_root / batch_id / sku_id
    work_dir = work_root / batch_id / sku_id
    vdirs = sorted(work_dir.glob("v*"), key=lambda p: int(p.name[1:])) if work_dir.is_dir() else []
    versions = []
    for vd in vdirs:
        glog = _load_json(vd / "hunyuan.log.json")
        vrep = _load_json(vd / "report.json")
        versions.append({
            "version": vd.name,
            "has_model": (vd / "model.glb").exists(),
            "status": (glog or {}).get("status", "?"),
            "job_id": (glog or {}).get("job_id", ""),
            "validate_verdict": (vrep or {}).get("verdict"),
            "checks": (vrep or {}).get("checks", []),
            "metrics": (vrep or {}).get("metrics", {}),
        })
    archived = bool(list(archive_root.glob(f"*/{batch_id}/{sku_id}/meta.json")))
    sku_meta = _load_json(work_dir / "meta.json") or {}

    return {
        "sku_id": sku_id,
        "batch_id": batch_id,
        "size_mm": sku_meta.get("size_mm"),
        "status": status,
        "captured_indices": captured,
        "missing_required": missing,
        "required": list(DEFAULT_REQUIRED),
        "views": views,
        "capture_meta": {
            "started_at": (cap or {}).get("started_at"),
            "operator": (cap or {}).get("operator"),
            "station_id": (cap or {}).get("station_id"),
            "tilt_deg": ((cap or {}).get("camera", {}) or {}).get("tilt_deg"),
            "barcode": (cap or {}).get("barcode"),
        },
        "capture_report": rep,
        "pipeline": {
            "api": (api_dir / "report.json").exists(),
            "work": bool(vdirs),
            "model_present": any(v["has_model"] for v in versions),
            "latest_version": versions[-1]["version"] if versions else "",
            "archived": archived,
            "versions": versions,
        },
    }
