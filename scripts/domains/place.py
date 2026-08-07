"""Place domain — Korea Tourism Organization TourAPI.

Places are the one domain whose identifiers are issued by us: TourAPI has no
GS1 code, so each place gets a GLN under the 952 demo prefix, numbered by its
position in selection.yaml. That makes selection order part of an entity's
identity — entries are append-only and retired numbers are never reused.

Structured TourAPI fields become facts by rule. The prose overview is the one
place an LLM is used, and it may only point at text: it must return a substring
that exists verbatim, and anything we cannot re-locate is dropped.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from scripts.clients import tour_client
from scripts.common.config import WORK_DIR
from scripts.common.facts import fact_adder, is_junk, make_fact, strip_br
from scripts.common.identifiers import make_demo_gln
from scripts.common.llm import llm_json
from scripts.domains.base import Domain

MINE_MODEL = "gpt-4o-mini"
REQUIRED = frozenset({"pip", "locationInfo"})
LICENSE = "공공누리 Type1 (Korea TourAPI)"
MEDIA_LICENSE = "공공누리 제1유형 (한국관광공사 TourAPI)"
#: Pre-bulk entities with no Type1 gallery keep their representative photo, which
#: carries no copyright code. Flagged so the public-release pass can re-decide.
FALLBACK_LICENSE = "UNVERIFIED (대표사진 — 저작권 코드 미제공, 공개 전 재결정)"

_PLACE_ROWS: list[dict[str, Any]] | None = None


def _place_rows() -> list[dict[str, Any]]:
    """candidates-place.jsonl, parsed once. Re-parsing it per entity was O(n^2)."""
    global _PLACE_ROWS
    if _PLACE_ROWS is None:
        cand = WORK_DIR / "candidates-place.jsonl"
        _PLACE_ROWS = [json.loads(l) for l in cand.read_text(encoding="utf-8").splitlines() if l]
    return _PLACE_ROWS


def cached_base(content_id: str) -> list[dict[str, Any]]:
    """The list row for this contentid, from candidate output — zero API calls.

    Bulk-harvested candidates carry the whole row under "base". Legacy candidates
    predate that field and fall back to the API.
    """
    rows = _place_rows()
    names = {r["id"]: r["name"] for r in rows}
    for r in rows:
        if r["id"] == content_id and r.get("base"):
            return [r["base"]]
    kw = names.get(content_id)
    if kw:
        hits = tour_client.search_keyword(kw, content_type_id=12)
        exact = [h for h in hits if h.get("contentid") == content_id]
        if exact:
            return exact
    for item in tour_client.area_based_list(6):
        if item.get("contentid") == content_id:
            return [item]
    return []


def identify(_content_id: str, ordinal: int) -> str:
    return f"414/{make_demo_gln(ordinal)}"


def extract(content_id: str, entity: str) -> tuple[str, list[dict[str, Any]]]:
    gln = entity.split("/")[1]
    hits = cached_base(content_id)
    base = hits[0] if hits else {}
    common = tour_client.detail_common(content_id)
    intro = tour_client.detail_intro(content_id, base.get("contenttypeid", "12"))
    name = base.get("title") or common.get("title") or content_id
    facts: list[dict[str, Any]] = []
    add = fact_adder(facts, entity, "place", gln, "tour")

    add("place_name", name, "pip", "title")
    overview = (common.get("overview") or "").strip()
    add("overview", overview, "pip", "overview")

    addr = " ".join(x for x in (base.get("addr1"), base.get("addr2")) if x)
    add("address", addr, "locationInfo", "addr1")
    if base.get("mapx") and base.get("mapy"):
        add("coordinates", {"lon": float(base["mapx"]), "lat": float(base["mapy"])},
            "locationInfo", "mapx/mapy")
    region = (base.get("addr1") or "").split()[:2]
    if region:
        f = make_fact(entity, "place", gln, "located_in_region", " ".join(region),
                      "locationInfo", "tour", "addr1")
        f["relation"] = True                      # same-region multi-hop material
        facts.append(f)

    add("opening_hours", strip_br(intro.get("usetime")), "openingHoursInfo", "usetime")
    add("closed_days", strip_br(intro.get("restdate")), "openingHoursInfo", "restdate")
    add("info_center", strip_br(intro.get("infocenter")), "support", "infocenter")
    add("parking", strip_br(intro.get("parking")), "support", "parking")

    if overview:
        facts.extend(mine_overview(entity, gln, name, overview))
    return name, facts


def mine_overview(entity: str, gln: str, name: str, overview: str) -> list[dict[str, Any]]:
    """Pull up to 4 verifiable facts out of the prose, each anchored to a span.

    The model must echo value_literal verbatim; anything str.find() cannot
    re-locate is discarded, so the LLM can point at values but never invent them.
    """
    prompt = (
        f"다음은 한국 관광지 '{name}'의 공식 소개문이다.\n---\n{overview}\n---\n"
        "질의응답 벤치마크의 골드 팩트로 쓸 구체적 사실을 최대 4개 추출하라. "
        "수치·연도·길이·고유명사 등 검증 가능한 것만. 각 항목: "
        'predicate(영문 snake_case), value(정규화된 값), value_literal(소개문에 문자 그대로 존재하는 부분 문자열). '
        'JSON 형식: {"facts": [{"predicate": ..., "value": ..., "value_literal": ...}]}'
    )
    key = hashlib.sha256(f"mine|{MINE_MODEL}|{overview}".encode()).hexdigest()[:24]
    data = llm_json(key, [{"role": "user", "content": prompt}], MINE_MODEL)

    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for item in data.get("facts", []):
        literal = str(item.get("value_literal") or "")
        start = overview.find(literal) if literal else -1
        pred = str(item.get("predicate") or "")
        if start < 0 or not pred:
            continue
        value = item.get("value")
        # The model sometimes answers "not_specified" for a slot it invented.
        if isinstance(value, str) and (
                is_junk(value) or value.strip().lower() in ("not_specified", "not specified")):
            continue
        if pred in used:
            pred = f"{pred}_{sum(p.startswith(pred) for p in used) + 1}"
        used.add(pred)
        out.append(make_fact(
            entity, "place", gln, pred, value, "pip", "tour", "overview",
            passage=overview, value_span=[start, start + len(literal)],
        ))
    return out


def media(content_id: str) -> list[tuple[str, str, str]]:
    """Gallery photos must be explicitly KOGL Type 1; untagged photos do not pass."""
    urls: list[tuple[str, str]] = []
    for item in tour_client.detail_images(content_id):
        if item.get("cpyrhtDivCd") != "Type1":
            continue
        u = item.get("originimgurl")
        if u and u not in [x[0] for x in urls]:
            urls.append((u, MEDIA_LICENSE))
    if not urls:
        base = (cached_base(content_id) or [{}])[0]
        if base.get("firstimage"):
            urls.append((base["firstimage"], FALLBACK_LICENSE))
    return [(f"img-{i}.jpg", u, lic) for i, (u, lic) in enumerate(urls[:4], start=1)]


DOMAIN = Domain(
    name="place",
    ai_prefix="414",
    page_language="ko",
    license=LICENSE,
    required_linktypes=REQUIRED,
    identify=identify,
    extract=extract,
    media=media,
)
