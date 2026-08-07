"""Bulk expansion — Open Food Facts harvester. Dump only, zero API calls.

    python -m scripts.bulk.off_harvest scan                                # CSV sweep -> candidates
    python -m scripts.bulk.off_harvest snapshot                            # JSONL pass: lang=en, snapshots, warehouse
    python -m scripts.bulk.off_harvest select --target 200 --batch day-03  # gate -> select -> append
    python -m scripts.bulk.off_harvest all --target 200 --batch day-03

Gates, in order: an English-market pre-filter with lang=en as the final say,
7+ as-sold nutrient values, a usable category, no duplicate brand + product name,
and a live nutrition photo. Snapshots are written in the same envelope the API
returned, so nothing downstream has to know where a record came from.

The old en:complete status filter is gone: it duplicated checks we already make
on the fields we actually use, and it discarded 99% of otherwise-valid products.
Volume is capped by CAND_CAP instead, keeping the most-scanned candidates.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import yaml

from scripts.common.config import DATA_DIR, RAW_OFF_DIR, USER_AGENT, WORK_DIR, ensure_dirs
from scripts.common.identifiers import gtin_to_14, is_valid
from scripts.select_entities import score_food

BULK_DIR = WORK_DIR / "bulk"
CSV_CAND = BULK_DIR / "off-csv-candidates.jsonl"
SHORTLIST = BULK_DIR / "off-shortlist.jsonl"
#: Warehouse holding the full dump line of every matched candidate, so adding a
#: field later reads this instead of re-scanning 13 GB. The pipeline never reads
#: it — data/raw snapshots remain the input.
RAW_MATCHED = BULK_DIR / "off-raw-matched.jsonl.gz"
SELECTION = WORK_DIR / "selection.yaml"
CAND_FOOD = WORK_DIR / "candidates-food.jsonl"

CSV_DUMP = DATA_DIR / "dump" / "en.openfoodfacts.org.products-20260720.csv.gz"
JSONL_DUMP = DATA_DIR / "dump" / "openfoodfacts-products-20260720.jsonl.gz"

#: English-market pre-filter; the label language is finally decided by lang=en.
COUNTRIES = ["United States", "United Kingdom", "Ireland", "Australia", "Canada", "New Zealand"]

#: Volume cap, not a quality filter: keep the most-scanned candidates only.
#: Raise it when the pool runs dry and re-scan.
CAND_CAP = 80000  # covers every product passing the basic filters

#: Fields kept in a snapshot: off_client.PRODUCT_FIELDS plus lang.
SNAPSHOT_FIELDS = [
    "code", "lang", "product_name", "brands", "categories_tags", "countries_tags",
    "nutriments", "serving_size", "nutrition_data_per",
    "allergens_tags", "traces_tags", "ingredients_text", "ingredients_text_en",
    "additives_tags", "ingredients_analysis_tags", "stores", "stores_tags",
    "labels_tags", "origins", "origins_tags", "manufacturing_places",
    "conservation_conditions", "packaging_tags",
    "image_front_url", "image_nutrition_url", "image_ingredients_url",
    "unique_scans_n", "completeness", "states_tags",
    "quantity", "food_groups", "nova_group", "nutriscore_grade",  # fact enrichment 2026-07-27
    "environmental_score_grade", "purchase_places", "emb_codes", "emb_codes_tags",
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]+", "", (s or "").lower())


def read_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def selected_food_ids() -> list[str]:
    sel = yaml.safe_load(SELECTION.read_text(encoding="utf-8"))
    return [str(x) for x in sel.get("food") or []]


# ---------------------------------------------------------------------------
# [1] CSV scan
# ---------------------------------------------------------------------------

def run_scan() -> None:
    csv.field_size_limit(10_000_000)
    # Sources pad barcodes differently (CSV 14 digits vs selection 8/13), so compare normalised.
    existing = {c.lstrip("0") for c in selected_food_ids()}
    total, out = 0, []
    with gzip.open(CSV_DUMP, "rt", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        g100 = [c for c in reader.fieldnames if c.endswith("_100g") and "prepared" not in c]
        for row in reader:
            total += 1
            if total % 1_000_000 == 0:
                print(f"  ..{total:,} rows, candidates {len(out)}", flush=True)
            countries = row.get("countries_en") or ""
            if not any(c in countries for c in COUNTRIES):
                continue
            if not (row.get("image_nutrition_url") or "").strip():
                continue
            if not (row.get("ingredients_text") or "").strip():
                continue
            code = (row.get("code") or "").strip()
            if not is_valid(code) or code.lstrip("0") in existing:
                continue
            filled = sum(1 for c in g100 if (row.get(c) or "").strip())
            if filled < 8:
                continue
            out.append({
                "code": code,
                "name": (row.get("product_name") or "").strip(),
                "brands": (row.get("brands") or "").strip(),
                "countries": countries,
                "scans": int(row.get("unique_scans_n") or 0),
                "image_front_url": (row.get("image_url") or "").strip(),
                "image_ingredients_url": (row.get("image_ingredients_url") or "").strip(),
                "image_nutrition_url": (row.get("image_nutrition_url") or "").strip(),
            })
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    n_passed = len(out)
    out.sort(key=lambda r: -r["scans"])
    out = out[:CAND_CAP]
    CSV_CAND.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    print(f"scan: {total:,}행 → 필터 통과 {n_passed:,}개 → 스캔 수 상위 {len(out)}개 -> {CSV_CAND}")


# ---------------------------------------------------------------------------
# [2] JSONL pass: lang=en + snapshot
# ---------------------------------------------------------------------------

#: Nutrients converted back to the legacy nutriments keys.
_NUTRIENT_IDS = ["energy-kcal", "fat", "saturated-fat", "carbohydrates", "sugars",
                 "fiber", "proteins", "salt", "sodium"]


def _round(v: Any) -> Any:
    return round(v, 4) if isinstance(v, float) else v


def _legacy_nutriments(rec: dict[str, Any]) -> dict[str, Any]:
    """New nutrition structure -> legacy nutriments keys, as-sold and label-measured only."""
    out: dict[str, Any] = {}
    nut = rec.get("nutrition") or {}
    agg = (nut.get("aggregated_set") or {}).get("nutrients") or {}
    for key in _NUTRIENT_IDS:
        e = agg.get(key)
        if e and e.get("source") == "packaging" and e.get("value") is not None:
            out[f"{key}_100g"] = _round(e["value"])
            if e.get("unit"):
                out[f"{key}_unit"] = e["unit"]
    for s in nut.get("input_sets") or []:
        if (s.get("per") == "serving" and s.get("source") == "packaging"
                and s.get("preparation", "as_sold") == "as_sold"):
            for key in _NUTRIENT_IDS:
                e = (s.get("nutrients") or {}).get(key)
                if e and e.get("value") is not None:
                    out.setdefault(f"{key}_serving", _round(e["value"]))
    return out

def run_snapshot() -> None:
    cand = {r["code"]: r for r in read_jsonl(CSV_CAND)}
    if not cand:
        sys.exit("먼저 scan을 실행하세요 (후보 없음)")
    # Every JSONL line starts {"_id":"<13-digit zero-padded barcode>", ...}.
    wanted_norm = {c.lstrip("0") or "0": c for c in cand}
    id_re = re.compile(r'^\{"_id":"(\d+)"')
    found: dict[str, str] = {}          # csv code -> lang
    n_written = 0
    with gzip.open(JSONL_DUMP, "rt", encoding="utf-8") as fh, \
         gzip.open(RAW_MATCHED, "wt", encoding="utf-8") as raw_out:
        for i, line in enumerate(fh, 1):
            if i % 500_000 == 0:
                print(f"  ..{i:,} records, matched {len(found)}/{len(wanted_norm)}", flush=True)
            if len(found) == len(wanted_norm):
                break
            m = id_re.match(line)
            code = wanted_norm.get((m.group(1).lstrip("0") or "0")) if m else None
            if code is None or code in found:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            lang = rec.get("lang") or "?"
            found[code] = lang
            # The warehouse keeps every match regardless of language, verbatim.
            raw_out.write(line if line.endswith("\n") else line + "\n")
            if lang != "en":
                continue
            product = {k: rec[k] for k in SNAPSHOT_FIELDS if k in rec}
            # The dump moved to a new nutrition structure and leaves the legacy
            # nutriments empty. Convert only label-measured values (source=packaging);
            # estimates are excluded.
            product["nutriments"] = _legacy_nutriments(rec)
            # Dump records carry no derived image_*_url; fill them from the CSV.
            for k in ("image_front_url", "image_ingredients_url", "image_nutrition_url"):
                if not product.get(k) and cand[code].get(k):
                    product[k] = cand[code][k]
            target = RAW_OFF_DIR / f"product-{code}.json"
            # Existing snapshots are never overwritten.
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"code": code, "product": product, "status": 1},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
                n_written += 1
    shortlist = [dict(cand[c], lang=found.get(c, "?")) for c in cand if found.get(c) == "en"]
    SHORTLIST.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in shortlist), encoding="utf-8")
    langs = Counter(found.values())
    print(f"snapshot: 대조 {len(found)}/{len(cand)}, lang 분포 {dict(langs)}")
    print(f"원본 창고: {RAW_MATCHED.name} {RAW_MATCHED.stat().st_size/1e6:.1f}MB ({len(found)}줄)")
    print(f"  영어 {len(shortlist)}개 -> {SHORTLIST} (스냅샷 신규 저장 {n_written})")


# ---------------------------------------------------------------------------
# [3~5] gates -> HEAD -> select -> append
# ---------------------------------------------------------------------------

def _head_alive(client: httpx.Client, url: str) -> bool:
    try:
        r = client.head(url, follow_redirects=True, timeout=10)
        if r.status_code == 405:            # server refuses HEAD; ask for one byte
            r = client.get(url, headers={"Range": "bytes=0-0"}, timeout=10)
        return r.status_code < 400
    except httpx.HTTPError:
        return False


def _snapshot_product(code: str) -> dict[str, Any]:
    p = RAW_OFF_DIR / f"product-{code}.json"
    return json.loads(p.read_text(encoding="utf-8")).get("product") or {}


def run_select(target: int, batch: str) -> None:
    shortlist = read_jsonl(SHORTLIST)
    if not shortlist:
        sys.exit("먼저 snapshot을 실행하세요")
    existing_ids = selected_food_ids()
    # Dedup keys of already-selected products, normalised (brand + product name;
    # the same name under a different brand is allowed). Cached code->key so a round
    # does not re-read 19k snapshot files.
    cache_p = BULK_DIR / "seen-names-cache.json"
    key_cache: dict[str, list[str]] = {}
    if cache_p.exists():
        try:
            key_cache = json.loads(cache_p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a broken cache just means reading them all
            key_cache = {}
    seen_names: set[tuple[str, str]] = set()
    cache_dirty = False
    for code in existing_ids:
        if code in key_cache:
            seen_names.add(tuple(key_cache[code]))
            continue
        try:
            p = _snapshot_product(code)
        except FileNotFoundError:
            continue
        k = (_norm((p.get("brands") or "").split(",")[0]), _norm(p.get("product_name") or ""))
        key_cache[code] = list(k)
        seen_names.add(k)
        cache_dirty = True
    if cache_dirty:
        cache_p.write_text(json.dumps(key_cache, ensure_ascii=False), encoding="utf-8")

    graded = []
    # Build the normalised set once. Building it inside the loop makes this
    # candidates x already-selected, which costs minutes.
    existing_norm = {c.lstrip("0") for c in existing_ids}
    for r in shortlist:
        code = r["code"]
        if code.lstrip("0") in existing_norm:
            continue                                     # padding guard, before any file read
        try:
            p = _snapshot_product(code)
        except FileNotFoundError:
            continue
        cats = p.get("categories_tags") or []
        if "en:null" in cats:
            continue                                     # unusable category
        # Needs 7+ as-sold nutrient values; prepared (after cooking) keys do not count.
        n100 = [k for k in (p.get("nutriments") or {}) if k.endswith("_100g") and "prepared" not in k]
        if len(n100) < 7:
            continue
        key = (_norm((p.get("brands") or "").split(",")[0]), _norm(p.get("product_name") or ""))
        if key in seen_names or not key[1]:
            continue                                     # duplicate brand + name
        graded.append({**r, "score": sum(score_food(p).values()), "score_parts": score_food(p),
                       "categories": (p.get("categories_tags") or [])[:4],
                       "allergens": p.get("allergens_tags") or [], "_key": key, "_p": p})
    graded.sort(key=lambda x: (-x["score"], -(x["scans"] or 0)))
    print(f"게이트 통과 {len(graded)}개 — 이미지 생존 확인하며 상위 {target}개 선별")

    picked, dead = [], 0
    picked_keys: set[tuple[str, str]] = set()
    # Liveness checks run 8 at a time; only the HEAD requests are concurrent, and
    # results are consumed in rank order, so selection matches the sequential version.
    # The concurrency cap replaces a per-request sleep.
    CHUNK = 32

    def _probe(r: dict) -> tuple[dict, bool, list[str]]:
        p = r["_p"]
        if not _head_alive(client, p["image_nutrition_url"]):
            return r, False, []                           # the nutrition photo is mandatory
        deadf = [k for k in ("image_front_url", "image_ingredients_url")
                 if p.get(k) and not _head_alive(client, p[k])]
        return r, True, deadf

    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client, \
            ThreadPoolExecutor(max_workers=8) as ex:
        for i in range(0, len(graded), CHUNK):
            if len(picked) >= target:
                break
            for r, alive, deadf in ex.map(_probe, graded[i:i + CHUNK]):
                if len(picked) >= target:
                    break
                if r["_key"] in picked_keys:
                    continue
                if not alive:
                    dead += 1
                    continue
                p = r["_p"]
                # A dead front/ingredients photo drops the field, not the product.
                for k in deadf:
                    p.pop(k, None)
                if deadf:
                    path = RAW_OFF_DIR / f"product-{r['code']}.json"
                    path.write_text(json.dumps({"code": r["code"], "product": p, "status": 1},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
                picked.append(r)
                picked_keys.add(r["_key"])
                if len(picked) % 25 == 0:
                    print(f"  ..{len(picked)}/{target} (죽은 라벨 {dead})", flush=True)

    if len(picked) < target:
        print(f"WARN: 목표 {target} 중 {len(picked)}개만 확보 (풀 소진/이미지 사망 {dead})")

    # append to selection.yaml, candidates-food.jsonl and the batch entity list
    before = SELECTION.read_text(encoding="utf-8")
    lines = [f"  # --- 대량 확장 {batch} (OFF 덤프 20260720, docs/08) ---"]
    for r in picked:
        brand = (r["_p"].get("brands") or "").split(",")[0].strip()
        lines.append(f'  - "{r["code"]}"   # {brand} — {r["name"][:40]}')
    place_idx = before.index("\nplace:")
    SELECTION.write_text(before[:place_idx] + "\n".join(lines) + "\n" + before[place_idx:], encoding="utf-8")

    with CAND_FOOD.open("a", encoding="utf-8") as fh:
        for r in picked:
            fh.write(json.dumps({
                "id": r["code"], "name": r["name"],
                "brand": (r["_p"].get("brands") or "").split(",")[0].strip(),
                "score": r["score"], "score_parts": r["score_parts"],
                "categories": r["categories"], "allergens": r["allergens"], "scans": r["scans"],
            }, ensure_ascii=False) + "\n")

    ents_path = WORK_DIR / "expansion" / f"{batch}-entities.txt"
    prev = ents_path.read_text(encoding="utf-8") if ents_path.exists() else ""
    ents_path.write_text(prev + "".join(f"01/{gtin_to_14(r['code'])}\n" for r in picked), encoding="utf-8")

    # The first tag is an umbrella category (plant-based and friends) and hides the
    # real distribution; count the first specific tag after it instead.
    _umbrella = {"plant-based-foods-and-beverages", "plant-based-foods",
                 "beverages-and-beverages-preparations", "fermented-foods"}

    def _cat(r: dict) -> str:
        tags = [str(t).removeprefix("en:") for t in r["categories"]]
        return next((t for t in tags if t not in _umbrella), tags[0] if tags else "?")

    cat_top = Counter(_cat(r) for r in picked).most_common(8)
    print(f"select: {len(picked)}개 append -> selection.yaml / {ents_path}")
    print("  카테고리 상위:", ", ".join(f"{k}×{v}" for k, v in cat_top))
    big = [f"{k}({v})" for k, v in cat_top if v > max(5, len(picked) * 0.15)]
    if big:
        print(f"  ⚠ 편중 경고: {', '.join(big)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["scan", "snapshot", "select", "all"])
    ap.add_argument("--target", type=int, default=200)
    ap.add_argument("--batch", default="day-03")
    a = ap.parse_args()
    ensure_dirs()
    if a.command in ("scan", "all") and not (a.command == "all" and CSV_CAND.exists()):
        run_scan()
    if a.command in ("snapshot", "all") and not (a.command == "all" and SHORTLIST.exists()):
        run_snapshot()
    if a.command in ("select", "all"):
        run_select(a.target, a.batch)


if __name__ == "__main__":
    main()
