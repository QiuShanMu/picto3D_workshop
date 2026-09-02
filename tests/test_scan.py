from __future__ import annotations

import base64
import io

import cv2
import numpy as np
import pytest

from pipeline.scan.app import create_app


def _code128_b64(value: str) -> str:
    import barcode
    from barcode.writer import ImageWriter

    cls = barcode.get_barcode_class("code128")
    bc = cls(value, writer=ImageWriter())
    buf = io.BytesIO()
    bc.write(buf, options={"write_text": False})
    buf.seek(0)
    arr = np.frombuffer(buf.read(), dtype=np.uint8)
    pil = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    pad = 40
    out = np.full((pil.shape[0] + pad * 2, pil.shape[1] + pad * 2, 3), 255, dtype=np.uint8)
    out[pad : pad + pil.shape[0], pad : pad + pil.shape[1]] = pil
    ok, enc = cv2.imencode(".jpg", out)
    assert ok
    return "data:image/jpeg;base64," + base64.b64encode(enc.tobytes()).decode()


def test_scan_rejects_empty_frame():
    c = create_app().test_client()
    r = c.post("/scan/frame", json={"image": ""})
    assert r.status_code == 400
    r = c.post("/scan/frame", json={})
    assert r.status_code == 400


def test_scan_closed_loop_decode():
    pytest.importorskip("barcode")
    c = create_app().test_client()
    b64 = _code128_b64("APP-0312-077")
    r = c.post("/scan/frame", json={"image": b64})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] and j["value"] == "APP-0312-077"

    r = c.get("/scan/result")
    jr = r.get_json()
    assert jr["value"] == "APP-0312-077"
    assert jr["count"] >= 1


def test_scan_preview_returns_jpeg():
    pytest.importorskip("barcode")
    c = create_app().test_client()
    b64 = _code128_b64("X")
    c.post("/scan/frame", json={"image": b64})
    r = c.get("/scan/preview")
    assert r.status_code == 200
    assert r.mimetype == "image/jpeg"
    assert len(r.data) > 0


def test_scan_degraded_when_pyzbar_absent(monkeypatch):
    monkeypatch.setattr("pipeline.scan.app.decode_barcode", lambda bgr: type(
        "R", (), {"ok": False, "value": "", "type": "UNKNOWN", "source": "none"}))
    c = create_app().test_client()
    # a blank frame (no decode) should produce ok:false but not crash
    r = c.post("/scan/frame", json={"image": "data:image/jpeg;base64," + base64.b64encode(
        cv2.imencode(".jpg", np.zeros((100, 100, 3), np.uint8))[1].tobytes()).decode()})
    assert r.status_code == 200
    assert r.get_json()["ok"] is False
