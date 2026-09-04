from __future__ import annotations

"""Flat-field / color-shading correction for D435i RGB frames.

D435i's RGB optics + sensor show a radial color cast (center vs edge) and a
circumferential imbalance. White balance fixes the *overall* tint but cannot
remove the *spatially varying* cast. This module builds a per-camera gain LUT
from a uniformly-lit reference frame and applies it to every captured frame.

Design notes
------------
- We do not assume the reference is pure white. We estimate, per block, the
  R/G and B/G ratio *relative to the brightest/central block*, then invert it
  to a gain. This removes the lens shading regardless of the reference grey
  level (as long as it is spatially uniform and not clipped).
- We tile the frame (e.g. 16x16), take a robust (median) ratio per tile, and
  upsample smoothly so the correction has no visible block seams.
- Gains are normalised so the brightest central tile is ~1.0 (no global
  brightening); we only rebalance the channels relative to each other.
- We keep green as the reference channel (gain 1.0) and only adjust R and B.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

DEFAULT_TILES = 16  # grid resolution of the shading LUT


@dataclass
class ShadingLUT:
    """Per-channel gain maps, one gain per channel per tile (H x W).

    Channels are stored in BGR order (matching cv2). Green is the neutral
    reference (gain 1.0); R and B carry the correction.

    Optional ``v_grid`` is a 1-D vertical residual (3 x bands) for thin
    bottom/top fringes that a coarse tile grid averages away.
    """

    grid: np.ndarray  # shape (3, tiles, tiles), float32
    tiles: int
    width: int
    height: int
    v_grid: np.ndarray | None = None  # shape (3, bands) or None

    @property
    def shape(self) -> tuple[int, int]:
        return self.height, self.width

    def save(self, path: Path, extra: dict | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "shading.v1",
            "tiles": self.tiles,
            "width": self.width,
            "height": self.height,
            "grid": self.grid.tolist(),
        }
        if self.v_grid is not None:
            payload["v_grid"] = self.v_grid.tolist()
        if extra:
            payload.update(extra)
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    @staticmethod
    def load(path: Path) -> "ShadingLUT":
        payload = json.loads(path.read_text(encoding="utf-8"))
        v = payload.get("v_grid")
        return ShadingLUT(
            grid=np.asarray(payload["grid"], dtype=np.float32),
            tiles=payload["tiles"],
            width=payload["width"],
            height=payload["height"],
            v_grid=None if v is None else np.asarray(v, dtype=np.float32),
        )

    def apply(self, bgr: np.ndarray) -> np.ndarray:
        """Return a color-corrected BGR frame (same dtype/shape)."""
        h, w = bgr.shape[:2]
        # Upsample 3x tiles grid to full resolution, normalized 0..1 coords.
        grid = self.grid
        full = np.empty((3, h, w), dtype=np.float32)
        for c in range(3):
            full[c] = cv2.resize(
                grid[c],
                (w, h),
                interpolation=cv2.INTER_CUBIC,
            )
        if self.v_grid is not None and self.v_grid.size:
            for c in range(3):
                col = cv2.resize(
                    self.v_grid[c].reshape(-1, 1),
                    (1, h),
                    interpolation=cv2.INTER_LINEAR,
                ).reshape(h, 1)
                full[c] *= col
        out = bgr.astype(np.float32) * full.transpose(1, 2, 0)
        return np.clip(out, 0, 255).astype(bgr.dtype)


def _fill_invalid(gain: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Propagate valid tile gains into holes (desk objects / dark corners)."""
    filled = gain.copy()
    known = valid.copy()
    inv = ~known
    for _ in range(gain.shape[0] + gain.shape[1]):
        if not inv.any():
            break
        new = filled.copy()
        ys, xs = np.where(inv)
        progressed = False
        for ty, tx in zip(ys.tolist(), xs.tolist()):
            y0, y1 = max(0, ty - 1), min(gain.shape[0], ty + 2)
            x0, x1 = max(0, tx - 1), min(gain.shape[1], tx + 2)
            neigh = filled[y0:y1, x0:x1][known[y0:y1, x0:x1]]
            if neigh.size:
                new[ty, tx] = float(np.median(neigh))
                known[ty, tx] = True
                inv[ty, tx] = False
                progressed = True
        filled = new
        if not progressed:
            break
    filled[inv] = 1.0
    return filled


