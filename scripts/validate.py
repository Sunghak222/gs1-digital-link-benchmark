"""§4 — validate the built corpus (and counterfactual/, if present). Exit 1 on any failure.

    python -m scripts.validate

Checks (design doc 01 §4):
  1 linkset schema        4 gate report          6 counterfactual divergence
  2 GTIN/GLN check digit  5 fact placement       7 EUC-KR decode (pipeline _decode, optional)
  3 link integrity          (rendered + unique)
QA gold integrity (part of check 5) runs only once qa.jsonl exists.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from jsonschema.validators import validator_for

from scripts.common.config import BENCH_ROOT, REPO_ROOT, SCHEMA_DIR, WORK_DIR
from scripts.common.identifiers import is_valid

LINKSET_SCHEMA = json.loads((SCHEMA_DIR / "linkset.schema.json").read_text(encoding="utf-8"))
_LINKSET_VALIDATOR = validator_for(LINKSET_SCHEMA)(LINKSET_SCHEMA)  # compiled once: 33 s -> 4 s

#: predicates whose value legitimately appears on several pages (titles, prose containers)
LEAK_EXEMPT = {"product_name", "place_name", "overview", "located_in_region", "categories"}

_errors: list[str] = []


def err(msg: str) -> None:
    _errors.append(msg)


def read_page(path: Path) -> str:
    raw = path.read_bytes()
    head = raw[:200].decode("ascii", "ignore")
    return raw.decode("cp949" if "euc-kr" in head else "utf-8")


def needles(fact: dict[str, Any]) -> list[str]:
    """Strings that must appear on the fact's page (empty -> unrenderable, skip)."""
    src = fact["source"]
    if src.get("value_span"):
        s, e = src["value_span"]
        return [src["passage"][s:e]]
    v = fact["value"]
    if isinstance(v, dict):
        if {"amount", "unit"} <= v.keys():
            return [str(v["amount"])]
        if {"lat", "lon"} <= v.keys():
            return [str(v["lat"])]
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def load_corpus_pages(root: Path, entity_map: dict[str, Any]) -> dict[str, dict[str, str]]:
    """entity -> {page filename -> decoded text} for one corpus root."""
    pages: dict[str, dict[str, str]] = {}
    for entity in entity_map:
        ent_dir = root / "entities" / entity.replace("/", "-")
        pages[entity] = {p.name: read_page(p) for p in sorted((ent_dir / "pages").glob("*.html"))}
    return pages


