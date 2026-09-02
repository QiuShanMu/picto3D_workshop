from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.capture.handoff import handoff_sku, DEFAULT_CAPTURE_ROOT, DEFAULT_INCOMING_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T4: export a captured SKU package to incoming/")
    parser.add_argument("capture_dir", type=Path, help="path to a capture package (…/<batch>/<sku>)")
    parser.add_argument("--incoming-root", type=Path, default=Path(DEFAULT_INCOMING_ROOT))
    parser.add_argument("--sku", default=None, help="override sku id (default from capture.json)")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    result = handoff_sku(args.capture_dir, args.incoming_root, sku_id=args.sku, out_report=args.report)
    print(
        f"incoming_dir={result.incoming_dir} exported={len(result.exported)} "
        f"skipped={len(result.skipped)} ok={result.ok}"
    )
    print(f"  exported indices: {result.exported}")
    if result.skipped:
        print(f"  skipped: {result.skipped}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
