"""Building blocks shared by every domain's fact extractor."""
from __future__ import annotations

import re
from typing import Any

#: Literal "no data" placeholders contributors type into free-text source fields.
#: "none" is deliberately absent: for allergens and traces it is a real negative claim.
JUNK_VALUE = re.compile(r"^\s*(not indicated\.?|n/?a|unknown|unspecified|-)\s*$", re.IGNORECASE)

_BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def clean_tags(tags: list[str] | None) -> list[str]:
    """Strip the language prefix TourAPI/OFF put on taxonomy tags: 'en:sauces' -> 'sauces'."""
    return [re.sub(r"^[a-z]{2}:", "", t).replace("-", " ") for t in (tags or [])]


def strip_br(value: str | None) -> str | None:
    """TourAPI intro fields embed literal <br> tags inside otherwise plain text."""
    if not value:
        return value
    v = _BR_TAG.sub("\n", value)
    v = re.sub(r"[ \t]*\n[ \t]*", "\n", v)
    return re.sub(r"\n{2,}", "\n", v).strip()


def is_junk(value: Any) -> bool:
    return isinstance(value, str) and bool(JUNK_VALUE.match(value))


def make_fact(entity: str, cls: str, code: str, predicate: str, value: Any,
              linktype: str, origin: str, field: str, **source_extra: Any) -> dict[str, Any]:
    return {
        "fact_id": f"{cls}-{code}-{slug(predicate)}",
        "entity": entity,
        "predicate": predicate,
        "value": value,
        "linktype": linktype,
        "source": {"origin": origin, "field": field, **source_extra},
    }


def fact_adder(collected: list[dict[str, Any]], entity: str, cls: str, code: str,
               origin: str, *, drop_junk: bool = True) -> Any:
    """Return an ``add(predicate, value, linktype, field, **extra)`` bound to one entity.

    Empty values are always skipped: absence is not a fact. ``drop_junk`` also
    discards typed-out placeholders ("N/A", "Unspecified"), which crowd-sourced
    sources are full of. Curated regulatory text does not need it, so the pharma
    domain leaves it off.
    """

    def add(predicate: str, value: Any, linktype: str, field: str, **extra: Any) -> None:
        if drop_junk:
            if is_junk(value):
                return
            if isinstance(value, list):
                value = [v for v in value if not is_junk(v)]
        if value not in (None, "", [], {}):
            collected.append(
                make_fact(entity, cls, code, predicate, value, linktype, origin, field, **extra))

    return add
