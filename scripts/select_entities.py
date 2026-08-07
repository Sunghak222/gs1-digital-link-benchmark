"""S1 — reverse-selection: score what the sources cover well, pick candidates from the top.

    python -m scripts.select_entities food    -> work/candidates-food.jsonl
    python -m scripts.select_entities place   -> work/candidates-place.jsonl
    python -m scripts.select_entities report  -> work/candidates-report.md
    python -m scripts.select_entities check   -> validate work/selection.yaml

Scoring formulas follow design doc 01 §S1 verbatim. Candidate files carry the
score breakdown so the human picking 10+10 can see *why* something ranked high.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from scripts.common.config import WORK_DIR, ensure_dirs
from scripts.common.identifiers import is_valid
from scripts.clients import off_client, tour_client

CAND_FOOD = WORK_DIR / "candidates-food.jsonl"
CAND_PLACE = WORK_DIR / "candidates-place.jsonl"
REPORT = WORK_DIR / "candidates-report.md"
SELECTION = WORK_DIR / "selection.yaml"

#: Busan keyword seeds — landmarks the area list may rank below its cap.
BUSAN_SEEDS = ["광안대교", "해동용궁사", "감천문화마을"]

#: Nationwide famous-place seeds mixed in with the systematic Busan area list.
NATIONAL_SEEDS = [
    "경복궁", "N서울타워", "불국사", "성산일출봉", "전주한옥마을",
    "수원화성", "설악산 국립공원", "안동하회마을", "남이섬", "보성녹차밭",
]


# ---------------------------------------------------------------------------
# food (Open Food Facts)
# ---------------------------------------------------------------------------


def score_food(p: dict[str, Any]) -> dict[str, int]:
    # count only as-sold nutrient values: "prepared" fields (powders mixed with milk
    # etc.) are not extracted downstream, so they must not earn richness points
    # (Nesquik case, 2026-07-15 — serving_size orphaned with zero nutrient rows).
    nutr = {k: v for k, v in (p.get("nutriments") or {}).items() if "prepared" not in k}
    return {
        "image_nutrition": 2 * bool(p.get("image_nutrition_url")),
        "image_front": 1 * bool(p.get("image_front_url")),
        "image_ingredients": 1 * bool(p.get("image_ingredients_url")),
        "nutriments_10plus": 2 * (len(nutr) >= 10),
        "allergens": 1 * bool(p.get("allergens_tags")),
        "ingredients_text": 1 * bool(p.get("ingredients_text") or p.get("ingredients_text_en")),
        "labels": 1 * bool(p.get("labels_tags")),
        "categories": 1 * bool(p.get("categories_tags")),
    }


def run_food(pages: int = 2) -> None:
    # Data-complete products, most-scanned first, biased to English-language
    # markets (design: the food half carries the "en" language axis).
    queries: list[dict[str, Any]] = [
        {**off_client.COMPLETE_POPULAR, **off_client.country_filter("en:united-states")},
        {**off_client.COMPLETE_POPULAR, **off_client.country_filter("en:united-kingdom")},
        {**off_client.COMPLETE_POPULAR},  # world pool as backfill
    ]
    seen: dict[str, dict[str, Any]] = {}
    for q in queries:
        for page in range(1, pages + 1):
            data = off_client.search({**q, "page": page})
            for p in data.get("products", []):
                code = str(p.get("code") or "")
                if not (code.isdigit() and is_valid(code)):
                    continue  # non-GS1 / malformed barcodes are useless to us
                if p.get("lang") != "en":
                    continue
                seen.setdefault(code, p)
    rows = []
    for code, p in seen.items():
        parts = score_food(p)
        rows.append({
            "id": code,
            "name": p.get("product_name") or "(no name)",
            "brand": (p.get("brands") or "").split(",")[0].strip(),
            "score": sum(parts.values()),
            "score_parts": parts,
            "categories": (p.get("categories_tags") or [])[:4],
            "allergens": p.get("allergens_tags") or [],
            "scans": p.get("unique_scans_n"),
        })
    rows.sort(key=lambda r: (-r["score"], -(r["scans"] or 0)))
    _write_jsonl(CAND_FOOD, rows[:40])
    print(f"food: {len(seen)} scanned -> top {min(40, len(rows))} written to {CAND_FOOD}")


# ---------------------------------------------------------------------------
# place (Korea TourAPI)
# ---------------------------------------------------------------------------


def score_place(common: dict[str, Any], intro: dict[str, Any], gallery_n: int, base: dict[str, Any]) -> dict[str, int]:
    overview = common.get("overview") or ""
    return {
        "firstimage": 2 * bool(base.get("firstimage") or common.get("firstimage")),
        "gallery_3plus": 2 * (gallery_n >= 3),
        "overview_300plus": 2 * (len(overview) >= 300),
        "usetime": 1 * bool(intro.get("usetime")),
        "infocenter": 1 * bool(intro.get("infocenter")),
        "parking": 1 * bool(intro.get("parking")),
    }


def run_place(busan_cap: int = 30) -> None:
    candidates: dict[str, dict[str, Any]] = {}
    for item in tour_client.area_based_list(6)[:busan_cap]:
        candidates[item["contentid"]] = {**item, "_region": "부산"}
    for kw in BUSAN_SEEDS + NATIONAL_SEEDS:
        hits = tour_client.search_keyword(kw, content_type_id=12)
        if hits:
            item = hits[0]
            region = "부산" if kw in BUSAN_SEEDS else "전국"
            candidates.setdefault(item["contentid"], {**item, "_region": region})

    rows = []
    for cid, base in candidates.items():
        try:
            common = tour_client.detail_common(cid)
            intro = tour_client.detail_intro(cid, base.get("contenttypeid", "12"))
            gallery = tour_client.detail_images(cid)
        except tour_client.TourApiError as exc:
            print(f"  skip {cid} ({base.get('title')}): {exc}", file=sys.stderr)
            continue
        parts = score_place(common, intro, len(gallery), base)
        rows.append({
            "id": cid,
            "name": base.get("title"),
            "region": base.get("_region"),
            "score": sum(parts.values()),
            "score_parts": parts,
            "overview_chars": len(common.get("overview") or ""),
            "usetime": (intro.get("usetime") or "")[:40],
            "gallery": len(gallery),
        })
    rows.sort(key=lambda r: -r["score"])
    _write_jsonl(CAND_PLACE, rows[:40])
    print(f"place: {len(candidates)} scanned -> top {min(40, len(rows))} written to {CAND_PLACE}")


# ---------------------------------------------------------------------------
# report + selection check
# ---------------------------------------------------------------------------


def run_report() -> None:
    lines = ["# S1 후보 리포트\n"]
    for title, path, cols in (
        ("음식 (OFF)", CAND_FOOD, ["id", "name", "brand", "score", "allergens", "scans"]),
        ("장소 (TourAPI)", CAND_PLACE, ["id", "name", "region", "score", "overview_chars", "usetime", "gallery"]),
    ):
        lines.append(f"## {title}\n")
        rows = _read_jsonl(path)
        if not rows:
            lines.append("(후보 파일 없음 — 먼저 food/place 서브커맨드 실행)\n")
            continue
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for r in rows:
            cells = []
            for c in cols:
                v = r.get(c)
                if isinstance(v, list):
                    v = ", ".join(str(x).removeprefix("en:") for x in v[:3])
                cells.append(str(v))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"report -> {REPORT}")


def run_check() -> None:
    """Validate selection.yaml: every pick exists in candidates + diversity constraints hold."""
    sel = yaml.safe_load(SELECTION.read_text(encoding="utf-8"))
    food_ids, place_ids = sel.get("food") or [], sel.get("place") or []
    cand_food = {r["id"]: r for r in _read_jsonl(CAND_FOOD)}
    cand_place = {r["id"]: r for r in _read_jsonl(CAND_PLACE)}
    errors: list[str] = []

    # expansion (2026-07-15): the corpus grows in daily batches of whole tens;
    # GLN numbering forbids reordering/removal. Since 2026-07-20 a batch may be
    # single-source (food-only day-02), so the two counts need not stay equal.
    if len(food_ids) < 10 or len(food_ids) % 10:
        errors.append(f"food must be a multiple of 10 (>=10), got {len(food_ids)}")
    if len(place_ids) < 10 or len(place_ids) % 10:
        errors.append(f"place must be a multiple of 10 (>=10), got {len(place_ids)}")
    missing = [str(i) for i in food_ids if str(i) not in cand_food] + [str(i) for i in place_ids if str(i) not in cand_place]
    if missing:
        errors.append(f"not in candidate files: {missing}")

    picked = [cand_food[str(i)] for i in food_ids if str(i) in cand_food]
    cats = {c for r in picked for c in r.get("categories", [])[:1]}
    if len(cats) < 5:
        errors.append(f"food category diversity < 5 (got {len(cats)})")
    if sum(bool(r.get("allergens")) for r in picked) < 5:
        errors.append("fewer than 5 food picks carry allergens")
    brand_counts = Counter(r.get("brand") for r in picked if r.get("brand"))
    if not brand_counts or brand_counts.most_common(1)[0][1] < 2:
        errors.append("need >= 2 products sharing one brand (multi-hop material)")

    picked_p = [cand_place[str(i)] for i in place_ids if str(i) in cand_place]
    if sum(r.get("region") == "부산" for r in picked_p) < 4:
        errors.append("need >= 4 Busan places (region multi-hop material)")

    if errors:
        print("selection.yaml INVALID:")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    print("selection.yaml OK (10+10, diversity constraints satisfied)")


# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["food", "place", "report", "check"])
    args = parser.parse_args()
    ensure_dirs()
    {"food": run_food, "place": run_place, "report": run_report, "check": run_check}[args.command]()


if __name__ == "__main__":
    main()
