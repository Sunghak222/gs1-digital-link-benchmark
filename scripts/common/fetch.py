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

#: 커넥션 재사용 (2026-08-05 감사 #3): 요청마다 새 TCP+TLS를 열던 것을 풀로 —
#: 이미지 수천 장 다운로드에서 핸드셰이크 비용 제거. httpx.Client는 스레드 안전.
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
                # 404/403/410 등 영구 실패는 재시도·대기 없이 즉시 (기존: 헛재시도 3회 + 12초 잠)
                raise RuntimeError(f"GET {url} -> {code}") from exc
            last_exc = exc
        except httpx.TransportError as exc:
            last_exc = exc
        if attempt < _RETRIES - 1:                       # 마지막 실패 후에는 잠들지 않는다
            time.sleep(_BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {_RETRIES} attempts") from last_exc


def get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    return _request(url, params=params, accept="application/json").json()


def get_bytes(url: str, *, image: bool = False) -> bytes:
    return _request(url, accept=_IMAGE_ACCEPT if image else None).content


def stream_to_file(url: str, dest: Path, *, resume: bool = True) -> int:
    """큰 파일을 메모리에 올리지 않고 디스크로 흘려보낸다. 받은 바이트 수를 돌려준다.

    벌크 덤프는 파일 하나가 수백 MB~1.8GB라 get_bytes()로 받으면 통째로 메모리에
    올라간다. 여기서는 청크 단위로 `.part` 파일에 쓰고, 다 받은 뒤에만 최종 이름으로
    바꾼다 — 중간에 끊겨도 반쪽짜리 파일이 "완성본"으로 남지 않는다.

    resume=True면 `.part`가 있을 때 Range 헤더로 **이어받는다**. 1.8GB를 처음부터
    다시 받지 않기 위한 것 (docs/14 패턴 B — 안 바뀐 것까지 다시 하지 않는다).
    """
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if (resume and part.exists()) else 0

    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"

    with _client.stream("GET", url, headers=headers) as resp:
        # 206 = 이어받기 승낙, 200 = 서버가 처음부터 다시 주겠다는 뜻 → 받은 만큼 버린다
        if have and resp.status_code == 200:
            have = 0
        elif resp.status_code not in (200, 206):
            resp.read()
            raise RuntimeError(f"GET {url} -> {resp.status_code}")
        with part.open("ab" if have else "wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):   # 1 MB씩
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