def main() -> None:
    facts = [json.loads(l) for l in (BENCH_ROOT / "facts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    entity_map = json.loads((WORK_DIR / "entity-map.json").read_text(encoding="utf-8"))
    manifest = json.loads((BENCH_ROOT / "manifest.json").read_text(encoding="utf-8"))
    by_id = {f["fact_id"]: f for f in facts}

    # --- 1+2+3: per-entity structural checks
    for entity, meta in entity_map.items():
        slug = entity.replace("/", "-")
        ent_dir = BENCH_ROOT / "entities" / slug
        code = entity.split("/")[1]
        if not is_valid(code):
            err(f"[2 checkdigit] {entity}: invalid identifier")
        ls_path = ent_dir / "linkset.json"
        if not ls_path.exists():
            err(f"[3 integrity] {entity}: linkset.json missing")
            continue
        linkset = json.loads(ls_path.read_text(encoding="utf-8"))
        try:
            _LINKSET_VALIDATOR.validate(linkset)
        except Exception as exc:
            err(f"[1 schema] {entity}: {str(exc).splitlines()[0]}")
        for rel, targets in linkset["linkset"][0].items():
            if rel == "anchor" or not isinstance(targets, list):
                continue
            for t in targets:
                href = t.get("href", "")
                if not href.startswith(("http://", "https://")) and not (ent_dir / href).exists():
                    err(f"[3 integrity] {entity}: broken href {href}")

    # --- 4: gate report re-check
    for entity, gate in manifest["gate"].items():
        if not gate["ok"]:
            err(f"[4 gate] {entity}: {gate}")

    # --- 5: fact placement — rendered on its page, absent from sibling pages.
    # A cross-page hit is NOT a build failure when the source data itself carries
    # the token in both fields (e.g. labels "made in italy" vs origins "italy") —
    # those facts lose gold-evidence uniqueness, so they go on the QA exclusion
    # list (work/qa-ambiguous-facts.json) and gen_qa must not use them as gold.
    pages = load_corpus_pages(BENCH_ROOT, entity_map)
    placement = manifest["placement"]
    ambiguous: list[dict[str, str]] = []
    for f in facts:
        page_rel = placement.get(f["fact_id"])
        if page_rel is None:
            err(f"[5 placement] {f['fact_id']}: not placed on any page")
            continue
        page_name = Path(page_rel).name
        ent_pages = pages[f["entity"]]
        text = ent_pages.get(page_name, "")
        for needle in needles(f):
            if needle not in text:
                err(f"[5 placement] {f['fact_id']}: needle {needle!r} not rendered on {page_name}")
            if f["predicate"] in LEAK_EXEMPT or len(needle) < 4:
                continue
            for other_name, other_text in ent_pages.items():
                if other_name != page_name and needle in other_text:
                    ambiguous.append({"fact_id": f["fact_id"], "needle": needle, "also_on": other_name})
    # Summarised to one line; the per-fact detail goes to the file.
    (WORK_DIR / "qa-ambiguous-facts.json").write_text(
        json.dumps(ambiguous, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  [5 unique] 모호 팩트 {len(ambiguous)}건 -> work/qa-ambiguous-facts.json (QA 골드 제외 목록)")

    #     5b: QA gold integrity (only once qa.jsonl exists)
    qa_path = BENCH_ROOT / "qa.jsonl"
    if qa_path.exists():
        for qa in (json.loads(l) for l in qa_path.read_text(encoding="utf-8").splitlines() if l.strip()):
            for fid in qa["gold_fact_ids"]:
                if fid not in by_id:
                    err(f"[5 qa-gold] {qa['qa_id']}: unknown fact {fid}")

    # --- 6: counterfactual divergence
    cf_root = BENCH_ROOT / "counterfactual"
    ov_path = WORK_DIR / "counterfactual-overrides.jsonl"
    if cf_root.exists() and ov_path.exists():
        cf_pages = load_corpus_pages(cf_root, entity_map)
        for ov in (json.loads(l) for l in ov_path.read_text(encoding="utf-8").splitlines() if l.strip()):
            f = by_id.get(ov["fact_id"])
            if f is None:
                err(f"[6 cf] override targets unknown fact {ov['fact_id']}")
                continue
            page_name = Path(placement[f["fact_id"]]).name
            orig_text = pages[f["entity"]].get(page_name, "")
            cf_text = cf_pages[f["entity"]].get(page_name, "")
            new = ov.get("value_literal") or (
                str(ov["value"]["amount"]) if isinstance(ov.get("value"), dict) and "amount" in ov["value"]
                else str(ov.get("value", "")))
            if new and new not in cf_text:
                err(f"[6 cf] {ov['fact_id']}: CF value {new!r} not rendered in CF corpus")
            if new and new in orig_text:
                err(f"[6 cf] {ov['fact_id']}: CF value {new!r} also present in ORIGINAL corpus (no divergence)")

    # --- 7: EUC-KR pages survive the pipeline's decoder (optional import)
    sys.path.insert(0, str(REPO_ROOT / "gs1-palantir-core-kg_neo4j"))
    try:
        from packages.vector_index.html import _decode  # type: ignore
    except Exception:
        print("  [7 encoding] pipeline _decode unavailable — skipped")
        _decode = None
    if _decode:
        for entity, meta in entity_map.items():
            if meta.get("class") != "place":
                continue  # only place T4 pages are EUC-KR
            for p in (BENCH_ROOT / "entities" / entity.replace("/", "-") / "pages").glob("*.html"):
                raw = p.read_bytes()
                if b"euc-kr" in raw[:200]:
                    decoded = _decode(raw)
                    if "�" in decoded or not any("가" <= ch <= "힣" for ch in decoded):
                        err(f"[7 encoding] {entity}/{p.name}: pipeline _decode mangled the page")

    if _errors:
        print(f"\nvalidate: {len(_errors)} FAILURE(S)")
        for e in _errors:
            print(" ", e)
        sys.exit(1)
    print("validate: all checks passed "
          f"({len(entity_map)} entities, {len(facts)} facts, cf={'yes' if cf_root.exists() else 'no'})")


if __name__ == "__main__":
    main()
