# EV-Daily Report by Gemini

> Daily EV & Battery Intelligence Dashboard — 전기차/배터리 산업 동향을 매일 자동으로 수집·정리해 보여주는 정적 웹 대시보드

> 실행 방법과 실행 결과는 **RUN_REPORT.md**, 평가 항목별 점검 내용은 **EVALUATION.md**에 별도로 정리되어 있습니다. 이 문서에서는 중복해서 다루지 않습니다.

---

## 1. 프로젝트 목적

전기차(EV) 및 배터리 산업 실무자가 매일 아침 확인해야 할 정보(신차 스펙·가격, 글로벌/China 뉴스, 배터리 특허 동향, 실제 분해(teardown) 데이터)를 한 화면에서 볼 수 있도록 만든 대시보드입니다.

- Google Gemini API를 이용해 신차 스펙, 뉴스 브리핑, 특허 동향 등 텍스트 콘텐츠를 생성합니다.
- GitHub Actions로 매일 자동 실행되어 데이터(`data.json`)를 갱신하고, GitHub Pages로 정적 배포됩니다.
- A2MAC1 실측 teardown 데이터와 Cell Report PDF 파싱 데이터를 결합해, AI가 생성한 스펙과 실측 데이터를 함께 비교할 수 있게 합니다.

## 2. 주요 기능

### 2.1 메인 대시보드 (`index.html`)
- **신규 전기차(EV) 기본 사양 & 가격 정보**: Gemini가 생성한 신차 스펙/가격 목록(최근 12개월 커버), 폼팩터(원통형/각형/파우치/블레이드)·조성(LFP/NCM/NCA/반고체 등) 필터, 발표/등록 기간 필터, 차량 검색(기존 데이터 검색 + 미등록 인지 브랜드에 대한 임시 항목 추가)
- **글로벌 EV & 배터리 최신 동향 리포트**: Google News RSS 수집 기사를 Gemini가 카테고리별로 선별·요약한 브리핑 카드
- **China EV & 배터리 최신 동향 리포트**: CnEVPost·CarNewsChina RSS 수집 기사를 Gemini가 선별·번역·요약한 브리핑 카드 (번역 실패 시 원문 영어로 노출되며 "Translation coming soon" 배지 표시)
- **CATL·BYD·Geely 배터리 특허 동향**: BigQuery 공개 특허 데이터셋(`patents-public-data.patents.publications`) 조회 결과를 Gemini가 분석(문제점/해결원리/청구항 기반 정량적 설계 기준 요약)한 브리핑 카드
- **배터리 벤치마킹 포인트**: 이미 수집된 신차/국내·China 뉴스/특허 데이터를 근거로, 실무자가 사내에서 직접 확인해볼 만한 포인트를 AI가 추출해주는 카드. 특정 국내 업체와의 직접 비교/평가는 하지 않고(내부 데이터를 가지고 있지 않으므로), 항목마다 "사내 확인 질문"을 함께 제공해 실제 비교/검증은 실무자가 직접 진행하도록 설계되었습니다. 각 항목은 입력 데이터 원문을 그대로 인용한 근거(`evidenceQuote`)를 코드로 대조 검증하며(원문에 없는 근거는 자동 폐기), 근거 강도를 "직접 인용/간접 서술/추론" 배지(`factLevel`)로 함께 표시합니다.
- **오늘의 리포트 PDF 다운로드**: 위 4개 브리핑 카드(글로벌/China 뉴스, 특허 동향, 벤치마킹 포인트) + 상위 5개 신차 정보를 인쇄용 레이아웃으로 재구성해 브라우저 인쇄(Save as PDF) 기능으로 내보내기
- **DB 히스토리**: 현재 차량 목록 상태를 브라우저 로컬스토리지에 스냅샷으로 저장/조회

### 2.2 뉴스 전체보기 (`news.html`)
- 국내/China 뉴스를 탭으로 구분해 전체 목록 열람, 각 기사 원문 링크 제공

### 2.3 Teardown Data (`teardown.html`)
- A2MAC1 실측 데이터 기반 Cell/Module/Battery Pack 스펙 비교 표, Pack 개요 및 실물 사진, BOM(부품 구성) 요약
- `parse_cell_report_pdf.py`로 추출한 Cell Report 데이터(Cell 구조 분석, 양극/음극 치수 도면, 분리막 치수, Overhangs, Cell 성분 분석, 양극/음극 단면 측정)를 Cell 스펙 카드에 추가 표시
- 선택한 차량들의 비교 데이터를 시트별로 나눈 Excel(.xlsx) 파일로 내보내기 (실물 사진·도면 이미지 포함)

