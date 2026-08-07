"""Disk-cached OpenAI JSON calls — deterministic re-runs, zero repeat cost.

Cache key is caller-provided (hash of model+input); cached outputs live in
data/raw/llm/ and double as the reproducibility record of every LLM assist.
"""
from __future__ import annotations

import json
from typing import Any

from scripts.common.config import DATA_DIR, OPENAI_API_KEY

LLM_CACHE_DIR = DATA_DIR / "raw" / "llm"


def llm_json(cache_key: str, messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = LLM_CACHE_DIR / f"{cache_key}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=model, messages=messages, temperature=0, response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data