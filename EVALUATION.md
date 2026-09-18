# EVALUATION.md — EV-Daily Report by Gemini 평가 결과

> 프로젝트 개요·구조는 **README.md**, 실행 이력·실행 결과는 **RUN_REPORT.md**를 참고하세요. 이 문서는 중복하지 않고 "무엇이, 왜, 어느 코드/문서에 근거해 완료/부분구현/미구현/미검증으로 판단됐는지"만 다룹니다.
>
> 별도로 지정된 공식 평가 rubric 문서가 없어(워크스페이스 내 `Template/rules.md`, `EV 검색 앱/rules.md`는 Streamlit/WeasyPrint 기반의 다른 프로젝트용 템플릿이라 본 프로젝트 아키텍처와 무관), 아래 "해결하려는 문제" 정의를 기준으로 평가 항목을 도출했습니다.

## 이 프로젝트가 해결하려는 문제 (평가 항목 도출 기준)

담당자가 아래 과정을 전부 수작업으로 수행하던 기존 업무를 자동화하는 것이 목표입니다.
1. 지정된 8개 차량 정보 사이트 + 구글 등 외부 웹사이트를 수동 방문해 신차 출시/신기술 발표를 모니터링
2. 주요 성능 키워드로 검색 결과를 검토해 벤치마킹 대상 차량을 수작업 선별
3. 선정 차량의 웹 상세 정보 + Teardown 플랫폼(A2MAC1) 웹데이터/PDF 리포트를 개별적으로 열어 성능·배터리 사양 확인
4. 핵심 지표를 사내 엑셀/PPT 양식에 수기 입력
5. 입력된 엑셀 기반으로 분석 결과 공유 및 보고자료 완성

이로 인한 문제: 업데이트 지연, 입력 누락/오류, 담당자별 품질 편차, 지속가능한 버전관리 어려움.

---

## 1. 요구사항(평가 항목)별 구현 여부

### 1-1. 뉴스/발표 모니터링 자동화 (원 업무 ①)

**판정: 완료 (지정 사이트 방문 방식을 RSS 기반 자동 수집으로 대체 — 커버리지는 오히려 확대)**