def _vertical_chroma_gains(
    reference: np.ndarray,
    *,
    bands: int = 36,
    min_gain: float,
    max_gain: float,
    anchor: str,
) -> np.ndarray:
    """1-D R/B gains vs row. Bottom bands use a high percentile so a thin
    magenta fringe is not washed out by the desk above it."""
    h = reference.shape[0]
    ratio_r = np.ones(bands, dtype=np.float32)
    ratio_b = np.ones(bands, dtype=np.float32)
    valid = np.zeros(bands, dtype=bool)
    ref = reference.astype(np.float32)
    for i in range(bands):
        y0, y1 = i * h // bands, (i + 1) * h // bands
        block = ref[y0:y1].reshape(-1, 3)
        g = float(np.median(block[:, 1]))
        if g < 12:
            continue
        r_med = float(np.median(block[:, 2]))
        b_med = float(np.median(block[:, 0]))
        # Last 20%: blend in the magenta-ward tail so a thin fringe counts,
        # but do not let a single hot object dominate the band.
        if i >= int(bands * 0.80):
            r = 0.55 * r_med + 0.45 * float(np.percentile(block[:, 2], 75))
            b = 0.55 * b_med + 0.45 * float(np.percentile(block[:, 0], 75))
        else:
            r, b = r_med, b_med
        ratio_r[i] = r / max(g, 1e-3)
        ratio_b[i] = b / max(g, 1e-3)
        valid[i] = True
    if not valid.any():
        return np.ones((3, bands), dtype=np.float32)
    if anchor == "white":
        target_r = target_b = 1.0
    else:
        mid = valid.copy()
        mid[: bands // 3] = False
        mid[2 * bands // 3 :] = False
        if not mid.any():
            mid = valid
        target_r = float(np.median(ratio_r[mid]))
        target_b = float(np.median(ratio_b[mid]))
    gain_r = np.where(valid, target_r / np.maximum(ratio_r, 1e-3), 1.0)
    gain_b = np.where(valid, target_b / np.maximum(ratio_b, 1e-3), 1.0)
    gain_r = _fill_invalid(gain_r.reshape(-1, 1), valid.reshape(-1, 1)).ravel()
    gain_b = _fill_invalid(gain_b.reshape(-1, 1), valid.reshape(-1, 1)).ravel()
    kernel = np.array([0.25, 0.50, 0.25], dtype=np.float32)
    gain_r = np.clip(np.convolve(gain_r, kernel, mode="same"), min_gain, max_gain)
    gain_b = np.clip(np.convolve(gain_b, kernel, mode="same"), min_gain, max_gain)
    gain_g = np.ones_like(gain_r)
    return np.stack([gain_b, gain_g, gain_r], axis=0).astype(np.float32)


def calibrate_shading(
    reference: np.ndarray,
    *,
    tiles: int = DEFAULT_TILES,
    min_gain: float = 0.80,
    max_gain: float = 1.25,
    anchor: str = "center",
    flatten_luma: bool = False,
) -> ShadingLUT:
    """Build a shading LUT from a single uniformly-lit reference frame.

    reference: BGR frame of an evenly lit neutral surface (white/gray) with NO
        product present. It should be exposed in the same way as captures.

    anchor:
      - ``center`` (default): match every tile's R/G and B/G to the central
        patch (legacy; preserves whatever hue the centre already has after WB).
      - ``white``: treat the reference surface as true white (R=G=B). Use this
        on a white-desk workstation when residual global magenta/cyan remains.
    flatten_luma: also even out brightness (green is no longer locked at 1.0).
        Dark / non-surface tiles are rejected and filled from neighbours.
    """
    if reference.ndim != 3 or reference.shape[2] != 3:
        raise ValueError("reference frame must be HxWx3 BGR")
    if anchor not in ("center", "white"):
        raise ValueError(f"anchor must be 'center' or 'white', got {anchor!r}")
    h, w = reference.shape[:2]
    ref = reference.astype(np.float32)

    # Tile the reference; per tile compute median B,G,R and channel ratios.
    tile_h = max(1, h // tiles)
    tile_w = max(1, w // tiles)
    ratio_r = np.zeros((tiles, tiles), dtype=np.float32)
    ratio_b = np.zeros((tiles, tiles), dtype=np.float32)
    med_g = np.zeros((tiles, tiles), dtype=np.float32)
    quality = np.zeros((tiles, tiles), dtype=np.float32)  # 0 if tile too dark/saturated

    for ty in range(tiles):
        for tx in range(tiles):
            y0, y1 = ty * tile_h, (ty + 1) * tile_h
            x0, x1 = tx * tile_w, (tx + 1) * tile_w
            block = ref[y0:y1, x0:x1].reshape(-1, 3)
            b = np.median(block[:, 0])
            g = np.median(block[:, 1])
            r = np.median(block[:, 2])
            med_g[ty, tx] = g
            if g < 12:  # too dark -> can't estimate reliably
                quality[ty, tx] = 0.0
                ratio_r[ty, tx] = 1.0
                ratio_b[ty, tx] = 1.0
                continue
            # channel ratios relative to green; over-exposure guard
            if g > 245:
                quality[ty, tx] = 0.5
            else:
                quality[ty, tx] = 1.0
            ratio_r[ty, tx] = r / g
            ratio_b[ty, tx] = b / g

    # Target neutral = central region's channel ratios (the region nearest the
    # lens axis, where shading is minimal). Anchoring to the center makes every
    # other tile converge to the same colour, removing spatial cast while
    # preserving the center's (already WB-corrected) hue.
    # ``white`` instead forces R=G=B so a globally tinted white desk is pulled
    # back to neutral, not to the still-tinted centre.
    valid = quality > 0
    if valid.sum() == 0:
        raise RuntimeError("reference frame too dark; cannot calibrate shading")
    if anchor == "white":
        target_r = 1.0
        target_b = 1.0
    else:
        c = tiles // 2
        center_mask = valid & (np.abs(np.arange(tiles)[:, None] - c) <= 2) & (np.abs(np.arange(tiles)[None, :] - c) <= 2)
        if center_mask.sum() == 0:
            center_mask = valid
        target_r = float(np.median(ratio_r[center_mask]))
        target_b = float(np.median(ratio_b[center_mask]))

    # Gain per tile brings each tile's ratio toward the target.
    # Green stays 1.0 unless flatten_luma is on.
    gain_r = np.where(valid, target_r / np.maximum(ratio_r, 1e-3), 1.0)
    gain_b = np.where(valid, target_b / np.maximum(ratio_b, 1e-3), 1.0)
    gain_g = np.ones_like(gain_r)

    if flatten_luma:
        bright = valid & (med_g >= max(12.0, float(np.median(med_g[valid])) * 0.70))
        if bright.sum() == 0:
            bright = valid
        target_luma = float(np.median(med_g[bright]))
        # Do not try to lift clipped highlights or dark objects.
        luma_ok = bright & (med_g < 250)
        gain_luma = np.where(
            luma_ok,
            target_luma / np.maximum(med_g, 1e-3),
            1.0,
        )
        gain_luma = np.clip(gain_luma, min_gain, max_gain)
        gain_r = gain_r * gain_luma
        gain_b = gain_b * gain_luma
        gain_g = gain_luma
        gain_r = _fill_invalid(gain_r, luma_ok)
        gain_b = _fill_invalid(gain_b, luma_ok)
        gain_g = _fill_invalid(gain_g, luma_ok)
    else:
        gain_r = np.where(valid, gain_r, 1.0)
        gain_b = np.where(valid, gain_b, 1.0)

    gain_r = np.clip(gain_r, min_gain, max_gain)
    gain_b = np.clip(gain_b, min_gain, max_gain)
    gain_g = np.clip(gain_g, min_gain, max_gain)

    # Smooth so the correction has no block seams.
    gain_r = cv2.GaussianBlur(gain_r, (0, 0), sigmaX=0.9, sigmaY=0.9)
    gain_b = cv2.GaussianBlur(gain_b, (0, 0), sigmaX=0.9, sigmaY=0.9)
    if flatten_luma:
        gain_g = cv2.GaussianBlur(gain_g, (0, 0), sigmaX=0.9, sigmaY=0.9)
    else:
        gain_g = np.ones_like(gain_r)

    grid = np.stack([gain_b, gain_g, gain_r], axis=0)  # BGR
    spatial = ShadingLUT(grid=grid.astype(np.float32), tiles=tiles, width=w, height=h)
    # Vertical residual on the already spatially-corrected frame, so a thin
    # bottom fringe is fixed without double-counting the 2-D LUT.
    residual = spatial.apply(reference)
    # Residual gains stay close to 1; a wide clip would over-green the bottom.
    v_grid = _vertical_chroma_gains(
        residual, bands=max(24, tiles * 2), min_gain=0.88, max_gain=1.12, anchor=anchor,
    )
    return ShadingLUT(
        grid=grid.astype(np.float32),
        tiles=tiles,
        width=w,
        height=h,
        v_grid=v_grid,
    )


def apply_shading(bgr: np.ndarray, lut: ShadingLUT) -> np.ndarray:
    """Convenience wrapper: apply a pre-built LUT to a BGR frame."""
    return lut.apply(bgr)
