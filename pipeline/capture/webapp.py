from __future__ import annotations

"""Interactive capture web page: live preview + one-click frame capture.

Single background thread owns the RealSense pipeline (preview via MJPEG, and
captures on request). A click on the page posts /capture; the thread writes
the next frame to <capture_root>/<batch>/<sku>/ with WB + shading applied and
an on-the-spot gate, then returns the path.

Run:  python -m pipeline.capture.webapp --batch 0812 --sku APP-0812-001
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template_string, request

from pipeline.capture.barcode import decode_barcode
from pipeline.capture.camera import ColorControls
from pipeline.capture.device import make_capture_device
from pipeline.capture.device_base import CaptureDevice, DeviceCapabilities
from pipeline.capture.gate import gate_frame
from pipeline.capture.run import _atomic_write_bgr, _apply_rotate, _load_shading_lut, _long_edge_resize, OUTPUT_EDGE, JPEG_QUALITY, SHADING_DIR
from pipeline.capture.shading import calibrate_shading

DEFAULT_CAPTURE_ROOT = "data/captures"

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>D435i 工位采集</title>
<style>
  :root { --accent:#2563eb; --bg:#0f172a; --panel:#1e293b; --text:#e2e8f0; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,"Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); }
  header { padding:18px 28px; background:var(--panel); border-bottom:1px solid #334155; display:flex; align-items:center; justify-content:space-between; }
  header h1 { font-size:1.05rem; font-weight:600; margin:0; }
  header .meta { font-size:.8rem; color:#94a3b8; }
  main { max-width:1000px; margin:24px auto; padding:0 20px; display:grid; gap:20px; }
  .card { background:var(--panel); border:1px solid #334155; border-radius:14px; padding:18px; }
  .preview { position:relative; border-radius:10px; overflow:hidden; background:#000; }
  .preview img { display:block; width:100%; height:auto; }
  .preview .badge { position:absolute; top:10px; left:10px; background:rgba(0,0,0,.55); padding:4px 10px; border-radius:6px; font-size:.75rem; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
  label { display:block; font-size:.78rem; color:#94a3b8; margin-bottom:4px; }
  input,select { background:#0f172a; color:var(--text); border:1px solid #334155; border-radius:8px; padding:9px 11px; font-size:.9rem; min-width:150px; }
  .btn { background:var(--accent); color:#fff; border:0; border-radius:10px; padding:12px 22px; font-size:.95rem; font-weight:600; cursor:pointer; transition:opacity .15s; }
  .btn:hover { opacity:.9; }
  .btn:disabled { opacity:.4; cursor:not-allowed; }
  .status { font-size:.85rem; margin-top:12px; min-height:1.2em; color:#94a3b8; }
  .status.ok { color:#4ade80; } .status.err { color:#f87171; }
  .gates { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:14px; font-size:.8rem; }
  .gate { border:1px solid #334155; border-radius:8px; padding:9px; text-align:center; background:#0f172a; }
  .gate .v { font-size:1.05rem; font-weight:600; }
  .gate .l { color:#94a3b8; }
</style>
</head>
<body>
<header>
  <h1>D435i 工位采集 · 点击即拍</h1>
  <div class="meta">serial <b id="serial">—</b> · <span id="res">—</span></div>
</header>
<main>
  <section class="card">
    <div class="preview">
      <img id="live" src="/stream" alt="live">
      <span class="badge" id="badge">LIVE</span>
    </div>
    <div class="gates">
      <div class="gate"><div class="v" id="gSharp">—</div><div class="l">清晰度</div></div>
      <div class="gate"><div class="v" id="gExp">—</div><div class="l">过曝</div></div>
      <div class="gate"><div class="v" id="gObj">—</div><div class="l">主体占比</div></div>
    </div>
  </section>

  <section class="card">
    <div class="row">
      <div><label>批次 batch</label><input id="batch" value="{{ default_batch }}"></div>
      <div><label>SKU</label><input id="sku" value="{{ default_sku }}"></div>
      <div><label>档位 index</label>
        <select id="index">
          <option value="01">01 · 0° 正面</option>
          <option value="02">02 · 45° 左前</option>
          <option value="03">03 · 90° 左</option>
          <option value="04">04 · 135°</option>
          <option value="05">05 · 180° 后</option>
          <option value="06">06 · 225°</option>
          <option value="07">07 · 270° 右</option>
          <option value="08">08 · 315° 右前</option>
        </select>
      </div>
    </div>
    <div class="row" style="margin-top:14px;">
      <button class="btn" id="shoot">采集一张</button>
      <div class="status" id="status">就绪</div>
    </div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
const STATUS = { sharp:70, exp:0.05, objLo:0.40, objHi:0.92 };
$('shoot').onclick = async () => {
  $('shoot').disabled = true;
  setStatus('采集请求中…', '');
  const body = { batch:$('batch').value, sku:$('sku').value, index:$('index').value };
  try {
    const r = await fetch('/capture', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    const j = await r.json();
    if (j.ok) {
      setStatus(`已保存 ${j.index} → ${j.relative} · ${j.gate.sharpness.toFixed(0)}清晰 / ${(j.gate.exposure*100).toFixed(1)}%过曝`, 'ok');
      $('gSharp').textContent = j.gate.sharpness.toFixed(0);
      $('gExp').textContent = (j.gate.exposure*100).toFixed(1)+'%';
      $('gObj').textContent = (j.gate.object_ratio*100).toFixed(0)+'%';
    } else {
      setStatus(`拒绝：${j.reason || j.error}`, 'err');
    }
  } catch(e) { setStatus('请求失败：'+e, 'err'); }
  $('shoot').disabled = false;
};
function setStatus(msg, cls){ const s=$('status'); s.textContent=msg; s.className='status '+cls; }
</script>
</body>
</html>
"""


