from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.preprocess.run import run_preprocess


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Closed-loop ingest + API image prep")
    parser.add_argument("incoming", type=Path, help="folder of SKU_01..SKU_10 images")
    parser.add_argument("--out", type=Path, required=True, help="API image output folder")
    parser.add_argument("--sku", default=None)
    parser.add_argument("--min-edge", type=int, default=2048)
    parser.add_argument("--api-edge", type=int, default=1600)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    report = run_preprocess(
        args.incoming,
        args.out,
        sku_id=args.sku,
        min_edge=args.min_edge,
        api_edge=args.api_edge,
        out_report=args.report,
    )
    print(f"verdict={report.verdict} labels={report.labels} report={report.output}")
    return 1 if report.verdict == "fail" else 0
