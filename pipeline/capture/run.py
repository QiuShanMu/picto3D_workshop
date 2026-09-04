from __future__ import annotations

"""Workstation capture program (spec-capture T1+T2+T3).

One session per SKU. Operator turns the item to each table-top mark and
confirms with a keypress; frames write atomically (.tmp -> final) and are
gated for sharpness/exposure/occupancy on the spot.

Does NOT call Hunyuan. Only captures into `data/captures/<batch>/<sku>/`.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from pipeline.capture.barcode import decode_barcode
from pipeline.capture.camera import ColorControls, save_camera_json
from pipeline.capture.device import make_capture_device, list_capture_devices
from pipeline.capture.gate import gate_frame
from pipeline.capture.shading import ShadingLUT
from pipeline.views import SLOTS

DEFAULT_CAPTURE_ROOT = "data/captures"
SHADING_DIR = "shadings"  # relative to capture root; files named <serial>_shading.json
OUTPUT_EDGE = 1920  # D435i native color long edge (spec-capture 2.1)
JPEG_QUALITY = 95


@dataclass
class CaptureOptions:
    batch_id: str
    sku_id: str
    station_id: str = "d435i-desk-1"
    operator: str = ""
    tilt_deg: int = 25
    capture_root: Path = Path(DEFAULT_CAPTURE_ROOT)
    camera_kind: str = "d435i"  # "d435i" | "android_usb"; drives make_capture_device
    serial: str | None = None
    enable_depth: bool = True
    pose_mode: str = "yaw_manual_marks"
    color_controls: ColorControls | None = None
    apply_shading: bool = True
    shading_lut: Path | None = None  # explicit LUT path; else auto-match by serial
    min_sharpness: float = 60.0
    max_exposure: float = 0.05
    min_object_ratio: float = 0.40
    max_object_ratio: float = 0.92
    preview: bool = True
    max_frames: int = len(SLOTS)
    barcode_enabled: bool = False  # optional barcode shot at session start (backward compatible)


@dataclass
class CaptureResult:
    capture_dir: Path
    frames: list[dict]
    report: dict
    ok: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _long_edge_resize(bgr: np.ndarray, edge: int) -> np.ndarray:
    h, w = bgr.shape[:2]
    current = max(w, h)
    if current <= edge:
        return bgr
    scale = edge / current
    return cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LANCZOS4)


def _atomic_write_bgr(path: Path, bgr: np.ndarray, quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    ok, buf = cv2.imencode(path.suffix, bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError(f"failed to encode {path.name}")
    tmp.write_bytes(buf.tobytes())
    os.replace(tmp, path)  # atomic; safe to overwrite an existing frame (re-shoot)


def _atomic_write_png(path: Path, depth: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    ok, buf = cv2.imencode(path.suffix, depth.astype(np.uint16), [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise RuntimeError(f"failed to encode {path.name}")
    tmp.write_bytes(buf.tobytes())
    os.replace(tmp, path)


def _confirm(prompt: str) -> str:
    """Blocking console prompt; returns the key pressed (lowercased)."""
    print(prompt, end="", flush=True)
    return input().strip().lower()


# Views the pipeline must have for a standard handoff. 01 is always required.
# 04/06 feed no Hunyuan slot (archive-only) but are still required to be present
# for a complete capture; 09/10 (top/bottom) are physically optional on the
# single tilted D435i (see spec-capture §2.2), so they default to false.
_VIEW_REQUIRED: dict[str, bool] = {
    "01": True,
    "02": True,
    "03": True,
    "04": True,
    "05": True,
    "06": True,
    "07": True,
    "08": True,
    "09": False,
    "10": False,
}


def _build_target_views() -> dict[str, dict[str, bool]]:
    return {
        "01": {"required": _VIEW_REQUIRED["01"]},
        "02": {"required": _VIEW_REQUIRED["02"]},
        "03": {"required": _VIEW_REQUIRED["03"]},
        "04": {"required": _VIEW_REQUIRED["04"]},
        "05": {"required": _VIEW_REQUIRED["05"]},
        "06": {"required": _VIEW_REQUIRED["06"]},
        "07": {"required": _VIEW_REQUIRED["07"]},
        "08": {"required": _VIEW_REQUIRED["08"]},
        "09": {"required": _VIEW_REQUIRED["09"]},
        "10": {"required": _VIEW_REQUIRED["10"]},
    }


def _resolved_lut_path(opts: CaptureOptions, serial: str) -> Path | None:
    """Resolve the path of the per-camera shading LUT (may not exist)."""
    if opts.shading_lut is not None:
        return Path(opts.shading_lut)
    return opts.capture_root / SHADING_DIR / f"{serial}_shading.json"


def _load_shading_lut(opts: CaptureOptions, serial: str) -> ShadingLUT | None:
    """Resolve and load the per-camera shading LUT, if one exists.

    Precedence: explicit --shading-lut, else <capture_root>/shadings/<serial>_shading.json.
    Returns None when shading is disabled or no calibration is available.
    """
    if not opts.apply_shading:
        return None
    path = _resolved_lut_path(opts, serial)
    if path is None or not path.is_file():
        return None
    try:
        return ShadingLUT.load(path)
    except Exception:
        return None


def capture_sku(opts: CaptureOptions) -> CaptureResult:
    """Drive a single SKU capture session. Returns result + writes files."""
    if not opts.sku_id:
        raise ValueError("sku_id is required")
    if not opts.batch_id:
        raise ValueError("batch_id is required")

    capture_dir = opts.capture_root / opts.batch_id / opts.sku_id
    color_dir = capture_dir / "color"
    depth_dir = capture_dir / "depth"
    # Non-D435i devices (e.g. Android phone) have no depth stream; don't build
    # an empty depth dir or request a stream they can't provide.
    if opts.camera_kind != "d435i":
        opts.enable_depth = False
    color_dir.mkdir(parents=True, exist_ok=True)
    if opts.enable_depth:
        depth_dir.mkdir(parents=True, exist_ok=True)

    devices = list_capture_devices(opts.camera_kind)
    if not devices:
        raise RuntimeError(
            f"no {opts.camera_kind} device found; check USB / driver"
            if opts.camera_kind == "d435i"
            else "no Android USB camera found; check `adb devices` / spyglass tunnel"
        )

    frames: list[dict] = []
    camera_info = None
    lut = None
    lut_path = None

    dev = make_capture_device(
        opts.camera_kind,
        serial=opts.serial,
        enable_depth=opts.enable_depth,
        tilt_deg=opts.tilt_deg,
        color_controls=opts.color_controls,
    )
    try:
        camera_info = dev.open()
        if opts.serial and camera_info.serial != opts.serial:
            raise RuntimeError(f"expected serial {opts.serial}, got {camera_info.serial}")

        save_camera_json(capture_dir / "camera.json", camera_info, tilt_deg=opts.tilt_deg)

        # Per-camera flat-field correction only for devices that support it
        # (D435i has a LUT; the phone relies on its ISP colour pipeline).
        if dev.capabilities.supports_shading:
            lut_path = _resolved_lut_path(opts, camera_info.serial)
            lut = _load_shading_lut(opts, camera_info.serial)
            if opts.apply_shading and lut is None:
                print(f"  [warn] no shading LUT for {camera_info.serial}; run calibration to fix color cast")
        else:
            print(f"  [info] device '{opts.camera_kind}' has no shading LUT; skip flat-field correction")

        # Session starts at 01 (front, yaw 0). The operator then turns CCW through the marks.
        ordered = [s for s in SLOTS if s.degrees >= 0]  # yaw only; 09/10 handled separately

        display_name = f"{opts.sku_id} @ {opts.station_id}"
        print("=" * 60)
        print(f"Capture session: {display_name}")
        print(f"  batch={opts.batch_id}  tilt={opts.tilt_deg}deg  depth={'on' if opts.enable_depth else 'off'}")
        print("  Front (0deg) = mark 01. Turn item counter-clockwise through the marks.")
        print("  Keys: SPACE=shoot  s=skip  q=end session")
        print("=" * 60)

        # Optional SKU barcode shot before the view loop (barcode image is kept
        # outside `frames` so it never flows into image-to-3D input).
        barcode_meta = None
        if opts.barcode_enabled:
            print("\n[SKU 条码采集] Put the SKU barcode label in front of the camera.")
            bkey = _confirm("  [b]shoot barcode  [s]skip  > ")
            if bkey == "b":
                bc_bundle = dev.grab(index="bc", yaw_deg=0)
                _atomic_write_bgr(color_dir / "barcode.jpg", bc_bundle.color, JPEG_QUALITY)
                bdec = decode_barcode(bc_bundle.color)
                bc_value = bdec.value if bdec.ok else ""
                barcode_meta = {
                    "value": bc_value,
                    "image": "color/barcode.jpg",
                    "captured_at": _now_iso(),
                    "auto": bdec.ok,
                }
                _atomic_write_json(capture_dir / "barcode.json", {
                    "value": bc_value,
                    "raw": bc_value,
                    "type": bdec.type if bdec.ok else "UNKNOWN",
                    "source": bdec.source if bdec.ok else "none",
                    "image": "color/barcode.jpg",
                    "captured_at": _now_iso(),
                    "manual": not bdec.ok,
                })
                if bdec.ok:
                    print(f"  Barcode OK: {bdec.value} ({bdec.type}/{bdec.source})")
                else:
                    print("  Barcode shot saved but nothing recognized; using sku_id from args.")
            else:
                print("  Skipped barcode; using sku_id from args.")

        # Iterate through all yaw slots in order; operator may skip or end.
        for slot in ordered:
            print(f"\n-> Next: {slot.index}  yaw={slot.degrees}deg  pose=yaw  hunyuan={slot.hunyuan_field}")
            key = _confirm("  [Space]shoot  [s]skip  [q]end  > ")

            if key == "q":
                print("Ending session on request.")
                break
            if key == "s":
                print(f"  Skipped {slot.index}.")
                continue
            # SPACE (or empty/enter) = shoot
            bundle = dev.grab(index=slot.index, yaw_deg=slot.degrees)
            gate = gate_frame(
                bundle.color,
                min_sharpness=opts.min_sharpness,
                max_exposure=opts.max_exposure,
                min_object_ratio=opts.min_object_ratio,
                max_object_ratio=opts.max_object_ratio,
            )

            if not gate.ok:
                print(f"  REJECT {slot.index}: {gate.reason}")
                print("  (sharp={:.1f} exp={:.1%} obj={:.1%})".format(gate.sharpness, gate.exposure, gate.object_ratio))
                continue

            # Filenames use index + yaw for traceability.
            frame_name = f"{slot.index}_yaw{slot.degrees:03d}"
            color_file = f"{frame_name}.jpg"
            depth_file = f"{frame_name}.png" if opts.enable_depth and bundle.depth is not None else None

            # Apply flat-field correction (center/edge color cast) before saving.
            saved_color = lut.apply(bundle.color) if lut is not None else bundle.color
            resized = _long_edge_resize(saved_color, OUTPUT_EDGE)
            _atomic_write_bgr(color_dir / color_file, resized, JPEG_QUALITY)
            if depth_file:
                _atomic_write_png(depth_dir / depth_file, bundle.depth)

            frames.append(
                {
                    "index": slot.index,
                    "yaw_deg": slot.degrees,
                    "pose": "yaw",
                    "hunyuan": slot.hunyuan_field,
                    "color": f"color/{color_file}",
                    "depth": f"depth/{depth_file}" if depth_file else None,
                    "gate": {
                        "sharpness": gate.sharpness,
                        "exposure": gate.exposure,
                        "object_ratio": gate.object_ratio,
                    },
                    "ok": True,
                    "attempt": 1,
                    "captured_at": _now_iso(),
                }
            )
            print(f"  Saved {slot.index} -> {color_file}")
    finally:
        dev.close()

    # Write capture.json (schema v1 per spec-capture).
    capture_json: dict = {
        "schema": "capture.v1",
        "sku_id": opts.sku_id,
        "batch_id": opts.batch_id,
        "station_id": opts.station_id,
        "operator": opts.operator,
        "started_at": _now_iso(),
        "pose_mode": opts.pose_mode,
        "rotation": {"method": "manual_marks", "step_deg": 45, "direction": "ccw"},
        "camera": {
            "kind": opts.camera_kind,  # "d435i" | "android_usb" (backward-compatible addition)
            "model": camera_info.model if camera_info else "",
            "serial": camera_info.serial if camera_info else "",
            "color": camera_info.color if camera_info else "1920x1080",
            "tilt_deg": opts.tilt_deg,
            "color_controls": (
                {
                    "white_balance": opts.color_controls.white_balance,
                    "exposure": opts.color_controls.exposure,
                    "gain": opts.color_controls.gain,
                    "auto_white_balance": opts.color_controls.auto_white_balance,
                    "auto_exposure": opts.color_controls.auto_exposure,
                }
                if opts.color_controls
                else None
            ),
            "shading_lut": str(lut_path) if lut_path else None,
            "transit": "USB3/RealSense" if opts.camera_kind == "d435i" else "USB/ADB tunnel",
            "gate_defaults": getattr(dev, "capabilities", None).gate_defaults
            if getattr(dev, "capabilities", None)
            else {},
        },
        "target_views": _build_target_views(),
        **( {"barcode": barcode_meta} if barcode_meta else {} ),
        "frames": frames,
        "session_metrics": {
            "ok_frames": len(frames),
            "captured_indices": [f["index"] for f in frames],
            "missing_required": [
                idx
                for idx, spec in _build_target_views().items()
                if spec["required"] and idx not in {f["index"] for f in frames}
            ],
        },
        "status": "captured",
    }
    _atomic_write_json(capture_dir / "capture.json", capture_json)

    report = {
        "module": "capture",
        "verdict": "ok" if frames else "warn",
        "sku_id": opts.sku_id,
        "batch_id": opts.batch_id,
        "capture_dir": str(capture_dir.resolve()),
        "frame_count": len(frames),
        "indices": [f["index"] for f in frames],
        "status": "captured",
    }
    _atomic_write_json(capture_dir / "report.json", report)

    return CaptureResult(
        capture_dir=capture_dir,
        frames=frames,
        report=report,
        ok=bool(frames),
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
