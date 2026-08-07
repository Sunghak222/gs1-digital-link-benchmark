



## 결론

현재 매핑은 **벤치마크용 정보 분할 방향은 좋지만, GS1 공식 linkType 기준으로는 그대로 사용하면 안 된다.** 정확한 명칭 오류가 3개 있고, `allergenInformation`에 원재료를 함께 넣은 부분도 GS1 의미와 맞지 않는다. 현재 문서의 필드 구성과 보유율은 확장하기에 충분하다. fileciteturn0file0

특히 GS1 Resolver 1.2에서는 linkType이 단순 라벨이 아니라 실제 라우팅 조건이다. 예를 들어 공식 요청값은 `linkType=gs1:nutritionalInfo`이며, 해당 타입이 없으면 Resolver가 404를 반환한다. 따라서 이름 차이는 단순 표기 문제가 아니다. citeturn627548view0turn201758view0

---

## 1. 현재 매핑 검증

| 현재 문서 | 판정 | 공식 linkType | 수정 사항 |
|---|---:|---|---|
| `pip` | 적합 | `gs1:pip` | 그대로 사용 |
| `nutritionalInformation` | **명칭 오류** | `gs1:nutritionalInfo` | 반드시 이름 변경 |
| `allergenInformation` | **명칭 오류 + 내용 혼합** | `gs1:allergenInfo` | 이름 변경, 원재료 분리 |
| `certificationInfo` | 조건부 적합 | `gs1:certificationInfo` | 인증에 해당하는 label만 필터링 |
| `locationInfo` | 조건부 적합 | `gs1:locationInfo` | 원산지·제조지가 실제 위치 정보로 구성돼야 함 |
| `consumerHandlingStorageInfo` | 적합 | `gs1:consumerHandlingStorageInfo` | 그대로 사용 |
| `recyclingInfo` | **공식 타입 아님** | `gs1:sustainabilityInfo` | 반드시 이름 변경 |

GS1 공식 vocabulary에는 `allergenInfo`, `ingredientsInfo`, `nutritionalInfo`, `certificationInfo`, `consumerHandlingStorageInfo`, `locationInfo`, `sustainabilityInfo`가 각각 별도 타입으로 정의돼 있다. `recyclingInfo`는 현재 공식 목록에 없으며, 재활용과 지속가능성은 `sustainabilityInfo`가 담당한다. citeturn462280view0

### 반드시 수정할 세 가지 이름

```text
nutritionalInformation  → nutritionalInfo
allergenInformation     → allergenInfo
recyclingInfo           → sustainabilityInfo
```

내부 Python enum이나 디렉터리 이름은 자유롭게 사용할 수 있지만, 최종 linkset의 `rel` 또는 `linkType` 값은 다음처럼 정확해야 한다.

```text
gs1:nutritionalInfo
gs1:allergenInfo
gs1:sustainabilityInfo
```

---

## 2. 가장 중요한 내용 수정: 원재료와 알레르기 분리

현재는 아래 세 필드가 모두 알레르기 페이지에 들어간다.

```text
allergens_tags
traces_tags
ingredients_text(_en)
```

GS1에는 원재료 전용 타입인 `gs1:ingredientsInfo`가 별도로 존재하며, 정의도 “원재료에 관한 사실로 연결되는 링크”다. 따라서 원재료 문장을 `allergenInfo`에 넣는 현재 구조는 공식 분류상 부정확하다. citeturn216879search1turn462280view0

권장 분리는 다음과 같다.

| linkType | 필드 |
|---|---|
| `gs1:allergenInfo` | `allergens_tags`, `traces_tags` |
| `gs1:ingredientsInfo` | `ingredients_text_en`, `ingredients_text`, 첨가물 정보 |

`additives_n`도 알레르기 정보가 아니다. 추가한다면 `ingredientsInfo`로 보내야 한다. 가능하면 첨가물 **개수만** 넣지 말고 `additives_tags`도 API 필드에 추가하는 편이 질문 생성에 유리하다.

```text
첨가물이 몇 개입니까?          ← additives_n
어떤 첨가물이 포함됩니까?       ← additives_tags
```

두 번째가 실제 검색·추론 평가에는 더 가치 있다.

---

## 3. `certificationInfo`는 labels_tags 전체를 넣으면 안 됨

`labels_tags`에는 실제 인증뿐 아니라 식이 특성, 마케팅 문구, 제품 주장도 섞일 수 있다.

따라서 다음처럼 처리해야 한다.

### `certificationInfo`에 넣어도 되는 것

