from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.capture.batch import assemble_batch, DEFAULT_CAPTURE_ROOT, DEFAULT_INCOMING_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch assembly: capture batch -> incoming + manifest")
    parser.add_argument("batch_id", help="batch id, e.g. 0812")
    parser.add_argument("--capture-root", type=Path, default=Path(DEFAULT_CAPTURE_ROOT))
    parser.add_argument("--incoming-root", type=Path, default=Path(DEFAULT_INCOMING_ROOT))
    parser.add_argument("--no-export", action="store_true", help="only write manifest, skip exporting ready SKUs")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    result = assemble_batch(
        args.batch_id,
        args.capture_root,
        args.incoming_root,
        export_ready=not args.no_export,
        out_report=args.report,
    )
    print(f"batch={result.batch_id} skus={result.report['sku_count']} ready={result.ready} incomplete={result.incomplete}")
    print(f"manifest={result.manifest_path}")
    for s in result.skus:
        if s.status != "ready":
            print(f"  [{s.status}] {s.sku_id} missing={s.missing_required}")
    return 0 if result.incomplete == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
