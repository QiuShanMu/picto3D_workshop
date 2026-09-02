from __future__ import annotations

"""Real Tencent ai3d provider (product 1804, version 2025-05-13).

Lazily imports the Tencent Cloud SDK so this module imports cleanly even before
the package / credentials are present. Activated only when
TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY are set.

Image upload: local api/<sku>/ images are uploaded to COS first (see
pipeline.hunyuan.upload) and the resulting public URLs are passed as ImageUrl /
MultiViewImages. Download uses plain HTTP GET against the returned ResultFile3Ds
URLs (24h expiry) — files must be saved before they lapse.
"""

import os
import re
from pathlib import Path

from pipeline.hunyuan.adapter import HunyuanResult
from pipeline.hunyuan.upload import upload_images

API_VERSION = "2025-05-13"
ENDPOINT = "ai3d.tencentcloudapi.com"

# Map Tencent ResultFile3D Type -> local filename (fallback when URL has no ext).
_TYPE_FILENAME = {
    "GLB": "model.glb",
    "OBJ": "model.obj",
    "STL": "model.stl",
    "USDZ": "model.usdz",
    "FBX": "model.fbx",
    "ZIP": "model.zip",
    "PREVIEW": "preview.jpg",
}
# Strip query/fragment and guard against path traversal.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"missing env {name}")
    return val


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _short_type(t: str) -> str:
    """Normalise a Type token (e.g. 'GLB', 'glb', 'model.glb') to an extension."""
    low = str(t).lower()
    for token in ("glb", "obj", "stl", "usdz", "fbx", "zip"):
        if token in low:
            return token
    return ""


class TencentHunyuan:
    """submit/poll/download against ai3d SubmitHunyuanTo3DProJob / Query."""

    def __init__(self, region: str | None = None) -> None:
        _load_env()
        self.region = region or os.environ.get("TENCENTCLOUD_REGION", "ap-guangzhou")
        self.url_mode = os.environ.get("TENCENTCLOUD_UPLOAD_URL_MODE", "public")
        self._client = None  # lazy

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.ai3d.v20250513 import ai3d_client
        except ImportError as e:  # pragma: no cover - SDK not installed yet
            raise RuntimeError(
                "tencentcloud-sdk-python not installed; pip install -e '.[hunyuan]'"
            ) from e
        cred = credential.Credential(_require_env("TENCENTCLOUD_SECRET_ID"), _require_env("TENCENTCLOUD_SECRET_KEY"))
        http = HttpProfile(endpoint=ENDPOINT, reqTimeout=30)
        prof = ClientProfile(httpProfile=http)
        self._client = ai3d_client.Ai3dClient(cred, self.region, prof)
        return self._client

    def submit(self, images: dict[str, str], params: dict) -> str:
        from tencentcloud.ai3d.v20250513 import models
        client = self._get_client()
        req = models.SubmitHunyuanTo3DProJobRequest()
        req.Model = params.get("model", "3.1")
        req.EnablePBR = params.get("enable_pbr", True)
        req.GenerateType = params.get("generate_type", "Normal")
        if params.get("face_count"):
            req.FaceCount = params["face_count"]

        # Upload any local paths (api/<sku>/...) to COS -> public URLs.
        remote = upload_images(images, url_mode=self.url_mode)

        if "front" in remote:
            req.ImageUrl = remote["front"]
        multi = [models.ViewImage(ViewType=k, ViewImageUrl=v) for k, v in remote.items() if k != "front" and v]
        if multi:
            req.MultiViewImages = multi
        resp = client.SubmitHunyuanTo3DProJob(req)
        return resp.JobId

    def poll(self, job_id: str) -> HunyuanResult:
        from tencentcloud.ai3d.v20250513 import models
        client = self._get_client()
        req = models.QueryHunyuanTo3DProJobRequest()
        req.JobId = job_id
        resp = client.QueryHunyuanTo3DProJob(req)
        status_map = {"WAIT": "wait", "RUN": "run", "DONE": "done", "FAIL": "fail"}
        files: dict[str, list[str]] = {}
        for f in resp.ResultFile3Ds or []:
            files.setdefault(f.Type, []).append(f.Url)
        return HunyuanResult(
            job_id=job_id,
            status=status_map.get(resp.Status, resp.Status),
            files=files,
            error=resp.ErrorMessage or "",
            credits=resp.ResultCreditConsumed or 0,
        )

    def download(self, files: dict[str, list[str]], out_dir, *, _get=None) -> dict[str, list[str]]:
        """HTTP-download every returned file into out_dir, named by Type.

        ``files`` maps a Type (e.g. 'GLB', 'OBJ') to a list of URLs. We download
        each URL and name the file by Type + extension inferred from the URL.
        ``_get`` may be injected for testing (defaults to requests.get).
        """
        import requests
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        getter = _get or requests.get
        def _url_basename(url: str) -> str:
            p = url.split("?", 1)[0].rstrip("/").split("/")[-1]
            return p if p else ""

        local: dict[str, list[str]] = {}
        for type_key, urls in (files or {}).items():
            ext = _short_type(type_key)
            dests = []
            downloaded = 0
            for url in (urls or []):
                if not url:
                    continue
                # Prefer the URL's own basename (e.g. model.glb?sig=.. -> model.glb)
                name = _url_basename(url)
                if not name:
                    # fall back to Type's canonical filename, or a generic name
                    name = _TYPE_FILENAME.get(str(type_key).upper(), f"model.{ext}" if ext else "file")
                elif downloaded > 0:
                    stem_p = Path(name).stem
                    suffix_p = Path(name).suffix or ("." + ext if ext else ".bin")
                    name = f"{stem_p}_{downloaded}{suffix_p}"
                dest = out_dir / _SAFE_NAME.sub("_", name)
                try:
                    r = getter(url, timeout=60)
                    r.raise_for_status()
                    dest.write_bytes(r.content)
                    dests.append(str(dest))
                    downloaded += 1
                except Exception as e:  # noqa: BLE001 - surface per-file failure
                    print(f"[hunyuan.download] failed {url}: {e}")
            local[type_key] = dests
        return local
