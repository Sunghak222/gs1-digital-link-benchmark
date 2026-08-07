# 벤치마크에서 사용한 LLM 프롬프트 전체 목록

파이프라인의 LLM 호출 지점은 아래 5곳이 전부다. 공통 설정: **gpt-4o-mini, temperature 0, JSON 강제(response_format), 디스크 캐시** — 같은 입력이면 항상 같은 출력(재현성).

| # | 용도 | 파일 | 언어 | 비고 |
|---|---|---|---|---|
| 1 | 장소 소개문 팩트 채굴 | `scripts/extract_facts.py` `mine_overview()` | 한국어 | 검증 가능한 값만, 원문 그대로 인용 강제 |
| 2 | 영양라벨 사진 판독 (VLM) | `scripts/extract_facts.py` `verify_nutrition_images()` | 영어 | 사진 vs 텍스트 교차검증용 |
| 3 | QA 초안 생성 | `scripts/gen_qa.py` `draft_page()` | 한국어 | 페이지 단위, 골드는 팩트 목록에서만 |
| 4 | linktype 매핑 감사 | `scripts/audit_linktype_mapping.py` | 영어 | 블라인드 분류 후 규칙표와 대조 |
| 5 | 답변 채점 (LLM judge) | `eval/grade.py` | 영어 | 평가 시스템 — 파이프라인과 다른 모델로 채점 |

---

## 1. 장소 소개문 팩트 채굴 — `mine_overview()`

관광지 소개 글(자유 서술)에서 검증 가능한 사실을 최대 4개 뽑는다. 핵심 장치: `value_literal`을 **소개문에 문자 그대로 존재하는 부분 문자열**로 강제하고, 코드가 `str.find()`로 원문에서 재탐색 — 못 찾으면 그 팩트는 버린다(LLM은 값을 가리킬 수만 있고 지어낼 수 없음).

```text
다음은 한국 관광지 '{name}'의 공식 소개문이다.
---
{overview}
---
질의응답 벤치마크의 골드 팩트로 쓸 구체적 사실을 최대 4개 추출하라. 수치·연도·길이·고유명사 등
검증 가능한 것만. 각 항목: predicate(영문 snake_case), value(정규화된 값),
value_literal(소개문에 문자 그대로 존재하는 부분 문자열).
JSON 형식: {"facts": [{"predicate": ..., "value": ..., "value_literal": ...}]}
```

후처리 안전장치: ① 원문 재탐색 실패 → 탈락 ② "not_specified" 류 값 → 탈락(없음은 사실이 아님) ③ predicate 중복 → `_N` 접미사. 캐시 키 = 소개문 내용의 해시(소개문이 바뀐 장소만 재채굴됨).

## 2. 영양라벨 사진 판독 (VLM) — `verify_nutrition_images()`

라벨 사진을 읽혀 OFF 텍스트 값과 대조한다. 20% 이상 어긋나는 (제품, 영양소) 쌍은 값을 고치지 않고(텍스트가 정본) **이미지 QA 금지 목록**(`work/image-qa-exclusions.json`)에 올린다.

```text
Read the nutrition facts panel in this photo. Return JSON with per-100g values
(numbers only, null if not shown): {"energy_kcal_100g": ..., "fat_100g": ...,
"saturated_fat_100g": ..., "carbohydrates_100g": ..., "sugars_100g": ...,
"proteins_100g": ..., "salt_100g": ...}
```

(+ 라벨 사진을 base64로 첨부. 캐시 키 = 사진 URL.)

## 3. QA 초안 생성 — `draft_page()`

페이지 하나의 팩트 목록을 주고, **그 팩트만 근거로 답할 수 있는** 질문을 페이지당 최대 2~3개 만들게 한다. 골드 답이 말뭉치 밖에서 오는 것을 프롬프트 수준에서 차단.

```text
벤치마크 QA 초안 작성. 대상: {name} ({class}), 페이지: {linktype}.
아래 팩트만 골드 근거로 사용할 수 있다:
- {fact_id}: {predicate} = {value}
- ...

이 페이지에서 답할 수 있는 자연스러운 사용자 질문을 최대 {n}개 작성하라. 규칙:
1. 질문은 {한국어|English}로. 답은 질문과 같은 언어로 간결하게.
2. gold_fact_ids에는 위 목록의 fact_id만, 답의 근거가 되는 것만 넣는다.
3. 값이 위 팩트에 없으면 그 질문은 만들지 않는다. 상식으로 답을 지어내지 마라.
4. difficulty: 값 하나를 바로 찾으면 easy, 비교·계산·조건 해석이 필요하면 hard.
JSON: {"questions": [{"question": ..., "gold_fact_ids": [...], "gold_answer": ..., "difficulty": "easy|hard"}]}
```

