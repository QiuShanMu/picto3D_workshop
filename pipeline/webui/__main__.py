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
    parser.add_argument("--no-camera", action="store_true", help="run board only, do not open the D435i")
    parser.add_argument("--serial", default=None)
    parser.add_argument("--wb", type=int, default=5500)
    parser.add_argument("--capture-root", type=Path, default=Path("data/captures"))
    parser.add_argument("--no-shading", action="store_true")
    parser.add_argument("--provider", default="auto", choices=["auto", "mock"],
                        help="hunyuan provider for the generate workbench: auto=mock when no keys, or mock to force")
    args = parser.parse_args(argv)

    cam_opts = WebOptions(
        host=args.host, port=args.port, batch_id=args.batch, sku_id="",
        capture_root=args.capture_root, serial=args.serial, wb=args.wb,
        apply_shading=not args.no_shading,
    )
    app = create_app(batch=args.batch, cam_opts=cam_opts, start_camera=not args.no_camera,
                     provider=args.provider)
    mode = "board+camera" if not args.no_camera else "board (no camera)"
    print(f"WebUI {mode}: http://{args.host}:{args.port}  batch={args.batch}  provider={args.provider}")
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
