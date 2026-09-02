from __future__ import annotations

import numpy as np

from pipeline.capture.shading import calibrate_shading, ShadingLUT


def _make_reference() -> np.ndarray:
    """Synthetic 'uniform grey' frame with a radial color cast to remove.

    Center is blue-shifted (low R), edge is red-shifted (high R) — mirroring
    the real D435i vignetting measured on the rig.
    """
    h, w = 480, 640
    cy, cx = h / 2, w / 2
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - cx) / (w / 2)) ** 2 + ((yy - cy) / (h / 2)) ** 2)
    # base grey
    base = 120.0
    # R kept lower in center, higher at edge; B roughly flat.
    r_factor = 0.85 + 0.20 * (r**2)  # 0.85 center -> ~1.05 edge
    b_factor = 1.15
    img = np.zeros((h, w, 3), dtype=np.float32)
    img[:, :, 2] = base * r_factor  # R
    img[:, :, 1] = base  # G
    img[:, :, 0] = base * b_factor  # B
    return np.clip(img, 0, 255).astype(np.uint8)


def _band_rg(img: np.ndarray, lo: float, hi: float) -> float:
    h, w = img.shape[:2]
    cy, cx = h / 2, w / 2
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - cx) / (w / 2)) ** 2 + ((yy - cy) / (h / 2)) ** 2)
    p = img[(r >= lo) & (r < hi)].astype(np.float32)
    return float(p[:, 2].mean() / max(p[:, 1].mean(), 1e-6))


def test_calibrate_reduces_radial_cast():
    ref = _make_reference()
    lut = calibrate_shading(ref, tiles=12)
    corrected = lut.apply(ref)

    # Before: strong radial R/G gradient (center ~0.85, edge ~1.05).
    before_center = _band_rg(ref, 0.0, 0.3)
    before_edge = _band_rg(ref, 0.85, 1.2)
    assert before_edge - before_center > 0.1, "setup: expected a visible cast"

    # After: the two bands should converge (spread shrinks dramatically).
    after_center = _band_rg(corrected, 0.0, 0.3)
    after_edge = _band_rg(corrected, 0.85, 1.2)
    spread_before = abs(before_edge - before_center)
    spread_after = abs(after_edge - after_center)
    assert spread_after < spread_before * 0.25, (
        f"cast not removed: before spread={spread_before:.3f}, after spread={spread_after:.3f}"
    )


def test_shading_lut_roundtrip(tmp_path):
    ref = _make_reference()
    lut = calibrate_shading(ref, tiles=8)
    path = tmp_path / "shading.json"
    lut.save(path)
    loaded = ShadingLUT.load(path)
    assert loaded.tiles == lut.tiles
    assert loaded.grid.shape == lut.grid.shape
    np.testing.assert_allclose(loaded.grid, lut.grid, atol=1e-5)
