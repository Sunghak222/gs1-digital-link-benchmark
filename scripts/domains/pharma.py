"""Pharma domain — openFDA drug labels (OTC only), with DailyMed package photos.

Label body text is kept as submitted rather than mined for numbers: the whole
point of this axis is prose a reader has to work through. Only the section
heading, which SPL repeats inside the field value, is trimmed.
"""
from __future__ import annotations

import json
from typing import Any

from scripts.common.config import DATA_DIR
from scripts.common.facts import fact_adder, make_fact
from scripts.common.identifiers import gtin_to_14
from scripts.domains.base import Domain

REQUIRED = frozenset({"pip", "instructions"})   # every label carries uses + directions
LICENSE = "Public domain (US FDA openFDA / NLM DailyMed)"
MEDIA_LICENSE = "Public domain (US FDA / NLM DailyMed)"

#: SPL repeats the section title once or twice at the head of the field value
#: ("Purpose Purpose Pain reliever..."). Strip only from the front.
_SPL_HEADERS = {
    "purpose": ["Purpose"],
    "indications_and_usage": ["Uses", "Use"],
    "dosage_and_administration": ["Directions"],
    "warnings": ["Warnings", "Warning"],
    "active_ingredient": ["Active ingredients", "Active ingredient"],
    "inactive_ingredient": ["Inactive ingredients", "Inactive ingredient"],
    "storage_and_handling": ["Other information", "Storage and handling"],
    "questions": ["Questions or comments?", "Questions?", "Questions"],
    "do_not_use": ["Do not use"],
    "stop_use": ["Stop use and ask a doctor if", "Stop use"],
    "when_using": ["When using this product"],
    "ask_doctor": ["Ask a doctor before use if you have", "Ask a doctor before use if",
                   "Ask a doctor"],
    "ask_doctor_or_pharmacist": ["Ask a doctor or pharmacist before use if you are",
                                 "Ask a doctor or pharmacist"],
    "keep_out_of_reach_of_children": ["Keep out of reach of children"],
    "pregnancy_or_breast_feeding": ["If pregnant or breast-feeding", "Pregnancy or breast feeding"],
}

#: (openFDA field, predicate, linktype). The safetyInfo group lands on one page,
#: where each fact becomes its own subheading.
_FIELDS = [
    ("purpose", "purpose", "pip"),
    ("indications_and_usage", "indications_and_usage", "pip"),
    ("dosage_and_administration", "directions", "instructions"),
    ("warnings", "warnings", "safetyInfo"),
    ("do_not_use", "do_not_use", "safetyInfo"),
    ("stop_use", "stop_use", "safetyInfo"),
    ("when_using", "when_using", "safetyInfo"),
    ("ask_doctor", "ask_doctor", "safetyInfo"),
    ("ask_doctor_or_pharmacist", "ask_doctor_or_pharmacist", "safetyInfo"),
    ("keep_out_of_reach_of_children", "keep_out_of_reach_of_children", "safetyInfo"),
    ("pregnancy_or_breast_feeding", "pregnancy_or_breast_feeding", "safetyInfo"),
    ("active_ingredient", "active_ingredients", "ingredientsInfo"),
    ("inactive_ingredient", "inactive_ingredients", "ingredientsInfo"),
    ("storage_and_handling", "storage", "consumerHandlingStorageInfo"),
    ("questions", "contact", "support"),
]


def _snapshot(upc: str) -> dict[str, Any]:
    path = DATA_DIR / "raw" / "openfda" / f"label-{upc}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _strip_header(field: str, text: str) -> str:
    v = text.strip()
    for header in _SPL_HEADERS.get(field, []):
        for _ in range(2):
            if v.lower().startswith(header.lower()):
                v = v[len(header):].lstrip(" :.-—\n")
    return v


def identify(upc: str, _ordinal: int = 0) -> str:
    return f"01/{gtin_to_14(upc)}"


def extract(upc: str, entity: str) -> tuple[str, list[dict[str, Any]]]:
    snap = _snapshot(upc)
    label, of = snap.get("label") or {}, snap.get("openfda") or {}

    def first(k: str) -> str:
        v = of.get(k) or []
        return str(v[0]).strip() if v else ""

    name = first("brand_name") or upc
    facts: list[dict[str, Any]] = []
    add = fact_adder(facts, entity, "pharma", upc, "openfda", drop_junk=False)

    # pip — identity. Manufacturer and generic name are relation facts: they carry
    # the multi-hop axes (same maker, same active ingredient).
    add("product_name", name, "pip", "openfda.brand_name")
    for key, predicate in (("manufacturer_name", "manufacturer"), ("generic_name", "generic_name")):
        value = first(key)
        if value:
            f = make_fact(entity, "pharma", upc, predicate, value, "pip",
                          "openfda", f"openfda.{key}")
            f["relation"] = True
            facts.append(f)
    add("route", first("route").capitalize(), "pip", "openfda.route")
    add("product_type", "OTC drug" if "OTC" in first("product_type") else first("product_type"),
        "pip", "openfda.product_type")

    for field, predicate, linktype in _FIELDS:
        raw = label.get(field)
        if not raw:
            continue
        add(predicate, _strip_header(field, str(raw[0] if isinstance(raw, list) else raw)),
            linktype, field)
    return name, facts


def media(upc: str) -> list[tuple[str, str, str]]:
    """Package-label scans; the select step embeds the DailyMed URLs in the snapshot."""
    snap = _snapshot(upc)
    return [(f"label-{i}.jpg", m["url"], MEDIA_LICENSE)
            for i, m in enumerate(snap.get("media") or [], start=1)][:4]


DOMAIN = Domain(
    name="pharma",
    ai_prefix="01",
    page_language="en",
    license=LICENSE,
    required_linktypes=REQUIRED,
    identify=identify,
    extract=extract,
    media=media,
)