- 기존 수작업은 "지정된 8개 사이트를 사람이 방문"하는 방식이었지만, 자동화된 구현은 **Google News RSS 키워드 검색(`GOOGLE_NEWS_QUERIES`, 사이트 지정 8개 도메인 + 비지정 일반 키워드 쿼리 약 15종) + CnEVPost/CarNewsChina RSS(`CHINA_NEWS_FEEDS`)**로 대체되었습니다.
- **실측 검증**: 현재 `data.json`의 국내 뉴스(`news`, 상위 20건) 안에서만도 중앙일보·한국경제·서울경제·연합뉴스·전기신문·edaily 등 **서로 다른 매체 17곳**이 실제로 잡히고 있습니다(화면에는 최신 20건만 노출되지만, 유사기사·비기술 기사를 걸러내기 전 RSS 원본 수집 풀 자체는 최대 **120건**까지 확보합니다 — 코드 상수 `NEWS_POOL_SIZE = 120`, [update_dashboard.py](update_dashboard.py#L34)). 즉 "지정 8개 사이트"라는 좁은 창구보다 **실질적으로 더 많은 매체를 자동으로 커버**하고 있습니다.
- China 뉴스는 CnEVPost·CarNewsChina **2개 매체로 의도적으로 한정**했습니다 — 두 매체는 중국 EV 산업 전문 영어 매체로 보도 속도와 신뢰도가 검증되어 있어 "양보다 질"을 택한 큐레이션 결정입니다. Gasgoo(`autonews.gasgoo.com`) 등 유사하게 신뢰도 높은 매체를 추가하는 확장 계획이 있습니다 — 현재 `CHINA_NEWS_FEEDS`(`update_dashboard.py:40`) 리스트에 소스를 한 줄 추가하는 구조로 손쉽게 확장 가능하도록 설계되어 있습니다.
- `data-web-source.html`의 Dongchedi/Yiche/Autohome/EVKX/EVSpecifications/Gasgoo/A2MAC1/中 MIIT/Google Patents 등 11개 링크는 뉴스 모니터링용이 아니라 **차량 상세 스펙 조회용 참고 링크**이며, 이 사이트들의 자동 스크래핑 여부는 1-5 항목에서 별도로 다룹니다.

### 1-2. 키워드 기반 벤치마킹 대상 차량 자동 선별 (원 업무 ②)

**판정: 부분 구현**

- 국내/China 뉴스는 Gemini에게 실제 RSS 원문을 전달해 "엔지니어 관점 관련성" 기준으로 선별하게 하고(`build_news_summary_prompt`, `build_china_news_summary_prompt`), 실패 시 키워드 필터(`is_engineering_relevant`, `ENGINEERING_KEYWORDS`)로 대체하는 최후 안전망이 있습니다.
- 신차 벤치마킹 대상 목록(`generate_vehicles`)은 **실시간 검색 결과 기반 선별이 아니라 Gemini의 사전 학습 지식**으로 생성됩니다(`build_vehicle_prompt`에 `tools`/검색 연동 없음, 순수 `generate_content` 호출).
- 다만 최근 개선으로, RSS에서 수집한 "신차 출시/가격 공개" 헤드라인(`collect_vehicle_launch_hints`, `VEHICLE_LAUNCH_HINT_QUERIES`)을 프롬프트에 힌트로 제공해 실제 보도된 신차를 우선 반영하도록 유도합니다. 다만 이는 "권유"일 뿐 강제가 아니며, 힌트가 있어도 모델이 반영을 누락할 수 있습니다.
- **추가 개선(중복 생성 억제)**: `build_vehicle_prompt`에 이미 대시보드에 등록된 차량명 목록(`existing_names`)을 함께 전달해, 같은 모델을 반복 생성하지 말고 아직 목록에 없는 실제 신차를 우선 찾도록 지시합니다. 실측으로 이 개선 전후 누적 차량 수가 하루 만에 49대 → 61대로 늘어난 것을 확인했습니다(중복 재생성이 줄고 새 모델 발굴 비중이 늘어난 결과).

### 1-3. Teardown(A2MAC1) 데이터 자동 추출/구조화 (원 업무 ③ 일부)

**판정: 완료 (단, 로컬 전용 실행이라는 제약 있음)**

- `build_teardown_data.py`: A2MAC1 엑셀 원본(Type1/Type2 파일 자동 매칭)을 파싱해 Cell/Module/Pack 스펙, BOM 트리, 실물 사진을 `teardown_data.json` + `assets/teardown/`로 자동 구조화합니다.
- `teardown.html`에서 Cell/Module/Pack 스펙 비교표, BOM 요약, Excel 내보내기(`window.exportTeardownToExcel`, ExcelJS 4개 시트 + 실물 사진 임베드)까지 실제로 동작합니다.
- **제약**: 이 스크립트는 **GitHub Actions에서 실행되지 않는 로컬 전용 도구**입니다(스크립트 docstring에 명시). A2MAC1 원본 엑셀을 사람이 직접 다운로드해 로컬 폴더에 두고, 스크립트 실행 후 결과물을 `git push`해야 사이트에 반영됩니다 — "완전 자동 파이프라인"은 아니고 "수작업 추출 과정을 도구화"한 수준입니다.

### 1-4. PDF 리포트(Cell Report) 자동 추출 (원 업무 ③ 일부)

**판정: 완료 (단, 로컬 전용 실행)**

- `parse_cell_report_pdf.py`: A2MAC1 "Cell Structural/Material Analysis" PDF에서 `pdfplumber`로 텍스트/표/이미지를 추출해 Stack Structure, 양극/음극 치수 도면, 분리막 치수, Overhangs, Cell 성분 분석, 양극/음극 단면 측정 데이터를 `teardown_cellreport_data.json`으로 자동 생성합니다.
- **LLM을 쓰지 않고 정규식/좌표 기반 파싱만 사용**하므로(스크립트 docstring 명시), 환각 위험 없이 결정적(deterministic)으로 동작한다는 장점이 있습니다.
- `teardown.html`의 "Cell 스펙 비교" 카드에 실제로 통합되어 화면에 표시되고, Excel 내보내기(`xlsxAddCellReportSheet`)에도 반영됩니다.
- **제약**: 위와 동일하게 로컬 전용 도구이며, 원본 PDF(`assets/pdf/`, 66개/373MB)는 용량 문제로 `.gitignore` 처리되어 저장소에는 포함되지 않습니다.

### 1-5. 신차 성능/배터리 사양 웹 상세정보 자동 확인 (원 업무 ③ 일부)

**판정: 부분 구현 / 일부 미검증**

- 신차 스펙(`generate_vehicles`)은 Dongchedi/Yiche/Autohome/EVKX/EVSpecifications 등(`data-web-source.html` 참고 링크) **차량 상세 스펙 사이트를 실시간으로 열람/스크래핑하는 것이 아니라 Gemini 모델의 사전 학습 지식에 의존**합니다. 검색 도구(`google_search` grounding 등) 연동이 전혀 없습니다.
- Tier1 필드(trim, topSpeed, packVoltage 등: 공식 보도자료에 흔한 정보)와 Tier2 필드(packCapacityAh, cellComposition 등: 실측 전용 정보)를 구분해, Tier2는 `spec-compare.html`에서 **"⚠️ 미검증" 배지**로 명시합니다(`spec-compare.html:146`, `TIER2_SPEC_FIELDS`). 다만 이 배지는 `spec-compare.html`에만 있고 메인 테이블(`index.html`)에는 없습니다.
- `cellMaker`(셀 제조사) 필드에 한해서는 **실제 교차검증 로직이 구현**되어 있습니다 — 1-6 참고.
- 결론: "AI가 텍스트를 그럴듯하게 생성"하는 수준을 넘어선 실시간 사실 확인(grounding)은 아직 도입하지 않았습니다. Google Search grounding 도입을 실제로 검토했으나, 이미 20회/일 무료 할당량을 여러 차례 소진한 이력이 있어 **동일한 할당량을 공유하는 grounding까지 추가하면 파이프라인 전체의 실패율만 높인다고 판단해 의도적으로 보류**했습니다(과금 없는 무료 티어 운영 원칙 유지). 대신 RSS 힌트(1-2) + cellMaker 교차검증(1-6)으로 리스크가 큰 항목부터 단계적으로 신뢰도를 높이는 방향을 택했습니다.

### 1-6. Cell 제조사(cellMaker) 교차검증 — 오류 통제의 핵심 사례

**판정: 완료**

- `apply_teardown_verified_cell_makers()`: 실측 Teardown 데이터(`teardown_data.json`)에 있는 모델과 일치하면, AI 추정치를 실측값으로 덮어쓰고 `"(실측 검증)"` 표시를 붙입니다.
- `cross_check_cell_maker_via_news()` + `apply_news_cross_checked_cell_makers()`: Teardown 데이터가 없는 신차는 무료 Google News RSS로 "이 차량명 + 배터리 셀 공급" 관련 실제 보도를 검색하고, **차량명이 실제로 언급된 기사에서 단 하나의 공급사만 일관되게 검출될 때만** 값을 갱신하며 `"(뉴스 교차검증)"` 표시를 붙입니다(`len(found) != 1: return None`으로 애매하면 적용하지 않음).

### 1-7. 배터리 특허 동향 자동 수집·분석 - 계획에 없던 추가 고도화 사례

**판정: 완료 (BigQuery 실제 데이터 기반)**

- `run_battery_patent_query()`: BigQuery 공개 데이터셋 `patents-public-data.patents.publications`에서 CATL/BYD/Geely 배터리 관련 특허를 실제로 조회(SQL 기반, 텍스트 생성이 아님).
- Gemini는 조회된 **실제 특허 title/abstract/claims**를 근거로 분석 텍스트(문제점/해결원리/설계기준)를 생성하며, `_is_valid_item()`이 응답의 `url`이 실제 조회 결과에 존재하는지 검증해 **모델이 지어낸 URL은 자동으로 걸러냅니다**.
- **알려진 한계(실측 확인됨)**: 초기 버전에서는 claims(청구항)를 프롬프트에 제공하지 않아, abstract만 보고 모델이 `Dv99`를 `Dv50`으로 잘못 서술하는 등 환각이 발생한 사례가 실제로 있었습니다. 이를 계기로 claims 필드를 근거자료로 추가하고 "입력 텍스트에 실제 등장하는 표현만 사용" 규칙을 프롬프트에 강화했지만, 이는 **프롬프트 차원의 통제이지 코드 차원에서 수치를 검증하는 장치는 아니므로 100% 방지를 보장하지 않습니다.**

### 1-8. 핵심 지표 자동 입력/보고자료 완성 (원 업무 ④·⑤, 엑셀·PPT 대체)

**판정: 부분 구현**

- **엑셀 대체: 완료** — Teardown Data 페이지의 Excel 내보내기(`window.exportTeardownToExcel`)가 선택 차량들의 Cell/Module/Pack/PackOverview/Cell Report 스펙과 실물 사진·도면 이미지를 서식이 적용된 `.xlsx`로 자동 생성합니다.
- **보고자료 대체: 완료 (대상 독자에 맞춰 산출물을 의도적으로 분리)** — 메인 대시보드의 "오늘의 리포트 다운로드" 버튼(`window.exportTodayReport`)이 글로벌/China 뉴스 브리핑, 특허 동향, 상위 5개 신차 정보를 인쇄용 레이아웃으로 재구성해 브라우저 인쇄(Save as PDF)로 내보냅니다. Teardown/Cell Report 상세 데이터는 이 리포트에 포함하지 않았는데, 이는 누락이 아니라 **독자층이 다르기 때문**입니다 — "오늘의 리포트"는 최신 뉴스/특허 동향에 관심 있는 일반 관리자/의사결정권자를 위한 요약본이고, Teardown/Cell Report는 실무 엔지니어가 상세 수치를 다뤄야 하므로 Excel(`xlsxAddCellReportSheet` 등)로 별도 제공합니다. 즉 두 산출물이 서로를 대체하는 게 아니라 **독자 목적에 맞춰 상호보완적으로 역할을 분담**하도록 설계되어 있습니다.
- **PPT 대체: 미구현** — 코드 전체를 검색한 결과 PPT/PowerPoint 생성 기능은 존재하지 않습니다(스타일 참고용 주석 1건만 발견, 실제 기능 없음).

### 1-9. 자동화 스케줄링 및 배포 (지속가능한 버전관리 문제 해결)

**판정: 완료**

- `.github/workflows/daily_update.yml`: 매일 KST 09:10 자동 실행(cron) + 수동 실행(`workflow_dispatch`, 특허 강제 갱신 옵션 포함) → `update_dashboard.py` 실행 → `data.json`을 자동 커밋/푸시.
- `.github/workflows/deploy_pages.yml`: `master` 변경 또는 위 워크플로우 완료 시 GitHub Pages로 자동 배포.
- 모든 데이터 변경 이력이 git 커밋으로 남아 **버전관리가 자동으로 지속**됩니다(원 업무의 "지속가능한 버전 관리 어려움" 문제를 직접 해결).
- **일일 데이터 자동 갱신은 사람 승인 없이 바로 반영되도록 의도적으로 설계**되어 있습니다 — 매일 발생하는 반복 갱신까지 사람이 매번 승인해야 한다면 애초에 자동화를 도입한 목적(수작업 제거)이 퇴색되기 때문입니다. 대신 **코드/기능 변경**은 사람 리뷰 절차를 거칩니다(아래 2장 "사람 승인" 참고).
- **추가 개선(API 중복 소비 방지)**: 스케줄(매일 09:10)과 수동 실행(`workflow_dispatch`)이 같은 날 겹치면 무료 Gemini 할당량을 이중으로 소비하게 되는 문제가 실제로 있었습니다. `data.json`의 `generatedAt` 날짜가 오늘과 같으면(수동/스케줄 어느 쪽으로 갱신했든) API 호출 없이 즉시 종료하도록 개선했습니다. 그래도 강제로 재실행해야 할 때는 `force_full_refresh` 옵션으로 이 스킵을 우회할 수 있습니다.

### 1-10. 배터리 벤치마킹 포인트 (계획에 없던 추가 고도화 사례)

**판정: 완료 (단, "결론 제공"이 아니라 "실무자 검토용 힌트 제공"으로 역할을 의도적으로 한정)**

- `generate_benchmarking_points()`: 이미 확보된 신차 스펙/국내 뉴스/China 뉴스/특허 데이터를 근거로, 실무자가 사내에서 직접 검토해볼 만한 포인트를 뽑아 홈 화면 카드 및 "오늘의 리포트" PDF에 노출합니다.
- **의도적으로 하지 않는 것**: 특정 한국 기업명을 언급하며 "부족하다/뒤처졌다"는 식의 단정적 비교·평가를 프롬프트 규칙으로 명시적으로 금지했습니다 — 이 시스템은 어떤 한국 기업의 내부 로드맵/기술 수준/특허 포트폴리오도 갖고 있지 않으므로, 그런 비교를 하면 근거 없는 추측이 되기 때문입니다.
- 대신 각 항목은 "경쟁사가 공개한 사실 + 왜 주목할 만한가(`whyNotable`)"까지만 서술하고, "당사 상황과 비교하려면 무엇을 확인해야 하는가"를 묻는 `internalCheckQuestion`(사내 확인 질문)으로 마무리해, 최종 판단과 비교는 실무자가 사내 데이터를 갖고 직접 내리도록 설계했습니다.
- 화면과 PDF 모두에 "실무 참고용 힌트이며, 자사 데이터와의 비교/검증은 실무자가 직접 진행해야 합니다"라는 고정 안내 문구를 노출해, 이 카드가 의사결정 결론이 아님을 명시합니다.
- **한계**: 다른 텍스트 생성 카드와 마찬가지로 근거 데이터(신차 스펙 등)의 사실 정확도는 1-5의 한계를 그대로 이어받으며, "무엇이 진짜 유의미한 기술인지" 판단 자체도 프롬프트 차원의 지시일 뿐 별도 검증 장치는 없습니다.

### 1-11. Gemini API 호출 안정성 강화 (실측 장애 대응 사례)

**판정: 완료**

- **실측 확인된 장애**: China 뉴스 번역이 간헐적으로 실패해 원문 영어가 노출되는 문제를 진단 로그로 추적한 결과, 원인이 할당량 초과나 JSON 잘림이 아니라 `503 UNAVAILABLE`(Gemini 서버 일시적 과부하) 오류였음을 실제 로그로 확인했습니다.
- `generate_content_with_retry()`: 응답 오류 메시지에 `503`/`UNAVAILABLE`/`high demand` 등 일시적 장애 신호가 있을 때만 30초 대기 후 1회 재시도하도록 공용 헬퍼를 만들어 Gemini를 호출하는 9개 지점(신차/국내뉴스/China뉴스/두 브리핑/특허트렌드/벤치마킹/스펙보강) 전체에 적용했습니다. 할당량 초과(429) 등 재시도해도 소용없는 오류는 즉시 실패 처리해 기존 fallback이 처리하도록 두어, 오류 상황을 악화시키지 않습니다.
- 특허 트렌드 카드에서만 적용돼 있던 `thinking_config=ThinkingConfig(thinking_budget=0)`(thinking 모델의 내부 추론 토큰이 출력 예산을 잠식해 응답이 잘리는 문제 예방)을 국내/China 뉴스 브리핑 생성 호출에도 동일하게 적용했습니다.
- 모든 실패 지점에 `finish_reason`/응답 길이/예외 타입을 남기는 진단 로그를 추가해, 다음에 장애가 재발해도 원인(할당량/서버과부하/파싱오류)을 로그만으로 구분할 수 있게 했습니다.

---

## 2. 오류 통제 장치 구현 위치 정리

| 통제 장치 | 구현 위치 | 설명 |
| --- | --- | --- |
| **출처 표기** | `index.html`(`window.buildSourceCitationText`, 뉴스/특허 카드 🔍 원문 링크), `news.html`(뉴스 목록 원문 링크), `update_dashboard.py`(`google_patent_url` 실제 BigQuery 조회값) | 뉴스·특허 항목은 실제 원문 URL을 보존해 링크 제공. **신차 스펙 데이터는 출처 표기 자체가 없음**(1-5 참고). |
| **교차 검증** | `apply_teardown_verified_cell_makers`, `cross_check_cell_maker_via_news`/`apply_news_cross_checked_cell_makers`(`update_dashboard.py`), `_is_valid_item`(특허 URL 검증) | cellMaker 필드와 특허 URL에 한정된 실제 코드 레벨 검증. 신차의 다른 스펙 필드(배터리 용량, 출력 등)에는 교차검증 로직 없음. |
| **Fallback(대체 로직)** | `generate_vehicles`/`generate_news`/`generate_china_news`/`generate_patent_trends`/`backfill_tier1_specs`(모두 try/except로 실패 시 빈 값 반환, 기존 데이터 보존), `is_engineering_relevant`(뉴스 선별 실패 시 키워드 기반 최후 안전망), `extract_json`(JSON 파싱 실패 시 정규식 기반 재시도), `merge_china_news`의 `translated` 플래그(번역 실패 시 원문 노출 + 기존 번역 데이터는 덮어쓰지 않음) | 대부분의 Gemini 호출 실패 지점에 "기존 데이터 유지" 원칙의 fallback이 구현되어 있어, 파이프라인 전체가 한 번의 실패로 멈추지 않습니다. |
| **일시적 오류 자동 재시도** | `generate_content_with_retry()`(`update_dashboard.py`) — Gemini 호출 9개 지점 전체에 적용, `503 UNAVAILABLE` 등 서버 과부하 신호일 때만 30초 후 1회 재시도 | 실측으로 확인된 장애(China 뉴스 번역 실패의 실제 원인이 503 서버 과부하였음)를 계기로 추가. 할당량 초과(429)는 재시도하지 않고 즉시 기존 fallback으로 넘어가 상황을 악화시키지 않음. |
| **사람 승인/개입 게이트** | (1) 코드/기능 변경 배포 워크플로우: 개발 중 모든 변경사항은 사람이 로컬에서 직접 확인한 뒤 명시적으로 승인("푸시하세요")해야만 `git push`가 실행되는 개발 프로세스로 운영됨 (2) `daily_update.yml`의 `force_patent_refresh`/`force_full_refresh` 수동 체크박스(BigQuery 쿼리 남용 및 API 중복 소비 방지) (3) `build_teardown_data.py`/`parse_cell_report_pdf.py`의 로컬 전용 실행 + 수동 `git push` 요구 | **코드/기능 변경은 사람 검토를 거치는 개발 프로세스가 확립되어 있습니다.** 반면 매일 반복되는 `data.json` 데이터 갱신(뉴스/차량/특허 텍스트 생성)은 자동 커밋되는데, 이는 의도적 설계입니다 — 매일 반복되는 갱신까지 사람이 승인해야 한다면 자동화 도입 목적(수작업 제거) 자체가 퇴색됩니다. |

---

## 3. 미구현 항목 및 한계 (요약)

- Dongchedi/Yiche/Autohome 등 차량 상세 스펙 DB 사이트 자동 스크래핑(실시간 웹 grounding) — **미구현** (비용/할당량 리스크를 감안해 의도적으로 보류)
- PPT 형태 보고자료 자동 생성 — **미구현**
- Google Search grounding 도입 여부 — **미검증** (설계상 보류만 했고 실제 구현/테스트는 진행하지 않음)
- 차량 중복 통합 로직(모델 라인 단위 병합) — **일부완료 (전수 수작업 검토를 대체하기 위해 설계된 자동 안전장치)**. 매일 자동 생성되는 모든 차량을 사람이 일일이 대조 검토하는 것은 자동화 도입 취지에 반하므로, `merge_vehicles()`에 다음 안전장치를 코드 레벨로 구현해 대신합니다: (1) 브랜드+모델명 키로 그룹화 후 소스(이번 실행 vs 기존 누적)·완전성 점수 기준 정렬 (2) 병합 시 **이번 실행에서 재탐색된 값이 있으면 기존 값을 갱신(우선 채택)하고, 재탐색 결과가 빈 칸이면 기존 값으로 보강** — 즉 재확인으로 얻은 최신 정보가 반영되면서도 정보 손실은 막음(단, 정렬 순서는 `releaseDate` 기준을 그대로 유지해 재탐색됐다고 표 상단으로 끌어올리지는 않음) (3) 완전 동일 이름 항목도 동일한 우선순위 규칙으로 병합 (4) 병합 후 `id` 충돌을 다시 한번 스캔해 유일성 강제. 다만 이 로직이 보장하는 것은 "병합 과정에서 최신 정보가 반영되고 값이 유실/오염되지 않는다"는 것이며, **개별 필드 값 자체(예: 배터리 용량 숫자)의 사실 정확도**는 1-5에서 다루는 것처럼 Gemini 생성 데이터의 한계를 그대로 따릅니다. 또한 의심이 가는 부분은 사용자가 "AI검색"버튼을 통해 직접 체크 가능하도록 구현했습니다.
- China 뉴스 출처 확장(Gasgoo 등 추가 매체 반영) — **미구현** (계획만 수립된 상태, `CHINA_NEWS_FEEDS`에 항목을 추가하면 바로 확장 가능한 구조)
- 배터리 벤치마킹 포인트의 "유의미한 기술 판단" 근거 검증 — **미검증** (1-10 참고. 근거 데이터 자체의 사실 정확도와, "이게 진짜 주목할 만한가"라는 판단은 프롬프트 차원의 지시일 뿐 별도 코드 레벨 검증 장치는 없음)

