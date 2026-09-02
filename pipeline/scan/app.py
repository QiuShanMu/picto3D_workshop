from __future__ import annotations

"""Standalone SKU scan service.

Phone-as-camera architecture:
  - Phone opens /scan (getUserMedia), samples a frame every ~300ms, POSTs it to
    /scan/frame as base64 JPEG.
  - The service decodes each frame with pipeline.capture.barcode.decode_barcode
    (pyzbar -> opencv -> enhanced) and keeps the most recent scan result.
  - A desktop page polls /scan/result and auto-fills the recognised SKU.

The module is self-contained (no dependency on the D435i capture station) so the
"phone scan" closed loop can be validated independently before wiring into the
production WebUI.
"""

import base64
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template_string, request

from pipeline.capture.barcode import decode_barcode, BarcodeResult


@dataclass
class ScanStore:
    """Thread-safe holder for the most recent decoded frame + result."""

    latest_bgr: np.ndarray | None = None
    latest_at: str | None = None
    result: BarcodeResult = field(default_factory=lambda: BarcodeResult(value="", ok=False))
    result_at: str | None = None
    count: int = 0

    def set_frame(self, bgr: np.ndarray) -> None:
        self.latest_bgr = bgr
        self.latest_at = _now()

    def set_result(self, res: BarcodeResult) -> None:
        self.result = res
        self.result_at = _now()
        self.count += 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64_to_bgr(b64: str) -> np.ndarray | None:
    try:
        raw = base64.b64decode(b64.split(",")[-1])
        arr = np.frombuffer(raw, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _downscale(bgr: np.ndarray, edge: int = 960) -> np.ndarray:
    h, w = bgr.shape[:2]
    cur = max(w, h)
    if cur <= edge:
        return bgr
    sc = edge / cur
    return cv2.resize(bgr, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)


PHONE_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>SKU 扫码 · 手机摄像头</title>
<style>
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,"Microsoft YaHei",sans-serif; background:#0b1220; color:#e2e8f0; }
  header { padding:14px 18px; background:#111a2c; border-bottom:1px solid #223052; }
  header h1 { font-size:1rem; margin:0; }
  header .sub { font-size:.74rem; color:#8ca3c7; margin-top:2px; }
  main { padding:16px; }
  .cam { position:relative; border-radius:14px; overflow:hidden; background:#000; aspect-ratio:3/4; }
  .cam video { width:100%; height:100%; object-fit:cover; }
  .overlay { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; pointer-events:none; }
  .overlay .frame { width:70%; height:50%; border:2px dashed rgba(255,255,255,.55); border-radius:12px; }
  .status { margin-top:14px; font-size:.85rem; }
  .status .row { display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid #1c2a45; }
  .status .k { color:#8ca3c7; }
  .ok { color:#4ade80; } .warn { color:#fbbf24; } .err { color:#f87171; }
  .count { margin-top:12px; font-size:.8rem; color:#8ca3c7; }
  button { width:100%; margin-top:14px; padding:12px; background:#2563eb; color:#fff; border:0; border-radius:12px; font-size:.95rem; font-weight:600; }
  button:disabled { opacity:.4; }
</style>
</head>
<body>
<header><h1>SKU 扫码</h1><div class="sub">把条码对准取景框，持续自动识别</div></header>
<main>
  <div class="cam">
    <video id="v" autoplay muted playsinline></video>
    <div class="overlay"><div class="frame"></div></div>
  </div>
  <div class="status">
    <div class="row"><span class="k">状态</span><span id="st">未开始</span></div>
    <div class="row"><span class="k">最近识别</span><span id="val">—</span></div>
    <div class="row"><span class="k">类型</span><span id="type">—</span></div>
  </div>
  <div class="count" id="count"></div>
  <button id="startBtn">开始扫码</button>
</main>
<script>
const $ = id => document.getElementById(id);
let stream = null, timer = null, scanning = false;
const INTERVAL = 300; // ms — sample a frame ~3x/s (gun-like "always looking")

function setStatus(text, cls){ $('st').textContent = text; $('st').className = cls; }

$('startBtn').onclick = async () => {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setStatus('无法访问相机：请用 HTTPS 或 localhost 打开本页，或在 Chrome 里为本地址开启不安全来源为安全', 'err');
    return;
  }
  let startErr = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
  } catch (e) {
    startErr = e;
    try {
      // fall back to any camera
      stream = await navigator.mediaDevices.getUserMedia({ video: true });
      startErr = null;
    } catch (e2) {
      startErr = e2;
    }
  }
  if (!stream) {
    setStatus('相机打开失败：' + ((startErr && startErr.name) || 'NotAllowedError') + '（请允许相机权限；若为局域网 HTTP 请开 HTTPS）', 'err');
    return;
  }
  $('v').srcObject = stream;
  $('startBtn').disabled = true;
  setStatus('扫描中…', 'ok');
  scanning = true;
  loop();
  timer = setInterval(loop, INTERVAL);
};
function loop(){
  if (!scanning) return;
  const v = $('v');
  if (!v.videoWidth) return;
  const c = document.createElement('canvas');
  const w = v.videoWidth, h = v.videoHeight;
  c.width = w; c.height = h;
  c.getContext('2d').drawImage(v, 0, 0, w, h);
  const jpg = c.toDataURL('image/jpeg', 0.8);
  fetch('/scan/frame', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ image: jpg }) })
    .then(r => r.json()).then(j => {
      if (j.ok && j.value) {
        $('val').textContent = j.value; $('val').className = 'ok';
        $('type').textContent = (j.type||'?') + ' / ' + (j.source||'?');
        $('count').textContent = '已识别 ' + j.count + ' 次';
      }
    })
    .catch(()=>{});
}
</script>
</body>
</html>
"""

DESKTOP_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SKU 扫码 · 电脑结果端</title>
<style>
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,"Microsoft YaHei",sans-serif; background:#0f172a; color:#e2e8f0; }
  header { padding:16px 24px; background:#1e293b; border-bottom:1px solid #334155; }
  header h1 { font-size:1.05rem; margin:0; }
  main { max-width:880px; margin:24px auto; padding:0 20px; display:grid; gap:20px; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:14px; padding:18px; }
  .big { font-size:1.6rem; font-weight:700; letter-spacing:.5px; }
  .big.ok { color:#4ade80; } .big.warn { color:#fbbf24; }
  .meta { color:#94a3b8; font-size:.85rem; margin-top:6px; }
  .preview { width:100%; border-radius:10px; background:#000; }
  .preview img { width:100%; display:block; border-radius:10px; }
  .row { display:flex; gap:14px; align-items:flex-start; }
  .left { flex:1; }
  .chip { display:inline-block; padding:3px 10px; border-radius:14px; font-size:.74rem; background:#0f172a; border:1px solid #334155; color:#94a3b8; }
</style>
</head>
<body>
<header><h1>SKU 扫码 · 电脑结果端</h1></header>
<main>
  <div class="card">
    <div class="chip">手机连到 /scan 页面即可开始</div>
    <div class="big" id="val">—</div>
    <div class="meta" id="meta">等待扫码…</div>
  </div>
  <div class="card">
    <h3 style="margin:0 0 10px">手机画面</h3>
    <div class="preview"><img id="preview" src="/scan/preview" alt="手机画面"></div>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
async function poll(){
  try {
    const r = await fetch('/scan/result');
    const j = await r.json();
    if (j.value) {
      $('val').textContent = j.value; $('val').className = 'big ok';
      $('meta').textContent = (j.type||'?') + ' · ' + (j.source||'?') + ' · ' + (j.result_at||'') + ' · 扫码 ' + j.count + ' 次';
    } else {
      $('val').textContent = '—'; $('val').className = 'big warn';
      $('meta').textContent = '等待扫码…';
    }
    // refresh preview (cache bust)
    $('preview').src = '/scan/preview?t=' + Date.now();
  } catch(e){}
}
setInterval(poll, 800);
poll();
</script>
</body>
</html>
"""


def create_app() -> Flask:
    app = Flask(__name__)
    store = ScanStore()
    lock = threading.Lock()

    @app.route("/")
    def index():
        # desktop result page by default; phone page accessible at /scan
        return render_template_string(DESKTOP_PAGE)

    @app.route("/scan")
    def scan_page():
        return render_template_string(PHONE_PAGE)

    @app.route("/scan/frame", methods=["POST"])
    def scan_frame():
        payload = request.get_json(silent=True) or {}
        bgr = _b64_to_bgr(payload.get("image", ""))
        if bgr is None or bgr.size == 0:
            return jsonify({"ok": False, "error": "bad frame"}), 400
        small = _downscale(bgr, 960)
        res = decode_barcode(small)
        with lock:
            store.set_frame(small)
            store.set_result(res)
        return jsonify({
            "ok": res.ok,
            "value": res.value,
            "type": res.type if res.ok else "",
            "source": res.source if res.ok else "",
            "count": store.count,
        })

    @app.route("/scan/result")
    def scan_result():
        with lock:
            r = store.result
            return jsonify({
                "ok": r.ok,
                "value": r.value,
                "type": r.type,
                "source": r.source,
                "result_at": store.result_at,
                "count": store.count,
            })

    @app.route("/scan/preview")
    def scan_preview():
        with lock:
            bgr = store.latest_bgr
        if bgr is None:
            return Response(status=204)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return Response(buf.tobytes(), mimetype="image/jpeg")

    app.config["scan_store"] = store
    return app
