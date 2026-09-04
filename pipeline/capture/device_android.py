from __future__ import annotations

"""Android phone camera device via USB (ADB tunnel) — spyglass backend.

Bridges the stage-0 experiment (`experiments/android_usb_cam`) into the main
pipeline without touching downstream modules. Uses the patched `spyglass`
app: `GET /snap` for a JPEG still, `GET/POST /config` to control
camera_id / resolution / fps / jpegQuality, `GET /status` for battery/cameras.

Device differences vs D435i: no depth, no IMU, no shading LUT, no live
exposure control (the phone ISP handles colour; exposure is automatic).
"""

import subprocess
import time
from typing import Any

import cv2
import numpy as np

from pipeline.capture.camera import CameraInfo, FrameBundle, Intrinsics
from pipeline.capture.device_base import CaptureDevice, DeviceCapabilities

try:  # requests is part of the hunyuan extra; degrade gracefully
    import requests
except Exception as exc:  # pragma: no cover
    requests = None
    _REQ_IMPORT_ERR = exc


def _default_intrinsics(width: int, height: int) -> Intrinsics:
    """Placeholder intrinsics for a phone lens; marked approximately below."""
    return Intrinsics(fx=float(width), fy=float(height), cx=float(width) / 2.0,
                      cy=float(height) / 2.0, width=width, height=height)


class AndroidUsbDevice(CaptureDevice):
    """Drive an Android phone camera over an ADB-forwarded HTTP channel."""

    def __init__(
        self,
        *,
        device_id: str | None = None,          # adb device serial (optional)
        base_url: str = "http://127.0.0.1:4747",
        snapshot_url: str | None = None,
        camera_id: int = 0,
        resolution: str = "3264x2448",
        fps: int = 15,
        jpeg_quality: int = 95,
        adb: str = "adb",
        adb_forward: str = "tcp:4747:4747",
        output_edge: int = 1920,
        settle: float = 0.8,  # seconds to wait after POST /config for CameraX rebind
        **kwargs,  # tolerate extra options from the factory
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.snapshot_url = snapshot_url or f"{self.base_url}/snap"
        self.camera_id = camera_id
        self.resolution = resolution
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.adb = adb
        self.adb_forward = adb_forward
        self.output_edge = output_edge
        self.settle = settle
        self.device_id = device_id
        self._info: CameraInfo | None = None

        self.capabilities = DeviceCapabilities(
            kind="android_usb",
            model=f"Android cam{camera_id}",
            has_depth=False,
            has_imu=False,
            supports_shading=False,
            supports_exposure_control=False,
            max_resolution=(3072, 3072),
            color_controls={"wb": False, "exposure": False, "gain": False, "ae": True, "awb": True},
            gate_defaults={"min_sharpness": 30, "max_exposure": 0.08,
                           "min_object_ratio": 0.08, "max_object_ratio": 0.97},
        )

    # -- lifecycle -----------------------------------------------------------
    def open(self) -> CameraInfo:
        self._adb_forward()
        self._post_config({"camera": self.camera_id, "resolution": self.resolution,
                           "fps": self.fps, "jpegQuality": self.jpeg_quality})
        # CameraX rebinds after POST /config; the next frame may still be the
        # pre-rebind (lower-res) buffer. Wait a settle window so /snap reads
        # the new stream's frame.
        if self.settle:
            time.sleep(self.settle)
        w, h = self._parse_res(self.resolution)
        self._info = CameraInfo(
            model=f"Android cam{self.camera_id}",
            serial=self.device_id or f"android-cam{self.camera_id}",
            firmware="spyglass",
            color=f"{w}x{h}",
            depth="disabled",
            intrinsics=_default_intrinsics(w, h),
            depth_scale=1.0,
            imu={},
        )
        return self._info

    def grab(self, *, index: str, yaw_deg: int) -> FrameBundle:
        jpg = self._snap()
        bgr = self._decode_jpeg(jpg)
        return FrameBundle(color=bgr, yaw_deg=yaw_deg, index=index, depth=None,
                           ts_ns=time.time_ns())

    def close(self) -> None:
        # Nothing to release on the host; the stream lives on the phone.
        pass

    # -- helpers -------------------------------------------------------------
    def _adb_forward(self) -> None:
        if not self.adb_forward:
            return
        # Accept either a pre-split spec ("tcp:4747 tcp:4747") or a compact
        # string ("tcp:4747:4747") and build the canonical 2-arg form:
        #   adb forward <src> <dst>
        parts = self.adb_forward.split()
        if len(parts) >= 2:
            args = parts[:2]
        else:
            seg = self.adb_forward.split(":")
            # "tcp:4747:4747" -> src="tcp:4747", dst="tcp:4747"
            proto, *ports = seg
            if len(ports) >= 2:
                args = [f"{proto}:{ports[0]}", f"{proto}:{ports[1]}"]
            else:
                args = parts
        cmd = [self.adb, "forward", *args]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except FileNotFoundError:
            print("  [warn] adb not found; tunnel may already be up")
        except subprocess.CalledProcessError as e:
            print(f"  [warn] adb forward failed: {(e.stderr or b'').decode().strip() or e}")

    def _post_config(self, payload: dict[str, Any]) -> None:
        if requests is None:
            raise RuntimeError(f"requests not importable: {_REQ_IMPORT_ERR}")
        r = requests.post(f"{self.base_url}/config", json=payload, timeout=8)
        r.raise_for_status()

    def _snap(self) -> bytes:
        if requests is None:
            raise RuntimeError(f"requests not importable: {_REQ_IMPORT_ERR}")
        r = requests.get(self.snapshot_url, timeout=8)
        r.raise_for_status()
        return r.content

    @staticmethod
    def _decode_jpeg(data: bytes) -> np.ndarray:
        arr = np.frombuffer(data, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None or bgr.size == 0:
            raise RuntimeError("snap produced no valid JPEG")
        return bgr

    @staticmethod
    def _parse_res(resolution: str) -> tuple[int, int]:
        w, h = resolution.lower().split("x")
        return int(w), int(h)
