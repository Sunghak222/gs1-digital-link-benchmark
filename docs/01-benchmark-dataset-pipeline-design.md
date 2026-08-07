# 01 — GS1 Digital Link 벤치마크 데이터셋 구축 파이프라인 설계

> 작성: 2026-07-08. 상태: 설계 확정. 구현 계획은 `02-implementation-plan.md`.
> **2026-07-27 이후 주의**: 본문의 linktype 명칭 중 3개는 GS1 공식명으로 개정되었고
> (nutritionalInformation→nutritionalInfo, allergenInformation→allergenInfo,
> recyclingInfo→sustainabilityInfo), 원재료는 별도 ingredientsInfo 페이지로 분리,
> hasRetailers 페이지가 추가되었다. 현행 매핑은 `docs/10` 참조. 이 문서는 설계 당시
> 기록 그대로 둔다 (동결본 releases/v1.0/과 짝).
> 목적: 데모 서버(id.oliot.org) 의존 없이, 공개 소스에서 **음식 10 + 장소 10 엔티티**의
> linkset + 리소스 fixture와 QA 데이터셋을 반자동 구축한다.
> 평가 하네스(추후 doc 04)의 입력이 되고, RAG 최적화 논문의 evaluation 기반이 된다.

---

## 0. 목표와 산출물

| 산출물 | 내용 | 규모 |
|---|---|---|
| **facts.jsonl** | 엔티티별 atomic fact 레지스트리 — 모든 하위 산출물의 단일 원천 | 팩트 300~600개 |
| **fixture 코퍼스** | 엔티티별 linkset.json (RFC 9264) + linktype별 HTML 페이지 + 미디어 파일 | 엔티티 20개, linktype 5~8개/엔티티 |
| **counterfactual 코퍼스** | 팩트 일부를 변조해 재생성한 병렬 fixture 1벌 | 변조 팩트 20~30개 |
| **QA 데이터셋** | 질문 + 골드 답변 + 골드 팩트 ID + 태그 | 100~250쌍 |
| **manifest** | 엔티티·리소스·출처·라이선스 목록, 게이트 리포트, 팩트 배치표 | 1파일 |

### 설계 원칙

1. **팩트 중심 아키텍처**: 소스에서 먼저 atomic fact를 추출하고, HTML 페이지·QA·counterfactual은
   전부 facts.jsonl에서 **파생**시킨다. 팩트가 단일 원천이므로 페이지와 골드 답변이 어긋날 수 없고,
   팩트 값을 바꾸면 counterfactual 코퍼스가 기계적으로 나온다.
2. **역선택(source-first)**: 엔티티를 정하고 소스를 찾는 게 아니라, 소스가 잘 커버하는 엔티티를 고른다.
3. **2단 충분성 게이트**: 클래스별 필수 linktype 세트 통과 + 선택 linktype k개 이상.
4. **팩트 유일 배치(순환성 방어)**: 하나의 팩트는 원칙적으로 하나의 linktype 페이지에만 존재.
   골드 근거가 유일해져 "추출"이 아니라 "검색+근거 찾기"를 평가하게 된다.
5. **prior leakage 방어**: 실존 유명 제품을 쓰는 이상 LLM이 사전학습으로 정답(예: 누텔라 영양성분)을
   이미 알 수 있다. counterfactual 코퍼스(§S8)에서 원본과 다른 값을 근거로 답하는지 검증해,
   검색 없이 prior로 맞히는 케이스를 무효화한다.
6. **라이선스 클린**: 전 소스가 재배포 가능 라이선스(ODbL/공공누리/CC) → 데이터셋 자체 공개 가능.
7. **데이터셋은 시스템 내부에 결합하지 않는다**: KG 스키마 슬롯, 라우팅 정책(어느 경로가 답해야 하는가)
   같은 "오늘의 아키텍처"를 골드로 박지 않는다. 그건 데이터셋이 아니라 시스템 테스트의 영역이며,
   아키텍처가 진화할 때마다 데이터셋이 깨지는 결합을 만든다. (phase 2 논의는 §9.)
8. 언어 축 분담: 음식 = 글로벌/영어(OFF), 장소 = 한국/한국어(TourAPI).

---

## 1. 데이터 소스 (전부 실측 검증 완료, 2026-07-08)

### 1.1 Open Food Facts — 음식 10개

