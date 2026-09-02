from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

from pipeline.report import Check, ModuleReport

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MIN_BYTES = 0
MANIFOLD_MIN = 0.95
SCALE_MAX_DEV = 0.10
TEXTURE_MIN_PX = 1024


def _as_mesh(loaded: trimesh.Trimesh | trimesh.Scene) -> trimesh.Trimesh:
    if isinstance(loaded, trimesh.Scene):
        geoms = [g for g in loaded.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not geoms:
            raise ValueError("scene contains no triangular meshes")
        return trimesh.util.concatenate(geoms)
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError(f"unsupported mesh type: {type(loaded)}")
    return loaded


def _degenerate_faces(mesh: trimesh.Trimesh) -> int:
    areas = mesh.area_faces
    return int(np.count_nonzero(areas <= 1e-12))


def _manifold_ratio(mesh: trimesh.Trimesh) -> float:
    if len(mesh.faces) == 0:
        return 0.0
    edges = mesh.edges_sorted
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    edge_count = {tuple(e): int(c) for e, c in zip(unique, counts)}
    good = 0
    for face in mesh.faces:
        verts = [tuple(sorted((int(face[i]), int(face[(i + 1) % 3])))) for i in range(3)]
        if all(edge_count.get(v, 0) == 2 for v in verts):
            good += 1
    return good / len(mesh.faces)


def _texture_max_edge(mesh: trimesh.Trimesh) -> int | None:
    visual = getattr(mesh, "visual", None)
    material = getattr(visual, "material", None) if visual is not None else None
    image = getattr(material, "baseColorTexture", None) if material is not None else None
    if image is None:
        image = getattr(material, "image", None) if material is not None else None
    if image is None:
        return None
    size = getattr(image, "size", None)
    if not size:
        return None
    return int(max(size))


def _scale_deviation(bbox: np.ndarray, size_mm: tuple[float, float, float]) -> float:
    target = np.array(sorted(size_mm), dtype=float)
    actual = np.array(sorted(bbox), dtype=float)
    if target[-1] <= 0 or actual[-1] <= 0:
        return 1.0
    scaled = actual * (target[-1] / actual[-1])
    rel = np.abs(scaled - target) / np.maximum(target, 1e-6)
    return float(rel.max())


def run_validate(
    model_path: Path,
    *,
    size_mm: tuple[float, float, float] | None = None,
    min_bytes: int = DEFAULT_MIN_BYTES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    out_path: Path | None = None,
) -> ModuleReport:
    model_path = model_path.resolve()
    report = ModuleReport(module="validate", verdict="ok", input=str(model_path))
    if out_path is None:
        out_path = model_path.with_name("report.json")

    if not model_path.is_file():
        report.add(Check("file_exists", False, "model file not found", "download_error"))
        report.finalize()
        report.write(out_path)
        report.output = str(out_path)
        return report

    size = model_path.stat().st_size
    report.metrics["file_bytes"] = size
    if min_bytes and size < min_bytes:
        report.add(
            Check("file_size_min", False, f"{size} < min {min_bytes}", "download_error")
        )
    elif max_bytes and size > max_bytes:
        report.add(
            Check("file_size_max", False, f"{size} > max {max_bytes}", "download_error")
        )
    else:
        report.add(Check("file_size", True, f"{size} bytes"))

    try:
        loaded = trimesh.load(model_path, force="mesh")
        mesh = _as_mesh(loaded)
        report.add(Check("file_open", True, f"faces={len(mesh.faces)}"))
    except Exception as exc:
        report.add(Check("file_open", False, str(exc), "download_error"))
        report.finalize()
        report.write(out_path)
        report.output = str(out_path)
        return report

    report.metrics["faces"] = int(len(mesh.faces))
    report.metrics["vertices"] = int(len(mesh.vertices))
    bbox = mesh.bounding_box.extents.astype(float)
    report.metrics["bbox"] = [float(x) for x in bbox]

    degenerate = _degenerate_faces(mesh)
    report.metrics["degenerate_faces"] = degenerate
    report.add(
        Check(
            "degenerate_faces",
            degenerate == 0,
            f"{degenerate} zero-area faces",
            None if degenerate == 0 else "geometry_error",
        )
    )

    ratio = _manifold_ratio(mesh)
    report.metrics["manifold_ratio"] = ratio
    report.add(
        Check(
            "manifold_ratio",
            ratio >= MANIFOLD_MIN,
            f"{ratio:.4f} (min {MANIFOLD_MIN})",
            None if ratio >= MANIFOLD_MIN else "geometry_error",
        )
    )

    winding_ok = bool(mesh.is_winding_consistent)
    report.metrics["winding_consistent"] = winding_ok
    report.add(
        Check(
            "normals",
            winding_ok,
            "winding consistent" if winding_ok else "inconsistent winding",
            None if winding_ok else "geometry_error",
        )
    )

    tex_edge = _texture_max_edge(mesh)
    if tex_edge is None:
        report.add(Check("texture", True, "no texture; skipped"))
    else:
        report.metrics["texture_max_edge"] = tex_edge
        report.add(
            Check(
                "texture",
                tex_edge >= TEXTURE_MIN_PX,
                f"{tex_edge}px (min {TEXTURE_MIN_PX})",
                None if tex_edge >= TEXTURE_MIN_PX else "texture_error",
            )
        )

    if size_mm is not None:
        dev = _scale_deviation(bbox, size_mm)
        report.metrics["size_mm"] = list(size_mm)
        report.metrics["scale_deviation"] = dev
        report.add(
            Check(
                "scale",
                dev <= SCALE_MAX_DEV,
                f"max relative deviation {dev:.4f} (limit {SCALE_MAX_DEV})",
                None if dev <= SCALE_MAX_DEV else "scale_review",
            )
        )
    else:
        report.add(Check("scale", True, "size_mm not provided; skipped"))

    report.finalize()
    report.output = str(out_path.resolve())
    report.write(out_path)
    return report
