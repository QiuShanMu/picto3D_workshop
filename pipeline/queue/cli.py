from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.queue.run import run_queue, DEFAULT_API_ROOT, DEFAULT_WORK_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local generation queue (submit/poll/download via adapter)")
    parser.add_argument("batch_id", help="batch id")
    parser.add_argument("--skus", nargs="*", help="explicit sku ids (default: all ready from batch manifest)")
    parser.add_argument("--api-root", type=Path, default=Path(DEFAULT_API_ROOT))
    parser.add_argument("--work-root", type=Path, default=Path(DEFAULT_WORK_ROOT))
    parser.add_argument("--inflight", type=int, default=3)
    parser.add_argument("--provider", default="auto", choices=["auto", "mock"], help="mock runs offline with fixture")
    parser.add_argument("--fixture-dir", type=Path, default=None, help="fixture GLB dir for mock")
    parser.add_argument("--manifest", type=Path, default=None, help="batch manifest path (else auto)")
    args = parser.parse_args(argv)

    sku_ids = list(args.skus or [])
    if not sku_ids:
        manifest_path = args.manifest or Path(f"data/incoming/{args.batch_id}/_batch_manifest.json")
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            sku_ids = [s["sku_id"] for s in data.get("skus", []) if s.get("status") == "ready"]
        else:
            # fallback: all sku folders under api
            api_dir = args.api_root / args.batch_id
            sku_ids = [d.name for d in api_dir.iterdir() if d.is_dir()]

    if not sku_ids:
        print("no ready SKUs to process")
        return 0

    results = run_queue(
        args.batch_id,
        sku_ids,
        args.api_root,
        args.work_root,
        inflight=args.inflight,
        provider=args.provider,
        fixture_dir=args.fixture_dir,
    )
    for r in results:
        print(f"  [{r.status}] {r.sku_id} job={r.job_id} version={r.version} files={list(r.files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