@dataclass
class WebOptions:
    host: str = "127.0.0.1"
    port: int = 5000
    batch_id: str = "0812"
    sku_id: str = "APP-0812-001"
    capture_root: Path = Path(DEFAULT_CAPTURE_ROOT)
    camera_kind: str = "d435i"  # "d435i" | "android_usb"; drives make_capture_device
    serial: str | None = None
    enable_depth: bool = False
    wb: int = 5500
    exposure: int | None = None
    gain: int | None = None
    apply_shading: bool = True
    shading_lut: Path | None = None
    min_sharpness: float = 12.0
    max_exposure: float = 0.10
    min_object_ratio: float = 0.10
    max_object_ratio: float = 0.98
    # Android USB camera options (only used when camera_kind == "android_usb")
    android: dict | None = None  # {camera_id, resolution, fps, jpeg_quality, base_url, adb, adb_forward}


def _gate_payload(gate) -> dict:
    warnings = []
    if not gate.sharp_ok:
        warnings.append(f"清晰度偏低（{gate.sharpness:.1f}）")
    if gate.brightness_status == "too_bright":
        warnings.append(f"画面过亮（高光剪切 {gate.overexposure:.1%}），建议降低 EV")
    elif gate.brightness_status == "too_dark":
        warnings.append(f"画面过暗（暗部剪切 {gate.underexposure:.1%}），建议提高 EV")
    elif gate.brightness_status == "mixed_clipping":
        warnings.append(f"明暗同时剪切 {gate.exposure:.1%}，建议调整布光或 EV")
    if not gate.object_ok:
        warnings.append(f"主体占比 {gate.object_ratio:.1%} 不在建议范围内")
    return {
        "ok": gate.ok,
        "sharpness": gate.sharpness,
        "sharp_ok": gate.sharp_ok,
        "exposure": gate.exposure,
        "exposure_ok": gate.exposure_ok,
        "underexposure": gate.underexposure,
        "overexposure": gate.overexposure,
        "brightness": gate.brightness,
        "brightness_status": gate.brightness_status,
        "object_ratio": gate.object_ratio,
        "object_ok": gate.object_ok,
        "reason": gate.reason,
        "warnings": warnings,
    }


