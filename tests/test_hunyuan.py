from __future__ import annotations

import os
from pathlib import Path

import pytest

from pipeline.hunyuan.tencent import TencentHunyuan
from pipeline.hunyuan.upload import upload_image, upload_images


class _Resp:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content = content
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code != 200:
            raise RuntimeError(f"http {self.status_code}")


def _fake_get(url: str, timeout: int = 60) -> _Resp:
    # Return bytes whose tail matches the extension for naming checks.
    ext = url.rsplit(".", 1)[-1].split("?")[0]
    return _Resp(b"%s-data" % ext.encode())


def test_download_named_by_type_and_ext(tmp_path: Path) -> None:
    h = TencentHunyuan()
    files = {"GLB": ["https://x/cos/model.glb?t=1"], "OBJ": ["https://x/cos/model.obj"]}
    local = h.download(files, tmp_path, _get=_fake_get)
    assert (tmp_path / "model.glb").exists()
    assert (tmp_path / "model.obj").exists()
    assert set(local) == {"GLB", "OBJ"}
    assert (tmp_path / "model.glb").read_bytes() == b"glb-data"


def test_download_multi_and_unknown_type(tmp_path: Path) -> None:
    h = TencentHunyuan()
    files = {"FOO": ["https://x/a.bin", "https://x/b.bin"]}
    local = h.download(files, tmp_path, _get=_fake_get)
    # unknown type -> names derived from URL basename + index
    assert len(local["FOO"]) == 2
    assert (tmp_path / "a.bin").exists()
    assert (tmp_path / "b_1.bin").exists()


def test_download_skips_empty_url(tmp_path: Path) -> None:
    h = TencentHunyuan()
    local = h.download({"GLB": ["", "https://x/model.glb"]}, tmp_path, _get=_fake_get)
    assert local["GLB"] == [str(tmp_path / "model.glb")]


def test_upload_passes_through_http_url() -> None:
    # A value already starting with http(s) is not uploaded.
    imgs = {"front": "https://bucket.cos.ap-guangzhou.myqcloud.com/01.jpg", "left": "local.jpg"}
    # monkeypatch the helpers to avoid COS client creation for the URL case
    from pipeline.hunyuan import upload as u
    u.upload_image = lambda p, **k: "https://uploaded.local/" + str(p)  # type: ignore
    out = u.upload_images(imgs)
    assert out["front"] == "https://bucket.cos.ap-guangzhou.myqcloud.com/01.jpg"
    assert out["left"].startswith("https://uploaded.local/")


def test_upload_invalid_mode_rejected(tmp_path: Path) -> None:
    p = tmp_path / "x.jpg"
    p.write_bytes(b"x")
    with pytest.raises(ValueError):
        upload_image(p, url_mode="bogus")
