from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.webui.app import create_app
from pipeline.capture.webapp import WebOptions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Production capture WebUI (batch board + capture wizard)")
    parser.add_argument("--batch", default="0812")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5010)
    parser.add_argument("--no-camera", action="store_true", help="run board only, do not open any camera")
    parser.add_argument("--camera", default="d435i", choices=["d435i", "android_usb"],
                        help="capture device kind for the capture workbench")
    parser.add_argument("--serial", default=None)
    parser.add_argument("--wb", type=int, default=5500)
    parser.add_argument("--capture-root", type=Path, default=Path("data/captures"))
    parser.add_argument("--no-shading", action="store_true")
    # Android USB camera options (only relevant with --camera android_usb)
    parser.add_argument("--android-base-url", default="http://127.0.0.1:4747")
    parser.add_argument("--android-camera-id", type=int, default=0)
    parser.add_argument("--android-resolution", default="3264x2448")
    parser.add_argument("--android-fps", type=int, default=15)
    parser.add_argument("--android-jpeg-quality", type=int, default=95)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--adb-forward", default="tcp:4747:4747")
    parser.add_argument("--provider", default="auto", choices=["auto", "mock"],
                        help="hunyuan provider for the generate workbench: auto=mock when no keys, or mock to force")
    args = parser.parse_args(argv)

    android_opts = {
        "base_url": args.android_base_url,
        "camera_id": args.android_camera_id,
        "resolution": args.android_resolution,
        "fps": args.android_fps,
        "jpeg_quality": args.android_jpeg_quality,
        "adb": args.adb,
        "adb_forward": args.adb_forward,
    } if args.camera == "android_usb" else None

    cam_opts = WebOptions(
        host=args.host, port=args.port, batch_id=args.batch, sku_id="",
        capture_root=args.capture_root, serial=args.serial, wb=args.wb,
        apply_shading=not args.no_shading, camera_kind=args.camera, android=android_opts,
    )
    app = create_app(batch=args.batch, cam_opts=cam_opts, start_camera=not args.no_camera,
                     provider=args.provider)
    mode = "board+camera" if not args.no_camera else "board (no camera)"
    print(f"WebUI {mode}: http://{args.host}:{args.port}  batch={args.batch}  provider={args.provider}  camera={args.camera}")
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