캐시 키에 팩트 **값**의 해시 포함 — 값이 고쳐지면 낡은 초안이 재사용되지 않는다. (멀티홉 QA는 LLM 없이 프로그램으로 계산.)

## 4. linktype 매핑 감사 — `audit_linktype_mapping.py`

규칙표(결정적)가 정답이지만, 규칙이 GS1 의미론과 어긋난 채 굳는 것을 막는 검증 단계. **앵커링 방지를 위해 현재 매핑을 숨기고(블라인드)** 분류시킨 뒤 코드에서 대조한다. LLM은 지적만 하고 결정은 사람이 한다.

```text
[system]
You audit a fact-to-page mapping table for a GS1 Digital Link benchmark.
GS1 Digital Link linktypes used in this benchmark, with their meanings:
- pip: general product/place information page — identity, description, summary prose.
- nutritionalInformation: nutrition facts (energy, sugars, fat, salt, serving size...).
- allergenInformation: allergens, may-contain traces, and the ingredient list they derive from.
- certificationInfo: certifications and label marks (organic, vegan, gluten-free, eco marks).
- locationInfo: address/coordinates of a place; for products, origin and manufacturing places.
- consumerHandlingStorageInfo: how the consumer should handle and store the product.
- recyclingInfo: packaging materials and recycling guidance.
- openingHoursInfo: opening hours and closed days.
- support: contact points and practical visitor help (information center, parking).
- masterData: structured master attributes page.

For each fact predicate below (with its source field and an example value), judge which
linktype's page it belongs on by GS1 semantics. If none of the listed linktypes fits well,
answer "other:<gs1-linktype-you-would-expect>".
Return ONLY JSON: {"rows": [{"predicate": "...", "best": "...", "reason": "<one short sentence>"}]}
— one row per input, same order.

[user]
[{"predicate": ..., "source_field": ..., "example": ...}, ...]   ← 전 predicate 목록(JSON)
```

## 5. 답변 채점 (LLM judge) — `eval/grade.py`

평가 시스템의 채점자. 파이프라인과 **다른 모델**을 써서 자기 채점 편향을 피한다. 표현이 아니라 의미를 채점하고, 정답 외 추가 정보는 감점하지 않는다.

```text
[system]
You grade answers from a question-answering system against an expected answer.
Given QUESTION, EXPECTED_ANSWER, EXPECTED_VALUES (the source-of-truth values), and SYSTEM_ANSWER,
return a verdict:

- "correct": the system answer conveys the expected value(s). Allow rounding, unit formatting
  differences (22.8 g vs 22.8g), and answering in a different language than the expected answer —
  judge meaning, not wording. Grade ONLY whether the asked value is conveyed: additional
  information beyond the expected answer does NOT lower the verdict unless it contradicts the
  expected value (e.g. expected "allergen: peanuts" + answer also mentioning may-contain traces
  is still "correct").
- "partial": some of the asked values are right, others missing or wrong (a missing expected
  value is "partial"; extra information is not).
- "incorrect": the answer contradicts the expected value(s) or answers something else.
- "no_answer": the answer is a progress/deferral message ("I am checking additional sources...")
  or says it does not know.

Return ONLY JSON: {"verdict": "...", "reason": "<one short sentence>"}

[user]
QUESTION: {질문}
EXPECTED_ANSWER: {골드 답}
EXPECTED_VALUES: {골드 팩트의 원본 값들}
SYSTEM_ANSWER: {파이프라인의 답}
```

---

**LLM을 안 쓰는 곳** (전부 결정적 규칙/코드): 엔티티 선별 게이트(식품·장소), 팩트 추출의 구조화 필드 매핑, 페이지·링크셋 생성, CF 값 치환, validate 7종 검사, verify_batch, 멀티홉 QA 생성, 검수 리포트 생성. 평가 시스템이 돌리는 RAG 파이프라인 자체의 프롬프트(답변 생성·critic 등)는 core(kg_neo4j) 쪽 코드라 이 목록에서 제외.
