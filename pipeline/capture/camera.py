from __future__ import annotations

"""Thin wrapper over pyrealsense2 for a D435i capture workstation.

Keeps SDK types out of callers: everything capture/run.py and
incoming validation needs is surfaced as plain dataclasses / numpy arrays.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

try:  # import is top-level (no delay-load in the wheel on Windows)
    import pyrealsense2 as rs
except Exception as exc:  # pragma: no cover - environment helper
    rs = None
    _RS_IMPORT_ERR = exc


# D435i workstation defaults (see docs/spec-capture.md section 2.1).
COLOR_W, COLOR_H = 1920, 1080
DEPTH_W, DEPTH_H = 1280, 720
FPS = 30


@dataclass
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int

    def as_dict(self) -> dict:
        return {
            "fx": self.fx,
            "fy": self.fy,
            "cx": self.cx,
            "cy": self.cy,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class CameraInfo:
    model: str
    serial: str
    firmware: str
    color: str
    depth: str
    intrinsics: Intrinsics
    depth_scale: float = 1.0
    imu: dict = field(default_factory=dict)


@dataclass
class FrameBundle:
    """One synchronized color + optional aligned-depth sample."""

    color: np.ndarray
    yaw_deg: int
    index: str
    depth: np.ndarray | None = None
    ts_ns: int = 0


@dataclass
class ColorControls:
    """Manual color pipeline controls applied once the stream is live.

    D435i AWB drifts under warm neutral backgrounds (a known cause of color
    cast). Freezing WB + AE keeps every frame consistent and neutral. Values
    are tuned for a 5500K daylight workstation (docs/spec-capture 2.2 P0).
    """

    white_balance: int = 5500
    exposure: int | None = None  # None => leave at sensor default
    gain: int | None = None  # None => leave at sensor default
    auto_exposure: bool = False
    auto_white_balance: bool = False


def list_devices() -> list[dict]:
    """Return serial/name/firmware for every connected RealSense device."""
    if rs is None:
        return []
    ctx = rs.context()
    out: list[dict] = []
    for dev in ctx.query_devices():
        out.append(
            {
                "serial": dev.get_info(rs.camera_info.serial_number),
                "name": dev.get_info(rs.camera_info.name),
                "firmware": dev.get_info(rs.camera_info.firmware_version),
            }
        )
    return out


class D435iCamera:
    """Open a single D435i, RGB + depth-alignment + IMU, and grab frames.

    Depth is optional: the workstation tolerates a missing depth stream
    (spec-capture section 4). IMU is recorded once per session to detect
    if the camera was knocked.
    """

    def __init__(
        self,
        *,
        serial: str | None = None,
        color_res: tuple[int, int] = (COLOR_W, COLOR_H),
        depth_res: tuple[int, int] = (DEPTH_W, DEPTH_H),
        fps: int = FPS,
        enable_depth: bool = True,
        tilt_deg: int = 0,
        color_controls: ColorControls | None = None,
    ) -> None:
        if rs is None:
            raise RuntimeError(
                f"pyrealsense2 is not importable: {_RS_IMPORT_ERR}. "
                "Install the RealSense SDK / pyrealsense2 wheel."
            )
        self.serial = serial
        self.fps = fps
        self.enable_depth = enable_depth
        self.tilt_deg = tilt_deg
        self.color_res = color_res
        self.depth_res = depth_res
        self.color_controls = color_controls or ColorControls()

        self._pipeline = rs.pipeline()
        self._config = rs.config()
        self._profile: rs.pipeline_profile | None = None
        self._dev: rs.device | None = None

        cfg = self._config
        if serial:
            cfg.enable_device(serial)
        cfg.enable_stream(rs.stream.color, color_res[0], color_res[1], rs.format.bgr8, fps)
        if enable_depth:
            cfg.enable_stream(rs.stream.depth, depth_res[0], depth_res[1], rs.format.z16, fps)

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> CameraInfo:
        self._profile = self._pipeline.start(self._config)
        dev = self._profile.get_device()
        self._dev = dev
        self._apply_color_controls(dev)
        return self._read_info(dev)

    def _apply_color_controls(self, dev: rs.device) -> None:
        """Freeze WB/AE so captures don't drift (kills color cast on 5500K rig).

        White balance is set deterministically to the rig's colour temp. For
        exposure/gain, snapshot the sensor's current auto-adjusted value and
        harden it, rather than guessing: this keeps brightness correct for the
        operator's actual lighting while still preventing frame-to-frame drift.
        """
        ctrl = self.color_controls
        if ctrl is None:
            return
        try:
            rgb = next(s for s in dev.query_sensors() if "RGB" in s.get_info(rs.camera_info.name))
        except StopIteration:
            return

        def _get(option: rs.option, default: float) -> float:
            try:
                return float(rgb.get_option(option)) if rgb.supports(option) else default
            except Exception:
                return default

        def _set(option: rs.option, value: float) -> None:
            try:
                if rgb.supports(option):
                    rgb.set_option(option, value)
            except Exception:
                pass

        # Snapshot current auto values before disabling (they are sane at this point).
        snap_exposure = _get(rs.option.exposure, ctrl.exposure if ctrl.exposure is not None else 156.0)
        snap_gain = _get(rs.option.gain, ctrl.gain if ctrl.gain is not None else 64.0)

        _set(rs.option.enable_auto_white_balance, 1.0 if ctrl.auto_white_balance else 0.0)
        _set(rs.option.enable_auto_exposure, 1.0 if ctrl.auto_exposure else 0.0)

        if not ctrl.auto_white_balance:
            _set(rs.option.white_balance, float(ctrl.white_balance))
        if not ctrl.auto_exposure:
            _set(rs.option.exposure, ctrl.exposure if ctrl.exposure is not None else snap_exposure)
            _set(rs.option.gain, ctrl.gain if ctrl.gain is not None else snap_gain)

        # Expose the applied manual values so capture.json can record them.
        ctrl.white_balance = int(ctrl.white_balance)
        ctrl.exposure = int(_get(rs.option.exposure, snap_exposure))
        ctrl.gain = int(_get(rs.option.gain, snap_gain))

    def close(self) -> None:
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception:
                pass

    def __enter__(self) -> "D435iCamera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- hardware info -------------------------------------------------------

    def _read_info(self, dev: rs.device) -> CameraInfo:
        serial = dev.get_info(rs.camera_info.serial_number)
        name = dev.get_info(rs.camera_info.name)
        firmware = dev.get_info(rs.camera_info.firmware_version)

        color_profile = self._profile.get_stream(rs.stream.color).as_video_stream_profile()
        intr = color_profile.get_intrinsics()
        intrinsics = Intrinsics(intr.fx, intr.fy, intr.ppx, intr.ppy, intr.width, intr.height)

        depth_scale = 1.0
        if self.enable_depth:
            try:
                depth_sensor = dev.first_depth_sensor()
                depth_scale = depth_sensor.get_depth_scale()
            except Exception:
                depth_scale = 1.0

        # A single imu sample at open time, to catch a knocked camera later.
        # IMU is optional metadata; a read failure must never break capture.
        try:
            imu = self._read_imu(dev)
        except Exception:
            imu = {}

        return CameraInfo(
            model=name,
            serial=serial,
            firmware=firmware,
            color=f"{color_profile.width()}x{color_profile.height()}",
            depth=f"{self.depth_res[0]}x{self.depth_res[1]}" if self.enable_depth else "disabled",
            intrinsics=intrinsics,
            depth_scale=depth_scale,
            imu=imu,
        )

    @staticmethod
    def _read_imu(dev: rs.device) -> dict:
        """Best-effort one accel + one gyro sample from the Motion Module.

        Uses a short-lived callback so it never blocks the pipeline. IMU is a
        nice-to-have (detecting a knocked camera); an empty result is tolerated.
        """
        out: dict = {}
        try:
            sensors = dev.query_sensors()
            motion = next(
                (s for s in sensors if "Motion" in s.get_info(rs.camera_info.name)),
                None,
            )
        except Exception:
            return out
        if motion is None:
            return out

        frames: dict = {}

        def _cb(frame):
            ftype = frame.profile.stream_type()
            for stream_type, key in ((rs.stream.accel, "accel"), (rs.stream.gyro, "gyro")):
                if key not in frames and ftype == stream_type:
                    try:
                        d = frame.as_motion_frame().get_motion_data()
                        frames[key] = [float(d.x), float(d.y), float(d.z)]
                    except Exception:
                        pass

        for stream_type in (rs.stream.accel, rs.stream.gyro):
            try:
                profile = next((p for p in motion.get_stream_profiles() if p.stream_type() == stream_type), None)
                if profile is None:
                    continue
                motion.open(profile)
                motion.start(_cb)
                time.sleep(0.15)
                motion.stop()
                motion.close()
            except Exception:
                try:
                    if motion.is_streaming():
                        motion.stop()
                except Exception:
                    pass
                continue
        return out

    # -- grabbing ------------------------------------------------------------

    def grab(self, *, index: str, yaw_deg: int) -> FrameBundle:
        """Wait for a synchronized color (+aligned depth) frame."""
        frames = self._pipeline.wait_for_frames()

        ts_ns = frames.get_timestamp()
        color_frame = frames.get_color_frame()
        if not color_frame:
            raise RuntimeError("no color frame produced")

        color = np.asanyarray(color_frame.get_data())  # HxWx3 BGR
        color = color.copy()

        depth = None
        if self.enable_depth:
            depth_frame = frames.get_depth_frame()
            if not depth_frame:
                raise RuntimeError("depth stream disabled but a depth frame was expected")
            depth = np.asanyarray(depth_frame.get_data()).astype(np.uint16)
            depth = depth.copy()

        return FrameBundle(
            color=color,
            yaw_deg=yaw_deg,
            index=index,
            depth=depth,
            ts_ns=int(ts_ns),
        )


def save_camera_json(path: Path, info: CameraInfo, *, tilt_deg: int) -> dict:
    """Persist session-level camera metadata as camera.json."""
    payload = {
        "schema": "camera.v1",
        "model": info.model,
        "serial": info.serial,
        "firmware": info.firmware,
        "color": info.color,
        "depth": info.depth,
        "depth_scale": info.depth_scale,
        "tilt_deg": tilt_deg,
        "intrinsics": info.intrinsics.as_dict(),
        "imu": info.imu,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