### 2.4 기본사양비교 (`spec-compare.html`)
- 메인 페이지에서 체크한 차량들의 배터리 팩/셀 기본 정보를 나란히 비교하는 표

### 2.5 Data Source (`data-web-source.html`)
- 중국/글로벌 EV 정보 수집에 참고하는 웹사이트(Dongchedi, Yiche, Autohome, CnEVPost, CarNewsChina, Google Patents 등) 링크 모음

### 2.6 데이터 자동 갱신 파이프라인 (`update_dashboard.py`)
- 매일 실행되어 아래 데이터를 생성/갱신하고 `data.json`으로 저장합니다.
  - 신차 스펙(Gemini 생성) — 기존 차량명 목록을 프롬프트에 함께 전달해 중복 생성을 줄이고 신규 모델 발굴을 유도하며, 모델 라인 단위로 병합해 중복 항목을 통합(재탐색으로 새 정보가 확인되면 기존 값을 갱신, 빈 필드만 보강 — 정렬 순서는 유지)
  - 국내 뉴스(Google News RSS 수집 + Gemini 선별/요약)
  - China 뉴스(CnEVPost·CarNewsChina RSS 수집 + Gemini 선별/번역/요약, 번역 실패 시 원문 노출 후 다음 실행에서 재번역 시도)
  - 배터리 특허 동향(BigQuery 조회 + Gemini 분석, 주 1회 또는 수동 강제 갱신)
  - 배터리 벤치마킹 포인트(위 데이터들을 근거로 Gemini가 추출, url/원문 인용구를 코드로 대조해 근거 없는 항목은 자동 폐기)
  - 환율 정보
- **중복 실행 방지**: `data.json`의 `generatedAt` 날짜가 오늘 날짜와 같으면(스케줄/수동 구분 없이) API 호출 없이 실행을 건너뜁니다 — 같은 날 스케줄+수동 중복 실행으로 무료 API 할당량이 두 배로 소모되는 것을 막기 위함입니다(강제 재실행은 `force_full_refresh` 옵션으로 가능).
- **일시적 서버 오류 재시도**: Gemini API가 `503 UNAVAILABLE`(서버 과부하) 오류를 반환하면 30초 대기 후 1회 자동 재시도합니다(할당량 초과는 재시도하지 않고 즉시 기존 데이터를 유지).

## 3. 사용 기술 스택

### Frontend
- 순수 HTML/CSS/JavaScript (프레임워크 없음)
- Tailwind CSS (CDN)
- Pretendard 폰트 (CDN)
- ExcelJS (CDN) — Teardown Data 페이지의 Excel 내보내기 기능

### Backend / 데이터 파이프라인 (Python)
- `google-genai` — Google Gemini API 클라이언트
- `google-cloud-bigquery`, `google-auth` — BigQuery 공개 특허 데이터셋 조회
- 표준 라이브러리(`urllib`, `xml.etree.ElementTree`) 기반 RSS 수집 (별도 외부 RSS 라이브러리 미사용)
- `openpyxl`, `Pillow` — 로컬 전용 teardown 데이터 빌드 도구(`build_teardown_data.py`)에서 엑셀/이미지 처리
- `pdfplumber`, `Pillow` — 로컬 전용 Cell Report PDF 파서(`parse_cell_report_pdf.py`)

### 자동화 / 배포
- GitHub Actions
  - `daily_update.yml`: 매일(KST 09:10) `update_dashboard.py` 실행 후 `data.json`을 자동 커밋/푸시 (수동 실행 + 특허 동향 강제 갱신/전체 강제 재실행 옵션 지원)
  - `deploy_pages.yml`: `master` 브랜치 변경 또는 위 자동 갱신 워크플로우 완료 시 GitHub Pages로 자동 배포
- GitHub Pages — 정적 사이트 호스팅

### 외부 데이터 소스
- Google Gemini API (신차 스펙, 뉴스 요약/번역, 특허 분석 텍스트 생성)
- Google News RSS, CnEVPost RSS, CarNewsChina RSS
- BigQuery 공개 데이터셋 `patents-public-data.patents.publications`
- 무료 환율 API

