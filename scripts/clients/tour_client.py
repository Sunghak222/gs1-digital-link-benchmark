"""Korea TourAPI (KorService2) wrapper — endpoints verified live 2026-07-08.

Every call is cached under data/raw/tour/ (daily quota is 1,000 requests, and
the cache is our reproducibility record). Responses use the standard
data.go.kr envelope; resultCode "0000" is success.
"""
from __future__ import annotations

from typing import Any

from scripts.common.config import RAW_TOUR_DIR, TOUR_API_KEY
from scripts.common.fetch import cached_json

_BASE = "https://apis.data.go.kr/B551011/KorService2"


class TourApiError(RuntimeError):
    pass


def _call(endpoint: str, cache_key: str, **params: Any) -> list[dict[str, Any]]:
    if not TOUR_API_KEY:
        raise TourApiError("TOUR_API_KEY is not set (checked benchmark/.env and kg_neo4j/.env)")
    merged = {
        "serviceKey": TOUR_API_KEY,
        "MobileOS": "ETC",
        "MobileApp": "gs1bench",
        "_type": "json",
        "numOfRows": 100,
        "pageNo": 1,
        **params,
    }
    cache = RAW_TOUR_DIR / f"{endpoint}-{cache_key}.json"
    data = cached_json(cache, f"{_BASE}/{endpoint}", params=merged)
    header = data.get("response", {}).get("header", {})
    if header.get("resultCode") != "0000":
        cache.unlink(missing_ok=True)  # do not cache errors
        raise TourApiError(f"{endpoint} failed: {header.get('resultCode')} {header.get('resultMsg')}")
    items = data["response"]["body"].get("items") or {}
    item = items.get("item", []) if isinstance(items, dict) else []
    return item if isinstance(item, list) else [item]


def search_keyword(keyword: str, *, content_type_id: int | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"keyword": keyword}
    if content_type_id:
        params["contentTypeId"] = content_type_id
    slug = f"{keyword}-{content_type_id or 'all'}"
    return _call("searchKeyword2", slug, **params)


def detail_common(content_id: str) -> dict[str, Any]:
    items = _call("detailCommon2", content_id, contentId=content_id)
    return items[0] if items else {}


def detail_intro(content_id: str, content_type_id: str) -> dict[str, Any]:
    items = _call("detailIntro2", f"{content_id}-{content_type_id}", contentId=content_id, contentTypeId=content_type_id)
    return items[0] if items else {}


def detail_images(content_id: str) -> list[dict[str, Any]]:
    return _call("detailImage2", f"{content_id}-img", contentId=content_id)


def area_based_list(area_code: int, *, content_type_id: int = 12, rows: int = 50) -> list[dict[str, Any]]:
    """Attractions in one area (6 = Busan). arrange=Q sorts by modified date, image-having entries first."""
    return _call(
        "areaBasedList2", f"area{area_code}-{content_type_id}",
        areaCode=area_code, contentTypeId=content_type_id, numOfRows=rows, arrange="Q",
    )