- 유기농 인증
- Fairtrade
- Rainforest Alliance
- MSC·ASC처럼 명시적인 인증 체계
- 공식 인증기관이나 표준에 연결되는 라벨

### 무조건 인증으로 보면 안 되는 것

- vegetarian
- vegan
- no preservatives
- gluten-free
- made in France
- low sugar

이런 값은 인증 여부가 별도로 확인되지 않는 한 `pip` 또는 `ingredientsInfo` 쪽이 더 적절하다.

즉 현재의

```python
labels_tags → certificationInfo
```

는 다음으로 바꾸는 것이 좋다.

```python
certification_labels = filter_certification_taxonomy(labels_tags)
```

필터링 후 값이 하나도 없으면 `certificationInfo` 페이지를 생성하지 않는다.

---

## 4. 추가 가능한 linkType

### 우선순위 1: 바로 추가할 수 있는 타입

#### ① `gs1:ingredientsInfo`

이미 필드가 있으므로 즉시 추가 가능하다.

```text
ingredients_text_en
ingredients_text
additives_n
추가 권장: additives_tags
추가 권장: ingredients_analysis_tags
```

OFF는 vegan·vegetarian·palm-oil 관련 분석 태그와 제조시설 코드 등의 필드를 제공한다. citeturn917458search0turn917458search2

---

#### ② `gs1:hasRetailers`

문서상 `stores` 보유율이 82%이므로 상당히 안정적으로 생성할 수 있다. 공식 정의도 “판매점 목록으로 연결되는 링크”이므로 정확히 맞는다. citeturn216879search2turn462280view0

```text
stores → gs1:hasRetailers
```

예시 질문:

```text
이 제품은 어느 매장에서 판매됩니까?
Carrefour에서 살 수 있습니까?
이 제품을 판매하는 소매업체를 알려주세요.
```

기존 프로젝트 문서나 KG 코드에 `whereToBuy`가 있다면 재검토해야 한다. 현재 공식 vocabulary에는 `hasRetailers`가 있으며, `whereToBuy`는 현재 공식 목록에서 확인되지 않는다.

---

#### ③ `gs1:relatedImage`

현재 다운로드하고만 있는 이미지 3종을 공식 linkType으로 노출할 수 있다.

```text
image_front_url       → relatedImage
image_nutrition_url   → relatedImage
image_ingredients_url → relatedImage
```

GS1은 `relatedImage`를 식별된 제품과 관련된 이미지 전반에 사용하는 타입으로 정의한다. citeturn462280view0

세 이미지 모두 동일한 linkType을 사용하되 `title`, MIME type, 파일명 등으로 구분하면 된다.

```json
{
  "rel": "gs1:relatedImage",
  "title": "Front package image",
  "type": "image/jpeg"
}
```

```json
{
  "rel": "gs1:relatedImage",
  "title": "Nutrition label image",
  "type": "image/jpeg"
}
```

멀티모달 RAG 평가용으로도 가치가 높다. 다만 영양 텍스트 페이지와 영양 라벨 이미지가 같은 답을 포함하므로, 이미지 QA subset을 따로 두거나 각 질문의 canonical evidence를 명시해야 한다.

---

### 우선순위 2: 조건부로 추가

#### ④ `gs1:traceability`

후보 필드:

```text
origins
origins_tags
manufacturing_places
emb_codes
```

공식 `traceability`는 사람이나 시스템이 소비하는 추적·이력 정보를 위한 타입이다. EPCIS 저장소 자체라면 별도의 `epcisRepository` 타입을 사용한다. citeturn462280view0

하지만 단순히 다음 정도만 있다면:

```text
Origin: France
Manufacturing place: Lyon
```

`locationInfo`가 더 자연스럽다.

다음처럼 시설 코드와 provenance를 함께 구성할 수 있을 때만 `traceability`를 추천한다.

```text
Ingredient origin
Manufacturing country/location
EU establishment or packaging facility code
Facility identification
```

따라서 단계는 다음이 적절하다.

```text
1단계: origins + manufacturing_places → locationInfo
2단계: emb_codes를 실제 시설 정보로 해석 가능
     → origins + facility + code를 traceability로 이동
```

같은 사실을 `locationInfo`와 `traceability` 양쪽에 복제하지 않는 것이 좋다.

---

## 5. 아직 안 쓰는 필드의 권장 위치

