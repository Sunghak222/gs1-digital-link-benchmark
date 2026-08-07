"""What every corpus domain must declare.

Adding a domain used to mean editing six files (extract_facts, build_fixtures,
gen_qa, validate, make_review, a harvester). Everything domain-specific now
lives in one module per domain; the pipeline just walks the registry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class Extractor(Protocol):
    def __call__(self, source_id: str, entity: str) -> tuple[str, list[dict[str, Any]]]:
        """Return (display name, atomic facts) for one entity."""


class MediaCollector(Protocol):
    def __call__(self, source_id: str) -> list[tuple[str, str, str]]:
        """Return [(filename, url, license)], capped by the domain."""


def no_extra_copy(_linktype: str, _page_facts: list[dict[str, Any]]) -> list[str]:
    return []


@dataclass(frozen=True)
class Domain:
    name: str
    #: GS1 application identifier the entity path is built on: "01" GTIN, "414" GLN.
    ai_prefix: str
    #: Document language for rendered pages.
    page_language: str
    #: Rights statement stamped into the manifest.
    license: str
    #: Linktypes an entity must have to enter the corpus.
    required_linktypes: frozenset[str]
    #: schema.org type used in the masterData JSON-LD.
    schema_type: str
    #: (source_id, 1-based selection ordinal) -> entity path, e.g. "01/00000050457250".
    #: The ordinal only matters for places, whose GLNs are issued by selection order.
    identify: Callable[[str, int], str]
    extract: Extractor
    media: MediaCollector
    #: Encoding for the noisy T4 template. Set only where we deliberately exercise
    #: the pipeline's decoding fallback; None means UTF-8 like everything else.
    legacy_encoding: str | None = None
    #: Extra page copy a domain wants rendered that is not itself a fact.
    extra_page_copy: Callable[[str, list[dict[str, Any]]], list[str]] = no_extra_copy

    def entity_of(self, source_id: str, ordinal: int = 0) -> str:
        return self.identify(source_id, ordinal)
