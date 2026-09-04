from __future__ import annotations

"""Abstract capture-device protocol + capability descriptor.

The capture stack (run.py / webapp.py / webui) historically hard-coded
`D435iCamera`. This module introduces a thin seam so the same code can drive
a D435i, an Android phone via USB (spyglass), or any future device — without
touching downstream preprocess/hunyuan/validate/archive, which only read
`capture.json`'s `frames[].color`.

Kept SDK-free: only plain dataclasses / ABC here. `CameraInfo` / `FrameBundle`
are reused from `pipeline.capture.camera` (no change to that module).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from pipeline.capture.camera import CameraInfo, FrameBundle


@dataclass
class DeviceCapabilities:
    """Static capability description of a capture device (JSON-friendly).

    This is the single source of truth for what a device can do, used by the
    capture loop to decide (e.g.) whether to grab depth, apply a shading LUT,
    or expose the live exposure-control UI.
    """

    kind: str                      # "d435i" | "android_usb" | ...
    model: str                     # display name, e.g. "RealSense D435I"
    has_depth: bool                # device produces depth (phone: False)
    has_imu: bool                  # device exposes IMU (phone: False)
    supports_shading: bool         # flat-field LUT supported (phone: False; uses ISP)
    supports_exposure_control: bool  # live EV/gain/auto-exposure (D435i: True; phone: False)
    max_resolution: tuple[int, int]  # recommended (w, h)
    color_controls: dict           # which color knobs exist (wb/exposure/gain/ae/awb)
    gate_defaults: dict            # gate thresholds {min_sharpness, max_exposure,
                                   #             min_object_ratio, max_object_ratio}

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "model": self.model,
            "has_depth": self.has_depth,
            "has_imu": self.has_imu,
            "supports_shading": self.supports_shading,
            "supports_exposure_control": self.supports_exposure_control,
            "max_resolution": list(self.max_resolution),
            "color_controls": dict(self.color_controls),
            "gate_defaults": dict(self.gate_defaults),
        }


class CaptureDevice(ABC):
    """Unified lifecycle a device exposes to the capture loop.

    Mirrors the `open / grab / close` contract of the legacy `D435iCamera` so
    callers (`run.py::capture_sku`, `webapp.py::CameraWorker`) can drive any
    device identically.
    """

    capabilities: DeviceCapabilities

    @abstractmethod
    def open(self) -> CameraInfo:
        """Open the stream, apply per-device config, return camera metadata."""

    @abstractmethod
    def grab(self, *, index: str, yaw_deg: int) -> FrameBundle:
        """Wait for / fetch one synchronized color (+optional depth) sample."""

    @abstractmethod
    def close(self) -> None:
        """Release the stream. Must be safe to call more than once."""

    # -- optional pass-throughs (defaults are null-safe for devices that lack
    #    the capability; overridden by D435i). --------------------------------
    def read_info(self, camera_info: CameraInfo | None = None) -> CameraInfo | None:
        return camera_info

    def shading_lut_path(self) -> Path | None:
        return None

    def exposure_controls(self) -> dict | None:
        """Live exposure control read-back, or None if unsupported."""
        return None

    def set_exposure_controls(self, *, auto_exposure: bool, exposure: float | None = None,
                              gain: float | None = None) -> dict | None:
        return None

    def __enter__(self) -> "CaptureDevice":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
