# 03 — 파이프라인 워크스루: 예시 엔티티로 따라가는 전 과정 (Heinz Tomato Ketchup, GTIN 50457250)

Heinz 케첩 하나를 골라서, API 호출부터 QA 초안까지 실제로 어떤 일이 일어나는지 따라가 본다.
각 단계마다 세 가지를 보여준다. 무엇을 읽는지, 무엇을 만드는지, 그리고 만든 것을 다음 단계가 어떻게 받아 쓰는지.
Heinz는 음식이라서 거의 모든 단계를 거친다. 장소에만 있는 단계 하나(소개글에서 사실 뽑아내기)는 중간에 광안대교 예시로 끼워 넣었다.

```
[OFF API] ─→ data/raw/off/*.json ─→ work/candidates-food.jsonl ─→ work/selection.yaml (사람 확정)
                    │                                                      │
                    └──────────── scripts/extract_facts.py ←───────────────┘
                                          │
                                          ├─→ facts.jsonl (단일 원천, 407팩트)
                                          ├─→ work/entity-map.json
                                          └─→ work/image-qa-exclusions.json (VLM 라벨 대조)
                                          │
                     scripts/build_fixtures.py (팩트만 읽음, API 안 봄)
                            │                         │ --overrides work/counterfactual-overrides.jsonl
                            ▼                         ▼
                  entities/01-00000050457250/   counterfactual/entities/...
                  (pages/ + media/ + linkset.json + manifest.json)
                            │
                  scripts/validate.py (7종 검사) ─→ work/qa-ambiguous-facts.json
                            │
                  scripts/gen_qa.py draft ─→ work/qa-draft.jsonl + qa-review.csv
                            │
                  (사람 검수) → gen_qa finalize → qa.jsonl  ← 지금 여기 대기 중
```

---

## S1. 후보 수집·채점 — `scripts/select_entities.py food`

먼저 "벤치마크에 쓸 만한 제품"을 찾아야 한다. OFF 검색 API에 미국/영국/월드 세 번의 검색을 던진다. 조건은 두 가지다. 제보가 완성 상태(`states=en:complete`)일 것, 그리고 인기순일 것. 검색 결과에서 영어 제품이 아니거나 GTIN 체크디지트가 틀린 것은 버린다.

살아남은 제품마다 점수를 매긴다. 기준은 단순하다 — 자료가 많을수록 좋은 재료다. 사진 3종(정면/영양라벨/성분표)이 있는지, 영양소가 10개 이상인지, 알러젠·성분 텍스트·라벨·카테고리가 있는지를 각각 점수로 더한다.

그 결과가 `work/candidates-food.jsonl`이다. Heinz의 실제 행은 이렇다:

```json
{"id": "50457250", "name": "Tomato Ketchup", "brand": "Heinz", "score": 7,
 "score_parts": {"image_front": 1, "nutriments_10plus": 2, "allergens": 1,
                 "ingredients_text": 1, "labels": 1, "categories": 1, ...}}
```

마지막 결정은 기계가 아니라 사람이 한다. 이 후보표를 보고 카테고리가 겹치지 않게, 알러젠 종류가 다양하게, 브랜드 쌍(멀티홉용)과 GTIN-8 같은 특수 케이스가 섞이게 10개를 골라 `work/selection.yaml`에 적는다:

```yaml
- "50457250"        # Heinz Tomato Ketchup — 소스류 / celery / GTIN-8 케이스
```

이 파일이 다음 단계의 입력이다.

## S2. 원본 응답 캐시 — `scripts/clients/off_client.py`

selection.yaml에 적힌 GTIN으로 OFF 제품 API를 한 번 호출한다. 응답은 통째로 `data/raw/off/product-50457250.json`에 저장된다(5KB, 26개 키).

이 캐시가 중요한 이유: **이후 모든 단계는 이 파일만 읽는다.** OFF는 크라우드소싱이라 값이 수시로 바뀌는데, 우리는 이 시점의 스냅샷에 고정되므로 언제 다시 돌려도 같은 벤치마크가 나온다.

응답에서 실제로 쓰이는 부분만 발췌하면:

```json
{"product_name": "Tomato Ketchup", "brands": "Heinz",
 "allergens_tags": ["en:celery"], "origins": "Netherlands",
 "serving_size": "1 serving(s) (15 g)",
 "nutriments": {"sugars_100g": 22.8, "sugars_serving": 3.42, ...}}
```

## S3. 팩트 추출 — `scripts/extract_facts.py`

