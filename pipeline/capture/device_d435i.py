from __future__ import annotations

"""D435i capture device — a thin wrapper over the existing `D435iCamera`.

Only adds the `CaptureDevice` protocol adaptation; keeps the RealSense SDK
types contained inside `camera.py` so callers get plain dataclasses / arrays.
"""

from pipeline.capture.camera import CameraInfo, ColorControls, D435iCamera, FrameBundle
from pipeline.capture.device_base import CaptureDevice, DeviceCapabilities


class D435iDevice(CaptureDevice):
    """Adapts `D435iCamera` to the unified `CaptureDevice` protocol."""

    def __init__(
        self,
        *,
        serial: str | None = None,
        color_res: tuple[int, int] | None = None,
        depth_res: tuple[int, int] | None = None,
        fps: int | None = None,
        enable_depth: bool = True,
        tilt_deg: int = 0,
        color_controls: ColorControls | None = None,
        **kwargs,  # tolerate extra options so the factory can forward **opts
    ) -> None:
        from pipeline.capture.camera import COLOR_H, COLOR_W, DEPTH_H, DEPTH_W, FPS

        self._cam = D435iCamera(
            serial=serial,
            color_res=color_res or (COLOR_W, COLOR_H),
            depth_res=depth_res or (DEPTH_W, DEPTH_H),
            fps=fps or FPS,
            enable_depth=enable_depth,
            tilt_deg=tilt_deg,
            color_controls=color_controls,
        )
        self.serial = serial
        self.enable_depth = enable_depth
        self.tilt_deg = tilt_deg
        self.color_controls = color_controls
        self._info: CameraInfo | None = None
        self.capabilities = DeviceCapabilities(
            kind="d435i",
            model="RealSense D435I",
            has_depth=True,
            has_imu=True,
            supports_shading=True,
            supports_exposure_control=True,  # production c161ecf added live exposure
            max_resolution=(1920, 1080),
            color_controls={"wb": True, "exposure": True, "gain": True, "ae": True, "awb": True},
            gate_defaults={"min_sharpness": 60, "max_exposure": 0.05,
                           "min_object_ratio": 0.40, "max_object_ratio": 0.92},
        )

    def open(self) -> CameraInfo:
        self._info = self._cam.open()
        return self._info

    def grab(self, *, index: str, yaw_deg: int) -> FrameBundle:
        return self._cam.grab(index=index, yaw_deg=yaw_deg)

    def close(self) -> None:
        self._cam.close()

    # Live exposure control pass-through (production feature; phone has none).
    def exposure_controls(self) -> dict | None:
        return self._cam.exposure_controls()

    def set_exposure_controls(self, *, auto_exposure: bool, exposure: float | None = None,
                              gain: float | None = None) -> dict | None:
        return self._cam.set_exposure_controls(auto_exposure=auto_exposure,
                                               exposure=exposure, gain=gain)

    # Expose shading resolution helpers to callers that previously reached
    # into `D435iCamera` internals (e.g. webapp._load_shading_lut uses serial).
    @property
    def serial_actual(self) -> str:
        return (self._info.serial if self._info else "") or (self.serial or "")
