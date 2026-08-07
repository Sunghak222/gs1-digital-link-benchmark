"""Shared HTTP layer.

Every outbound request carries our User-Agent (Open Food Facts blocks
anonymous clients) and image downloads carry a browser-like Accept header
(Next.js-style image proxies return 400 without one — measured on the demo
resolver 2026-07-08).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from scripts.common.config import USER_AGENT

_IMAGE_ACCEPT = "image/avif,image/webp,image/*,*/*;q=0.8"
_RETRIES = 3
_BACKOFF_S = 2.0
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)

#: One pooled client, not one connection per request: downloading thousands of
#: images otherwise pays a TCP+TLS handshake each time. httpx.Client is thread safe.
_client = httpx.Client(timeout=30, follow_redirects=True)


def _request(url: str, *, params: dict[str, Any] | None = None, accept: str | None = None) -> httpx.Response:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    last_exc: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            resp = _client.get(url, params=params, headers=headers)
            if resp.status_code in _RETRYABLE_STATUS:
                raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code is not None and code not in _RETRYABLE_STATUS:
                # Permanent failures (404/403/410) give up at once — retrying and
                # sleeping on them only burns wall-clock.
                raise RuntimeError(f"GET {url} -> {code}") from exc
            last_exc = exc
        except httpx.TransportError as exc:
            last_exc = exc
        if attempt < _RETRIES - 1:                       # never sleep after the last try
            time.sleep(_BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {_RETRIES} attempts") from last_exc


def get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    return _request(url, params=params, accept="application/json").json()


def get_bytes(url: str, *, image: bool = False) -> bytes:
    return _request(url, accept=_IMAGE_ACCEPT if image else None).content


def stream_to_file(url: str, dest: Path, *, resume: bool = True) -> int:
    """Stream a large file to disk without holding it in memory. Returns bytes written.

    Bulk dumps run from hundreds of MB to 1.8 GB, so get_bytes() would load the
    whole thing. Chunks land in a `.part` file that is only renamed once complete,
    so an interrupted download never masquerades as a finished one.

    With resume=True an existing `.part` continues via a Range request rather than
    starting the 1.8 GB over.
    """
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if (resume and part.exists()) else 0

    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"

    with _client.stream("GET", url, headers=headers) as resp:
        # 206 means the server honoured the range; 200 means it is starting over,
        # so whatever we already had is worthless.
        if have and resp.status_code == 200:
            have = 0
        elif resp.status_code not in (200, 206):
            resp.read()
            raise RuntimeError(f"GET {url} -> {resp.status_code}")
        with part.open("ab" if have else "wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):   # 1 MB at a time
                fh.write(chunk)

    size = part.stat().st_size
    part.replace(dest)
    return size


def cached_json(cache_path: Path, url: str, *, params: dict[str, Any] | None = None) -> Any:
    """Fetch JSON through a whole-file disk cache (also our reproducibility record)."""
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    data = get_json(url, params=params)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data