| OFF 필드 | 권장 linkType | 판단 |
|---|---|---|
| `quantity` | `pip` | 제품 기본 사양 |
| `food_groups_en` | `pip` | 제품 분류 |
| `additives_n` | `ingredientsInfo` | 알레르기 아님 |
| `nova_group` | `pip` | 가공도 분류이지 영양성분 자체는 아님 |
| `nutriscore_grade` | `nutritionalInfo` | 영양 기반 평가 |
| `environmental_score_grade` | `sustainabilityInfo` | 매우 적합 |
| `stores` | `hasRetailers` | 공식 의미와 직접 대응 |
| `purchase_places` | `locationInfo` | 구매 지역 정보 |
| `emb_codes` | 조건부 `traceability` | 시설 코드가 해석될 때 |
| `countries_tags` | `pip` 또는 미사용 | 판매 국가이지 원산지가 아님 |
| `packaging_tags` | `sustainabilityInfo` | 현재 방향 적합 |

특히 `nova_group`은 `nutritionalInfo`보다 `pip`가 낫다. NOVA는 단백질·지방처럼 영양 수치가 아니라 가공 정도에 관한 제품 분류이기 때문이다.

---

## 6. 최종 권장 linkType 구성

### 안정적으로 만들 수 있는 10종

```text
1. gs1:pip
2. gs1:nutritionalInfo
3. gs1:ingredientsInfo
4. gs1:allergenInfo
5. gs1:certificationInfo
6. gs1:locationInfo
7. gs1:consumerHandlingStorageInfo
8. gs1:sustainabilityInfo
9. gs1:hasRetailers
10. gs1:relatedImage
```

현재 7종 구조에서:

- 공식 명칭 수정
- 원재료 페이지 분리
- 판매처 페이지 추가
- 이미지 linkType 추가

를 하면 **실질적인 공식 콘텐츠 타입 10종**까지 무리 없이 늘어난다.

### 데이터 품질이 확보되면 11종

```text
11. gs1:traceability
```

`emb_codes`나 제조시설 식별 정보를 실제 provenance 정보로 풀어낼 수 있을 때 추가한다.

---

## 7. `defaultLink`도 설정해야 함

`defaultLink`는 별도 콘텐츠 페이지를 새로 만드는 타입이 아니다. 일반적으로 PIP 대상 URL에 `pip`와 `defaultLink` 역할을 함께 부여한다.

```text
Product PIP URL
├─ gs1:pip
└─ gs1:defaultLink
```

GS1 Resolver 표준은 각 요청 URI에 동일하거나 상위 granularity에서 하나의 default link가 존재하도록 요구한다. 공식 예시에서도 제품 페이지가 `gs1:pip`이면서 `gs1:defaultLink`로 등록된다. citeturn627548view0

따라서 콘텐츠 타입 수에는 세지 않지만 resolver 검증 항목에는 반드시 넣어야 한다.

---

## 8. 억지로 늘리면 안 되는 타입

다음은 공식 타입이더라도 현재 OFF 데이터로 생성하면 부정확해질 가능성이 크다.

| 타입 | 제외 이유 |
|---|---|
| `dpp` | 여러 OFF 필드를 모은 HTML을 DPP라고 부르는 것은 과도함 |
| `smartLabel` | 실제 SmartLabel 서비스 페이지에만 사용 |
| `recallStatus` | 실제 리콜 데이터나 API가 없음 |
| `safetyInfo` | 알레르기 정보를 중복시켜 타입 수만 늘리게 됨 |
| `recipeInfo` | 제품별 실제 레시피 URL이 없음 |
| `masterData` | 생성은 가능하지만 모든 팩트를 중복해 검색을 지나치게 쉽게 만듦 |
| `instructions` | 실제 조리·사용 설명 필드가 확보된 제품에만 사용 |

`masterData`는 정규화된 OFF JSON 페이지를 만들면 기술적으로 유효하지만, core benchmark가 아니라 **structured retrieval 전용 subset**으로 분리하는 편이 낫다.

---

## 9. 벤치마크 설계 규칙도 약간 수정해야 함

현재 문서의 **“한 팩트 = 한 페이지”**는 synthetic benchmark 관리에는 편하지만 GS1 linkType의 원래 의미와는 조금 다르다.

GS1 linkType은 팩트를 상호배타적으로 분할하는 taxonomy가 아니라 **링크 대상 리소스의 목적**을 표현한다. 공식 `pip` 정의도 제품 설명·사양·추가 정보 등을 포함할 수 있다고 되어 있어, 실제 GS1 환경에서는 PIP와 영양·원재료 페이지가 일부 겹칠 수 있다. citeturn462280view0