class CameraWorker:
    """Background thread that owns the pipeline: serves preview & captures."""

    def __init__(self, opts: WebOptions) -> None:
        self.opts = opts
        self.latest_jpeg: bytes | None = None
        self.latest_raw: np.ndarray | None = None
        self.serial = ""
        self.color = ""
        self.lut = None
        self._stop = threading.Event()
        self._capture_req: dict | None = None
        self._capture_done = threading.Event()
        self._capture_result: dict | None = None
        self._barcode_req: dict | None = None
        self._barcode_done = threading.Event()
        self._barcode_result: dict | None = None
        self._control_req: dict | None = None
        self._control_done = threading.Event()
        self._control_result: dict | None = None
        self.latest_quality: dict = {}
        self.camera_controls: dict = {}
        self._base_exposure = 1.0
        self._ev = 0.0
        self._frame_count = 0
        self._device = None
        self.capabilities: DeviceCapabilities | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if hasattr(self, "_thread"):
            self._thread.join(timeout=5)

    def request_capture(self, payload: dict) -> dict:
        self._capture_result = None
        self._capture_req = payload
        self._capture_done.clear()
        # Wait (max ~3s) for the worker to capture the next frame.
        ok = self._capture_done.wait(timeout=3.0)
        result = self._capture_result or {"ok": False, "error": "capture timeout"}
        return result

    def request_barcode_capture(self, payload: dict) -> dict:
        """Grab the current frame, decode the SKU barcode, archive it, return the
        recognition result. The barcode image is stored outside the `frames` list
        so it never flows into the image-to-3D input."""
        self._barcode_result = None
        self._barcode_req = payload
        self._barcode_done.clear()
        self._barcode_done.wait(timeout=3.0)
        result = self._barcode_result or {"ok": False, "error": "barcode capture timeout"}
        return result

    def request_exposure(self, payload: dict) -> dict:
        self._control_result = None
        self._control_req = payload
        self._control_done.clear()
        self._control_done.wait(timeout=3.0)
        return self._control_result or {"ok": False, "error": "曝光参数设置超时"}

    def status(self) -> dict:
        return {
            "serial": self.serial,
            "color": self.color,
            "ev": self._ev,
            "controls": dict(self.camera_controls),
            "quality": dict(self.latest_quality),
            "supports_exposure_control": bool(
                self.capabilities and self.capabilities.supports_exposure_control
            ),
        }

    def _apply_exposure_request(self, cam: CaptureDevice, payload: dict) -> dict:
        try:
            auto = bool(payload.get("auto_exposure", False))
            ev = float(payload.get("ev", self._ev))
            ev = min(3.0, max(-3.0, ev))
            gain = payload.get("gain")
            gain = None if gain in (None, "") else float(gain)
            exposure = None if auto else self._base_exposure * (2.0 ** ev)
            controls = cam.set_exposure_controls(
                auto_exposure=auto,
                exposure=exposure,
                gain=gain,
            )
            self._ev = 0.0 if auto else ev
            self.camera_controls = controls
            self.opts.exposure = int(round(controls["exposure"]))
            self.opts.gain = int(round(controls["gain"]))
            return {
                "ok": True,
                "ev": self._ev,
                "controls": controls,
                "message": (
                    "已启用自动曝光"
                    if auto
                    else f"已应用 EV {self._ev:+.1f}，曝光 {controls['exposure']:.0f}，增益 {controls['gain']:.0f}"
                ),
            }
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"曝光参数无效: {exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"相机曝光设置失败: {exc}"}

    def register_sku(self, batch: str, sku: str, source: str = "") -> dict:
        """Persist the first barcode/input time without overwriting it later."""
        batch = str(batch or self.opts.batch_id).strip()
        sku = str(sku or "").strip()
        if not batch or not sku:
            return {"ok": False, "error": "缺少 batch / sku"}
        if (
            batch in {".", ".."} or sku in {".", ".."}
            or Path(batch).name != batch or Path(sku).name != sku
            or "/" in batch or "\\" in batch or "/" in sku or "\\" in sku
        ):
            return {"ok": False, "error": "batch / sku 包含非法路径字符"}

        capture_dir = Path(self.opts.capture_root) / batch / sku
        cap_path = capture_dir / "capture.json"
        if cap_path.exists():
            try:
                cap = json.loads(cap_path.read_text(encoding="utf-8"))
            except Exception:
                cap = {}
        else:
            cap = {}

        created = not bool(cap.get("registered_at"))
        registered_at = cap.get("registered_at") or _now_iso()
        cap.setdefault("schema", "capture.v1")
        cap.setdefault("sku_id", sku)
        cap.setdefault("batch_id", batch)
        cap.setdefault("station_id", "d435i-desk-1")
        cap.setdefault("operator", "")
        cap.setdefault("started_at", registered_at)
        cap.setdefault("pose_mode", "yaw_manual_marks")
        cap.setdefault("rotation", {"method": "manual_marks", "step_deg": 45, "direction": "ccw"})
        cap.setdefault("camera", {
            "model": "RealSense D435I",
            "serial": self.serial,
            "color": self.color,
            "tilt_deg": 25,
            "color_controls": {
                "white_balance": self.opts.wb,
                "exposure": self.opts.exposure,
                "gain": self.opts.gain,
                "auto_white_balance": False,
                "auto_exposure": False,
            },
        })
        cap.setdefault("frames", [])
        cap.setdefault("status", "captured")
        cap["registered_at"] = registered_at
        if created:
            cap["registration_source"] = source or "manual"
        _write_json(cap_path, cap)
        return {
            "ok": True,
            "registered_at": registered_at,
            "registration_source": cap.get("registration_source", ""),
            "created": created,
        }

    def recalibrate_from_live(self, n: int = 8) -> dict:
        """Build a new shading LUT from live RAW frames and hot-swap it."""
        frames: list[np.ndarray] = []
        for _ in range(n):
            raw = self.latest_raw
            if raw is not None:
                frames.append(raw.astype(np.float32).copy())
            time.sleep(0.08)
        if len(frames) < 3:
            return {"ok": False, "error": "预览帧不足，请等相机出画后再标定"}
        ref = np.mean(frames, axis=0).astype(np.uint8)
        lut = calibrate_shading(
            ref, tiles=24, min_gain=0.65, max_gain=1.45,
            anchor="white", flatten_luma=True,
        )
        serial = self.serial or "unknown"
        out = Path(self.opts.capture_root) / SHADING_DIR / f"{serial}_shading.json"
        lut.save(out, extra={
            "serial": serial, "anchor": "white", "flatten_luma": True,
            "white_balance": int(self.opts.wb),
        })
        self.lut = lut
        return {
            "ok": True, "path": str(out), "tiles": lut.tiles,
            "v_bands": 0 if lut.v_grid is None else int(lut.v_grid.shape[1]),
            "message": f"已更新校色 LUT（{lut.tiles}×{lut.tiles} + 竖直残差）",
        }

    def _run(self) -> None:
        opts = self.opts
        ctrl = ColorControls(white_balance=opts.wb, exposure=opts.exposure, gain=opts.gain)
        dev_kwargs = {}
        if opts.camera_kind == "android_usb" and opts.android:
            dev_kwargs.update(opts.android)
        with make_capture_device(opts.camera_kind, serial=opts.serial,
                                 enable_depth=opts.enable_depth, color_controls=ctrl,
                                 **dev_kwargs) as dev:
            self._device = dev
            self.capabilities = dev.capabilities
            info = dev.open()
            self.serial = info.serial
            self.color = info.color
            if dev.capabilities.supports_exposure_control:
                self.camera_controls = dev.exposure_controls() or {}
                self._base_exposure = max(1.0, float(self.camera_controls.get("exposure") or 1.0))
                self.opts.exposure = int(round(self._base_exposure))
                self.opts.gain = int(round(float(self.camera_controls.get("gain") or 0.0)))
            else:
                self.camera_controls = {}
                self._base_exposure = 1.0
            if dev.capabilities.supports_shading:
                self.lut = _load_shading_lut(
                    _ShadingOpts(opts.apply_shading, opts.shading_lut, opts.capture_root),
                    info.serial,
                )
            else:
                self.lut = None
            while not self._stop.is_set():
                if self._control_req is not None and dev.capabilities.supports_exposure_control:
                    request_payload = self._control_req
                    self._control_req = None
                    self._control_result = self._apply_exposure_request(dev, request_payload)
                    self._control_done.set()
                elif self._control_req is not None:
                    self._control_req = None
                    self._control_result = {"ok": False, "error": "该设备不支持曝光控制"}
                    self._control_done.set()

                bundle = dev.grab(index="01", yaw_deg=0)
                self.latest_raw = bundle.color
                preview_bgr = self.lut.apply(bundle.color) if self.lut is not None else bundle.color
                # JPEG for MJPEG preview (downscale a bit to keep it snappy)
                prev = _long_edge_resize(preview_bgr, 960)
                ok, buf = cv2.imencode(".jpg", prev, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    self.latest_jpeg = buf.tobytes()
                self._frame_count += 1
                if self._frame_count % 6 == 1:
                    quality_frame = _long_edge_resize(bundle.color, 960)
                    self.latest_quality = _gate_payload(gate_frame(
                        quality_frame,
                        min_sharpness=opts.min_sharpness,
                        max_exposure=opts.max_exposure,
                        min_object_ratio=opts.min_object_ratio,
                        max_object_ratio=opts.max_object_ratio,
                    ))
                    if self.camera_controls.get("auto_exposure") and dev.capabilities.supports_exposure_control:
                        try:
                            self.camera_controls = dev.exposure_controls()
                        except Exception:
                            pass

                if self._capture_req is not None:
                    req = self._capture_req
                    self._capture_req = None
                    self._do_capture(req, bundle.color)
                    self._capture_done.set()

                if self._barcode_req is not None:
                    breq = self._barcode_req
                    self._barcode_req = None
                    self._do_barcode_capture(breq, bundle.color)
                    self._barcode_done.set()

    def _do_capture(self, req: dict, raw_bgr: np.ndarray) -> None:
        opts = self.opts
        batch = req.get("batch") or opts.batch_id
        sku = req.get("sku") or opts.sku_id
        index = req.get("index") or "01"
        yaw = int(req.get("yaw_deg", _index_yaw(index)))

        gate = gate_frame(
            raw_bgr,
            min_sharpness=opts.min_sharpness,
            max_exposure=opts.max_exposure,
            min_object_ratio=opts.min_object_ratio,
            max_object_ratio=opts.max_object_ratio,
        )
        # WebUI gates are advisory: operators must always be able to capture.
        # The quality verdict and warnings are persisted for later review.
        gate_data = _gate_payload(gate)

        capture_dir = opts.capture_root / batch / sku
        color_dir = capture_dir / "color"
        color_dir.mkdir(parents=True, exist_ok=True)

        saved_color = self.lut.apply(raw_bgr) if self.lut is not None else raw_bgr
        rotate = int(req.get("rotate", 0) or 0)
        rotated = _apply_rotate(saved_color, rotate)
        resized = _long_edge_resize(rotated, OUTPUT_EDGE)
        frame_name = f"{index}_yaw{yaw:03d}"
        color_file = f"{frame_name}.jpg"
        _atomic_write_bgr(color_dir / color_file, resized, JPEG_QUALITY)

        # Append / update capture.json
        cap_path = capture_dir / "capture.json"
        cap = json.loads(cap_path.read_text(encoding="utf-8")) if cap_path.exists() else {
            "schema": "capture.v1", "sku_id": sku, "batch_id": batch,
            "station_id": "d435i-desk-1", "operator": "", "started_at": _now_iso(),
            "pose_mode": "yaw_manual_marks",
            "rotation": {"method": "manual_marks", "step_deg": 45, "direction": "ccw"},
            "camera": {"model": "RealSense D435I", "serial": self.serial, "color": self.color,
                       "tilt_deg": 25, "color_controls": {"white_balance": self.opts.wb,
                       "exposure": self.opts.exposure, "gain": self.opts.gain,
                       "auto_white_balance": False, "auto_exposure": False}},
            "frames": [], "status": "captured",
        }
        camera_meta = cap.setdefault("camera", {})
        camera_meta["kind"] = self.opts.camera_kind
        camera_meta["serial"] = self.serial
        camera_meta["color"] = self.color
        camera_meta["color_controls"] = {
            "white_balance": self.opts.wb,
            "exposure": self.camera_controls.get("exposure", self.opts.exposure),
            "gain": self.camera_controls.get("gain", self.opts.gain),
            "ev": self._ev,
            "auto_white_balance": False,
            "auto_exposure": bool(self.camera_controls.get("auto_exposure", False)),
        }
        frames = cap.setdefault("frames", [])
        existing = next((f for f in frames if f.get("index") == index), None)
        frame_entry = {
            "index": index, "yaw_deg": yaw, "pose": "yaw",
            "rotate": rotate,
            "hunyuan": _hunyuan_field(index),
            "color": f"color/{color_file}", "depth": None,
            "gate": gate_data,
            "ok": True, "captured_at": _now_iso(),
        }
        if existing:
            frames.remove(existing)
        frames.append(frame_entry)
        cap["status"] = "captured"
        _write_json(cap_path, cap)

        self._capture_result = {
            "ok": True,
            "quality_ok": gate.ok,
            "warning": "；".join(gate_data["warnings"]),
            "index": index,
            "relative": f"color/{color_file}",
            "path": str(color_dir / color_file),
            "gate": frame_entry["gate"],
        }

    def _do_barcode_capture(self, req: dict, raw_bgr: np.ndarray) -> None:
        """Grab + decode the SKU barcode, archive the image (outside `frames`),
        and record it into `capture.json`'s top-level `barcode` field."""
        opts = self.opts
        batch = req.get("batch") or opts.batch_id
        sku = req.get("sku") or opts.sku_id
        manual = bool(req.get("manual", False))

        capture_dir = opts.capture_root / batch / sku
        color_dir = capture_dir / "color"
        color_dir.mkdir(parents=True, exist_ok=True)

        # Barcode frames are stored at native resolution for the best decode odds.
        # We intentionally do NOT apply the shading LUT (barcode needs high contrast).
        _atomic_write_bgr(color_dir / "barcode.jpg", raw_bgr, JPEG_QUALITY)

        decoded = decode_barcode(raw_bgr)
        value = decoded.value
        btype = decoded.type if decoded.ok else "UNKNOWN"
        source = decoded.source if decoded.ok else "none"

        barcode_payload = {
            "value": value,
            "raw": value,
            "type": btype,
            "source": source,
            "image": "color/barcode.jpg",
            "captured_at": _now_iso(),
            "manual": manual,
        }
        _write_json(capture_dir / "barcode.json", barcode_payload)

        # Merge into capture.json top-level `barcode` (create skeleton when absent).
        cap_path = capture_dir / "capture.json"
        if cap_path.exists():
            try:
                cap = json.loads(cap_path.read_text(encoding="utf-8"))
            except Exception:
                cap = {}
        else:
            cap = {
                "schema": "capture.v1", "sku_id": sku, "batch_id": batch,
                "station_id": "d435i-desk-1", "operator": "", "started_at": _now_iso(),
                "pose_mode": "yaw_manual_marks",
                "rotation": {"method": "manual_marks", "step_deg": 45, "direction": "ccw"},
                "camera": {"model": "RealSense D435I", "serial": self.serial, "color": self.color,
                           "tilt_deg": 25, "color_controls": {"white_balance": self.opts.wb,
                           "exposure": self.opts.exposure, "gain": self.opts.gain,
                           "auto_white_balance": False, "auto_exposure": False}},
                "frames": [], "status": "barcode_only",
            }
        cap["barcode"] = {
            "value": value,
            "image": "color/barcode.jpg",
            "captured_at": _now_iso(),
            "auto": (source != "none"),
        }
        _write_json(cap_path, cap)

        self._barcode_result = {
            "ok": True,
            "value": value,
            "type": btype,
            "source": source,
            "manual": manual,
            "relative": "color/barcode.jpg",
            "path": str(color_dir / "barcode.jpg"),
        }


# small helpers
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _index_yaw(index: str) -> int:
    from pipeline.views import SLOT_BY_INDEX
    s = SLOT_BY_INDEX.get(index)
    return s.degrees if s and s.degrees >= 0 else 0


def _hunyuan_field(index: str) -> str | None:
    from pipeline.views import SLOT_BY_INDEX
    s = SLOT_BY_INDEX.get(index)
    return s.hunyuan_field if s else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class _ShadingOpts:
    def __init__(self, apply_shading: bool, shading_lut: Path | None, capture_root: Path) -> None:
        self.apply_shading = apply_shading
        self.shading_lut = shading_lut
        self.capture_root = capture_root


def create_app(opts: WebOptions) -> Flask:
    app = Flask(__name__)
    worker = CameraWorker(opts)
    worker.start()

    @app.route("/")
    def index():
        return render_template_string(PAGE, default_batch=opts.batch_id, default_sku=opts.sku_id)

    @app.route("/stream")
    def stream():
        def gen():
            while True:
                if worker.latest_jpeg is not None:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + worker.latest_jpeg + b"\r\n")
                time.sleep(0.03)
        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/info")
    def info():
        return jsonify({"serial": worker.serial, "color": worker.color})

    @app.route("/camera/status")
    def camera_status():
        return jsonify(worker.status())

    @app.route("/camera/exposure", methods=["POST"])
    def camera_exposure():
        payload = request.get_json(silent=True) or {}
        result = worker.request_exposure(payload)
        return jsonify(result), 200 if result.get("ok") else 400

    @app.route("/capture", methods=["POST"])
    def capture():
        payload = request.get_json(silent=True) or {}
        result = worker.request_capture(payload)
        return jsonify(result), 200 if result.get("ok") else 400

    @app.route("/barcode", methods=["POST"])
    def barcode():
        payload = request.get_json(silent=True) or {}
        result = worker.request_barcode_capture(payload)
        return jsonify(result), 200 if result.get("ok") else 400

    @app.teardown_appcontext
    def _cleanup(exc):
        pass

    app.config["worker"] = worker
    return app


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="D435i interactive capture web page")
    parser.add_argument("--batch", default="0812")
    parser.add_argument("--sku", default="APP-0812-001")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--capture-root", type=Path, default=Path(DEFAULT_CAPTURE_ROOT))
    parser.add_argument("--serial", default=None)
    parser.add_argument("--wb", type=int, default=5500)
    parser.add_argument("--exposure", type=int, default=None)
    parser.add_argument("--gain", type=int, default=None)
    parser.add_argument("--no-shading", action="store_true")
    parser.add_argument("--shading-lut", type=Path, default=None)
    parser.add_argument("--min-sharpness", type=float, default=WebOptions.min_sharpness)
    parser.add_argument("--max-exposure", type=float, default=WebOptions.max_exposure)
    parser.add_argument("--min-object-ratio", type=float, default=WebOptions.min_object_ratio)
    parser.add_argument("--max-object-ratio", type=float, default=WebOptions.max_object_ratio)
    args = parser.parse_args(argv)

    opts = WebOptions(
        host=args.host, port=args.port, batch_id=args.batch, sku_id=args.sku,
        capture_root=args.capture_root, serial=args.serial, wb=args.wb,
        exposure=args.exposure, gain=args.gain,
        apply_shading=not args.no_shading, shading_lut=args.shading_lut,
        min_sharpness=args.min_sharpness, max_exposure=args.max_exposure,
        min_object_ratio=args.min_object_ratio, max_object_ratio=args.max_object_ratio,
    )
    app = create_app(opts)
    print(f"Capture web page: http://{args.host}:{args.port}  (serial: {opts.serial or 'first found'})")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
