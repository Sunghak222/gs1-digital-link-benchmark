# OFF API 필드 설명과 linktype 매핑

우리가 Open Food Facts(OFF)에서 가져오는 필드 전체를 쉽게 설명하고, 각 필드가 벤치마크의
어느 linktype(제품 페이지 종류)으로 들어가는지 정리한 문서.

- 필드 목록의 출처: `scripts/clients/off_client.py`의 `PRODUCT_FIELDS` (API 요청 시 이 필드들만 달라고 명시)
- 매핑의 출처: `scripts/extract_facts.py`의 `food_facts()` (필드 → 팩트 → linktype 변환 코드)
- 덤프 수확(`scripts/bulk/off_harvest.py`)도 같은 필드 목록을 쓰므로 이 문서가 그대로 적용됨

> **2026-07-27 개정**: GS1 공식 어휘 대조 결과에 따라 3개 명칭을 공식명으로 교정
> (nutritionalInformation→`nutritionalInfo`, allergenInformation→`allergenInfo`,
> recyclingInfo→`sustainabilityInfo`)하고, 원재료를 알레르기 페이지에서 분리해
> `ingredientsInfo` 페이지를 신설했다(첨가물·식이 분석 필드 증량 포함). 동결본
> `releases/v1.0/`은 재현성을 위해 옛 명칭 그대로 유지된다.

## 1. 전체 필드 정리 (코드 선언 순서대로)

`PRODUCT_FIELDS`에 적힌 순서 그대로. linktype 열이 비어 있으면 페이지에 실리지 않는
선별·빌드 전용 필드라는 뜻.

| # | 필드 | 쉬운 설명 | linktype |
|---|---|---|---|
| 1 | `code` | 바코드(GTIN). 제품의 신분증 번호 | — (엔티티 ID의 뼈대) |
| 2 | `product_name` | 제품 이름 (예: "Tomato Ketchup") | pip |
| 3 | `brands` | 브랜드 이름. 쉼표로 여러 개일 수 있음 | pip |
| 4 | `categories_tags` | 제품 분류 태그 목록 (예: en:sauces, en:ketchup) | pip |
| 5 | `countries_tags` | 어느 나라에서 팔리는지 | — (선별 참고·통계) |
| 6 | `nutriments` | 영양소 수치 묶음 (100g당·1회 제공량당·단위) | nutritionalInfo |
| 7 | `serving_size` | 1회 제공량이 얼마인지 (예: "15 ml") | nutritionalInfo |
| 8 | `nutrition_data_per` | 영양 수치가 100g 기준인지 1회 제공량 기준인지 | — (참고용) |
| 9 | `allergens_tags` | 알레르기 유발 성분 목록 (예: en:milk) | allergenInfo |
| 10 | `traces_tags` | "미량 포함 가능" 성분 목록 | allergenInfo |
| 11 | `ingredients_text` | 원재료 전체 문장 (기본 언어판) | ingredientsInfo |
| 12 | `ingredients_text_en` | 원재료 전체 문장 (영어판, 있으면 우선) | ingredientsInfo |
| 13 | `additives_tags` | 첨가물 목록 (예: en:e270 = 젖산) | ingredientsInfo |
| 14 | `ingredients_analysis_tags` | 식이 분석 (채식/비건/팜유 여부) | ingredientsInfo |
| 15 | `labels_tags` | 라벨·인증 목록 (예: en:organic 유기농) | certificationInfo |
| 16 | `origins` | 원재료가 어디서 왔는지 (문장형) | locationInfo |
| 17 | `origins_tags` | 원산지 태그판 (문장형 없을 때 대체) | locationInfo |
| 18 | `manufacturing_places` | 어디서 만들었는지 (공장 위치) | locationInfo |
| 19 | `stores` | 파는 매장 (자유 입력, 예: "Sainsbury's") | hasRetailers |
| 20 | `stores_tags` | 파는 매장 (정규화 태그판, 커버리지 더 넓음) | hasRetailers (자유 입력 없을 때 대체) |
| 21 | `conservation_conditions` | 보관 방법 문장 (예: "개봉 후 냉장 보관") | consumerHandlingStorageInfo |
| 22 | `packaging_tags` | 포장재 종류 목록 (예: en:plastic) | sustainabilityInfo |
| 23 | `image_front_url` | 제품 앞면 사진 주소 | relatedImage (front.jpg로 다운로드 후 linkset에 노출) |
| 24 | `image_nutrition_url` | 영양성분표 라벨 사진 주소 | relatedImage (nutrition-label.jpg) |
| 25 | `image_ingredients_url` | 원재료 표기 사진 주소 | relatedImage (ingredients.jpg) |
| 26 | `unique_scans_n` | 이 제품을 스캔한 사람 수 (인기도) | — (선별 정렬·통계) |
| 27 | `completeness` | OFF가 매긴 데이터 완성도 점수 (0~1) | — (선별 참고) |
| 28 | `states_tags` | 데이터 상태 태그 (en:complete = 등록 완료) | — (덤프 수확 1차 거름망) |
| 29 | `lang` | 등록 데이터의 주 언어 | — (선별 게이트: 영어만 채택) |
| 30 | `quantity` | 내용량 (예: "500 g") | pip |
| 31 | `food_groups` | 식품군 분류 (예: en:sweets) | pip |
| 32 | `nova_group` | 가공 정도 등급 (1=비가공 ~ 4=초가공) | pip |
| 33 | `nutriscore_grade` | 영양 등급 (a~e) | nutritionalInfo (unknown/not-applicable은 제외) |
| 34 | `environmental_score_grade` | 환경 영향 등급 (에코스코어 a-plus~f) | sustainabilityInfo (unknown류 제외) |
| 35 | `purchase_places` | 구매 지역 (예: "France,United Kingdom") | locationInfo |
| 36 | `emb_codes` | 포장 시설 코드 (자유 입력, 예: "FR 55.551.001 CE") | pip (딱 맞는 타입 없음 — 아래 원칙 참조) |
| 37 | `emb_codes_tags` | 포장 시설 코드 (정규화 태그판) | pip (자유 입력 없을 때 대체) |