따라서 다음 규칙이 더 현실적이다.

```text
기존:
정답은 말뭉치의 정확히 한 곳에만 존재

권장:
각 질문은 하나의 canonical answer source를 갖는다.
다른 리소스에 supporting evidence가 일부 존재할 수 있다.
```

이렇게 해야 `ingredientsInfo`, `allergenInfo`, `relatedImage`, `pip`가 현실적으로 공존할 수 있다.

### 최종 판단

**당장 적용할 수정안은 `8개 텍스트 linkType + hasRetailers + relatedImage = 총 10종`이다.**  
`traceability`는 시설 코드 해석 품질을 확인한 뒤 11번째 타입으로 추가하는 것이 가장 타당하다.

---

# [2026-07-31] drug/label 원본 레코드 전체 — Children's TYLENOL (발표 샘플용)

창고 pharma-raw-matched.jsonl.gz에서 추출, 무삭제 전문. 필드 24개, set_id 3162733b-9382-39f1-e063-6294a90ac420

```json
{
  "spl_product_data_elements": [
    "CHILDRENS TYLENOL Acetaminophen SUCROSE XANTHAN GUM MICROCRYSTALLINE CELLULOSE POTASSIUM SORBATE SUCRALOSE ACETAMINOPHEN ACETAMINOPHEN CARBOXYMETHYLCELLULOSE SODIUM, UNSPECIFIED ANHYDROUS CITRIC ACID GLYCERIN SORBITOL SOLUTION WATER White to off-white"
  ],
  "spl_unclassified_section": [
    "Drug Facts"
  ],
  "active_ingredient": [
    "Active ingredient Active ingredient (in each 5 mL) Acetaminophen 160 mg"
  ],
  "purpose": [
    "Purpose Purpose Pain reliever/fever reducer"
  ],
  "indications_and_usage": [
    "Uses Uses temporarily: ■ reduces fever ■ relieves minor aches and pains due to: -the common cold -flu -headache -sore throat -toothache"
  ],
  "warnings": [
    "Warnings Liver warning Liver warning: This product contains acetaminophen. Severe liver damage may occur if your child takes ■ more than 5 doses in 24 hours, which is the maximum daily amount ■ with other drugs containing acetaminophen Allergy alert: acetaminophen may cause severe skin reactions. Symptoms may include: ■ skin reddening ■ blisters ■ rash If a skin reaction occurs, stop use and seek medical help right away. Sore throat warning: if sore throat is severe, persists for more than 2 days, is accompanied or followed by fever, headache, rash, nausea, or vomiting, consult a doctor promptly. Do not use ■ with any other drug containing acetaminophen (prescription or nonprescription). If you are not sure whether a drug contains acetaminophen, ask a doctor or pharmacist. ■ if your child is allergic to acetaminophen or any of the inactive ingredients in this product Ask a doctor before use if your child has liver disease Ask a doctor or pharmacist before use if your child is taking the blood thinning drug warfarin When using this product do not exceed recommended dose (see overdose warning) Stop use and ask a doctor if ■ pain gets worse or lasts more than 5 days ■ fever gets worse or lasts more than 3 days ■ new symptoms occur ■ redness or swelling is present These could be signs of a serious condition. Keep out of reach of children. Overdose warning: In case of overdose, get medical help or contact a Poison Control Center right away. (1-800-222-1222) Quick medical attention is critical for adults as well as for children even if you do not notice any signs or symptoms."
  ],
  "do_not_use": [
    "Do not use ■ with any other drug containing acetaminophen (prescription or nonprescription). If you are not sure whether a drug contains acetaminophen, ask a doctor or pharmacist. ■ if your child is allergic to acetaminophen or any of the inactive ingredients in this product"
  ],
  "ask_doctor": [
    "Ask a doctor before use if your child has liver disease"
  ],
  "ask_doctor_or_pharmacist": [
    "Ask a doctor or pharmacist before use if your child is taking the blood thinning drug warfarin"
  ],
  "when_using": [
    "When using this product do not exceed recommended dose (see overdose warning)"
  ],
  "stop_use": [
    "Stop use and ask a doctor if ■ pain gets worse or lasts more than 5 days ■ fever gets worse or lasts more than 3 days ■ new symptoms occur ■ redness or swelling is present These could be signs of a serious condition."
  ],
  "keep_out_of_reach_of_children": [
    "Keep out of reach of children."
  ],
  "overdosage": [
    "Overdose warning: In case of overdose, get medical help or contact a Poison Control Center right away. (1-800-222-1222) Quick medical attention is critical for adults as well as for children even if you do not notice any signs or symptoms."
  ],
  "dosage_and_administration": [
    "Directions Directions ■ this product does not contain directions or complete warnings for adult use ■ do not give more than directed (see overdose warning) ■ shake well before using ■ mL = milliliter ■ find right dose on chart below. If possible, use weight to dose; otherwise, use age. ■ remove the child protective cap and squeeze your child’s dose into the dosing cup ■ repeat dose every 4 hours while symptoms last ■ do not give more than 5 times in 24 hours Weight (lb) Age (yr) Dose (mL)* under 24 under 2 years ask a doctor 24-35 lbs 2-3 years 5 mL 36-47 lbs 4-5 years 7.5 mL 48-59 lbs 6-8 years 10 mL 60-71 lbs 9-10 years 12.5 mL 72-95 lbs 11 years 15 mL * or as directed by a doctor Attention: use only enclosed dosing cup specifically designed for use with this product. Do not use any other dosing device."
  ],
  "dosage_and_administration_table": [
    "<table border=\"1\" width=\"60%\"><tbody><tr><td><content styleCode=\"bold\">Weight (lb)</content></td><td><content styleCode=\"bold\">Age (yr)</content></td><td><content styleCode=\"bold\">Dose (mL)*</content></td></tr><tr><td>under 24</td><td>under 2 years</td><td>ask a doctor</td></tr><tr><td>24-35 lbs</td><td>2-3 years</td><td>5 mL</td></tr><tr><td>36-47 lbs</td><td>4-5 years</td><td>7.5 mL</td></tr><tr><td>48-59 lbs</td><td>6-8 years</td><td>10 mL</td></tr><tr><td>60-71 lbs</td><td>9-10 years</td><td>12.5 mL</td></tr><tr><td>72-95 lbs</td><td>11 years</td><td><paragraph>15 mL</paragraph></td></tr></tbody></table>"
  ],
  "storage_and_handling": [
    "Other information Other information ■ each 5 mL contains: potassium 5 mg ■ store between 20-25°C (68-77°F) ■ do not use if carton is opened. Do not use if bottle wrap imprinted with “TYLENOL” is broken or missing"
  ],
  "inactive_ingredient": [
    "Inactive ingredients anhydrous citric acid, flavors, glycerin, microcrystalline cellulose and carboxymethylcellulose sodium, potassium sorbate, purified water, sorbitol solution, sucralose, sucrose, xanthan gum"
  ],
  "questions": [
    "Questions or comments? call 1-800-458-1635 (toll-free) or 215-273-8755 (collect)"
  ],
  "package_label_principal_display_panel": [
    "NDC 50580-139-04 Children's TYLENOL ® Acetaminophen (160 mg per 5 mL) Oral Suspension Pain Reliever-Fever Reducer Pain+Fever Ages 2-11 Years DYE-FREE Free Of: • Dyes • Alcohol • Ibuprofen • Aspirin • Parabens • High Fructose Corn Syrup • Artificial Flavoring Natural Apple Flavor with Other Natural Flavors 4 fl oz (120 mL) 160 mg per 5 mL Tylenol-1"
  ],
  "set_id": "3162733b-9382-39f1-e063-6294a90ac420",
  "id": "5409ac69-c01e-e5d6-e063-6294a90a14b2",
  "effective_time": "20260612",
  "version": "3",
  "openfda": {
    "application_number": [
      "M013"
    ],
    "brand_name": [
      "CHILDRENS TYLENOL"
    ],
    "generic_name": [
      "ACETAMINOPHEN"
    ],
    "manufacturer_name": [
      "Kenvue Brands LLC"
    ],
    "product_ndc": [
      "50580-139"
    ],
    "product_type": [
      "HUMAN OTC DRUG"
    ],
    "route": [
      "ORAL"
    ],
    "substance_name": [
      "ACETAMINOPHEN"
    ],
    "rxcui": [
      "307668",
      "828555"
    ],
    "spl_id": [
      "5409ac69-c01e-e5d6-e063-6294a90a14b2"
    ],
    "spl_set_id": [
      "3162733b-9382-39f1-e063-6294a90ac420"
    ],
    "package_ndc": [
      "50580-139-04",
      "50580-139-08"
    ],
    "is_original_packager": [
      true
    ],
    "upc": [
      "0300450146045"
    ],
    "unii": [
      "362O9ITL9D"
    ]
  }
}
```
