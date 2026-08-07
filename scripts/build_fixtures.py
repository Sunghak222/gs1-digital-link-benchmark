"""S4~S7 — build fixture corpus from facts.jsonl: gate -> pages -> media -> linkset -> manifest.

    python -m scripts.build_fixtures                        # original corpus (entities/)
    python -m scripts.build_fixtures --overrides work/counterfactual-overrides.jsonl   # -> counterfactual/

Pages are rendered from facts only (unique placement holds by construction).
Templates T1~T4 rotate deterministically per entity (hash of aiPath); the T4
variant of *place* pages is written in EUC-KR to exercise the pipeline's
decoding fallback. The --overrides run re-renders everything with overridden
fact values (passage facts get their value_literal spliced in) so the
counterfactual corpus differs from the original in exactly those values.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from jsonschema import validate

from scripts.common.config import BENCH_ROOT, DATA_DIR, SCHEMA_DIR, WORK_DIR
from scripts.common.fetch import get_bytes
from scripts.clients import off_client, tour_client
from scripts.extract_facts import _cached_place_base

FACTS_PATH = BENCH_ROOT / "facts.jsonl"
ENTITY_MAP = WORK_DIR / "entity-map.json"
LINKSET_SCHEMA = json.loads((SCHEMA_DIR / "linkset.schema.json").read_text(encoding="utf-8"))

TEMPLATES = ["t1_plain", "t2_table", "t3_jsonld", "t4_noisy"]
ENV = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"))

REL_BASE = "https://ref.gs1.org/voc/"
ANCHOR_BASE = "https://id.oliot.org"

REQUIRED = {  # design doc 01 §S4 (relatedImage is checked against downloaded media)
    "food": {"pip", "nutritionalInfo"},
    "place": {"pip", "locationInfo"},
    "pharma": {"pip", "instructions"},  # 효능·복용법은 전 라벨 100% (docs/11 §3)
}
OPTIONAL_MIN = 3

#: 증분 빌드 캐시 무효화 키 (2026-08-05). 페이지 생성 규칙(템플릿·라벨·page_context·
#: masterdata_jsonld·linkset 구성 등 산출물에 영향 주는 코드)을 바꾸면 반드시 올릴 것.
BUILD_RULES_VERSION = "2026-08-05.1"

LINKTYPE_LABELS = {  # (ko, en)
    "pip": ("소개", "Product Information"),
    "nutritionalInfo": ("영양 정보", "Nutrition Facts"),
    "allergenInfo": ("알레르겐 정보", "Allergen Information"),
    "ingredientsInfo": ("원재료·성분", "Ingredients"),
    "hasRetailers": ("판매처", "Where to Buy"),
    "certificationInfo": ("인증·라벨", "Labels & Certifications"),
    "locationInfo": ("위치·원산지", "Origin & Location"),
    "consumerHandlingStorageInfo": ("보관·취급", "Storage & Handling"),
    "sustainabilityInfo": ("포장·재활용", "Packaging & Recycling"),
    "openingHoursInfo": ("운영 시간", "Opening Hours"),
    "support": ("방문자 지원", "Support & Contact"),  # en은 pharma 문의처용 (기존 en support 페이지 없음)
    "masterData": ("마스터 데이터", "Master Data"),
    "relatedImage": ("사진", "Photos"),
    "instructions": ("복용법", "Directions"),
    "safetyInfo": ("경고·주의", "Warnings & Safety"),
}

UI_LABELS = {
    "ko": {"details": "상세 정보", "nutrition": "영양 성분표", "serving": "1회 제공량",
           "nutrient": "영양소", "per100": "100g당", "per_serving": "1회 제공량당",
           "item": "항목", "value": "값",
           "nav_home": "홈", "nav_services": "서비스", "nav_events": "이벤트", "nav_contact": "문의",
           "banner": "지금 회원가입하고 소식을 받아보세요!", "promo_title": "이달의 소식",
           "promo_body": "다양한 소식과 혜택이 준비되어 있습니다.", "quick_links": "바로가기",
           "footer": "본 페이지의 정보는 참고용입니다.", "nav_privacy": "개인정보처리방침", "nav_terms": "이용약관"},
    "en": {"details": "Details", "nutrition": "Nutrition Facts", "serving": "Serving size",
           "nutrient": "Nutrient", "per100": "Per 100 g", "per_serving": "Per serving",
           "item": "Item", "value": "Value",
           "nav_home": "Home", "nav_services": "Services", "nav_events": "Events", "nav_contact": "Contact",
           "banner": "Sign up today for news and offers!", "promo_title": "This month",
           "promo_body": "News and offers are waiting for you.", "quick_links": "Quick links",
           "footer": "Information on this page is provided for reference.",
           "nav_privacy": "Privacy", "nav_terms": "Terms"},
}

BOILERPLATE = {  # no-claim filler sections (must contain no verifiable facts)
    "ko": [("방문 전 안내", "최신 정보는 공식 채널에서 다시 한번 확인해 주세요. 현장 사정에 따라 내용이 변경될 수 있습니다."),
           ("문의", "궁금한 점은 안내 페이지의 연락처를 통해 문의해 주세요.")],
    "en": [("Before you buy", "Always check the product label for the most up-to-date details."),
           ("Questions", "Please reach out through the contact options on the support page.")],
}


# ---------------------------------------------------------------------------
# overrides (S8 counterfactual)
# ---------------------------------------------------------------------------


def apply_overrides(facts: list[dict[str, Any]], overrides_path: Path) -> None:
    """Mutate facts in place. Passage facts splice value_literal at their span —
    facts sharing the same passage get their spans shifted consistently."""
    overrides = [json.loads(l) for l in overrides_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {f["fact_id"]: f for f in facts}
    splices: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)

    for ov in overrides:
        f = by_id.get(ov["fact_id"])
        if f is None:
            sys.exit(f"override targets unknown fact_id: {ov['fact_id']}")
        if "value" in ov:
            f["value"] = ov["value"]
        if ov.get("value_literal") and f["source"].get("value_span"):
            splices[f["source"]["passage"]].append((f, str(ov["value_literal"])))

    for passage, jobs in splices.items():
        group = [f for f in facts if f["source"].get("passage") == passage]
        jobs.sort(key=lambda j: j[0]["source"]["value_span"][0], reverse=True)
        new_passage = passage
        for target, literal in jobs:
            s, e = target["source"]["value_span"]
            new_passage = new_passage[:s] + literal + new_passage[e:]
            delta = len(literal) - (e - s)
            for f in group:
                fs, fe = f["source"]["value_span"]
                if f is target:
                    f["source"]["value_span"] = [s, s + len(literal)]
                elif fs > s:
                    f["source"]["value_span"] = [fs + delta, fe + delta]
        for f in group:
            f["source"]["passage"] = new_passage
        # facts that carry the prose as their VALUE (e.g. the place `overview` fact)
        # must follow the splice too, or the old text re-enters the page beside the new one
        for f in facts:
            if f["value"] == passage:
                f["value"] = new_passage


# ---------------------------------------------------------------------------
# page content model
# ---------------------------------------------------------------------------


def _render_value(v: Any) -> str:
    if isinstance(v, dict):
        if {"amount", "unit"} <= v.keys():
            return f"{v['amount']} {v['unit']}"
        if {"lat", "lon"} <= v.keys():
            return f"{v['lat']}, {v['lon']}"
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def _pretty(pred: str) -> str:
    return pred.replace("_", " ")


def page_context(entity: str, name: str, cls: str, linktype: str,
                 page_facts: list[dict[str, Any]], template: str) -> dict[str, Any]:
    lang = "ko" if cls == "place" else "en"  # food·pharma는 영어 문서
    passages: list[str] = []
    scalars: list[tuple[str, str]] = []
    lists: list[tuple[str, list[str]]] = []
    nutrition_by_name: dict[str, dict[str, str]] = defaultdict(dict)
    serving_size = None
    jsonld_fields: dict[str, Any] = {}

    for f in page_facts:
        src = f["source"]
        if src.get("passage"):
            if src["passage"] not in passages:
                passages.append(src["passage"])
            continue  # the value lives inside the passage; do not render it separately
        pred, val = f["predicate"], f["value"]
        if linktype == "nutritionalInfo" and (pred.endswith("_100g") or pred.endswith("_serving")):
            base, basis = pred.rsplit("_", 1)
            nutrition_by_name[_pretty(base)][basis] = _render_value(val)
            continue
        if pred == "serving_size":
            serving_size = str(val)
            continue
        if pred == "overview":
            if val not in passages:
                passages.append(str(val))
            continue
        jsonld_fields[pred] = val
        if isinstance(val, list):
            lists.append((_pretty(pred), [str(x) for x in val]))
        else:
            scalars.append((_pretty(pred), _render_value(val)))

    # absence stated positively, as page copy only (NOT a fact — OFF silence is not
    # a negative claim; wording says "declared" for that reason, 2026-07-27)
    if cls == "food" and linktype == "allergenInfo" \
            and not any(f["predicate"] == "traces" for f in page_facts):
        passages.append("No ‘may contain’ warnings declared for this product.")

    nutrition = None
    if nutrition_by_name:
        nutrition = {
            "serving_size": serving_size,
            "rows": [(n, cells.get("100g", "—"), cells.get("serving", "—"))
                     for n, cells in nutrition_by_name.items()],
        }

    jsonld = None
    if template == "t3_jsonld" and jsonld_fields:
        jsonld = json.dumps(
            {"@context": "https://schema.org/", "@type": "Place" if cls == "place" else "Product",
             "name": name, **{k: v for k, v in jsonld_fields.items()}},
            ensure_ascii=False)

    lt_label = LINKTYPE_LABELS.get(linktype, (linktype, linktype))[0 if lang == "ko" else 1]
    return {
        "lang": lang, "charset": "utf-8",
        "title": f"{name} - {lt_label}",  # hyphen, not em dash — must survive EUC-KR (T4 place pages)
        "labels": UI_LABELS[lang],
        "passages": passages, "scalars": scalars, "lists": lists,
        "nutrition": nutrition, "jsonld": jsonld,
        "boilerplate": BOILERPLATE[lang],
    }


def masterdata_jsonld(entity: str, name: str, cls: str) -> str:
    """Identity-only JSON-LD (no facts from other pages — unique placement holds)."""
    anchor = f"{ANCHOR_BASE}/{entity}"
    if cls != "place":  # food·pharma 모두 실물 GTIN 제품
        doc = {"@context": {"gs1": "https://gs1.org/voc/", "schema": "https://schema.org/"},
               "@id": anchor, "@type": ["gs1:Product", "schema:Product"],
               "schema:name": name, "gs1:gtin": entity.split("/")[1]}
    else:
        doc = {"@context": {"gs1": "https://gs1.org/voc/", "schema": "https://schema.org/"},
               "@id": anchor, "@type": "schema:Place",
               "schema:name": name, "schema:identifier": entity.split("/")[1]}
    return json.dumps(doc, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# media (S6)
# ---------------------------------------------------------------------------


def collect_media(cls: str, source_id: str) -> list[tuple[str, str, str]]:
    """[(filename, url, license)] — capped at 4.

    Place photos (2026-07-21 policy, public-benchmark quality): gallery must be
    EXPLICITLY KOGL Type1 — untagged photos no longer pass. The representative
    photo (firstimage, no copyright tag available) survives only as a legacy
    fallback for pre-bulk entities with zero Type1 gallery (9 places as of
    2026-07-21) and is stamped license-unverified for the public-release pass.
    New entities never take the fallback: the bulk selection gate requires
    Type1 >= 3 up front.
    """
    if cls == "food":
        p = off_client.get_product(source_id)
        pairs = [("front.jpg", p.get("image_front_url")),
                 ("nutrition-label.jpg", p.get("image_nutrition_url")),
                 ("ingredients.jpg", p.get("image_ingredients_url"))]
        return [(n, u, "CC-BY-SA (Open Food Facts contributors)") for n, u in pairs if u][:4]
    if cls == "pharma":
        # 스냅샷에 select 단계가 DailyMed 이미지 URL을 동봉해둠 (pharma_harvest.run_select)
        snap = json.loads((DATA_DIR / "raw" / "openfda" / f"label-{source_id}.json")
                          .read_text(encoding="utf-8"))
        return [(f"label-{i}.jpg", m["url"], "Public domain (US FDA / NLM DailyMed)")
                for i, m in enumerate(snap.get("media") or [], start=1)][:4]
    urls: list[tuple[str, str]] = []
    for item in tour_client.detail_images(source_id):
        if item.get("cpyrhtDivCd") != "Type1":
            continue
        u = item.get("originimgurl")
        if u and u not in [x[0] for x in urls]:
            urls.append((u, "공공누리 제1유형 (한국관광공사 TourAPI)"))
    if not urls:
        base = (_cached_place_base(source_id) or [{}])[0]
        if base.get("firstimage"):
            urls.append((base["firstimage"], "UNVERIFIED (대표사진 — 저작권 코드 미제공, 공개 전 재결정)"))
    return [(f"img-{i}.jpg", u, lic) for i, (u, lic) in enumerate(urls[:4], start=1)]


def _mime(filename: str) -> str:
    return "image/png" if filename.endswith(".png") else "image/jpeg"


def _to_jpeg(data: bytes) -> bytes:
    """TourAPI serves some originals as PNG/BMP regardless of the URL; the linkset
    declares image/jpeg and VLM APIs reject BMP, so transcode anything non-JPEG."""
    if data[:3] == b"\xff\xd8\xff":
        return data
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build(out_root: Path, overrides: Path | None, full: bool = False) -> None:
    facts = [json.loads(l) for l in FACTS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    entity_map = json.loads(ENTITY_MAP.read_text(encoding="utf-8"))
    if overrides:
        apply_overrides(facts, overrides)

    by_entity: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for f in facts:
        by_entity[f["entity"]][f["linktype"]].append(f)

    manifest: dict[str, Any] = {
        "generated_from": "facts.jsonl", "overrides": str(overrides) if overrides else None,
        "entities": {}, "gate": {}, "placement": {},
    }
    gate_failures = []
    build_errors = []  # 엔티티 1개의 예외가 전체 빌드를 죽이지 않게 격리(2026-08-04, 몰아서 빌드 전제)

    # --- 증분 빌드 (2026-08-05): 엔티티별 입력 지문(팩트+미디어+메타+규칙 버전)이 이전
    # 빌드와 같으면 렌더링을 건너뛰고 이전 manifest 항목을 재사용한다. 페이지 생성 규칙
    # (템플릿·라벨·컨텍스트 로직)을 바꿀 때는 반드시 BUILD_RULES_VERSION을 올릴 것 —
    # 안 올리면 기존 엔티티에 새 규칙이 반영되지 않는다. --full로 강제 전체 재생성 가능.
    prev_entities: dict[str, Any] = {}
    prev_gate: dict[str, Any] = {}
    if not full and (out_root / "manifest.json").exists():
        try:
            _prev = json.loads((out_root / "manifest.json").read_text(encoding="utf-8"))
            prev_entities = _prev.get("entities") or {}
            prev_gate = _prev.get("gate") or {}
        except Exception:  # noqa: BLE001 — 캐시 불러오기 실패는 전체 재생성으로 자연 강등
            pass
    reused = 0

    def _fingerprint(meta: dict, lt_facts: dict, media: list) -> str:
        basis = json.dumps({"rules": BUILD_RULES_VERSION, "meta": meta,
                            "facts": lt_facts, "media": media},
                           ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(basis.encode()).hexdigest()

    # --- 미디어 프리페치 (2026-08-04): 이미지 다운로드가 빌드 시간의 ~90%(순차 개당 ~4초).
    # 게이트를 통과할 엔티티의 미보유 이미지만 동시 8개로 먼저 받아둔다. 본 루프는 기존
    # 순차 경로 그대로(파일이 있으면 지나감) — 프리페치 실패분은 본 루프가 재시도 후
    # BUILD FAIL로 보고하므로 여기서는 조용히 넘어간다. 게이트 판정식은 본 루프와 동일해야 함.
    to_fetch: list[tuple[Path, str]] = []
    for entity, meta in entity_map.items():
        lts = set(by_entity[entity])
        media = collect_media(meta["class"], meta["source_id"])
        if (REQUIRED[meta["class"]] - lts) or not media \
                or len(lts - REQUIRED[meta["class"]]) + 1 < OPTIONAL_MIN:
            continue  # 게이트 탈락 예정 — 이미지·폴더 잔재를 만들지 않는다
        slug = entity.replace("/", "-")
        for fname, url, _lic in media:
            target = BENCH_ROOT / "entities" / slug / "media" / fname
            if not target.exists():
                to_fetch.append((target, url))
    if to_fetch:
        print(f"media prefetch: {len(to_fetch)}건 (동시 8)", flush=True)

        def _prefetch(item: tuple[Path, str]) -> None:
            target, url = item
            try:
                data = _to_jpeg(get_bytes(url, image=True))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except Exception:  # noqa: BLE001
                pass

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_prefetch, to_fetch))

    for entity, meta in entity_map.items():
        cls, name, source_id = meta["class"], meta["name"], meta["source_id"]
        slug = entity.replace("/", "-")
        ent_dir = out_root / "entities" / slug
        pages_dir, media_dir = ent_dir / "pages", ent_dir / "media"

        try:
            linktypes = dict(by_entity[entity])
            media = collect_media(cls, source_id)

            # --- 증분: 입력이 안 바뀐 엔티티는 이전 결과 재사용 (렌더·다운로드 생략)
            fp = _fingerprint(meta, linktypes, media)
            prev = prev_entities.get(entity)
            if (prev and prev.get("fingerprint") == fp
                    and (ent_dir / "linkset.json").exists()
                    and all((media_dir / m["file"]).exists() for m in prev.get("media", []))):
                manifest["gate"][entity] = prev_gate.get(entity, {"ok": True})
                manifest["entities"][entity] = prev
                for lt, page_facts in linktypes.items():
                    for f in page_facts:
                        manifest["placement"][f["fact_id"]] = f"entities/{slug}/pages/{lt}.html"
                reused += 1
                continue

            # --- gate (rule part; design 01 §S4)
            missing = REQUIRED[cls] - set(linktypes)
            if not media:
                missing.add("relatedImage")
            optional_n = len(set(linktypes) - REQUIRED[cls]) + 1  # +1: masterData page below
            gate = {"missing_required": sorted(missing), "optional_count": optional_n,
                    "ok": not missing and optional_n >= OPTIONAL_MIN}
            manifest["gate"][entity] = gate
            if not gate["ok"]:
                gate_failures.append((entity, name, gate))
                continue

            # 폴더는 게이트 통과 후에만 생성 — 탈락 엔티티가 잔재(빈 폴더)를 남기지 않게(2026-08-04)
            pages_dir.mkdir(parents=True, exist_ok=True)
            media_dir.mkdir(parents=True, exist_ok=True)

            template = TEMPLATES[int(hashlib.sha256(entity.encode()).hexdigest(), 16) % len(TEMPLATES)]

            # --- pages (S5)
            for lt, page_facts in linktypes.items():
                ctx = page_context(entity, name, cls, lt, page_facts, template)
                encoding = "cp949" if template == "t4_noisy" and cls == "place" else "utf-8"
                if encoding == "cp949":
                    ctx["charset"] = "euc-kr"
                html = ENV.get_template(f"{template}.html.j2").render(**ctx)
                (pages_dir / f"{lt}.html").write_bytes(html.encode(encoding, errors="replace"))
                for f in page_facts:
                    manifest["placement"][f["fact_id"]] = f"entities/{slug}/pages/{lt}.html"

            md_lang = "ko" if cls == "place" else "en"
            md_ctx = {
                "lang": md_lang, "charset": "utf-8",
                "title": f"{name} - Master Data", "labels": UI_LABELS[md_lang],
                "passages": [], "scalars": [], "lists": [], "nutrition": None,
                "jsonld": masterdata_jsonld(entity, name, cls), "boilerplate": [],
            }
            (pages_dir / "masterData.html").write_text(
                ENV.get_template("t3_jsonld.html.j2").render(**md_ctx), encoding="utf-8")

            # --- media (S6): download once into the ORIGINAL tree, copy for overrides builds
            media_entries = []
            for fname, url, lic in media:
                target = (BENCH_ROOT / "entities" / slug / "media" / fname)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(_to_jpeg(get_bytes(url, image=True)))
                else:
                    data = target.read_bytes()
                    fixed = _to_jpeg(data)
                    if fixed != data:
                        target.write_bytes(fixed)
                if target != media_dir / fname:
                    shutil.copyfile(target, media_dir / fname)
                media_entries.append({"file": fname, "source_url": url, "type": _mime(fname), "license": lic})

            # --- linkset (S7)
            anchor = f"{ANCHOR_BASE}/{entity}"
            ls: dict[str, Any] = {"anchor": anchor}
            ls[f"{REL_BASE}defaultLink"] = [{"href": "pages/pip.html", "type": "text/html"}]
            for lt in [*linktypes, "masterData"]:
                ko, en = LINKTYPE_LABELS.get(lt, (lt, lt))
                ls[f"{REL_BASE}{lt}"] = [{
                    "href": f"pages/{lt}.html", "type": "text/html",
                    "title": ko if cls == "place" else en,
                    "hreflang": ["ko" if cls == "place" else "en"],
                }]
            if media_entries:
                ls[f"{REL_BASE}relatedImage"] = [
                    {"href": f"media/{m['file']}", "type": m["type"],
                     "title": ("사진" if cls == "place" else "Photo") + f" {i}",
                     "hreflang": ["ko" if cls == "place" else "en"]}
                    for i, m in enumerate(media_entries, start=1)
                ]
            linkset = {"linkset": [ls]}
            validate(linkset, LINKSET_SCHEMA)
            (ent_dir / "linkset.json").write_text(json.dumps(linkset, ensure_ascii=False, indent=1), encoding="utf-8")

            manifest["entities"][entity] = {
                "name": name, "class": cls, "source_id": source_id, "template": template,
                "pages": sorted([*linktypes, "masterData"]), "media": media_entries,
                "license": {"food": "ODbL/CC-BY-SA (Open Food Facts)",
                            "pharma": "Public domain (US FDA openFDA / NLM DailyMed)",
                            }.get(cls, "공공누리 Type1 (Korea TourAPI)"),
                "fingerprint": fp,
            }
            print(f"  {entity} [{template}] pages={len(linktypes) + 1} media={len(media_entries)}")
        except Exception as e:  # noqa: BLE001 — 실패 엔티티는 흔적 없이 제거하고 끝에 보고
            build_errors.append((entity, name, repr(e)))
            manifest["gate"].pop(entity, None)
            manifest["entities"].pop(entity, None)
            for lt_facts in by_entity[entity].values():  # (감사 F9) 전체 placement 스캔 대신 해당 키만
                for f in lt_facts:
                    manifest["placement"].pop(f["fact_id"], None)
            shutil.rmtree(ent_dir, ignore_errors=True)

    (out_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"증분: 재사용 {reused} / 렌더링 {len(manifest['entities']) - reused}", flush=True)
    for entity, name, gate in gate_failures:
        print(f"GATE FAIL {entity} {name}: {gate}", file=sys.stderr)
    for entity, name, err in build_errors:
        print(f"BUILD FAIL {entity} {name}: {err}", file=sys.stderr)
    if gate_failures or build_errors:
        sys.exit(1)
    print(f"manifest -> {out_root / 'manifest.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overrides", type=Path, default=None,
                        help="cf overrides jsonl; output goes to counterfactual/ instead of the repo root")
    parser.add_argument("--full", action="store_true",
                        help="증분 캐시 무시하고 전체 재생성 (규칙 변경 의심 시 안전판)")
    args = parser.parse_args()
    out_root = (BENCH_ROOT / "counterfactual") if args.overrides else BENCH_ROOT
    build(out_root, args.overrides, full=args.full)


if __name__ == "__main__":
    main()
