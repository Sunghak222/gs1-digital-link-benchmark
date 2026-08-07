"""Food domain — Open Food Facts.

Field-to-linktype mapping is a flat rule list, not a scraper: each rule says
"this source field becomes this predicate on this page". Rules follow the GS1
web vocabulary; when nothing fits, the value goes to pip rather than being
dropped (the "lose no information" rule, docs/10).
"""
from __future__ import annotations

from typing import Any

from scripts.clients import off_client
from scripts.common.facts import clean_tags, fact_adder, make_fact
from scripts.common.identifiers import gtin_to_14
from scripts.domains.base import Domain

#: Nutrients we carry, per 100 g and — when the source has it — per serving.
NUTRIMENT_KEYS = [
    "energy-kcal", "fat", "saturated-fat", "carbohydrates", "sugars",
    "fiber", "proteins", "salt", "sodium",
]

REQUIRED = frozenset({"pip", "nutritionalInfo"})
LICENSE = "ODbL/CC-BY-SA (Open Food Facts)"
MEDIA_LICENSE = "CC-BY-SA (Open Food Facts contributors)"


def identify(gtin: str, _ordinal: int = 0) -> str:
    return f"01/{gtin_to_14(gtin)}"


def extract(gtin: str, entity: str) -> tuple[str, list[dict[str, Any]]]:
    p = off_client.get_product(gtin)
    if not p:
        raise RuntimeError(f"OFF has no product for {gtin}")
    name = p.get("product_name") or gtin
    facts: list[dict[str, Any]] = []
    add = fact_adder(facts, entity, "food", gtin, "off")

    # pip — identity and summary only. Numbers live on their own pages so that
    # every fact has exactly one page that can answer it.
    add("product_name", name, "pip", "product_name")
    brand = (p.get("brands") or "").split(",")[0].strip()
    if brand:
        f = make_fact(entity, "food", gtin, "brand", brand, "pip", "off", "brands")
        f["relation"] = True                      # same-brand multi-hop material
        facts.append(f)
    add("categories", clean_tags(p.get("categories_tags"))[:5], "pip", "categories_tags")
    add("quantity", p.get("quantity"), "pip", "quantity")
    if p.get("food_groups"):
        add("food_group", clean_tags([p["food_groups"]])[0], "pip", "food_groups")
    add("nova_group", p.get("nova_group"), "pip", "nova_group")
    # Packaging-facility codes have no clean linktype of their own; traceability
    # would need richer provenance, so they default to pip and stay remappable.
    emb = [x.strip() for x in (p.get("emb_codes") or "").split(",") if x.strip()] \
        or clean_tags(p.get("emb_codes_tags"))
    add("packaging_facility_codes", emb, "pip",
        "emb_codes" if p.get("emb_codes") else "emb_codes_tags")

    # nutritionalInfo — 100 g and per-serving side by side.
    nutr = p.get("nutriments") or {}
    for key in NUTRIMENT_KEYS:
        for basis in ("100g", "serving"):
            amount = nutr.get(f"{key}_{basis}")
            if amount is None:
                continue
            unit = nutr.get(f"{key}_unit") or ("kcal" if key == "energy-kcal" else "g")
            add(f"{key.replace('-', '_')}_{basis}", {"amount": amount, "unit": unit},
                "nutritionalInfo", f"nutriments.{key}_{basis}")
    add("serving_size", p.get("serving_size"), "nutritionalInfo", "serving_size")
    # "unknown" / "not-applicable" grades are absence typed as text.
    if (p.get("nutriscore_grade") or "").lower() in ("a", "b", "c", "d", "e"):
        add("nutriscore_grade", p["nutriscore_grade"].upper(), "nutritionalInfo", "nutriscore_grade")

    add("allergens", clean_tags(p.get("allergens_tags")), "allergenInfo", "allergens_tags")
    add("traces", clean_tags(p.get("traces_tags")), "allergenInfo", "traces_tags")

    add("ingredients_text", p.get("ingredients_text_en") or p.get("ingredients_text"),
        "ingredientsInfo", "ingredients_text")
    add("additives", clean_tags(p.get("additives_tags")), "ingredientsInfo", "additives_tags")
    analysis = [t for t in clean_tags(p.get("ingredients_analysis_tags"))
                if "unknown" not in t and "maybe" not in t]
    add("ingredients_analysis", analysis, "ingredientsInfo", "ingredients_analysis_tags")

    # Optional linktypes — the build gate counts how many of these an entity has.
    # Free-text `stores` keeps nicer casing ("Sainsbury's"); the normalized tags
    # cover more products, so they are the fallback.
    retailers = [x.strip() for x in (p.get("stores") or "").split(",") if x.strip()] \
        or clean_tags(p.get("stores_tags"))
    add("retailers", retailers, "hasRetailers", "stores" if p.get("stores") else "stores_tags")
    add("labels", clean_tags(p.get("labels_tags")), "certificationInfo", "labels_tags")
    add("origins", p.get("origins") or clean_tags(p.get("origins_tags")), "locationInfo", "origins")
    add("manufacturing_places", p.get("manufacturing_places"), "locationInfo", "manufacturing_places")
    add("purchase_places", [x.strip() for x in (p.get("purchase_places") or "").split(",") if x.strip()],
        "locationInfo", "purchase_places")
    add("storage_conditions", p.get("conservation_conditions"),
        "consumerHandlingStorageInfo", "conservation_conditions")
    # Packaging material is a sustainability claim, not a handling instruction:
    # gs1:sustainabilityInfo officially covers "sustainability and recycling".
    add("packaging", clean_tags(p.get("packaging_tags")), "sustainabilityInfo", "packaging_tags")
    env = (p.get("environmental_score_grade") or "").lower()
    if env in ("a-plus", "a", "b", "c", "d", "e", "f"):
        add("environmental_score_grade", "A+" if env == "a-plus" else env.upper(),
            "sustainabilityInfo", "environmental_score_grade")
    return name, facts


def media(gtin: str) -> list[tuple[str, str, str]]:
    p = off_client.get_product(gtin)
    pairs = [("front.jpg", p.get("image_front_url")),
             ("nutrition-label.jpg", p.get("image_nutrition_url")),
             ("ingredients.jpg", p.get("image_ingredients_url"))]
    return [(n, u, MEDIA_LICENSE) for n, u in pairs if u][:4]


DOMAIN = Domain(
    name="food",
    ai_prefix="01",
    page_language="en",
    license=LICENSE,
    required_linktypes=REQUIRED,
    identify=identify,
    extract=extract,
    media=media,
)
