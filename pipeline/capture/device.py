from __future__ import annotations

"""Capture-device factory + discovery.

Central place to construct a `CaptureDevice` by kind and to enumerate what's
connected. Callers (`run.py`, `webapp.py`, `webui/__main__.py`) should use
these instead of instantiating `D435iCamera` directly, so the same code can
drive a D435i, an Android USB camera, or a future device without edits.
"""

import subprocess

from pipeline.capture.device_base import CaptureDevice, DeviceCapabilities
from pipeline.capture.device_d435i import D435iDevice
from pipeline.capture.device_android import AndroidUsbDevice

VALID_KINDS = ("d435i", "android_usb")


def make_capture_device(kind: str, *, device_id: str | None = None, **opts) -> CaptureDevice:
    """Build a `CaptureDevice` for `kind`.

    Args:
        kind: 'd435i' | 'android_usb'.
        device_id: natural key — for d435i that's a RealSense serial; for
            android_usb it's the ADB device serial (optional).
        **opts: forwarded to the concrete device constructor (camera_id,
            resolution, base_url, enable_depth, color_controls, ...).
    """
    kind = (kind or "d435i").strip().lower()
    if kind == "d435i":
        return D435iDevice(serial=device_id, **opts)
    if kind == "android_usb":
        return AndroidUsbDevice(device_id=device_id, **opts)
    raise ValueError(f"unknown capture device kind: {kind!r} (valid: {list(VALID_KINDS)})")


def list_capture_devices(kind: str | None = None) -> list[dict]:
    """Enumerate connected capture devices as plain JSON-friendly dicts.

    kind=None returns all known kinds; 'd435i' lists RealSense, 'android_usb'
    lists ADB devices in the 'device' state. Each entry carries `kind` so a
    caller can match devices to the factory.
    """
    out: list[dict] = []
    kinds = (kind.lower(),) if kind else VALID_KINDS
    for k in kinds:
        if k == "d435i":
            out.extend(_list_d435i())
        elif k == "android_usb":
            out.extend(_list_android_usb())
    return out


def _list_d435i() -> list[dict]:
    from pipeline.capture.camera import list_devices

    devs = []
    for d in list_devices():
        devs.append({"kind": "d435i", "serial": d.get("serial"), "name": d.get("name"),
                     "firmware": d.get("firmware"), "model": "RealSense D435I"})
    return devs


def _list_android_usb() -> list[dict]:
    adb = "adb"
    out: list[dict] = []
    try:
        proc = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return out
    lines = (proc.stdout or "").splitlines()
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue  # only "device" state is usable
        out.append({"kind": "android_usb", "serial": parts[0], "model": "Android USB Camera"})
    return out


# Re-export for convenience/callers that want the descriptor.
__all__ = ["make_capture_device", "list_capture_devices", "CaptureDevice",
           "DeviceCapabilities", "D435iDevice", "AndroidUsbDevice", "VALID_KINDS"]
