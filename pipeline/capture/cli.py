from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.capture import run
from pipeline.capture.run import CaptureOptions, capture_sku
from pipeline.capture.camera import ColorControls, D435iCamera, list_devices
from pipeline.capture.shading import calibrate_shading


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D435i workstation capture (T1+T2+T3)")
    parser.add_argument("--batch", help="batch id, e.g. 0812")
    parser.add_argument("--sku", help="sku id, e.g. APP-0812-001")
    parser.add_argument("--station", default="d435i-desk-1", help="station id")
    parser.add_argument("--operator", default="")
    parser.add_argument("--tilt", type=int, default=25, help="camera tilt (deg)")
    parser.add_argument("--capture-root", type=Path, default=Path(run.DEFAULT_CAPTURE_ROOT))
    parser.add_argument("--serial", default=None, help="device serial (default: first found)")
    parser.add_argument("--no-depth", action="store_true", help="disable depth stream")
    parser.add_argument("--list-devices", action="store_true", help="list connected devices and exit")
    # Color controls (default freeze to 5500K to kill D435i AWB drift).
    parser.add_argument("--wb", type=int, default=5500, help="manual white balance (K), default 5500")
    parser.add_argument("--exposure", type=int, default=None, help="manual exposure (usec)")
    parser.add_argument("--gain", type=int, default=None, help="manual gain")
    parser.add_argument("--auto-color", action="store_true", help="keep sensor auto WB/AE (off by default)")
    # Flat-field / shading correction.
    parser.add_argument("--no-shading", action="store_true", help="disable shading (flat-field) correction")
    parser.add_argument("--shading-lut", type=Path, default=None, help="explicit shading LUT path")
    parser.add_argument(
        "--calibrate-shading",
        action="store_true",
        help="capture a reference frame, build the shading LUT and save it (then exit; no SKU capture)",
    )
    parser.add_argument(
        "--with-barcode",
        action="store_true",
        help="ask to shoot an SKU barcode at session start (archived, not fed to image-to-3D)",
    )
    args = parser.parse_args(argv)

    if args.list_devices:
        devs = list_devices()
        if not devs:
            print("no RealSense device found")
            return 1
        for d in devs:
            print(f"serial={d['serial']} name={d['name']} firmware={d['firmware']}")
        return 0

    if args.calibrate_shading:
        return _calibrate_shading_cli(args)

    if not args.batch or not args.sku:
        parser.error("--batch and --sku are required (or use --list-devices / --calibrate-shading)")

    opts = CaptureOptions(
        batch_id=args.batch,
        sku_id=args.sku,
        station_id=args.station,
        operator=args.operator,
        tilt_deg=args.tilt,
        capture_root=args.capture_root,
        serial=args.serial,
        enable_depth=not args.no_depth,
        color_controls=ColorControls(
            white_balance=args.wb,
            exposure=args.exposure,
            gain=args.gain,
            auto_exposure=args.auto_color,
            auto_white_balance=args.auto_color,
        ),
        apply_shading=not args.no_shading,
        shading_lut=args.shading_lut,
        barcode_enabled=args.with_barcode,
    )
    result = capture_sku(opts)
    print(f"capture_dir={result.capture_dir} frames={len(result.frames)} ok={result.ok}")
    return 0 if result.ok else 1


def _calibrate_shading_cli(args) -> int:
    """Capture a neutral reference frame and build the per-camera shading LUT."""
    import cv2
    import numpy as np

    from pipeline.capture.camera import ColorControls

    cc = ColorControls(white_balance=args.wb, exposure=args.exposure, gain=args.gain)
    print("Calibrating shading. Make sure the frame is filled by a uniform, evenly-lit")
    print("neutral surface (white/gray) with NO product. Capturing 8 frames ...")
    frames = []
    with D435iCamera(serial=args.serial, enable_depth=False, color_controls=cc) as cam:
        for _ in range(8):
            b = cam.grab(index="01", yaw_deg=0)
            frames.append(b.color.astype(np.float32))
        info = cam._read_info(cam._profile.get_device())
        serial = info.serial
    ref = np.mean(frames, axis=0).astype(np.uint8)
    lut = calibrate_shading(ref, tiles=16)
    out_dir = args.capture_root / run.SHADING_DIR
    out_path = out_dir / f"{serial}_shading.json"
    lut.save(out_path)
    print(f"Saved shading LUT: {out_path}")
    print(f"  serial={serial} grid={lut.grid.shape} tiles={lut.tiles}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
