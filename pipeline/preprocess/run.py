from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from pipeline.report import Check, ModuleReport
from pipeline.views import SLOT_BY_INDEX, SLOTS, UPLOAD_SLOTS

NAME_RE = re.compile(r"^(?P<sku>.+)_(?P<idx>\d{2})\.(?P<ext>jpg|jpeg|png)$", re.IGNORECASE)
DEFAULT_MIN_EDGE = 2048
DEFAULT_API_EDGE = 1600
API_EDGE_FLOOR = 1024


def _discover(incoming: Path, sku_id: str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in incoming.iterdir():
        if not path.is_file():
            continue
        match = NAME_RE.match(path.name)
        if not match:
            continue
        if match.group("sku") != sku_id:
            continue
        found[match.group("idx")] = path
    return found


def _resize_to_api(src: Path, dest: Path, long_edge: int) -> tuple[int, int, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGBA") if src.suffix.lower() == ".png" else im.convert("RGB")
        w, h = im.size
        current = max(w, h)
        if current > long_edge:
            scale = long_edge / current
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        im.save(dest, quality=90, optimize=True)
        return w, h, dest.stat().st_size


def run_preprocess(
    incoming: Path,
    output_dir: Path,
    *,
    sku_id: str | None = None,
    min_edge: int = DEFAULT_MIN_EDGE,
    api_edge: int = DEFAULT_API_EDGE,
    out_report: Path | None = None,
) -> ModuleReport:
    incoming = incoming.resolve()
    output_dir = output_dir.resolve()
    sku_id = sku_id or incoming.name
    report = ModuleReport(module="preprocess", verdict="ok", input=str(incoming), output=str(output_dir))
    if out_report is None:
        out_report = output_dir / "report.json"

    if not incoming.is_dir():
        report.add(Check("incoming_dir", False, "incoming folder not found", "download_error"))
        report.finalize()
        report.write(out_report)
        return report

    found = _discover(incoming, sku_id)
    report.metrics["sku_id"] = sku_id
    report.metrics["found_indices"] = sorted(found)
    has_front = "01" in found
    report.add(
        Check(
            "front",
            has_front,
            f"{sku_id}_01 present" if has_front else f"{sku_id}_01 missing",
            None if has_front else "download_error",
        )
    )

    unexpected = sorted(set(found) - set(SLOT_BY_INDEX))
    if unexpected:
        report.add(Check("index_range", False, f"unknown indices {unexpected}"))
    else:
        report.add(Check("index_range", True, "indices are 01-10"))

    views = []
    output_dir.mkdir(parents=True, exist_ok=True)
    api_bytes = 0
    long_edge = api_edge

    for slot in SLOTS:
        src = found.get(slot.index)
        if src is None:
            required = slot.required_for_submit
            report.add(
                Check(
                    f"file_{slot.index}",
                    not required,
                    f"missing {sku_id}_{slot.index}",
                    "download_error" if required else None,
                )
            )
            continue
        with Image.open(src) as im:
            edge = max(im.size)
        if edge < min_edge:
            report.add(
                Check(
                    f"res_{slot.index}",
                    False,
                    f"{src.name} long edge {edge} < {min_edge}",
                    "download_error",
                )
            )
            continue
        report.add(Check(f"res_{slot.index}", True, f"{src.name} long edge {edge}"))

        if slot.hunyuan_field is None:
            views.append({"index": slot.index, "hunyuan": None, "upload": False, "source": src.name})
            continue

        dest = output_dir / f"{sku_id}_{slot.index}{src.suffix.lower().replace('jpeg', 'jpg')}"
        _, _, nbytes = _resize_to_api(src, dest, long_edge)
        api_bytes += nbytes
        views.append(
            {
                "index": slot.index,
                "hunyuan": slot.hunyuan_field,
                "upload": True,
                "source": src.name,
                "api_file": dest.name,
                "api_bytes": nbytes,
            }
        )

    # Shrink API set if over 8MB (Hunyuan multi-view cap).
    cap = 8 * 1024 * 1024
    while api_bytes > cap and long_edge > API_EDGE_FLOOR:
        long_edge = max(API_EDGE_FLOOR, int(long_edge * 0.85))
        api_bytes = 0
        for item in views:
            if not item.get("upload"):
                continue
            src = found[item["index"]]
            dest = output_dir / item["api_file"]
            _, _, nbytes = _resize_to_api(src, dest, long_edge)
            item["api_bytes"] = nbytes
            api_bytes += nbytes

    report.metrics["api_edge"] = long_edge
    report.metrics["api_bytes"] = api_bytes
    report.metrics["views"] = views
    report.metrics["upload_count"] = sum(1 for v in views if v.get("upload"))
    report.metrics["expected_upload"] = len(UPLOAD_SLOTS)

    if api_bytes > cap:
        report.add(Check("api_budget", False, f"API images {api_bytes} > 8MB after shrink"))
    else:
        report.add(Check("api_budget", True, f"API images {api_bytes} bytes at long_edge={long_edge}"))

    report.finalize()
    report.write(out_report)
    report.output = str(out_report.resolve())
    return report
