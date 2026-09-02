from __future__ import annotations

"""Upload local API images to Tencent COS and return publicly readable URLs.

Hunyuan Submit wants a single front ``ImageUrl`` + ``MultiViewImages``, and those
must be publicly reachable URLs (or base64) — the cloud cannot read a local path.
This module uploads each local image to a COS bucket and hands back a URL.

Two URL modes (both use the same SecretId/SecretKey as ai3d):

- ``public`` (default): ``https://<bucket>.cos.<region>.myqcloud.com/<key>`` —
  requires the bucket/objects to allow public read (or set via ACL).
- ``presign``: a short-lived pre-signed GET URL generated with the SDK. Use this
  if the bucket is private; the URL must stay valid until Hunyuan pulls the image.

Only activated when ``TENCENTCLOUD_SECRET_ID/KEY`` and ``TENCENTCLOUD_COS_BUCKET``
are present. Otherwise raise a clear error so the submit path fails loudly.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    pass

# Lazily-imported SDKs so this module imports cleanly before deps/keys exist.
_COS_CLIENT = None


def _load_env() -> None:
    """Best-effort load of .env so keys set in a file work without export."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _cos_client():
    global _COS_CLIENT
    if _COS_CLIENT is not None:
        return _COS_CLIENT
    _load_env()
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as e:  # pragma: no cover - SDK not installed
        raise RuntimeError(
            "cos-python-sdk-v5 not installed; pip install -e '.[hunyuan]'"
        ) from e
    secret_id = os.environ.get("TENCENTCLOUD_SECRET_ID")
    secret_key = os.environ.get("TENCENTCLOUD_SECRET_KEY")
    if not secret_id or not secret_key:
        raise RuntimeError("missing TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY")
    region = os.environ.get("TENCENTCLOUD_COS_REGION") or os.environ.get("TENCENTCLOUD_REGION", "ap-guangzhou")
    config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key)
    _COS_CLIENT = CosS3Client(config)
    return _COS_CLIENT


def _bucket() -> str:
    _load_env()
    bucket = os.environ.get("TENCENTCLOUD_COS_BUCKET")
    if not bucket:
        raise RuntimeError(
            "missing TENCENTCLOUD_COS_BUCKET (COS bucket) for image upload; "
            "set it in .env or export it"
        )
    return bucket


def _key_for(path: Path, kind: str | None = None) -> str:
    """Build a stable object key, e.g. sku/01/front.jpg."""
    return path.name


def upload_image(
    local_path: str | Path,
    *,
    key: str | None = None,
    url_mode: str = "public",
) -> str:
    """Upload a single local image and return the public URL for the cloud.

    ``url_mode``: 'public' (bucket endpoint URL) or 'presign' (signed GET).
    """
    if url_mode not in ("public", "presign"):
        raise ValueError(f"unknown url_mode: {url_mode}")
    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(f"image not found: {path}")
    client = _cos_client()
    bucket = _bucket()
    object_key = key or _key_for(path)
    _load_env()
    region = os.environ.get("TENCENTCLOUD_COS_REGION") or os.environ.get("TENCENTCLOUD_REGION", "ap-guangzhou")

    client.put_object(Bucket=bucket, Key=object_key, Body=open(path, "rb"))

    if url_mode == "presign":
        # 10-minute signed GET; long enough for submit, refreshed per task.
        return client.get_presigned_url(
            Bucket=bucket, Key=object_key, Method="GET", Expired=600,
        )
    return f"https://{bucket}.cos.{region}.myqcloud.com/{object_key}"


def upload_images(
    images: dict[str, str | Path],
    *,
    url_mode: str = "public",
) -> dict[str, str]:
    """Upload a dict of {view: local_path} and return {view: public_url}.

    Values that are already ``http(s)://`` URLs are passed through unchanged —
    so callers may mix pre-uploaded URLs with local api/ images.
    """
    out: dict[str, str] = {}
    for view, local in images.items():
        s = str(local)
        if s.startswith(("http://", "https://")):
            out[view] = s
            continue
        out[view] = upload_image(local, key=_key_for(Path(local), view), url_mode=url_mode)
    return out