## 2. linktype별로 다시 보기

모든 명칭은 GS1 공식 어휘(60종)에 있는 것만 사용한다.

| linktype | 페이지 역할 | 들어가는 OFF 필드 | 보유(채택 440) |
|---|---|---|---|
| `pip` | 제품 소개(대표 페이지) | product_name, brands, categories_tags, quantity, food_groups, nova_group, emb_codes(_tags) | 440 |
| `nutritionalInfo` | 영양 정보 | nutriments, serving_size, nutriscore_grade | 440 |
| `allergenInfo` | 알레르기 정보 | allergens_tags, traces_tags | 440 |
| `ingredientsInfo` | 원재료·첨가물·식이 분석 | ingredients_text(_en), additives_tags, ingredients_analysis_tags | 440 |
| `certificationInfo` | 인증 정보 | labels_tags | 440 |
| `hasRetailers` | 판매처 목록 | stores, stores_tags | 365 |
| `locationInfo` | 원산지·제조지·구매지 | origins, origins_tags, manufacturing_places, purchase_places | 일부 |
| `consumerHandlingStorageInfo` | 보관 방법 | conservation_conditions | 4 (희귀) |
| `sustainabilityInfo` | 포장재·재활용·환경 등급 | packaging_tags, environmental_score_grade | 440 |
| `relatedImage` | 사진 3종 (이미지 파일 링크) | image_front_url, image_nutrition_url, image_ingredients_url | — |
| (linktype 없음) | 선별·빌드 전용 | code, countries_tags, unique_scans_n, completeness, states_tags, lang, nutrition_data_per | — |

이 밖에 linkset에는 OFF 필드와 무관하게 빌드 때 만드는 `masterData`(이름+GTIN만 담은
JSON-LD 페이지)와 `defaultLink`(pip.html로 연결)가 추가로 들어간다.

### 처리 방식 요약

- **pip**: "이 제품이 뭔지"를 담는 대표 페이지 — 이름·브랜드·분류에 더해 제품 사양
  (내용량 quantity, 식품군 food_group, 가공도 nova_group)까지. GS1 pip 정의가 "제품 설명·
  **사양**"을 포함해서 여기가 정위치다 (NOVA는 영양 수치가 아니라 가공 분류라 영양 페이지가
  아님). 영양 숫자는 일부러 안 싣는다(정답이 한 곳에만 있도록). brands는 첫 번째 것만 쓰고
  다단계 질문 재료로 관계(relation) 표시. categories는 앞 5개.