- **키가 바코드(=GTIN)**: `code` 필드를 그대로 실제 GTIN으로 사용한다.
- 한국 제품은 커버리지 빈약(태그 3,061개, 필드 다수 공백) → **글로벌 인기 제품 사용** (검색 상위권은 영양·알레르겐·성분·사진이 충실함을 확인).
- 접근: 벌크 작업은 API가 아니라 **공식 데이터 덤프**(CSV/JSONL) 로컬 필터링. 개별 조회 시 `User-Agent` 헤더 필수 (없으면 차단, 검색 API는 불안정 — 실측에서 점검 페이지 반환됨).
- 이미지 3종이 핵심 자산: `image_front_url`(제품 전면), `image_nutrition_url`(**영양라벨 사진 — VLM/OCR성 질의의 골드 소스**), `image_ingredients_url`.
- 라이선스: ODbL (데이터) / CC BY-SA (사진). 출처 표기 필수.
- 데이터 성격 주의: 크라우드소싱이라 전사 오타·구버전 정보가 실재한다 (2026-07-08 실측: 10개 제품에서
  라벨 사진 대비 불일치 21건 — kJ→kcal 혼동, 1회제공량↔100g 혼동 등). **텍스트 필드를 정본**으로 삼는다 —
  벤치마크 값은 내적 일관성만 필요하고 실세계 정확성은 요구하지 않는다. 라벨 사진과의 교차확인(S3)은
  값 교정용이 아니라 **이미지-modality QA 제외 목록**(사진과 골드가 어긋날 쌍) 생성용.

### 1.2 Korea TourAPI (KorService2) — 장소 10개

- 인증: `TOUR_API_KEY` (`.env`에 저장 완료). 일 1,000건 제한 — 충분.
- **검증된 엔드포인트** (base: `https://apis.data.go.kr/B551011/KorService2/`, 공통 파라미터 `serviceKey, MobileOS=ETC, MobileApp=gs1bench, _type=json`):

| 엔드포인트 | 용도 | 실측 (광안대교, contentid=128164) |
|---|---|---|
| `searchKeyword2` | 키워드 → contentid | 주소·좌표·`firstimage`·수정일 반환 확인 |
| `detailCommon2` | 개요(overview) | 서술형 소개문 반환 확인 |
| `detailIntro2` | 운영시간·휴무·주차·문의처 | `usetime`("상시 개방"), `infocenter`, `parking` 확인 |
| `detailImage2` | 이미지 갤러리 | 4장, `originimgurl` 확인 |

- `contentTypeId`로 명소(12)/문화시설(14)/축제(15)/음식점(39) 등 구분 — 우리는 주로 12.
- 값 품질 주의: `usetime`은 "상시 개방", "문의", "시설별 상이" 같은 비정형 값이 흔하다 (광안대교 실측이 그 예).
  운영시간을 게이트 필수로 두지 않는 이유이자(§S4), 팩트 추출 시 정규화 대상.
- 라이선스: 공공누리. 이미지는 `cpyrhtDivCd` 유형별 조건이 달라 **Type1(출처표시)만 채택**.

### 1.3 Wikidata — masterData 전용 보조 소스 (선택)

- 서술형 소스로는 불합격 (key-value). 단 **masterData linktype은 원래 key-value(JSON-LD `gs1:Product`)**이므로,
  Wikidata 클레임 → JSON-LD 변환으로 masterData 페이지를 채우면 **파이프라인의 JSON-LD fast path**
  (팀원 작성, 현재 평가 커버리지 없음)를 평가할 유일한 케이스가 생긴다.
- 실측: 광안대교 Q485443 — 속성 17개 (좌표 P625, 이미지 P18, 개통일, 길이, 공식 웹사이트). 라이선스 CC0.
- v1에서 뺄 경우 masterData linktype은 소스 필드 조합으로 대체 생성.

### 1.4 Wikivoyage — 장소 서술 보강 (선택)

- backgroundInfo/activityIdeas 보강용. 도시 단위 문서라 엔티티별 섹션 절취 필요. CC BY-SA.

---

## 2. 파이프라인 (8 스테이지)

```
S1 역선택 → S2 식별자 → S3 팩트 추출(facts.jsonl) → S4 linktype 배치+게이트
                                   │
                 ┌─────────────────┼──────────────────┐
                 ↓                 ↓                  ↓
          S5 HTML 생성        S6 미디어 수집      S8 counterfactual
                 └────→ S7 linkset 조립 ←┘         (팩트 변조→재생성)
                              ↓
                          S9 QA 생성
```