이제 위 JSON을 "원자 팩트"로 쪼갠다. 방식은 regex가 아니라 필드 매핑이다. 코드는 `add(술어, 값, linktype, 소스필드)` 호출을 죽 늘어놓은 것이고, 각 줄이 규칙 하나다:

```python
add("allergens", _clean_tags(p.get("allergens_tags")), "allergenInfo", "allergens_tags")
add("origins",   p.get("origins"),                     "locationInfo",        "origins")
# 영양은 9개 키 × (100g, serving) 루프 → Heinz는 이것만 17팩트
```

여기서 눈여겨볼 점: 각 줄에 linktype이 하나씩 박혀 있다. 즉 **"이 팩트는 이 페이지에만 실린다"는 배치 결정이 추출 규칙에 이미 들어 있다.** 나중에 QA 채점에서 "정답이 있는 페이지"가 유일해지는 이유다.

Heinz에서는 32개 팩트가 나와 `facts.jsonl`에 쌓인다. 한 줄의 실물:

```json
{"fact_id": "food-50457250-sugars_100g", "entity": "01/00000050457250",
 "predicate": "sugars_100g", "value": {"amount": 22.8, "unit": "g"},
 "linktype": "nutritionalInfo",
 "source": {"origin": "off", "field": "nutriments.sugars_100g"}}
```

부산물로 `work/entity-map.json`도 만들어진다. 엔티티 ID와 클래스·이름·source_id를 잇는 표인데, 장소의 GLN 부여 결과도 여기 기록된다.

> **장소 전용 단계 — 소개글에서 사실 뽑아내기 (광안대교 예시)**
>
> 장소는 문제가 하나 더 있다. TourAPI의 overview는 긴 소개글이다. "다리 길이 7.4km"라는 사실이 별도 필드가 아니라 글 속에 파묻혀 있어서, 옮겨 담을 규칙을 쓸 수가 없다.
>
> 그래서 여기만 LLM을 쓴다. 단, 시키는 일을 둘로 제한한다. 문제로 낼 만한 사실(수치, 연도 등)을 찾는 것, 그리고 **그 사실이 적힌 부분을 글에서 그대로 복사해서 내는 것**. 코드는 LLM이 복사해 온 문구를 원문에서 `find()`로 다시 찾아본다. 원문에 정말 있으면 통과, 없으면 그 사실은 버린다. LLM이 지어낸 값은 원문에 없을 테니 이 검색에서 걸러진다.
>
> 통과한 팩트에는 원문 문단 전체(passage)와, 그 문구가 문단의 몇 번째 글자부터 몇 번째 글자까지인지(value_span)가 같이 실린다:
>
> ```json
> {"fact_id": "place-9520000000011-total_length_km", "value": 7.4,
>  "source": {"origin": "tour", "field": "overview",
>             "passage": "총연장 7.4km로 광역시도 66호선인 광안 대로는 ...(문단 전체)",
>             "value_span": [0, 9]}}
> ```
>
> 이 둘은 각자 쓸모가 있다. passage 덕분에 페이지에는 요약이 아니라 원문 문단이 통째로 실린다(요약만 실으면 문제가 너무 쉬워진다). value_span은 나중에 가짜 값 코퍼스를 만들 때 쓴다 — 문단에서 정확히 그 자리만 오려내고 새 값을 붙여 넣으면 되기 때문이다.

## S3.5. VLM 라벨 대조 — `extract_facts.py verify_nutrition_images()`

OFF는 크라우드소싱이라 오타가 실제로 있다. 그래서 영양라벨 사진을 gpt-4o-mini(비전)로 읽어서 텍스트 팩트와 대조해 본다.

대조 결과로 값을 고치지는 않는다. 텍스트가 정본이라는 정책 때문이다(벤치마크는 내부 일관성만 있으면 되고, 값이 실세계와 정확히 같을 필요는 없다). 대신 불일치를 두 파일에 기록한다. 사람이 읽는 `work/fact-conflicts.md`와, 기계가 읽는 `work/image-qa-exclusions.json`(21쌍).

Heinz는 라벨과 텍스트가 일치해서 여기 없다. 반면 Kinder는 라벨에 575kcal라고 찍혀 있는데 텍스트에 93kcal로 입력돼 있어서 5건이 등재됐다.

이 exclusions 파일의 용도는 뒤에 나온다. **"사진과 텍스트가 다른 영양소는 이미지 QA로 만들면 안 된다"는 차단 목록**으로 S10이 사용한다.

## S4~S7. 픽스처 빌드 — `scripts/build_fixtures.py`