- **nutritionalInfo**: 9개 영양소(열량·지방·포화지방·탄수화물·당·식이섬유·단백질·소금·나트륨)를
  100g당/1회 제공량당 **양쪽 다** 실음 — 기준 구분이 필요한 문제를 만들 수 있는 난이도 장치.
  Nutri-Score 등급(a~e를 대문자로 표기)도 여기 — 영양 성분에서 계산되는 등급이라 "영양 팩트"
  범위. "unknown"/"not-applicable" 값 30개는 판정 불가를 글자로 적은 것이라 버림.
- **allergenInfo**: allergens + traces만. traces가 없는 제품(310개)에는
  "No 'may contain' warnings declared for this product."라는 **고정 문장을 페이지에만**
  넣는다(팩트 아님 — OFF의 빈 필드는 "없다"는 주장이 아니라서, 문구도 '선언된 바 없다'로 씀).
- **ingredientsInfo**: 원재료 전문 + 첨가물 목록 + 식이 분석. 분석 태그 중 "-unknown"/"maybe-"
  꼴(판정 불가를 글자로 적은 것)은 버리고 확정 판정만 싣는다. 알레르기와 원재료가 페이지로
  갈라지면서 "알레르기 성분 X는 어느 원재료에서 오나?" 같은 2페이지 연결 문제가 가능해졌다.
- **hasRetailers**: 자유 입력 `stores`가 있으면 그걸 쓰고(표기가 예쁨: "Sainsbury's"),
  없으면 정규화 태그판으로 대체. 매장이 하나도 없는 75개 제품은 페이지 자체가 안 생김.
- **certificationInfo**: labels_tags 전체를 실음. (비인증 라벨 필터링은 차기 개선 후보 — temp.md 검증 §3)
- **locationInfo / consumerHandlingStorageInfo**: 문장형 우선, 있으면 그대로.
- **sustainabilityInfo**: 포장 "재질"은 보관 페이지가 아니라 지속가능성 페이지 소관
  (공식 정의가 "지속가능성 및 재활용"을 포함).

## 3. 공통 정리 규칙

- **빈 값·쓰레기 값 버림**: 값이 비어 있거나, "Not indicated." / "Unspecified" 같은
  "없음"을 글자로 적어둔 값은 팩트로 만들지 않음 (2026-07-20 결정).
- **태그 정리**: `en:organic` → `organic`처럼 언어 접두어를 떼고 하이픈을 공백으로 바꿔서 실음.
- **한 팩트 = 한 페이지**: 각 팩트는 자기 linktype 페이지에만 실린다. 정답이 말뭉치의
  정확히 한 곳에 있게 하는 설계의 기초. (예외: 사진은 텍스트와 답이 겹칠 수 있어 QA에서
  근거 위치를 따로 지정)

## 4. 배치 운영 원칙 (2026-07-27, 상사 지시)

1. **정보 손실 금지가 최우선.** 쓸 만한 필드는 매칭되는 linktype이 애매하다는 이유로
   버리지 않는다.
2. **애매하면 pip.** GS1 정의에 딱 맞는 타입이 없으면 일단 pip(제품 정보 대표 페이지)에
   싣는다. 현재 적용례: `emb_codes`(포장 시설 코드 — traceability는 더 풍부한 이력 정보를
   전제하므로 보류).
3. **재배치는 언제나 싸다.** 매핑은 `extract_facts.py`의 규칙 한 줄이고 페이지·정답키는
   전부 팩트에서 재생성되므로, 나중에 더 맞는 타입이 생기면 한 줄 수정 + 리빌드로 끝난다.

이 원칙에 따라 2026-07-27 기준 **증량 후보로 검토했던 필드는 전부 편입 완료**
(quantity·food_groups·nova_group·nutriscore_grade·environmental_score_grade·
purchase_places·emb_codes — §1·§2 참조). 덤프 원본은 `work/bulk/off-raw-matched*.jsonl.gz`
창고에 보존돼 있어, 새 필드가 필요해지면 13GB 재스캔 없이 몇 초 만에 꺼낼 수 있다.
