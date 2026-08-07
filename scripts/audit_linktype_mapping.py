"""Audit every linktype mapping rule against a model that knows the GS1 vocabulary.

    python -m scripts.audit_linktype_mapping

The rule table stays authoritative; this only stops a rule from quietly drifting
away from GS1 semantics, which is how packaging ended up on the storage page.
The model raises questions, a human decides. Writes work/notes/linktype-audit.md.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict

from openai import OpenAI

from scripts.common.config import BENCH_ROOT, OPENAI_API_KEY, WORK_DIR

GLOSSARY = """GS1 Digital Link linktypes used in this benchmark, with their meanings:
- pip: general product/place information page — identity, description, summary prose.
- nutritionalInfo: nutrition facts (energy, sugars, fat, salt, serving size...).
- allergenInfo: allergens and may-contain traces.
- ingredientsInfo: the full ingredient list, additives, and dietary analysis (vegan/vegetarian/palm oil).
- certificationInfo: certifications and label marks (organic, vegan, gluten-free, eco marks).
- locationInfo: address/coordinates of a place; for products, origin and manufacturing places.
- consumerHandlingStorageInfo: how the consumer should handle and store the product.
- sustainabilityInfo: packaging materials, recycling and sustainability guidance.
- openingHoursInfo: opening hours and closed days.
- support: contact points and practical visitor help (information center, parking).
- masterData: structured master attributes page."""

SYSTEM = (
    "You audit a fact-to-page mapping table for a GS1 Digital Link benchmark.\n"
    + GLOSSARY + "\n\n"
    "For each fact predicate below (with its source field and an example value), judge which "
    "linktype's page it belongs on by GS1 semantics. If none of the listed linktypes fits well, "
    "answer \"other:<gs1-linktype-you-would-expect>\".\n"
    'Return ONLY JSON: {"rows": [{"predicate": "...", "best": "...", '
    '"reason": "<one short sentence>"}]} — one row per input, same order.'
)
# Blind audit: the model never sees our current mapping, so it cannot anchor on
# it. The comparison happens in code afterwards.

NUTRIENT_ROW = re.compile(r"_(100g|serving)$")


def collect_rows() -> "OrderedDict[str, dict]":
    rows: "OrderedDict[str, dict]" = OrderedDict()
    for line in (BENCH_ROOT / "facts.jsonl").read_text(encoding="utf-8").splitlines():
        f = json.loads(line)
        pred = f["predicate"] if "predicate" in f else f["fact_id"].split("-", 2)[2]
        cls = "food" if f["fact_id"].startswith("food") else "place"
        if NUTRIENT_ROW.search(pred):
            pred = "nutrient_values(_100g/_serving 계열)"
        key = f"{cls}:{pred}"
        if key not in rows:
            rows[key] = {
                "class": cls, "predicate": pred, "current": f["linktype"],
                "source_field": f.get("source", {}).get("field", ""),
                "example": str(f.get("value"))[:80],
            }
    return rows


def main() -> None:
    rows = collect_rows()
    user = json.dumps(
        [{"predicate": k, **{x: v[x] for x in ("source_field", "example")}}
         for k, v in rows.items()],
        ensure_ascii=False, indent=1)
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o-mini", temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": user}],
    )
    verdicts = json.loads(resp.choices[0].message.content or "{}").get("rows", [])

    lines = ["# linktype 매핑 감사 — extract_facts.py 규칙표 vs GS1 의미론 (LLM 검증)",
             "", "| predicate | 현재 매핑 | LLM 판단 | 일치 | 근거 |", "|---|---|---|---|---|"]
    disagreements = 0
    for (key, row), v in zip(rows.items(), verdicts):
        agree = v.get("best") == row["current"]
        disagreements += 0 if agree else 1
        mark = "✓" if agree else "**✗**"
        lines.append(f"| {key} | {row['current']} | {v.get('best')} | {mark} | {v.get('reason','')} |")
        if not agree:
            print(f"✗ {key}: {row['current']} → LLM은 {v.get('best')} ({v.get('reason','')[:70]})")
    out = WORK_DIR / "notes" / "linktype-audit.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n감사 완료: {len(rows)}개 규칙, 불일치 {disagreements}건 → {out}")


if __name__ == "__main__":
    main()