facts.jsonl(S3)이 허리다: S5의 페이지 본문, S8의 변조본, S9의 골드가 전부 여기서 파생된다.

### S1. 엔티티 역선택

**음식 (OFF 덤프에서):** 충실도 점수로 정렬 → 상위 30 후보 → 사람이 10 선정.

```
score = 2·has(image_nutrition) + 1·has(image_front) + 1·has(image_ingredients)
      + 2·(nutriments 채워진 필드 수 ≥ 10) + 1·has(allergens_tags)
      + 1·has(ingredients_text) + 1·has(labels_tags) + 1·has(categories)
```

선정 시 다양성 강제: 카테고리(음료/과자/소스/유제품…) 최소 5종, 알레르겐 보유 제품 ≥ 5개
(allergenInformation 질의가 성립하려면 알레르겐이 실제로 있어야 함),
**동일 브랜드 제품 2~3개 포함** (실제 관계 기반 multi-hop용 — §S9).

**장소 (TourAPI):** 부산 중심 + 타 지역 혼합. `searchKeyword2`/지역 목록에서:

```
score = 2·has(firstimage) + 2·(detailImage2 갤러리 ≥ 3장) + 2·(overview ≥ 300자)
      + 1·has(usetime) + 1·has(infocenter) + 1·has(parking)
```

**같은 행정구역(부산) 장소를 다수 포함** — 지역 기반 multi-hop용.

### S2. 식별자 부여

| 클래스 | 형식 | 규칙 |
|---|---|---|
| 음식 | `01/{GTIN-13→14}` | **OFF의 실제 GTIN 그대로** (진정성 확보) |
| 장소 | `414/{GLN-13}` | **GS1 데모 예약 프리픽스 952** + 일련번호 + **올바른 체크디지트** (mod-10 계산 함수 포함) |

임의 번호에 실존 기업 프리픽스를 쓰지 않는 것이 목적. 데모 서버의 `414/880...` 형식과 구조 동일.

### S3. 팩트 추출 — facts.jsonl

소스 필드를 페이지로 바로 찍지 않고, 먼저 **atomic fact**로 정규화한다.

```json
{
  "fact_id": "food-3017620422003-sodium-100g",
  "entity": "01/3017620422003",
  "predicate": "sodium_per_100g",
  "value": {"amount": 0.107, "unit": "g"},
  "linktype": "nutritionalInformation",
  "source": {
    "origin": "off",
    "field": "nutriments.sodium_100g",
    "verified_against": "image_nutrition_url",
    "passage": "…(팩트가 포함된 소스 원문 문단, 있을 때)…",
    "value_span": [47, 52]
  }
}
```

**패시지 보존**: 소스에 팩트를 담은 산문이 실재하면(TourAPI overview, Wikivoyage 등) 그 문단을
`source.passage`로 보존하고 값의 위치를 `value_span`으로 표시한다. 페이지는 이 패시지를 원문 그대로
싣고, 팩트는 그 안에 **매몰**된다 — 팩트 요약 시트를 렌더링하면 실웹보다 쉬워지는 문제의 방어책.
counterfactual은 span 위치의 값만 치환하므로 비용이 늘지 않는다.

- `linktype` 필드가 곧 **유일 배치**(설계 원칙 4)의 선언이다: 이 팩트는 그 linktype 페이지에만 실린다
  (pip에는 요약 서술만 허용, 수치·고유값은 배치된 페이지에만).
- `verified_against`: 라벨 사진과 교차 확인한 항목을 표기. 불일치 시에도 텍스트 값을 유지하고
  해당 (엔티티, predicate) 쌍을 이미지-QA 제외 목록에 올린다 (§1.1 정책).
- 관계형 팩트도 같은 스키마로 담는다: `{"predicate": "brand", "value": "Ferrero"}`,
  `{"predicate": "located_in_region", "value": "부산 해운대구"}` — 소스에 실재하는 관계만.
  KG 스키마 슬롯 매핑은 **넣지 않는다** (설계 원칙 7 — KG 스키마는 유동적이고 팀원 영역).

### S4. linktype 배치 + 충분성 게이트

**소스 필드 → GS1 linktype 매핑표 (v1):**

