"""Helpers for downloading source data, with simple on-disk caching."""

import io
import logging
import shutil
import zipfile
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(60.0, read=300.0)
CHUNK = 1 << 20


def download(url: str, dest: Path) -> Path:
    """Download url to dest unless dest already exists."""
    if dest.exists():
        log.info("cached: %s", dest.name)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s -> %s", url, dest.name)
    with httpx.stream("GET", url, timeout=TIMEOUT, follow_redirects=True) as res:
        res.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in res.iter_bytes(CHUNK):
                f.write(chunk)
    tmp.rename(dest)
    return dest


class HttpRangeFile(io.RawIOBase):
    """Read-only, seekable file over HTTP using range requests.

    Lets zipfile read the central directory and single members of a huge remote
    zip without downloading the whole archive.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.pos = 0
        self.client = httpx.Client(timeout=TIMEOUT, follow_redirects=True)
        self.size = int(self.client.head(url).headers["content-length"])

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        n = min(len(buffer), self.size - self.pos)
        if n <= 0:
            return 0
        headers = {"Range": f"bytes={self.pos}-{self.pos + n - 1}"}
        res = self.client.get(self.url, headers=headers)
        res.raise_for_status()
        data = res.content
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)

    def close(self) -> None:
        self.client.close()
        super().close()


def extract_remote_zip_member(url: str, member: str, dest: Path) -> Path:
    """Extract a single member from a remote zip using HTTP range requests."""
    if dest.exists():
        log.info("cached: %s", dest.name)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("extracting %s from %s", member, url)
    raw = HttpRangeFile(url)
    with (
        zipfile.ZipFile(io.BufferedReader(raw, buffer_size=16 * CHUNK)) as zf,
        zf.open(member) as src,
        tmp.open("wb") as out,
    ):
        shutil.copyfileobj(src, out, 16 * CHUNK)
    tmp.rename(dest)
    return dest


def download_and_unzip(url: str, zip_dest: Path, out_dir: Path) -> Path:
    """Download a zip and extract it into out_dir (once)."""
    done_marker = out_dir / ".extracted"
    if done_marker.exists():
        log.info("cached: %s", out_dir.name)
        return out_dir
    download(url, zip_dest)
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_dest) as zf:
        zf.extractall(out_dir)
    done_marker.touch()
    zip_dest.unlink()
    return out_dir


def order_geonorge_download(
    api_base: str, metadata_uuid: str, area: dict[str, str], projection: str, fmt: str
) -> str:
    """Place an order in a Geonorge download API and return the file URL.

    Used for NGU datasets, which are only available through the order API.
    """
    body = {
        "orderLines": [
            {
                "metadataUuid": metadata_uuid,
                "areas": [area],
                "projections": [{"code": projection}],
                "formats": [{"name": fmt}],
            }
        ]
    }
    res = httpx.post(f"{api_base}/api/v2/order", json=body, timeout=TIMEOUT)
    res.raise_for_status()
    files = res.json()["files"]
    ready = [f for f in files if f["status"] == "ReadyForDownload"]
    if not ready:
        raise RuntimeError(f"Geonorge order not ready for download: {files}")
    return str(ready[0]["downloadUrl"])