## 4. 프로젝트 구조

```
EV-Daily Report by gemini/
├── index.html                    # 메인 대시보드 (신차 목록/뉴스/특허 브리핑/오늘의 리포트 PDF)
├── news.html                     # 뉴스 전체보기 (국내/China 탭)
├── teardown.html                 # Teardown Data 비교 + Cell Report 상세 + Excel 내보내기
├── spec-compare.html             # 배터리 스펙 기본 정보 비교표
├── data-web-source.html          # 데이터 수집 참고 웹사이트 목록
├── update_dashboard.py           # 매일 실행되는 메인 데이터 갱신 스크립트 (data.json 생성)
├── build_teardown_data.py        # 로컬 전용: A2MAC1 teardown 엑셀 원본 → teardown_data.json 생성
├── parse_cell_report_pdf.py      # 로컬 전용: A2MAC1 Cell Report PDF → teardown_cellreport_data.json/js 생성
├── data.json                     # 메인 대시보드 데이터(차량/뉴스/특허/환율) — update_dashboard.py 산출물
├── teardown_data.json            # Teardown Data 데이터 — build_teardown_data.py 산출물
├── teardown_cellreport_data.json # Cell Report 파싱 데이터 — parse_cell_report_pdf.py 산출물
├── teardown_cellreport_data.js   # 위 데이터의 JS 전역변수 버전 (parse_cell_report_pdf.py 산출물)
├── requirements.txt              # Python 의존성 목록
├── .github/
│   └── workflows/
│       ├── daily_update.yml      # 매일 데이터 자동 갱신 워크플로우
│       └── deploy_pages.yml      # GitHub Pages 자동 배포 워크플로우
├── assets/
│   ├── images/                   # 사이트 공용 이미지(마스코트 등)
│   ├── teardown/<vehicle-id>/    # 차량별 Cell/Pack 실물 사진 및 Cell Report 도면 이미지
│   ├── pdf/                      # (git 미포함, 로컬 전용) parse_cell_report_pdf.py 입력 원본 PDF
│   └── video/
└── .gitignore
```

## 5. 환경변수 설정 방법

`update_dashboard.py`는 아래 환경변수를 사용합니다.

| 환경변수 | 필수 여부 | 설명 |
| --- | --- | --- |
| `GEMINI_API_KEY` | 필수 | Google Gemini API 키. 설정되어 있지 않으면 `data.json` 갱신 자체를 건너뜁니다. |
| `GCP_SA_KEY_JSON` | 선택 | BigQuery 조회용 GCP 서비스 계정 키의 JSON **내용 전체**(파일 경로가 아님). 설정되어 있지 않으면 특허 동향 조회만 건너뛰고 나머지 파이프라인은 정상 동작합니다. |
| `FORCE_PATENT_REFRESH` | 선택 | `"true"`로 설정하면 요일과 무관하게 특허 동향을 즉시 재조회합니다(기본은 매주 월요일에만 조회). |
| `FORCE_FULL_REFRESH` | 선택 | `"true"`로 설정하면 오늘 이미 갱신 이력이 있어도 강제로 다시 실행합니다(기본은 같은 날 중복 실행을 자동으로 건너뛰어 API 할당량을 절약). |

### GitHub Actions에서의 설정
`daily_update.yml` 워크플로우는 위 값을 GitHub 저장소의 **Settings → Secrets and variables → Actions**에 등록된 시크릿에서 읽어옵니다.
- `GEMINI_API_KEY`, `GCP_SA_KEY_JSON` 시크릿을 등록해야 합니다.
- `FORCE_PATENT_REFRESH`/`FORCE_FULL_REFRESH`는 시크릿이 아니라, Actions 탭에서 워크플로우를 수동 실행(`workflow_dispatch`)할 때 체크박스로 지정합니다.

### 로컬 실행 시 설정
PowerShell 기준 예시:

```powershell
$env:GEMINI_API_KEY = "발급받은 API 키"
$env:GCP_SA_KEY_JSON = Get-Content "_gcp_sa_key.json" -Raw   # 특허 동향 조회가 필요할 때만
python update_dashboard.py
```

`_gcp_sa_key.json`(GCP 서비스 계정 키 파일)은 `.gitignore`에 등록되어 저장소에 커밋되지 않으므로, 로컬에서 특허 동향 기능을 테스트하려면 직접 발급받아 프로젝트 루트에 두어야 합니다.
