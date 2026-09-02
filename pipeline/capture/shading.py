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
    """

    grid: np.ndarray  # shape (3, tiles, tiles), float32
    tiles: int
    width: int
    height: int

    @property
    def shape(self) -> tuple[int, int]:
        return self.height, self.width

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "shading.v1",
            "tiles": self.tiles,
            "width": self.width,
            "height": self.height,
            "grid": self.grid.tolist(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    @staticmethod
    def load(path: Path) -> "ShadingLUT":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ShadingLUT(
            grid=np.asarray(payload["grid"], dtype=np.float32),
            tiles=payload["tiles"],
            width=payload["width"],
            height=payload["height"],
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
                interpolation=cv2.INTER_LINEAR,
            )
        out = bgr.astype(np.float32) * full.transpose(1, 2, 0)
        return np.clip(out, 0, 255).astype(bgr.dtype)


def calibrate_shading(
    reference: np.ndarray,
    *,
    tiles: int = DEFAULT_TILES,
    min_gain: float = 0.80,
    max_gain: float = 1.25,
) -> ShadingLUT:
    """Build a shading LUT from a single uniformly-lit reference frame.

    reference: BGR frame of an evenly lit neutral surface (white/gray) with NO
        product present. It should be exposed in the same way as captures.
    """
    if reference.ndim != 3 or reference.shape[2] != 3:
        raise ValueError("reference frame must be HxWx3 BGR")
    h, w = reference.shape[:2]
    ref = reference.astype(np.float32)

    # Tile the reference; per tile compute median B,G,R and channel ratios.
    tile_h = max(1, h // tiles)
    tile_w = max(1, w // tiles)
    ratio_r = np.zeros((tiles, tiles), dtype=np.float32)
    ratio_b = np.zeros((tiles, tiles), dtype=np.float32)
    quality = np.zeros((tiles, tiles), dtype=np.float32)  # 0 if tile too dark/saturated

    for ty in range(tiles):
        for tx in range(tiles):
            y0, y1 = ty * tile_h, (ty + 1) * tile_h
            x0, x1 = tx * tile_w, (tx + 1) * tile_w
            block = ref[y0:y1, x0:x1].reshape(-1, 3)
            b = np.median(block[:, 0])
            g = np.median(block[:, 1])
            r = np.median(block[:, 2])
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
    valid = quality > 0
    if valid.sum() == 0:
        raise RuntimeError("reference frame too dark; cannot calibrate shading")
    c = tiles // 2
    center_mask = valid & (np.abs(np.arange(tiles)[:, None] - c) <= 2) & (np.abs(np.arange(tiles)[None, :] - c) <= 2)
    if center_mask.sum() == 0:
        center_mask = valid
    target_r = float(np.median(ratio_r[center_mask]))
    target_b = float(np.median(ratio_b[center_mask]))

    # Gain per tile brings each tile's ratio toward the central target.
    # Green stays exactly 1.0 (neutral reference); we only fix R and B.
    gain_r = np.where(valid, target_r / np.maximum(ratio_r, 1e-3), 1.0)
    gain_b = np.where(valid, target_b / np.maximum(ratio_b, 1e-3), 1.0)
    gain_r = np.clip(gain_r, min_gain, max_gain)
    gain_b = np.clip(gain_b, min_gain, max_gain)
    gain_g = np.ones_like(gain_r)

    # Smooth only R and B so the correction has no block seams. G stays 1.
    gain_r = cv2.GaussianBlur(gain_r, (0, 0), sigmaX=0.9, sigmaY=0.9)
    gain_b = cv2.GaussianBlur(gain_b, (0, 0), sigmaX=0.9, sigmaY=0.9)
    gain_g = np.ones_like(gain_r)

    grid = np.stack([gain_b, gain_g, gain_r], axis=0)  # BGR
    return ShadingLUT(
        grid=grid.astype(np.float32),
        tiles=tiles,
        width=w,
        height=h,
    )


def apply_shading(bgr: np.ndarray, lut: ShadingLUT) -> np.ndarray:
    """Convenience wrapper: apply a pre-built LUT to a BGR frame."""
    return lut.apply(bgr)
