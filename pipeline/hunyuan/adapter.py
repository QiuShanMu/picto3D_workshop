from __future__ import annotations

"""Hunyuan 3D adapter: a small, SDK-free boundary.

The orchestration layer only depends on these three calls (spec-architecture §1):

    submit(sku_images, params) -> job_id
    poll(job_id) -> {status, files?, error?, credits?}
    download(files, out_dir) -> local_paths

Concrete providers:
  - MockHunyuan  : no cloud, returns a fixture GLB so the whole pipeline runs
                   before real credentials exist (requirements §78).
  - TencentHunyuan: real ai3d 2025-05-13 client, filled in when keys arrive.

Run with --mock (default when TENCENTCLOUD_SECRET_ID is absent) to exercise the
full chain turn-key. Swap to the real provider by setting env vars.
"""

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class HunyuanResult:
    job_id: str
    status: str          # submitted | wait | run | done | fail
    files: dict = field(default_factory=dict)   # {"glb": [url], "obj": [url], ...}
    error: str = ""
    credits: int = 0


class BaseHunyuan(Protocol):
    def submit(self, images: dict[str, str], params: dict) -> str: ...
    def poll(self, job_id: str) -> HunyuanResult: ...
    def download(self, files: dict[str, list[str]], out_dir) -> dict[str, list[str]]: ...


class MockHunyuan:
    """Offline provider. 'generates' a fixture GLB and returns it after a short
    fake poll so the pipeline (queue -> store -> validate) runs end-to-end."""

    def __init__(self, fixture_dir=None, poll_seconds: float = 1.5) -> None:
        from pathlib import Path
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self.poll_seconds = poll_seconds
        self._jobs: dict[str, dict] = {}

    def submit(self, images: dict[str, str], params: dict) -> str:
        job_id = f"mock-{int(time.time())}-{len(self._jobs)}"
        self._jobs[job_id] = {"status": "wait", "started": time.time()}
        return job_id

    def poll(self, job_id: str) -> HunyuanResult:
        job = self._jobs.get(job_id)
        if job is None:
            return HunyuanResult(job_id=job_id, status="fail", error="unknown job")
        elapsed = time.time() - job["started"]
        if elapsed < self.poll_seconds:
            return HunyuanResult(job_id=job_id, status="run")
        # done -> attach fixture files (or synthesize if none)
        files = {"glb": ["mock://glb/model.glb"], "obj": ["mock://obj/model.obj"]}
        if self.fixture_dir:
            pass  # real local fixture path handled by caller override
        job["status"] = "done"
        return HunyuanResult(job_id=job_id, status="done", files=files, credits=40)

    def download(self, files: dict[str, list[str]], out_dir) -> dict[str, list[str]]:
        from pathlib import Path
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        fixture = _find_fixture_anywhere(self.fixture_dir) if self.fixture_dir else None
        local: dict[str, list[str]] = {}
        for kind in files:
            path_map = {"glb": "model.glb", "obj": "model.obj"}
            dest = out_dir / path_map.get(kind, kind)
            if fixture is not None and fixture.exists():
                shutil.copy2(fixture, dest)
                local[kind] = [str(dest)]
            elif kind == "glb":  # no fixture: leave a placeholder so validate reports a file
                dest.write_bytes(b"")
                local[kind] = [str(dest)]
            else:
                local[kind] = []
        return local


def _find_fixture_anywhere(root):
    from pathlib import Path
    root = Path(root)
    for p in root.glob("**/*.glb"):
        return p
    for p in root.glob("**/*.obj"):
        return p
    return None


def _load_env() -> None:
    """Best-effort load of .env so keys set in a file work without export."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def make_adapter(*, provider: str = "auto", fixture_dir=None) -> BaseHunyuan:
    """Factory: returns the real provider when keys exist, else MockHunyuan."""
    _load_env()
    if provider == "auto":
        if os.environ.get("TENCENTCLOUD_SECRET_ID") and os.environ.get("TENCENTCLOUD_SECRET_KEY"):
            from pipeline.hunyuan.tencent import TencentHunyuan
            return TencentHunyuan()
        return MockHunyuan(fixture_dir=fixture_dir)
    if provider == "mock":
        return MockHunyuan(fixture_dir=fixture_dir)
    raise ValueError(f"unknown provider: {provider}")
