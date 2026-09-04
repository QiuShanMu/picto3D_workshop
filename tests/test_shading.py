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


def test_calibrate_white_anchor_kills_global_magenta():
    """White-desk scene: whole frame is magenta; center-anchor would keep it."""
    h, w = 240, 320
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :, 0] = 180  # B
    img[:, :, 1] = 140  # G
    img[:, :, 2] = 170  # R  (R/G=1.21, B/G=1.29)
    # slight extra magenta toward the right edge
    xx = np.linspace(0, 1, w, dtype=np.float32)
    img[:, :, 2] = np.clip(img[:, :, 2].astype(np.float32) + 20 * xx, 0, 255).astype(np.uint8)

    center_lut = calibrate_shading(img, tiles=8, anchor="center")
    white_lut = calibrate_shading(img, tiles=8, anchor="white")
    after_center = center_lut.apply(img).astype(np.float32)
    after_white = white_lut.apply(img).astype(np.float32)

    def rg_bg(arr: np.ndarray) -> tuple[float, float]:
        g = float(arr[:, :, 1].mean())
        return float(arr[:, :, 2].mean() / g), float(arr[:, :, 0].mean() / g)

    rg_c, bg_c = rg_bg(after_center)
    rg_w, bg_w = rg_bg(after_white)
    assert abs(rg_c - 170 / 140) < 0.08, "center-anchor should preserve global magenta"
    assert abs(rg_w - 1.0) < 0.06 and abs(bg_w - 1.0) < 0.06, (
        f"white-anchor should neutralize: R/G={rg_w:.3f} B/G={bg_w:.3f}"
    )


def test_vertical_grid_kills_bottom_magenta_fringe():
    """Thin purple strip at the bottom is averaged away by coarse tiles alone."""
    h, w = 320, 240
    img = np.full((h, w, 3), 140, dtype=np.uint8)
    img[-16:] = (190, 140, 185)  # B,G,R magenta fringe
    lut = calibrate_shading(img, tiles=16, anchor="white")
    assert lut.v_grid is not None
    out = lut.apply(img).astype(np.float32)
    fringe = out[-12:]
    g = float(fringe[:, :, 1].mean())
    rg = float(fringe[:, :, 2].mean() / g)
    bg = float(fringe[:, :, 0].mean() / g)
    assert abs(rg - 1.0) < 0.10 and abs(bg - 1.0) < 0.10, f"fringe still tinted R/G={rg:.3f} B/G={bg:.3f}"


def test_shading_lut_roundtrip(tmp_path):
    ref = _make_reference()
    lut = calibrate_shading(ref, tiles=8)
    path = tmp_path / "shading.json"
    lut.save(path)
    loaded = ShadingLUT.load(path)
    assert loaded.tiles == lut.tiles
    assert loaded.grid.shape == lut.grid.shape
    np.testing.assert_allclose(loaded.grid, lut.grid, atol=1e-5)
