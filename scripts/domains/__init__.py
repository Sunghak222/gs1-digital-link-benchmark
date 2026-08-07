"""Domain registry.

Order matters: it fixes the order entities are extracted in, and therefore the
line order of facts.jsonl. Append new domains at the end.
"""
from __future__ import annotations

from scripts.domains.base import Domain
from scripts.domains.food import DOMAIN as FOOD
from scripts.domains.pharma import DOMAIN as PHARMA
from scripts.domains.place import DOMAIN as PLACE

#: selection.yaml section name -> domain
REGISTRY: dict[str, Domain] = {d.name: d for d in (FOOD, PHARMA, PLACE)}


def get(name: str) -> Domain:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown domain {name!r}; known: {sorted(REGISTRY)}") from None


__all__ = ["Domain", "REGISTRY", "get", "FOOD", "PHARMA", "PLACE"]