| linktype | 음식 (OFF) | 장소 (TourAPI) | 필수? |
|---|---|---|---|
| `pip` | product_name·brand·categories·개요 조합 | overview (detailCommon2) | **필수** |
| `nutritionalInformation` | nutriments (100g/1회 제공량) | — | **필수(음식)** |
| `relatedImage` | image_front / image_nutrition / image_ingredients | firstimage + detailImage2 갤러리 | **필수** (type=image/*) |
| `locationInfo` | origins·manufacturing_places | 주소·좌표 (searchKeyword2) | **필수(장소)** / 강한 선택(음식) |
| `allergenInformation` | allergens_tags·traces | — | 선택 |
| `consumerHandlingStorageInfo` | conservation_conditions·packaging | — | 선택 |
| `certificationInfo` | labels_tags (유기농 등) | — | 선택 |
| `openingHoursInfo` | — | usetime·restdate (detailIntro2) | 선택 |
| `backgroundInfo` | — | overview 역사 부분 (+Wikivoyage) | 선택 |
| `activityIdeas` | — | 소개·주변정보 | 선택 |
| `support` | — | infocenter·parking | 선택 |
| `masterData` | 필드 → JSON-LD (또는 Wikidata) | 좌표·주소 → JSON-LD | 선택 |

필수 세트 설계 근거:
- **음식 = {pip, nutritionalInformation, relatedImage}.** 원산지(locationInfo)는 OFF에서 공백이 잦아
  필수로 두면 좋은 제품을 대량 탈락시킨다 — 강한 선택으로 두고 QA에서 있는 엔티티만 활용.
- **장소 = {pip, locationInfo, relatedImage}.** 다리·해변·사찰류는 운영시간 개념 자체가 어색하거나
  "상시 개방" 같은 무정보 값이라(§1.2 실측) openingHoursInfo는 선택. 장소 평가의 핵심은
  "어디인가 + 어떤 장소인가 + 시각 근거"다.
- relatedImage를 양쪽 필수로 두는 이유: **이미지 modality 커버리지를 게이트가 구조적으로 보장** —
  20개 엔티티 전부에서 이미지 질의가 성립한다.
- 표의 linktype 명칭은 데모 서버(id.oliot.org) linkset에서 실측된 것만 사용했다. 신규 명칭이 필요하면
  ref.gs1.org/voc 대조 후에만 추가한다 (어휘 임의 창작 금지).

**게이트 (2단):**

1. 필수 세트 (위).
2. 선택 linktype **k ≥ 3** (필수 제외).
3. linktype별 "충분" 판정 = 규칙(팩트 존재+최소 개수) **+ LLM 판정**("이 팩트들만으로 해당 linktype의
   대표 질문에 답할 수 있는가", 근거 인용 강제). 둘 다 통과해야 인정.
4. 탈락 엔티티는 사유와 함께 manifest에 기록 (silent drop 금지) → 후보 30에서 충원.

### S5. HTML 페이지 생성 (linktype당 1페이지, facts.jsonl에서 렌더링)

깨끗한 데이터로 찍은 HTML은 실웹보다 쉬워서 평가가 물러진다 → **템플릿 4종을 엔티티별로 순환 배정**:

| 템플릿 | 특징 | 파이프라인의 어느 경로를 때리나 |
|---|---|---|
| T1 plain | 시맨틱 h1/h2 + 문단 | trafilatura 본문 추출 |
| T2 table-heavy | 영양·운영시간을 `<table>`로 | pandas 표 추출 (step 2) |
| T3 jsonld | `<script type="application/ld+json">` 내장 + 본문 | JSON-LD fast path + extruct |
| T4 noisy | nav/footer/광고성 div/사이드바 추가, **EUC-KR 인코딩**(장소 일부) | 본문 정제·`_decode` 폴백 |

페이지 본문은 "팩트 요약 시트"가 아니라 **패시지 + 표 + distractor의 조립**이다. 난이도는 4개 노브로 제어한다:

1. **패시지 매몰** — 서술형 팩트는 `source.passage` 원문 안에 묻힌 채 실림 (장소·서술형에 주효).
2. **distractor 문단** — 같은 소스의 답 아닌 관련 내용(카테고리 설명·주변 정보 등)을 함께 배치 —
   청킹·검색이 골라내야 할 대상을 만든다.
3. **표 내 혼동 요소** — 수치형 팩트(영양 등)는 실웹처럼 표로 싣되, 100g 기준과 1회 제공량을 병기해
   질문이 묻는 쪽을 골라 읽어야 하게 한다 (수치를 억지로 산문화하는 것은 오히려 비현실적).
4. **구조 노이즈** — T4 템플릿의 nav/광고/인코딩 변형.

팩트-페이지 정합성은 팩트에서 렌더링하므로 생성 시점에 구조적으로 보장된다 (§4 검증에서 회귀 확인만).

> **TODO (보류)**: 산문이 없는 소스(OFF 서술형 linktype)의 **LLM 증량** — 슬롯 템플릿 방식
> (LLM이 최종 텍스트가 아닌 `{{value}}` 슬롯 템플릿을 1회 생성 → 원본/CF 동일 템플릿 렌더링으로
> 결정성 확보) + validate의 오염 스캔(생성 텍스트에 facts.jsonl 밖 수치·연도·고유명사 금지).
> 비용은 무시 가능(총 수십만 토큰) — v1에서는 distractor·표 혼동으로 난이도 확보하고, 증량은 보류.

### S6. 미디어 수집

- 이미지: OFF 3종 + TourAPI 갤러리 (원본 다운로드, `Accept: image/*` 헤더 포함 — Next.js류 프록시 400 방지).
  엔티티당 2~4장 캡. 파일명·출처·라이선스 manifest 기록.
- linkset에 `type=image/jpeg|png`로 **정식 등록** (데모 서버의 생선 사례처럼 페이지에만 박고 linkset 누락 금지).
- 이미지에 담긴 정보(영양라벨 수치 등)도 facts.jsonl에 팩트로 등록하되 `source.origin: "image"`로 표기 —
  이미지 modality QA의 골드가 된다.
- 영상: 자동 수집 대상 없음(정직하게 한계 명시). v1은 기존 티셔츠 mp4 재사용 또는 1~2개 수동 제작(장소 B-roll).

### S7. linkset 조립

- RFC 9264 JSON, **id.oliot.org 응답과 동일 구조** (실측 fixture 샘플 보유).
- anchor = `https://id.oliot.org/{aiPath}` 형식 유지 (fixture 경로라 리졸버 등록 불필요 — 파이프라인의
  `resolver_base` allowlist와 정합).
- 각 링크: `href`(로컬 fixture 상대경로 또는 file 서빙 URL), `title`(한/영), `type`(정확한 MIME — 라우팅이 이걸로 결정됨), `hreflang`.

### S8. counterfactual 코퍼스 (prior leakage 방어)

facts.jsonl에서 **검증 대상 팩트 20~30개를 변조**하고 S5~S7을 재실행해 병렬 fixture 1벌을 만든다.

```json
// counterfactual/facts.overrides.jsonl
{"fact_id": "food-3017620422003-sodium-100g", "value": {"amount": 0.42, "unit": "g"}}
{"fact_id": "place-9521234567893-parking",    "value": "불가능"}
```

- 변조 대상 선정: LLM prior가 강할 팩트 우선 (유명 제품 영양수치, 유명 장소 기본 정보).
  변조값은 그럴듯한 범위 내 + 원본과 명확히 구분되는 값.
- 평가 방식: **같은 QA를 두 코퍼스에서 실행** — 원본에서 원본값, counterfactual에서 변조값을 답해야 한다.
  counterfactual에서 원본값을 답하면 prior leakage 또는 grounding 실패로 판정.
- 팩트 중심 아키텍처 덕에 비용은 "오버라이드 파일 1개 + 재생성 실행"뿐이다.
- (KG 캐시가 낀 stale 시나리오는 phase 2 — §9.)

### S9. QA 생성 (LLM 초안 + 사람 전수 검수)

스키마 — 골드는 **팩트 ID가 1차, 답변 문자열이 2차**:

```json
{
  "qa_id": "food-nutella-nutri-01",
  "entity": "01/3017620422003",
  "question": "이 제품 100g당 나트륨은 얼마야?",
  "gold_fact_ids": ["food-3017620422003-sodium-100g"],
  "gold_answer": "100g당 0.107g입니다.",
  "tags": {"modality": "html|image|video", "lang": "ko|en",
           "hop": "single|multi", "difficulty": "easy|hard"}
}
```

- `gold_fact_ids` → 근거 linktype·페이지가 facts.jsonl에서 자동 유도된다 (retrieval hit 판정용).
  `gold_answer`는 답변 품질 판정용 — 역할이 다르므로 병존.
- 생성: linktype 페이지별로 LLM이 질문 2~3개 초안 (근거 팩트 ID 인용 강제) → **사람이 100% 검수**
  (음식은 라벨 사진 대조).
- 구성 목표: 엔티티당 8~12개, 총 160~240. modality 배분 — html 70%, image 25%(영양라벨 판독, 갤러리 묘사), video 5%.
- **multi-hop 10~20개는 실재 관계 기반으로만**: S1에서 확보한 동일 브랜드 제품군("이 브랜드의 다른 제품 중
  알레르겐 없는 것은?"), 동일 행정구역 장소군("해운대구 명소 중 주차 가능한 곳은?") — 관계 팩트가
  facts.jsonl에 실재하는 질문만 허용. 가상 공장·조직 같은 합성 관계 엔티티는 v1에서 만들지 않는다
  (실존 데이터 기반이라는 벤치마크 정체성 유지 — 도입하려면 phase 2에서 synthetic 마킹과 함께).

---

## 3. fixture 레이아웃

```
benchmark/
├── manifest.json                  # 엔티티·출처·라이선스·게이트 리포트·팩트 배치표·생성 파라미터
├── facts.jsonl                    # atomic fact 레지스트리 (단일 원천)
├── qa.jsonl                       # QA 데이터셋 (gold_fact_ids 참조)
├── entities/
│   └── 01-3017620422003/          # aiPath 슬러그
│       ├── linkset.json
│       ├── pages/
│       │   ├── pip.html
│       │   ├── nutritionalInformation.html
│       │   └── …
│       └── media/
│           ├── front.jpg
│           └── nutrition-label.jpg
├── counterfactual/
│   ├── facts.overrides.jsonl      # 변조 팩트 (이것만이 원본과의 차이)
│   └── entities/…                 # 오버라이드 적용해 재생성한 병렬 fixture
└── scripts/
    ├── select_entities.py         # S1 (OFF 덤프 필터 + TourAPI 스코어링)
    ├── extract_facts.py           # S3 (+ 라벨 사진 교차확인 보조)
    ├── build_fixtures.py          # S4~S7 (--overrides 옵션으로 S8 겸용)
    ├── gen_qa.py                  # S9 초안 생성
    └── validate.py                # §4 검증 일괄 실행
```

위치: 저장소 루트 `benchmark/` (kg_neo4j zip 밖 — 파이프라인 코드가 아니라 데이터셋 툴체인이므로 병합 대상 아님.
하네스가 소비할 땐 경로만 주입).

## 4. 검증 (validate.py)

1. linkset JSON 스키마 (RFC 9264 필수 필드, MIME 유효성).
2. GTIN/GLN 체크디지트.
3. 링크 무결성: linkset의 모든 href가 실재 파일/URL인지, 페이지 내 이미지 참조가 media/에 있는지.
4. 게이트 리포트: 엔티티별 필수 세트·k 충족 여부 표.
5. 팩트 무결성: 모든 gold_fact_ids가 facts.jsonl에 존재하고, 각 팩트가 배치된 linktype 페이지에
   실제로 렌더링됐는지 + **다른 linktype 페이지에 유출되지 않았는지** (유일 배치 검사).
6. counterfactual 발산 검사: 오버라이드된 팩트가 원본/변조 코퍼스에서 **다른 값으로** 렌더링됐는지.
7. 인코딩: T4 EUC-KR 페이지가 `_decode` 폴백으로 정상 복원되는지.

## 5. 평가 하네스 연결 (요약 — 상세는 doc 04에서)

하네스는 두 층으로 나뉘되, **점수와 진단을 분리**한다:

**A. 블랙박스 점수** (안정 계층) — 쿼리 in → 답변+출처 out 경계에만 훅.
- retrieval hit@k (gold_fact_ids → 근거 linktype 자동 유도, 결정적) / 인용 정확도 /
  faithfulness (LLM judge, Ragas류 재사용) / latency / **counterfactual 일치율** (§S8 — 두 코퍼스 교차 실행).
- 파이프라인 내부가 리팩터링돼도 이 층은 깨지지 않는다.

**B. trace 진단** (best-effort 계층) — 새 훅을 심지 않고 **기존 `verbose_log` 출력의 파서**로 구현.
- 후보 선택 action(traverse/preprocess), 인덱싱된 리소스, 검색 모드(dense/bm25), 구간별 latency 등
  로그에 이미 찍히는 것만 수집해 실패 원인 귀속(routing 실패 vs retrieval 실패 vs 생성 실패)에 사용.
- 로그 포맷이 바뀌면 해당 지표만 결측되고 A층은 무사하다 — 유지보수 결합을 의도적으로 낮춘 구조.
- trace 지표는 **진단 전용이며 절대 골드 단언 대상이 아니다** (설계 원칙 7).

실행 리포트에 설정 지문 포함: RECIPE_VERSION들, retrieval_mode, vector_backend, 모델명, 코퍼스(원본/CF).

## 6. 구현 순서와 견적

| 단계 | 작업 | 견적 |
|---|---|---|
| 1 | OFF 덤프 다운로드 + S1 스코어러 → 후보 30 리스트 | 0.5일 |
| 2 | TourAPI 클라이언트 + 장소 후보 스코어링 | 0.5일 |
| 3 | **사람 작업: 후보에서 10+10 선정** | 1~2시간 |
| 4 | extract_facts.py (S3, 라벨 교차확인 포함) | 0.5일 |
| 5 | build_fixtures.py (S4~S7, 템플릿 4종, 체크디지트, --overrides) | 1~1.5일 |
| 6 | counterfactual 오버라이드 작성 + 재생성 (S8) | 0.5일 |
| 7 | validate.py | 0.5일 |
| 8 | S9 QA 초안 생성 + **사람 전수 검수** | 1일 + 검수 반나절 |
| 계 | | **5~6일** (검수 포함) |

## 7. 리스크 및 결정 대기

| 항목 | 내용 | 상태 |
|---|---|---|
| OFF 덤프 크기 | 전체 JSONL 수십 GB — 필드 필터링 스트리밍 처리 필요 | 구현 시 처리 |
| linktype 어휘 | 신규 명칭은 ref.gs1.org/voc 대조 후에만 (현 표는 데모 서버 실측분) | 구현 시 처리 |
| TourAPI 이미지 라이선스 | `cpyrhtDivCd` Type1(출처표시)만 채택 | 규칙 반영 |
| 영상 modality | 자동 수집 불가 — 수동 1~2개로 한정, 한계로 명시 | 확정 |
| Wikidata masterData 포함 여부 | JSON-LD fast path 평가 가치 vs 작업량 | **사용자 결정 대기** |
| QA 언어 정책 | 질문 언어를 문서 언어와 교차시킬지 (en 문서에 ko 질문 = cross-lingual 평가) | **사용자 결정 대기** |

## 8. 이 벤치마크가 평가하는 것 / 안 하는 것

| 평가함 (v1) | 평가 안 함 (의도적) |
|---|---|
| linkset 순회 + linktype 라우팅 | KG 캐시 hit/miss, write-back (phase 2 — §9) |
| HTML 4경로 추출 (본문/표/JSON-LD/노이즈) | 라우팅 정책의 "올바름" (아키텍처 유동) |
| 이미지 VLM (영양라벨 판독 포함) | 합성 관계 엔티티 기반 deep multi-hop |
| 근거 grounding (팩트 단위) + prior leakage (CF) | 실시간 웹 변동 대응 (fixture는 정적) |
| 실재 관계 multi-hop (브랜드·지역) | |
| 한/영 + cross-lingual (정책 확정 시) | |

## 9. Phase 2 후보 (v1 범위 밖, 순서대로)

1. **KG 캐시 에피소드** (`episodes.jsonl`): 동일 팩트에 대한 연속 질의로 warm cache·write-back을 평가.
   v1에서 제외한 이유 — (a) KG 상태 리셋/관측 계측이 선행돼야 하고(팀원 코드 포함), (b) "2번째 질의는
   KG가 답해야 한다"는 기대는 ground truth가 아니라 현 아키텍처의 정책이라, 데이터셋 골드로 박으면
   아키텍처 진화마다 데이터셋이 깨진다. 벤치마크가 아닌 **시스템 테스트**로 소량(5~10개) 작성 예정.
   같은 facts.jsonl을 재사용하므로 미뤄도 데이터 비용은 없다.
2. **stale source 시나리오**: counterfactual 코퍼스를 "시점 v1→v2"로 해석해 KG의 구버전 답변을 검출 —
   1번 인프라에 의존.
3. **합성 운영 엔티티** (공장·인증 관계): 실재 관계 hop(§S9)으로 부족할 때만, synthetic 마킹과 함께 도입.
4. HTML 내장 이미지 → VLM 경로가 파이프라인에 구현되면 T4 템플릿에 이미지 임베드 케이스 추가.
