from __future__ import annotations

"""SKU barcode decode for the capture station.

Decoding priority: pyzbar (covers Code128/39, EAN/UPC, QR) -> OpenCV
barcode_BarcodeDetector (EAN/UPC/QR/DataMatrix as a fallback) -> no result.
The module degrades gracefully when pyzbar is not installed (opencv/typing only)
so the capture flow is never blocked by a missing optional dependency.

Input is a BGR frame (as grabbed by the D435i). Barcodes are usually dark-on-
light; we run a small contrast/auto-threshold pass to help blurred or dim labels.
"""

from dataclasses import dataclass

import cv2
import numpy as np

BARCODE_TYPES = {"CODE128", "CODE39", "EAN13", "EAN8", "UPC", "QRCODE", "DATAMATRIX", "AZTEC", "UNKNOWN"}
SOURCE_PYZBAR = "pyzbar"
SOURCE_OPENCV = "opencv"


@dataclass
class BarcodeResult:
    value: str
    type: str = "UNKNOWN"
    source: str = SOURCE_PYZBAR
    ok: bool = True

    def to_dict(self) -> dict:
        return {"value": self.value, "type": self.type, "source": self.source, "ok": self.ok}


def _normalize_barcode_types(barcode_type: str | None) -> str:
    """Map pyzbar / opencv type names to a stable BARCODE_TYPES token."""
    if not barcode_type:
        return "UNKNOWN"
    t = barcode_type.upper().replace("-", "").replace("_", "")
    mapping = {
        "CODE128": "CODE128",
        "CODE39": "CODE39",
        "EAN13": "EAN13",
        "EAN8": "EAN8",
        "UPCA": "UPC",
        "UPCE": "UPC",
        "UPC": "UPC",
        "QRCODE": "QRCODE",
        "QR": "QRCODE",
        "DATAMATRIX": "DATAMATRIX",
        "DATA-MATRIX": "DATAMATRIX",
        "AZTEC": "AZTEC",
        "ITF": "ITF",
    }
    for k, v in mapping.items():
        if k in t:
            return v
    return "UNKNOWN"


def _to_gray(img: np.ndarray) -> np.ndarray:
    """Return a single-channel gray image regardless of input channels."""
    if img is None or img.size == 0:
        return img
    if img.ndim == 2:
        return img
    if img.shape[2] == 1:
        return img[:, :, 0]
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _decode_pyzbar(bgr: np.ndarray) -> list[dict]:
    """Decode with pyzbar if available; returns list of {value,type,source}."""
    try:
        from pyzbar import pyzbar
    except Exception:
        return []
    gray = _to_gray(bgr)
    results = []
    try:
        decoded = pyzbar.decode(gray)
    except Exception:
        return []
    for d in decoded:
        try:
            value = d.data.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        if not value:
            continue
        results.append({
            "value": value,
            "type": _normalize_barcode_types(d.type),
            "source": SOURCE_PYZBAR,
        })
    return results


def _decode_opencv(bgr: np.ndarray) -> list[dict]:
    """Decode with OpenCV BarcodeDetector if available; list of {value,type,source}."""
    detector = None
    for name in ("barcode_BarcodeDetector", "BarcodeDetector"):
        if hasattr(cv2, name):
            d = getattr(cv2, name)()
            if d is not None:
                detector = d
                break
    if detector is None:
        return []
    results = []
    gray = _to_gray(bgr)
    try:
        ok, decoded, _ = detector.detectAndDecode(gray)
        if ok and decoded:
            results.append({"value": decoded.strip(), "type": _normalize_barcode_types("QRCODE"), "source": SOURCE_OPENCV})
    except Exception:
        pass
    return results


def _enhance(bgr: np.ndarray) -> np.ndarray:
    """Light contrast/sharpness pass to help blurred or dim barcode labels."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    # adaptive threshold to binarize dark-on-light barcodes
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 15
    )
    return binary


def decode_barcode(bgr: np.ndarray, *, try_enhance: bool = True) -> BarcodeResult:
    """Decode the first readable barcode in a BGR frame.

    Priority: pyzbar -> opencv -> (enhanced pass) pyzbar/opencv -> fail.
    Returns a BarcodeResult with ok=False when nothing is found.
    """
    if bgr is None or bgr.size == 0:
        return BarcodeResult(value="", ok=False)

    for source in (SOURCE_PYZBAR, SOURCE_OPENCV):
        if source == SOURCE_PYZBAR:
            hits = _decode_pyzbar(bgr)
        else:
            hits = _decode_opencv(bgr)
        if hits:
            h = hits[0]
            return BarcodeResult(value=h["value"], type=h["type"], source=h["source"], ok=True)

    # enhanced (binarized) pass
    if try_enhance:
        enhanced = _enhance(bgr)
        for source in (SOURCE_PYZBAR, SOURCE_OPENCV):
            if source == SOURCE_PYZBAR:
                hits = _decode_pyzbar(cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR))
            else:
                hits = _decode_opencv(enhanced)
            if hits:
                h = hits[0]
                return BarcodeResult(value=h["value"], type=h["type"], source=h["source"], ok=True)

    return BarcodeResult(value="", ok=False)


def decode_barcode_to_dict(bgr: np.ndarray) -> dict:
    return decode_barcode(bgr).to_dict()
