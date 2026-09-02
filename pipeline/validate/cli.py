from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.validate.run import run_validate


def _parse_size_mm(raw: str) -> tuple[float, float, float]:
    parts = [p.strip() for p in raw.replace("x", ",").split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("size-mm must be L,W,H")
    return float(parts[0]), float(parts[1]), float(parts[2])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Closed-loop mesh validation")
    parser.add_argument("model", type=Path, help="GLB/OBJ path")
    parser.add_argument("--size-mm", type=_parse_size_mm, default=None)
    parser.add_argument("--out", type=Path, default=None, help="report.json path")
    parser.add_argument("--min-bytes", type=int, default=0)
    parser.add_argument("--max-bytes", type=int, default=20 * 1024 * 1024)
    args = parser.parse_args(argv)

    report = run_validate(
        args.model,
        size_mm=args.size_mm,
        min_bytes=args.min_bytes,
        max_bytes=args.max_bytes,
        out_path=args.out,
    )
    print(f"verdict={report.verdict} labels={report.labels} report={report.output}")
    return 1 if report.verdict == "fail" else 0
