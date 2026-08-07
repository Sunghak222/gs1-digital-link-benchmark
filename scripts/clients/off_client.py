"""Open Food Facts access: per-product API (cached) + dump streaming.

Candidate selection tries the search API first (we only need the top ~100
products, so the 10 GB dump is a fallback, not a requirement). All responses
are cached under data/raw/off/ — the cache doubles as the reproducibility
record of what the source said at build time.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from scripts.common.config import RAW_OFF_DIR
from scripts.common.fetch import cached_json

_BASE = "https://world.openfoodfacts.org"

#: Fields we ever need downstream (S1 scoring + S3 fact extraction).
PRODUCT_FIELDS = ",".join([
    "code", "product_name", "brands", "categories_tags", "countries_tags",
    "nutriments", "serving_size", "nutrition_data_per",
    "allergens_tags", "traces_tags", "ingredients_text", "ingredients_text_en",
    "additives_tags", "ingredients_analysis_tags",
    "stores", "stores_tags",
    "labels_tags", "origins", "origins_tags", "manufacturing_places",
    "conservation_conditions", "packaging_tags",
    "image_front_url", "image_nutrition_url", "image_ingredients_url",
    "unique_scans_n", "completeness", "states_tags", "lang",
    "quantity", "food_groups", "nova_group", "nutriscore_grade",  # fact enrichment 2026-07-27
    "environmental_score_grade", "purchase_places", "emb_codes", "emb_codes_tags",
])


def get_product(barcode: str) -> dict[str, Any]:
    """One product by GTIN. Returns {} when OFF has no entry (status 0)."""
    cache = RAW_OFF_DIR / f"product-{barcode}.json"
    data = cached_json(cache, f"{_BASE}/api/v2/product/{barcode}.json", params={"fields": PRODUCT_FIELDS})
    return data.get("product") or {}


def search(params: dict[str, Any]) -> dict[str, Any]:
    """Product search with disk cache keyed by the param set.

    Uses the legacy CGI endpoint — the v2 /api/v2/search endpoint returned
    maintenance pages throughout 2026-07-08 testing, while cgi/search.pl
    answered reliably. Facet filters use its tagtype_N/tag_N convention.
    """
    merged = {"action": "process", "json": 1, "fields": PRODUCT_FIELDS, "page_size": 100, **params}
    key = hashlib.sha256(json.dumps(merged, sort_keys=True).encode()).hexdigest()[:16]
    cache = RAW_OFF_DIR / f"search-{key}.json"
    return cached_json(cache, f"{_BASE}/cgi/search.pl", params=merged)


#: cgi/search.pl facet filter for "data-complete products, most-scanned first".
COMPLETE_POPULAR = {
    "tagtype_0": "states", "tag_contains_0": "contains", "tag_0": "en:complete",
    "sort_by": "unique_scans_n",
}


def country_filter(country_tag: str, slot: int = 1) -> dict[str, Any]:
    return {f"tagtype_{slot}": "countries", f"tag_contains_{slot}": "contains", f"tag_{slot}": country_tag}


def stream_dump(dump_path: Path, *, fields: set[str] | None = None) -> Iterator[dict[str, Any]]:
    """Stream openfoodfacts-products.jsonl.gz line by line (fallback to the API path).

    ``fields`` projects each record to keep memory flat; malformed lines are skipped.
    """
    with gzip.open(dump_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if fields:
                rec = {k: rec[k] for k in fields if k in rec}
            yield rec
