from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.archive.run import archive_sku, DEFAULT_WORK_ROOT, DEFAULT_ARCHIVE_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive a validated SKU to the catalog")
    parser.add_argument("sku_id")
    parser.add_argument("batch_id")
    parser.add_argument("--work-root", type=Path, default=Path(DEFAULT_WORK_ROOT))
    parser.add_argument("--archive-root", type=Path, default=Path(DEFAULT_ARCHIVE_ROOT))
    parser.add_argument("--category", default="general")
    parser.add_argument("--source-work", type=Path, default=None)
    args = parser.parse_args(argv)

    result = archive_sku(
        args.sku_id, args.batch_id, args.work_root, args.archive_root,
        category=args.category, source_work=args.source_work,
    )
    print(f"archive_dir={result.archive_dir} model={result.model} ok={result.ok}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
