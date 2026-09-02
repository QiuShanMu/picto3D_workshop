from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from pipeline.capture.barcode import decode_barcode, decode_barcode_to_dict
from pipeline.capture.webapp import CameraWorker, WebOptions


def _barcode_bgr(value: str, kind: str = "code128") -> np.ndarray:
    """Generate a real barcode image (requires `python-barcode`)."""
    import barcode
    from barcode.writer import ImageWriter

    cls = barcode.get_barcode_class(kind)
    bc = cls(value, writer=ImageWriter())
    buf = io.BytesIO()
    bc.write(buf, options={"write_text": False})
    buf.seek(0)
    arr = np.frombuffer(buf.read(), dtype=np.uint8)
    pil = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    pad = 40
    out = np.full((pil.shape[0] + pad * 2, pil.shape[1] + pad * 2, 3), 255, dtype=np.uint8)
    out[pad : pad + pil.shape[0], pad : pad + pil.shape[1]] = pil
    return out


def test_decode_blank_graceful():
    blank = np.zeros((200, 400, 3), dtype=np.uint8)
    assert decode_barcode(blank).ok is False
    assert decode_barcode(None).ok is False
    assert decode_barcode(np.array([], dtype=np.uint8)).ok is False
    assert decode_barcode_to_dict(blank)["ok"] is False


def test_decode_real_code128():
    pytest.importorskip("barcode")
    img = _barcode_bgr("APP-0812-001")
    res = decode_barcode(img)
    assert res.ok, "expected a recognized barcode"
    assert res.value == "APP-0812-001"


def test_decode_surfaces_type():
    pytest.importorskip("barcode")
    img = _barcode_bgr("SKU-0001")
    res = decode_barcode(img)
    assert res.ok
    assert res.type in ("CODE128", "CODE39", "EAN13", "UPC")


def test_decode_falls_back_when_pyzbar_absent(monkeypatch):
    pytest.importorskip("barcode")
    # Simulate pyzbar being unavailable: only the opencv path runs.
    monkeypatch.setattr("pipeline.capture.barcode._decode_pyzbar", lambda bgr: [])
    img = _barcode_bgr("6901234567892", kind="ean13")
    res = decode_barcode(img)
    # opencv BarcodeDetector may or may not read the synthetic EAN13; either way
    # the call must not raise and must return a well-formed result.
    assert isinstance(res.value, str)


def test_barcode_img_not_in_frames():
    pytest.importorskip("barcode")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        opts = WebOptions(capture_root=root, batch_id="0812", sku_id="APP-0812-001")
        w = CameraWorker(opts)
        w.serial = "TEST-SERIAL"
        w.color = "1920x1080"
        img = _barcode_bgr("APP-0812-001")
        w._do_barcode_capture({"batch": "0812", "sku": "APP-0812-001"}, img)

        cap_dir = root / "0812" / "APP-0812-001"
        assert (cap_dir / "color" / "barcode.jpg").is_file()
        assert (cap_dir / "barcode.json").is_file()
        cap = json.loads((cap_dir / "capture.json").read_text(encoding="utf-8"))
        assert cap.get("barcode", {}).get("value") == "APP-0812-001"
        # The critical invariant: the barcode frame never enters `frames`.
        assert cap.get("frames") == []