여기서부터 성격이 바뀐다. 이 스크립트는 facts.jsonl과 entity-map.json **만** 읽는다. API도, raw 캐시도 안 본다. 팩트가 유일한 원천이라는 설계가 코드 수준에서 강제되는 지점이다.

**S4 게이트.** 엔티티가 벤치마크 자격이 있는지 먼저 검사한다. 음식이면 pip, nutritionalInfo, relatedImage가 반드시 있어야 하고, 선택 linktype도 3개 이상이어야 한다. Heinz의 실제 판정 결과가 manifest에 남아 있다: `{"missing_required": [], "optional_count": 7, "ok": true}`

**S5 페이지.** 엔티티 ID의 해시로 템플릿을 정한다(Heinz는 t3_jsonld가 걸렸다). 그다음 같은 linktype의 팩트끼리 모아 Jinja2 템플릿에 부어 넣는다. 문장을 생성하는 게 아니라 표와 목록으로 조립하는 것이다. Heinz의 25팩트는 이렇게 7페이지로 나뉜다:

| 페이지 | 실린 팩트 |
|---|---|
| pip.html | product_name, brand(관계), categories, quantity, food_group, nova_group (+T3라서 JSON-LD 동봉) |
| nutritionalInfo.html | 영양 9종 × 100g/serving 표 + serving_size, nutriscore_grade |
| allergenInfo.html | allergens=[celery] (+traces 없음 고정 문장) |
| ingredientsInfo.html | ingredients_text, ingredients_analysis |
| hasRetailers.html | retailers=[morrisons] |
| certificationInfo.html | labels (vegetarian, no gluten, ...) |
| locationInfo.html | origins=Netherlands |
| sustainabilityInfo.html | packaging, environmental_score_grade |
| masterData.html | (팩트 아님 — identity-only JSON-LD) |

렌더된 HTML에서 sugars가 실제로 이렇게 나온다:

```html
<tr><td>sugars</td><td>22.8 g</td><td>3.42 g</td></tr>
```

**S6 미디어.** OFF 사진 3장을 내려받아 `media/front.jpg` 등으로 저장한다. 받은 바이트가 JPEG가 아니면 JPEG로 변환한다(`_to_jpeg` — TourAPI가 URL과 다르게 BMP/PNG를 주는 경우가 있어서 2026-07-09에 추가).

**S7 링크셋.** 마지막으로, 방금 만든 페이지와 미디어를 가리키는 linkset.json을 조립한다. 페이지가 존재하는 linktype마다 항목 하나씩, RFC 9264 형태로:

```json
{"linkset": [{"anchor": "https://id.oliot.org/01/00000050457250",
  "https://ref.gs1.org/voc/nutritionalInfo":
    [{"href": "pages/nutritionalInfo.html", "type": "text/html", ...}],
  "https://ref.gs1.org/voc/relatedImage":
    [{"href": "media/front.jpg", "type": "image/jpeg", ...}, ...]}]}
```

이 단계의 산출물이 `entities/01-00000050457250/` 폴더 전체와 `manifest.json`이다. manifest 안에는 placement라는 맵이 있는데, 팩트마다 "어느 페이지에 실렸는지"를 적어둔 역인덱스다:

```json
"placement": {"food-50457250-sugars_100g": "entities/01-00000050457250/pages/nutritionalInfo.html"}
```

이 맵이 나중에 두 군데서 쓰인다. validate가 유일 배치를 검사할 때, 그리고 하네스가 "모델이 정답 페이지를 제대로 찾아갔는가"를 채점할 때.

## S8. Counterfactual 코퍼스 — 같은 빌더 + `--overrides`

이 벤치마크의 약점은 엔티티가 유명하다는 것이다. Heinz 케첩의 당류쯤은 LLM이 문서를 안 읽고도 맞힐 수 있다. 그래서 값을 일부러 바꾼 평행 세계를 하나 더 만든다.

사람이 검토해서 합격시킨 변조표 `work/counterfactual-overrides.jsonl`(29건)이 입력이다. Heinz 몫은 두 줄:

```json
{"fact_id": "food-50457250-sugars_100g",    "value": {"amount": 13.1, "unit": "g"}}
{"fact_id": "food-50457250-sugars_serving", "value": {"amount": 1.97, "unit": "g"}}
```

100g 값만 바꾸면 표 안에서 serving과 비율이 안 맞아 어색해진다. 그래서 둘을 같은 비율로 함께 바꾼다. 장소처럼 값이 문단 속에 있는 경우에는 S3에서 저장해 둔 value_span 좌표로 그 부분만 도려내고 새 값을 이어 붙인다(광안대교 7.4→8.9km).

