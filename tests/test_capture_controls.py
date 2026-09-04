from __future__ import annotations

import json

import numpy as np

from pipeline.capture.gate import gate_frame
from pipeline.capture.webapp import CameraWorker, WebOptions


def test_gate_distinguishes_too_bright_and_too_dark() -> None:
    bright = gate_frame(np.full((80, 120, 3), 255, dtype=np.uint8))
    dark = gate_frame(np.zeros((80, 120, 3), dtype=np.uint8))
    assert bright.brightness_status == "too_bright"
    assert bright.overexposure == 1.0
    assert dark.brightness_status == "too_dark"
    assert dark.underexposure == 1.0


def test_web_capture_saves_even_when_quality_advisory_fails(tmp_path) -> None:
    worker = CameraWorker(WebOptions(capture_root=tmp_path, batch_id="b1", sku_id="SKU-1"))
    worker.serial = "TEST"
    worker.color = "1920x1080"
    bad = np.full((120, 160, 3), 255, dtype=np.uint8)

    worker._do_capture({"batch": "b1", "sku": "SKU-1", "index": "01"}, bad)

    result = worker._capture_result
    assert result is not None
    assert result["ok"] is True
    assert result["quality_ok"] is False
    assert result["warning"]
    capture = json.loads((tmp_path / "b1/SKU-1/capture.json").read_text(encoding="utf-8"))
    assert capture["frames"][0]["ok"] is True
    assert capture["frames"][0]["gate"]["ok"] is False


def test_ev_is_applied_as_exposure_stops(tmp_path) -> None:
    class FakeCamera:
        def set_exposure_controls(self, *, auto_exposure, exposure, gain):
            assert auto_exposure is False
            assert exposure == 400.0
            assert gain == 32.0
            return {
                "auto_exposure": False,
                "exposure": exposure,
                "gain": gain,
                "exposure_range": {"min": 1.0, "max": 1000.0, "step": 1.0, "default": 100.0},
                "gain_range": {"min": 0.0, "max": 128.0, "step": 1.0, "default": 64.0},
            }

    worker = CameraWorker(WebOptions(capture_root=tmp_path))
    worker._base_exposure = 100.0
    result = worker._apply_exposure_request(
        FakeCamera(), {"auto_exposure": False, "ev": 2.0, "gain": 32},
    )
    assert result["ok"] is True
    assert result["ev"] == 2.0
    assert result["controls"]["exposure"] == 400.0
