from __future__ import annotations

"""On-the-fly capture gates (spec-capture T3).

Deliberately cheap and rule-based: Laplacian variance for sharpness, a
clipped-pixel ratio for over/under-exposure, and a min object-occupancy
guess via a saturated-background mask. No detection models.
"""

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np


@dataclass
class GateResult:
    ok: bool
    sharpness: float
    sharp_ok: bool
    exposure: float
    exposure_ok: bool
    underexposure: float
    overexposure: float
    brightness: float
    brightness_status: str
    object_ratio: float
    object_ok: bool
    reason: str = ""


_Gate = Literal["sharpness", "exposure", "object"]


def _sharpness(bgr: np.ndarray) -> float:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _exposure_metrics(bgr: np.ndarray) -> tuple[float, float, float]:
    """Return crushed-black ratio, blown-highlight ratio, and mean luminance."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    lo = float((gray <= 8).mean())
    hi = float((gray >= 247).mean())
    return lo, hi, float(gray.mean())


def _object_ratio(bgr: np.ndarray) -> float:
    """Rough object occupancy: non-background pixels on an assumed white/gray background."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # Background = bright, low-saturation. Foreground = saturated or dark.
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    foreground = ((sat > 40) | (val < 90)).astype(np.uint8)
    return float(foreground.mean())


def gate_frame(
    bgr: np.ndarray,
    *,
    min_sharpness: float = 60.0,
    max_exposure: float = 0.05,
    min_object_ratio: float = 0.40,
    max_object_ratio: float = 0.92,
) -> GateResult:
    """Evaluate a single RGB frame against the capture gates.

    Fails on the first violated gate so the operator can re-shoot immediately.
    """
    lap = _sharpness(bgr)
    under, over, brightness = _exposure_metrics(bgr)
    exp = under + over
    obj = _object_ratio(bgr)

    sharp_ok = lap >= min_sharpness
    exposure_ok = exp <= max_exposure
    object_ok = min_object_ratio <= obj <= max_object_ratio
    if over > max_exposure:
        brightness_status = "too_bright"
    elif under > max_exposure:
        brightness_status = "too_dark"
    elif not exposure_ok:
        brightness_status = "mixed_clipping"
    else:
        brightness_status = "ok"

    if not sharp_ok:
        reason = f"画面模糊（清晰度 {lap:.1f} < {min_sharpness}）"
    elif not exposure_ok:
        reason = (
            f"曝光异常（过暗 {under:.1%} / 过亮 {over:.1%}，"
            f"合计剪切 {exp:.1%} > {max_exposure:.1%}）"
        )
    elif not object_ok:
        reason = f"主体占比 {obj:.1%} 超出建议范围 [{min_object_ratio:.0%},{max_object_ratio:.0%}]"
    else:
        reason = ""

    return GateResult(
        ok=sharp_ok and exposure_ok and object_ok,
        sharpness=lap,
        sharp_ok=sharp_ok,
        exposure=exp,
        exposure_ok=exposure_ok,
        underexposure=under,
        overexposure=over,
        brightness=brightness,
        brightness_status=brightness_status,
        object_ratio=obj,
        object_ok=object_ok,
        reason=reason,
    )