빌더를 `--overrides`로 다시 돌리면 `counterfactual/entities/`가 나온다. 파일 구조도 미디어도 원본과 완전히 같고, 변조한 값만 다르다:

```
원본: <td>sugars</td><td>22.8 g</td><td>3.42 g</td>
CF  : <td>sugars</td><td>13.1 g</td><td>1.97 g</td>
```

쓰임새는 이렇다. 같은 질문을 두 코퍼스에서 던진다. 답이 22.8과 13.1로 갈리면 모델이 문서를 읽은 것이다. 둘 다 22.8이면 문서를 안 읽고 사전지식으로 답한 것이다.

## S9. 검증 — `scripts/validate.py`

빌드가 끝나면 7종 검사를 돌린다. 스키마, 게이트, 링크셋 무결성, 팩트가 페이지에 실제로 렌더됐는지, 유일 배치가 지켜졌는지, CF 29건이 전부 원본과 달라졌는지, 파이프라인의 `_decode`가 EUC-KR 페이지를 읽을 수 있는지.

이 중 유일 배치 검사가 흥미로운 걸 잡아냈다. 소스 데이터 자체에 같은 토큰이 두 필드에 들어 있는 경우가 4건 있다. 예를 들어 Barilla는 labels에 "made in italy", origins에 "italy"가 따로 있어서, "italy"라는 답이 두 페이지에 동시에 존재한다.

이걸 삭제하면 데이터 충실성이 깨진다. 그래서 팩트는 그대로 두고(오히려 distractor 역할을 한다) `work/qa-ambiguous-facts.json`에 기록만 한다. 이 파일도 exclusions처럼 S10의 골드 금지 목록이 된다.

## S10. QA 초안 — `scripts/gen_qa.py draft`

이제 재료가 다 모였다. facts.jsonl, placement 맵, 그리고 금지 목록 두 개(S3.5의 image-qa-exclusions, S9의 qa-ambiguous).

생성은 페이지 단위로 돈다. 페이지마다:

1. 그 페이지에 실린 팩트 목록을 LLM에 준다. 질문 2~3개를 만들되, **gold_fact_ids는 준 목록 안에서만** 고르게 강제한다. 금지 목록의 팩트는 목록에서 미리 뺀다.
2. 질문 언어는 문서 언어를 따른다(Heinz는 영어). 단, 매 7번째 페이지는 언어를 뒤집어 cross-lingual 문제로 만든다.
3. 생성 후, 영양 QA 중에서 라벨 사진이 있고 exclusion에 안 걸린 것만 골라 `modality: image`로 재태그한다.
4. 멀티홉 5건은 LLM 없이 별도로 만든다. 브랜드·지역 같은 관계 팩트에서 답을 계산식으로 뽑는다.

결과가 `work/qa-draft.jsonl` 230건이고, Heinz는 14건(html 11, image 3)이다. 그중 하나:

```json
{"qa_id": "food-00000050457250-nutritionalinformation-02",
 "entity": "01/00000050457250",
 "question": "What is the amount of sugars in 100g of Tomato Ketchup?",
 "gold_fact_ids": ["food-50457250-sugars_100g"],
 "gold_answer": "There are 22.8 g of sugars in 100g of Tomato Ketchup.",
 "tags": {"modality": "image", "lang": "en", "hop": "single", "difficulty": "easy"}}
```

이 QA 한 건을 뜯어보면 앞의 모든 단계가 들어 있다. gold_fact_ids는 S3이 만든 팩트를 가리킨다. 그 팩트의 placement(S7)가 채점 시 정답 페이지가 된다. modality가 image인 것은 S3.5의 차단 목록을 통과했다는 뜻이다. 그리고 CF 코퍼스(S8)에서 같은 질문을 던지면 답이 13.1g으로 나와야 한다.

같은 내용이 `work/qa-review.csv`로도 내보내진다. 사람이 verdict 열을 채우면(지금 대기 중인 작업), `gen_qa finalize`가 최종 `qa.jsonl`을 확정하고 validate의 5b 검사(골드 팩트 실재성)가 켜진다.

---

## 런타임(피평가 파이프라인)이 보는 것

위에서 만든 것 중 평가받는 파이프라인에게 보여주는 것은 **linkset.json, pages/, media/ 셋뿐이다.**

facts.jsonl, placement 맵, QA 골드는 채점자(하네스)만 본다. 파이프라인 입장에서는 실제 GS1 리졸버를 만났을 때와 똑같다 — 링크셋을 따라가서 문서를 읽는 것 말고는 할 수 있는 게 없다.
