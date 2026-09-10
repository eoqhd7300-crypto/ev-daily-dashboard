"""
Daily EV & Battery Dashboard Data Updater
------------------------------------------
매일 실행되어 다음 두 단계로 data.json 을 재생성합니다.
1) 뉴스: Google News RSS(무료, API 키/과금 불필요)에서 실제 기사 링크/발행일을 그대로 가져오고,
   Gemini에는 "이미 가져온 기사 내용을 한글로 요약"만 시킵니다 (검색 도구 미사용).
2) 차량 스펙: Gemini에 알고 있는 최신 지식으로 신차 스펙을 정리하게 합니다 (검색 도구 미사용).

Google Search grounding 도구를 전혀 사용하지 않으므로, 별도 결제(billing) 연결 없이도
무료 티어 할당량 안에서 안정적으로 매일 동작합니다.
index.html 은 이 data.json 을 fetch 하여 화면을 갱신합니다.
(GEMINI_API_KEY 가 없거나 호출이 모두 실패하면 기존 data.json 을 그대로 두고 종료합니다.)
"""

import html
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from google import genai

KST = timezone(timedelta(hours=9))
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

MAX_VEHICLES = 80  # 누적 상한 (최근 1년치 데이터를 충분히 보유)
MAX_NEWS = 20       # 항상 최신 20건만 유지 (기존 대시보드 사양과 동일)
NEWS_POOL_SIZE = 120  # 유사기사/비기술 기사 필터링 전, RSS에서 확보해 둘 후보 기사 풀 크기
# (엔지니어 관심 타겟 쿼리 추가 + 엄격한 기술 관련성 필터링으로 걸러내는 양이 늘어난 만큼,
# 필터링 후에도 최대한 20건에 근접하게 채울 수 있도록 후보 풀을 넘리 확보한다)

# China EV & 배터리 뉴스: 정보 출처를 아래 2개 사이트로 한정 (공식 RSS 피드 사용 - 웹 스크래핑 대비
# UI 개편에 안전하고 봇 차단 위험이 없음)
CHINA_NEWS_FEEDS = [
    ("CnEVPost", "https://cnevpost.com/feed/"),
    ("CarNewsChina", "https://carnewschina.com/category/electric-vehicles/feed/"),
]
MAX_CHINA_NEWS = 20
CHINA_NEWS_POOL_SIZE = 40

# 서로 다른 매체가 동일 사건을 보도해 제목만 비슷한 "유사 기사"를 걸러내기 위한 임계값
TITLE_DUP_SIMILARITY_THRESHOLD = 0.72

# 엔지니어 관점(전기차/배터리팩/셀 기술) 관련성 판단용 키워드.
# Gemini 필터링이 실패했을 때의 최후 안전망(fallback)으로만 사용한다.
ENGINEERING_KEYWORDS = [
    "배터리", "셀", "팩", "모듈", "전고체", "반고체", "양극재", "음극재", "전해질", "분리막",
    "에너지밀도", "에너지 밀도", "급속충전", "완속충전", "열관리", "냉각", "발화", "화재 원인",
    "안전성", "BMS", "재활용", "리사이클", "특허", "기술 개발", "양산", "파일럿 라인", "기가팩토리",
    "리튬", "니켈", "코발트", "실리콘 음극", "리튬인산철", "LFP", "NCM", "NCMA", "NCA", "나트륨이온",
    "폼팩터", "각형", "원통형", "파우치", "에너지저장", "ESS", "사이클 수명", "초고속충전", "800V",
    "kWh", "Wh/kg", "battery", "cell chemistry", "solid-state", "semi-solid", "cathode", "anode",
    "electrolyte", "separator", "energy density", "thermal management", "cooling system",
    "recycling", "patent", "gigafactory", "silicon anode", "sodium-ion", "cylindrical", "prismatic",
    "pouch cell", "cycle life", "fast charging", "teardown", "분해",
]

# 기술 콘텐츠 없이 시장/실적/소비자 팁 위주인 기사를 걸러내기 위한 제외 키워드.
# ENGINEERING_KEYWORDS의 "배터리" 같은 단어가 워낙 광범위해서, 시장점유율/실적 발표 기사도
# 함께 통과되는 문제가 있었다 - 아래 키워드가 하나라도 있으면 기술 기사가 아닌 것으로 간주한다.
NON_TECHNICAL_EXCLUDE_KEYWORDS = [
    "점유율", "판매량", "판매 순위", "베스트셀링", "매출", "영업이익", "실적", "주가", "캐즘",
    "순위", "랭킹", "top10", "톱10", "할인", "프로모션", "이벤트", "사전예약", "시승기",
    "충전하는 법", "충전 요령", "가격 비교", "가성비", "색상 공개", "트림 구성", "리스", "렌트",
    "딜러", "인수합병", "지분 투자", "상장", "배당", "혁신기술센터 설립", "브랜드 개발 현지화",
    "고금리", "금리 인상", "환율",
]

MODEL_NAME = "gemini-3.6-flash"

# Google News RSS 검색 쿼리 (실제 기사 링크/발행일을 그대로 가져오기 위함)
# 쿼리를 다양화해 하루 20건 이상의 고유 기사를 안정적으로 확보한다.
GOOGLE_NEWS_QUERIES = [
    "전기차 신차 출시",
    "전기차 배터리",
    "전기차 배터리 기술",
    "전기차 화재",
    "배터리 공장",
    "EV battery",
    "electric vehicle battery",
    "solid-state battery",
    "EV charging technology",
    "battery recycling",
    # 국내 주요 매체 8곳 지정 수집 (site: 연산자로 해당 매체 도메인의 기사만 조회)
    "site:joongang.co.kr 전기차 OR 배터리",
    "site:donga.com 전기차 OR 배터리",
    "site:chosun.com 전기차 OR 배터리",
    "site:mk.co.kr 전기차 OR 배터리",
    "site:sedaily.com 전기차 OR 배터리",
    "site:dt.co.kr 전기차 OR 배터리",
    "site:hankyung.com 전기차 OR 배터리",
    "site:edaily.co.kr 전기차 OR 배터리",
    # 엔지니어 관심사(안전/성능/환경/소재/신기술/개발) 타겟 검색어 - 일반 키워드 검색보다 기술 기사 획득률이 높다.
    "배터리 안전성 연구",
    "배터리 열관리 OR 열폭주",
    "배터리 성능 시험 OR 성능 향상",
    "배터리 재활용 OR 환경규제",
    "양극재 OR 음극재 OR 전해질 신소재",
    "전고체배터리 개발",
    "배터리 연구개발 OR R&D",
]

# Google News RSS의 <source url="..."> 도메인이 아래 패턴에 매칭되면, 계열사/서브도메인 바이라인
# (예: 한경매거진&북, 모바일한경)을 대표 매체명으로 통일해 하나의 매체로 인식되게 한다.
SOURCE_NAME_OVERRIDES = {
    "hankyung.com": "한국경제",
}

# site: 지정 검색이 원하는 본지(종합지/경제지) 기사 외에, 같은 도메인의 스포츠/연예 서브브랜드까지
# 함께 가져오는 경우가 있어(예: 스포츠동아), 기술과 무관한 이런 매체는 출처명 기준으로 원천 제외한다.
# 또한 일반(비 site:) 키워드 검색은 전국의 모든 매체를 대상으로 하므로, 지역 케이블방송/보도자료
# 배포 전문 군소 통신사처럼 편집 품질이 낮은 매체도 함께 섞여 들어온다 - 이런 매체도 함께 제외한다.
EXCLUDED_SOURCE_NAMES = {
    # 스포츠/연예 서브브랜드
    "스포츠동아", "스포츠조선", "스포츠경향", "스포츠서울", "일간스포츠",
    "스타투데이", "마이데일리", "OSEN", "뉴스엔", "톱스타뉴스", "텐아시아",
    # 지역 케이블방송/보도자료 배포 전문 매체 (기술 기사 편집 품질이 낮음)
    "LG헬로비전", "세계뉴스통신", "HCN", "딜라이브",
}


def _is_excluded_source(source: str, source_domain: str = "") -> bool:
    """기술과 무관한 스포츠/연예 서브브랜드 매체인지 판단한다. Google이 출처를 사람이 읽는
    매체명 대신 원시 도메인으로 줄 때도 있으므로(예: "sports.donga.com"), 이름 기준 목록
    (EXCLUDED_SOURCE_NAMES) 매칭뿐 아니라 도메인/이름에 남은 sports./star. 패턴도 함께 검사한다."""
    combined = f"{source} {source_domain}".lower()
    return source in EXCLUDED_SOURCE_NAMES or bool(re.search(r"(?:^|[./])(sports|star)\.", combined))


# "OO년 O월 배터리 사용량/점유율 순위" 류의 SNE Research 시장리포트는 거의 매달 수십개 매체가
# 거의 동일한 내용을 제목만 바꿔 재게재한다. 제목 유사도만으로는 거러내지 못하므로 주제 단위로 따로 감지한다.
MARKET_SHARE_REPORT_TRIGGERS = ["점유율", "사용량", "판매량 순위", "랑킹", "top10", "톱10"]


def _market_share_topic_key(title: str, date: str) -> str | None:
    """제목이 반복적으로 재게재되는 '배터리 사용량/점유율 순위' 류 리포트면 해당 월(YYYY-MM)을 키로 만든다.
    헤드라인마다 강조된 제조사가 다르더라도 같은 월의 동일 리포트로 보고 중복 제거한다.
    해당되지 않으면 None을 반환해 일반 제목 유사도 비교만 적용되게 한다."""
    lowered = (title or "").lower()
    if "배터리" not in lowered and "battery" not in lowered:
        return None
    if not any(trigger in lowered for trigger in MARKET_SHARE_REPORT_TRIGGERS):
        return None
    month = (date or "")[:7]  # YYYY-MM
    return f"market_share|{month}"

# 실제로 채워 넣은 예시 1건 - 모델이 이 스타일/디테일 수준을 그대로 모방하도록 함
VEHICLE_FILLED_EXAMPLE = {
    "id": "byd_fangchengbao_ti7_dmi",
    "selected": True,
    "releaseDate": "2026-01-14",
    "name": "方程豹 钛7 DM-i (BYD Fang Cheng Bao Ti 7)",
    "brand": "BYD (비야디 / 方程豹 Fangchengbao)",
    "type": "중대형 오프로드 SUV",
    "timeline": "2026년 01월 출시",
    "priceLocal": "¥239,800 RMB",
    "priceKRW": "약 4,832만 원",
    "batterySpec": "50 kWh 2세대 Blade Battery (LFP)",
    "cellMaker": "FinDreams (BYD 자회사)",
    "packMaker": "FinDreams Battery (BYD 자체)",
    "qcPerformance": "3.5C Peak (최대 180kW) | SOC 10% → 80% (약 16분)",
    "rangePerformance": "CLTC EV 315km / 합산 1,300km+ (DMO 오프로드 300kW)",
    "overview": "BYD DMO 플랫폼 기반 50kWh 대용량 LFP 블레이드 배터리를 탑재해 pure EV 모드로만 315km 주행 구현.",
    "adMessage": "Super Hybrid Off-road - 315km Pure Electric Long Range",
    "dimensions": "4,890mm × 1,970mm × 1,920mm / WB: 2,800mm",
    "powertrain": "DMO Dual Motor AWD (합산 최고출력 300kW / 408ps, 최대토크 650Nm)",
    "packInfo": "CTB 오프로드 특화 고강성 알루미늄 블레이드 팩",
    "cellInfo": "2세대 High-Safety LFP Blade Cell",
    "chargingSafety": "하부 3중 샌드위치 스틸 아머 보호 및 수심 1m 직접 침수 안전 인증",    "trim": "Premium AWD",
    "topSpeed": "180km/h",
    "zeroToHundred": "4.8초",
    "maxOutput": "300kW / 408ps",
    "maxChargePower": "180kW",
    "packVoltage": "400V",
    "packType": "CTB (Cell-to-Body)",
    "cellType": "각형 (Prismatic)",
    "coolingMethod": "액체 냉각 (Cooling Plate)",
    "packCapacityAh": "-",
    "packDimensions": "-",
    "packWeight": "-",
    "packMinusCellWeight": "-",
    "cellToPackWeightRatio": "-",
    "packEnergyDensity": "-",
    "cellConfiguration": "-",
    "cellEnergy": "-",
    "cellCapacityAh": "-",
    "cellComposition": "-",
    "cellDimensionsMeasured": "-",
    "cellWeightMeasured": "-",
    "cellEnergyDensity": "-",}

def _last_12_months(today_str: str) -> list:
    year, month = int(today_str[:4]), int(today_str[5:7])
    labels = []
    for i in range(12):
        m = month - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        labels.append(f"{y}-{m:02d}")
    return list(reversed(labels))


def build_vehicle_prompt(today_str: str) -> str:
    months = _last_12_months(today_str)
    months_list = ", ".join(months)
    return f"""
당신은 글로벌 전기차(EV) 및 배터리 산업 전문 애널리스트입니다.
오늘 날짜는 {today_str} (KST) 입니다. 당신이 알고 있는 지식 범위 내에서 아래 JSON 스키마에
맞춰 순수 JSON 한 개만 응답하세요. 마크다운 코드블록이나 설명 문장은 절대 포함하지 마세요.

vehicles 배열의 각 항목은 반드시 아래 예시와 동일한 수준의 상세함을 갖춰야 합니다 (이 예시의 문장 형식과 정보량을 그대로 모방하세요):
{json.dumps(VEHICLE_FILLED_EXAMPLE, ensure_ascii=False, indent=2)}

응답 JSON 구조:
{{
  "vehicles": [ 위 예시와 같은 형식의 객체 15~25건 ]
}}

규칙:
- vehicles는 아래 12개 월(YYYY-MM) 전체를 반드시 커버해야 합니다. 각 월마다 최소 1건 이상의 서로 다른 실제 차량을 배정하세요 (한 달에만 몰아넣지 마세요):
  {months_list}
  releaseDate의 연-월(YYYY-MM)이 위 12개 월 중 하나와 일치해야 합니다.
- releaseDate(발표일)와 timeline(출시/예상시점)은 서로 다른 개념입니다. releaseDate는 언론/업계에 공식 공개된 날짜이고, timeline은 실제 판매가 시작되거나 시작될 시점입니다. 실제로 알려진 차량은 발표와 출시 사이에 수주~수개월의 시차가 있는 경우가 많으므로, 이 둘을 동일한 달로 기계적으로 맞추지 말고 실제 맥락을 반영하세요 (예시처럼 발표후 수개월 뒤에 출시되는 경우가 흔함). 아직 출시되지 않았다면 timeline에 "출시예정"을 명시하세요.
- cellMaker/packMaker는 반드시 '영문사명 (한글표기)' 형식으로 예시처럼 상세히 표기하세요 (예: 'CATL (닝더시대)'). 간략화나 생략 금지.
- cellMaker는 특히 실수가 잦은 필드입니다. "이 브랜드는 보통 OO사를 쓴다"는 식으로 다른 차종의 공급사를 유추해 넣지 말고, 반드시 해당 "이 정확한 모델/트림"에 대해 실제로 확인된 공급 계약·보도자료가 있는 경우에만 기재하세요. 확신이 없으면 추측하지 말고 "확인 필요"라고 쓰세요.
- qcPerformance는 반드시 예시처럼 'X.XC Peak (최대 XXXkW) | SOC 10% → 80% (약 XX분)' 형식으로 C-rate, 최대 출력(kW), 충전시간을 모두 포함하세요.
- rangePerformance는 예시처럼 주행거리 수치 외에 가속성능/모터 출력/충전방식 등 추가 기술 정보를 함께 포함하세요. 단순 수치 한 개만 쓰는 요약형 문장은 금지.
- 추정/허구 데이터 금지. 실제로 확인되지 않는 수치는 만들지 말고 "정보 없음"을 넣으세요.
- id 값은 모두 서로 달라야 합니다.
- 이러한 필드(trim, topSpeed, zeroToHundred, maxOutput, maxChargePower, packVoltage, packType, cellType, coolingMethod)는 제조사 공식 보도자료/언론에 흔히 명시되는 정보이므로, 실제로 알고 있는 값을 최대한 채우고 모르면 "-"를 넣으세요.
- 이러한 필드(packCapacityAh, packDimensions, packWeight, packMinusCellWeight, cellToPackWeightRatio, packEnergyDensity, cellConfiguration, cellEnergy, cellCapacityAh, cellComposition, cellDimensionsMeasured, cellWeightMeasured, cellEnergyDensity)는 차량을 실제 분해해야만 알 수 있는 실측치로, 공식 자료에는 거의 공개되지 않습니다. 매우 유명하고 기술적으로 널리 보도된 차량(예: Tesla, BYD Blade Battery 등)에 대해서만, 실제로 신뢰할 수 있게 알고 있는 값이 있으면 채우고, 조금이라도 불확실하면 절대 임의로 만들지 말고 "-"를 넣으세요. 이 필드들은 대시보드에 "미검증(AI 추정)" 배지가 자동 표시되므로, 확신 없는 값은 절대 넣지 마세요.
- 어떤 경우에도 사과, 거절, 설명 문구를 출력하지 말고 위 JSON 구조만 응답하세요. 보유한 지식 중 가장 최근 정보로 추론해서 채우세요.
"""


def build_news_summary_prompt(raw_items: list) -> str:
    items_json = json.dumps(raw_items, ensure_ascii=False, indent=2)
    return f"""
아래는 RSS로 수집한 실제 전기차/배터리 관련 뉴스 원본 후보 목록입니다 (title, url, date, source, description 포함).
당신은 배터리/전기차 엔지니어를 위한 뉴스 큐레이터입니다. 이 목록에서 "엔지니어 관점에서 기술적으로 의미 있는 기사"만 선별하고,
같은 사건을 다룬 유사 기사는 1건만 남긴 뒤, 선택한 기사에 한글 요약(summary)을 작성해 JSON 배열로만 응답하세요.

선별 기준 (반드시 지킬 것):
- 포함: 배터리 셀/모듈/팩 기술(화학조성, 폼팩터, 에너지밀도, 열관리/냉각, BMS), 충전 기술(급속/초고속/충전속도),
  소재/공정(양극재·음극재·전해질·분리막, 실리콘음극, 전고체/반고체), 안전성/화재 원인 분석, 리사이클링/재활용 기술,
  특허/R&D/양산 기술, 분해(teardown)/실측 분석, 배터리 공장/생산기술, 환경(용매·유해물질 검출 등 제조 공정 이슈) 등
  실제 전기차/배터리 산업 엔지니어(안전·성능·환경·소재·신기술·개발 담당)에게 실질적으로 유용한 기술적 내용이 있는 기사.
- 제외(기술 콘텐츠가 없는 기사는 "배터리"라는 단어가 제목에 있어도 반드시 제외):
  * 시장점유율/판매량 순위/베스트셀링 통계 기사 (예: "OO월 배터리 사용량 20%↑…중국 점유율 73%")
  * 실적/매출/영업이익/주가/캐즘 탈출 등 재무·경영 관점 기사 (예: "LG엔솔, 캐즘 탈출…북미 반등")
  * 금리/거시경제 관점에서 배터리 업계를 다루는 기사 (예: "고금리에 전기차 부담…배터리 3사 ESS로 돌파구")
  * 단순 판매량/가격/할인/프로모션/시승기/색상·옵션 소개/이벤트/사전예약/딜러 소식 등 순수 마케팅·영업성 기사
  * 소비자 팁/생활 정보성 기사 (예: "싸게 전기차 충전하는 법", "고온다습한 날씨에도 강한 내구성" 같은 소비자 대상 홍보성 기사)
  * 합작법인/브랜드 현지화 등 사업 제휴 발표이지만 구체적 기술 내용이 없는 기사 (예: "OO와 혁신기술센터 설립…브랜드 개발 현지화")
  기사 제목에 차종명과 함께 배터리 용량(kWh) 등 스펙이 언급되더라도, 기사 전체가 신차 출시/가격 안내 위주라면 제외하세요.
- 같은 사건이나 같은 연구/발표(예: 특정 대학·기업의 공동 연구 결과, 리콜, 화재 사고)를 서로 다른 매체가 다른 제목/각도로
  보도한 경우, 제목이 크게 달라 보여도 같은 사건이면 가장 정보가 상세한 1건만 선택하고 나머지는 제외하세요
  (언론사가 다르다는 이유만으로 중복 포함하지 마세요. 같은 보도자료를 바탕으로 한 기사는 모두 "같은 사건"입니다).
- title, url, date, source 값은 선택한 항목에 대해 절대 변경하지 말고 원본 그대로 유지하세요.
- summary는 description 내용을 바탕으로 자연스러운 한글 뉴스 요약 문장(1~2문장)으로 작성하세요 (직역이 아니라 핵심 내용 요약).
  description이 비어있거나 정보가 부족하면 title을 근거로 합리적으로 요약하세요.
- 최대 {MAX_NEWS}건까지, 최신순으로 선택하세요. 기준을 통과하는 기사가 적으면 그보다 적은 건수만 반환해도 됩니다.
- 마크다운 코드블록이나 설명 문장 없이 순수 JSON 배열만 응답하세요.

원본 후보 목록:
{items_json}

응답 형식 (배열, 각 원소는 아래 5개 필드만 포함, 선택된 기사만):
[
  {{"title": "...", "summary": "...", "source": "...", "date": "YYYY-MM-DD", "url": "..."}}
]
"""


def build_news_briefing_prompt(news_items: list) -> str:
    items_json = json.dumps(
        [
            {
                "title": n.get("title", ""),
                "summary": n.get("summary", ""),
                "source": n.get("source", ""),
                "date": n.get("date", ""),
                "url": n.get("url", ""),
            }
            for n in news_items
        ],
        ensure_ascii=False,
        indent=2,
    )
    return f"""
아래는 오늘 대시보드에 실릴 전기차/배터리 기술 뉴스 목록입니다 (이미 엔지니어 관점으로 선별된 최종 기사들입니다).
당신은 배터리/전기차 산업 전문 애널리스트입니다. 이 기사들을 바탕으로 메인 대시보드에 표시할 "카테고리별 브리핑 리포트"를
아래 JSON 스키마에 맞춰 작성하세요.

작성 규칙:
1. categories: 기사들을 "기술/소재", "기업 동향/공급망", "시장/산업/규제", "안전성/열관리" 등 주제별로 2~4개 그룹으로 묶으세요.
   각 그룹 이름(name)은 기사 내용에 맞게 자유롭게 지어도 됩니다. 한 기사는 가장 적합한 그룹 하나에만 배치하세요.
2. 각 그룹의 items는 해당 그룹에 속한 기사 각각에 대해 아래 필드를 작성하세요.
   - headline: 기사 제목을 그대로 쓰지 말고, 핵심을 압축한 짧은 소제목(15자 내외)
   - summary: 기사 내용을 1문장으로 핵심만 요약 (원본 summary를 참고해 더 간결하게)
   - tags: 이 기사의 핵심 키워드 2~4개를 "#키워드" 형태 문자열 배열로 작성 (예: "#전고체", "#LG엔솔", "#열관리")
   - source, date, url: 입력값을 그대로 유지 (변경 금지)
3. keyTakeaways: 오늘 전체 기사를 관통하는 핵심 흐름 2~3개를 한 문장씩 배열로 작성하세요 (제목 나열이 아니라 종합적 인사이트).
4. implications: "이 뉴스들이 배터리 산업에 미치는 영향"을 2줄 이내(공백 포함 약 120자 이내)로 종합 분석하세요.
5. 모든 기사(url 기준)를 반드시 어느 한 카테고리에는 포함시키세요 (누락 금지). 순서는 최신순을 우선하되 카테고리 응집성을 우선하세요.
6. 마크다운 코드블록이나 설명 문장 없이 순수 JSON 객체만 응답하세요.

원본 기사 목록:
{items_json}

응답 형식 (JSON 객체 하나):
{{
  "keyTakeaways": ["...", "..."],
  "categories": [
    {{
      "name": "...",
      "items": [
        {{"headline": "...", "summary": "...", "tags": ["#...", "#..."], "source": "...", "date": "YYYY-MM-DD", "url": "..."}}
      ]
    }}
  ],
  "implications": "..."
}}
"""


def build_china_news_summary_prompt(raw_items: list) -> str:
    items_json = json.dumps(raw_items, ensure_ascii=False, indent=2)
    return f"""
아래는 CnEVPost, CarNewsChina(둘 다 중국 전기차/배터리 산업 전문 영어 매체) 공식 RSS 피드에서 수집한 실제 기사
후보 목록입니다 (title, url, date, source, description 포함, 원문은 영어입니다).
당신은 한국 배터리/완성차 업계 실무자를 위한 "중국 EV/배터리 산업 동향" 큐레이터입니다. 아래 기준에 따라 기사를 선별한 뒤,
선택한 기사의 title과 summary를 자연스러운 한국어로 번역/요약해 JSON 배열로만 응답하세요.

선별 기준:
- 포함: 중국 브랜드(BYD, CATL, Nio, Xpeng, Li Auto, Xiaomi, Zeekr, Huawei/Aito, Geely 등)의 신차/신기술 발표,
  배터리 셀/소재/생산기술, 공급망(해외 진출/합작투자/수출), 산업 정책/규제, 경쟁 구도를 보여주는 시장 동향(점유율/수출 등),
  자율주행/소프트웨어 등 한국 업계 실무자가 경쟁 동향 파악에 참고할 만한 내용.
- 제외: 단순 가격 할인/프로모션/이벤트성 기사, 정보가 거의 없는 스팟성 가십, 같은 사건을 다룬 중복 기사
  (그 중 가장 정보가 상세한 1건만 유지).
- title은 한국어로 자연스럽게 의역(직역 금지, 핵심이 드러나게 재구성), summary는 description을 바탕으로
  1~2문장 한국어 요약으로 작성하세요. description이 비어있으면 title을 근거로 합리적으로 요약하세요.
- url, date, source 값은 선택한 항목에 대해 절대 변경하지 말고 원본 그대로 유지하세요.
- 최대 {MAX_CHINA_NEWS}건까지, 최신순으로 선택하세요. 기준을 통과하는 기사가 적으면 그보다 적은 건수만 반환해도 됩니다.
- 마크다운 코드블록이나 설명 문장 없이 순수 JSON 배열만 응답하세요.

원본 후보 목록:
{items_json}

응답 형식 (배열, 각 원소는 아래 5개 필드만 포함, 선택된 기사만):
[
  {{"title": "...(한국어)", "summary": "...(한국어)", "source": "...", "date": "YYYY-MM-DD", "url": "..."}}
]
"""


def build_china_news_briefing_prompt(news_items: list) -> str:
    items_json = json.dumps(
        [
            {
                "title": n.get("title", ""),
                "summary": n.get("summary", ""),
                "source": n.get("source", ""),
                "date": n.get("date", ""),
                "url": n.get("url", ""),
            }
            for n in news_items
        ],
        ensure_ascii=False,
        indent=2,
    )
    return f"""
아래는 오늘 대시보드에 실릴 "중국 EV/배터리 산업 동향" 뉴스 목록입니다 (CnEVPost/CarNewsChina에서 선별 및
한국어로 번역된 기사들입니다). 당신은 중국 전기차/배터리 산업 전문 애널리스트입니다. 이 기사들을 바탕으로
"카테고리별 브리핑 리포트"를 아래 JSON 스키마에 맞춰 작성하세요.

작성 규칙:
1. categories: 기사들을 "중국 브랜드 신차/기술", "배터리·소재·공급망", "시장·정책·수출" 등 주제별로 2~4개
   그룹으로 묶으세요. 그룹 이름은 기사 내용에 맞게 자유롭게 지어도 됩니다. 한 기사는 가장 적합한 그룹 하나에만 배치하세요.
2. 각 그룹의 items는 아래 필드를 작성:
   - headline: 핵심을 압축한 짧은 소제목(15자 내외, 한국어)
   - summary: 1문장 핵심 요약(한국어)
   - tags: 핵심 키워드 2~4개를 "#키워드" 형태 문자열 배열로 작성 (예: "#BYD", "#해외진출", "#CATL")
   - source, date, url: 입력값을 그대로 유지 (변경 금지)
3. keyTakeaways: 오늘 중국 EV/배터리 산업 전체를 관통하는 핵심 흐름 2~3개를, 가능하면 한국 업계 관점의 시사점을
   담아 한 문장씩 배열로 작성하세요.
4. implications: "이 중국 동향이 한국 배터리/완성차 업계에 미치는 영향"을 2줄 이내(공백 포함 약 120자 이내)로 분석하세요.
5. 모든 기사(url 기준)를 반드시 어느 한 카테고리에는 포함시키세요 (누락 금지).
6. 마크다운 코드블록이나 설명 문장 없이 순수 JSON 객체만 응답하세요.

원본 기사 목록:
{items_json}

응답 형식 (JSON 객체 하나):
{{
  "keyTakeaways": ["...", "..."],
  "categories": [
    {{
      "name": "...",
      "items": [
        {{"headline": "...", "summary": "...", "tags": ["#...", "#..."], "source": "...", "date": "YYYY-MM-DD", "url": "..."}}
      ]
    }}
  ],
  "implications": "..."
}}
"""


def extract_json(text):
    """모델 응답에서 JSON(dict 또는 list)을 추출한다. 마크다운 펜스/부연설명을 허용한다."""
    cleaned = re.sub(r"^```json\s*", "", (text or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```\s*$", "", cleaned)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 모델이 설명/거절 문구를 섞어 보낸 경우, JSON 시작/종료 문자만 추출해 재시도
        first_brace = cleaned.find("{")
        first_bracket = cleaned.find("[")
        candidates = [i for i in (first_brace, first_bracket) if i != -1]
        if not candidates:
            raise
        start = min(candidates)
        closing = "}" if cleaned[start] == "{" else "]"
        end = cleaned.rfind(closing)
        if end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def fetch_google_news_rss(query: str, max_items: int = 15) -> list:
    """Google News RSS(무료, API 키 불필요)에서 실제 기사 목록을 가져온다."""
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=ko&gl=KR&ceid=KR:ko"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"RSS fetch 실패 ({query}): {exc}")
        return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        print(f"RSS 파싱 실패 ({query}): {exc}")
        return []

    items = []
    for item in root.findall(".//item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = item.findtext("pubDate") or ""
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        source_domain = (source_el.get("url") or "") if source_el is not None else ""
        for domain, override_name in SOURCE_NAME_OVERRIDES.items():
            if domain in source_domain:
                source = override_name
                break
        # Google이 출처를 사람이 읽는 매체명 대신 원시 도메인으로 줄 때도 있음(예: "sports.donga.com").
        # 이런 경우 EXCLUDED_SOURCE_NAMES(친숙한 이름 기준) 매칭을 우회하므로, 도메인 자체도 함께 검사한다.
        if _is_excluded_source(source, source_domain):
            continue  # 기술과 무관한 스포츠/연예 서브브랜드 기사는 원천 제외
        description = _strip_html(item.findtext("description") or "")
        try:
            pub_dt = parsedate_to_datetime(pub_date_raw).astimezone(KST)
            date_str = pub_dt.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            date_str = ""
        if not title or not link or not date_str:
            continue
        items.append(
            {
                "title": title,
                "url": link,
                "date": date_str,
                "source": source or "Google News",
                "description": description,
            }
        )
    return items


def fetch_generic_rss(source_name: str, url: str, max_items: int = 20) -> list:
    """표준 WordPress RSS 2.0 피드(CnEVPost/CarNewsChina 등, source 태그 없이 title/link/pubDate/description만
    있는 형식)에서 기사 목록을 가져온다. Google News RSS와 달리 매체명이 피드 자체에 없으므로 source_name을
    호출부에서 직접 지정한다."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"China RSS fetch 실패 ({source_name}): {exc}")
        return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        print(f"China RSS 파싱 실패 ({source_name}): {exc}")
        return []

    items = []
    for item in root.findall(".//item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = item.findtext("pubDate") or ""
        description = _strip_html(item.findtext("description") or "")
        try:
            pub_dt = parsedate_to_datetime(pub_date_raw).astimezone(KST)
            date_str = pub_dt.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            date_str = ""
        if not title or not link or not date_str:
            continue
        items.append(
            {
                "title": title,
                "url": link,
                "date": date_str,
                "source": source_name,
                "description": description,
            }
        )
    return items


def collect_china_news_raw(pool_size: int = CHINA_NEWS_POOL_SIZE) -> list:
    """CnEVPost/CarNewsChina 공식 RSS 피드에서만 기사를 수집한다 (정보 출처를 이 2개 사이트로 한정)."""
    collected = []
    for source_name, url in CHINA_NEWS_FEEDS:
        collected.extend(fetch_generic_rss(source_name, url, max_items=pool_size))

    seen = set()
    deduped = []
    for item in collected:
        key = _norm(item["url"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    deduped.sort(key=lambda n: n["date"], reverse=True)
    return dedup_similar_titles(deduped)[:pool_size]


def _normalize_title_for_dedup(title: str) -> str:
    """제목 유사도 비교용 정규화: 언론사 접미사/괄호/구두점/공백을 제거하고 소문자로 통일."""
    text = re.sub(r"\s*-\s*[^-]{1,20}$", "", title or "")  # Google 뉴스가 붙이는 "- 언론사명" 접미사 제거
    text = re.sub(r"[\[\](){}<>'\"“”‘’.,!?…·|]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _is_similar_title(a: str, b: str) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= TITLE_DUP_SIMILARITY_THRESHOLD


def dedup_similar_titles(items: list) -> list:
    """서로 다른 매체가 같은 사건을 보도해 제목만 비슷한 '유사 기사'를 걸러낸다.
    items는 최신순으로 정렬되어 있다고 가정하고, 먼저(=더 최신) 등장한 기사를 남긴다.
    일반 제목 유사도 비교 외에, '배터리 사용량/점유율 순위'처럼 매달 여러 매체가 거의 동일한
    내용을 제목만 바꿔(강조하는 제조사가 달라도) 반복 보도하는 리포트성 기사는 월 단위 주제 키로 추가 중복 제거한다."""
    kept = []
    kept_norms = []
    kept_topic_keys = set()
    for item in items:
        topic_key = _market_share_topic_key(item["title"], item.get("date", ""))
        if topic_key is not None and topic_key in kept_topic_keys:
            continue
        norm = _normalize_title_for_dedup(item["title"])
        if any(_is_similar_title(norm, existing) for existing in kept_norms):
            continue
        kept.append(item)
        kept_norms.append(norm)
        if topic_key is not None:
            kept_topic_keys.add(topic_key)
    return kept


def is_engineering_relevant(item: dict) -> bool:
    """전기차/배터리팩/셀 기술 관점에서 의미 있는 기사인지 키워드 기반으로 판단한다.
    시장점유율/실적/소비자 팁 위주 기사는 "배터리" 등 일반적인 단어가 섞여 있어도 제외한다.
    Gemini의 의미 기반 필터링이 실패했을 때의 최후 안전망(fallback)이자, Gemini가 선택한
    결과에 대해서도 한 번 더 적용하는 최종 방어선으로 사용한다."""
    haystack = f"{item.get('title', '')} {item.get('description', '')}".lower()
    if any(kw.lower() in haystack for kw in NON_TECHNICAL_EXCLUDE_KEYWORDS):
        return False
    return any(kw.lower() in haystack for kw in ENGINEERING_KEYWORDS)


def collect_recent_news_raw(pool_size: int = NEWS_POOL_SIZE) -> list:
    collected = []
    seen = set()
    for query in GOOGLE_NEWS_QUERIES:
        for item in fetch_google_news_rss(query, max_items=20):
            key = _norm(item["url"])
            if not key or key in seen:
                continue
            seen.add(key)
            collected.append(item)
    collected.sort(key=lambda x: x["date"], reverse=True)
    collected = dedup_similar_titles(collected)
    return collected[:pool_size]


def load_existing_data() -> dict:
    if not os.path.exists(DATA_PATH):
        return {"vehicles": [], "news": [], "fx": None, "newsBriefing": None, "chinaNews": [], "chinaNewsBriefing": None}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "vehicles": data.get("vehicles") or [],
            "news": data.get("news") or [],
            "fx": data.get("fx"),
            "newsBriefing": data.get("newsBriefing"),
            "chinaNews": data.get("chinaNews") or [],
            "chinaNewsBriefing": data.get("chinaNewsBriefing"),
        }
    except Exception:  # noqa: BLE001 - 손상된 파일이면 빈 값으로 시작
        return {"vehicles": [], "news": [], "fx": None, "newsBriefing": None, "chinaNews": [], "chinaNewsBriefing": None}


# 환율 조회에 실패했을 때 사용할 최종 안전값 (사이트에 기존에 표시되던 값과 동일)
FALLBACK_FX = {"usdKrw": 1442.96, "eurKrw": 1652.50, "cnyKrw": 201.50}


def fetch_fx_rates(existing_fx: dict | None) -> dict:
    """무료/무키 환율 API(open.er-api.com, 매일 갱신)에서 USD/EUR/CNY -> KRW 환율을 가져온다.
    실패 시 기존 data.json에 저장돼 있던 값, 그마저 없으면 최종 안전값을 사용한다."""
    fallback = existing_fx if isinstance(existing_fx, dict) and existing_fx.get("usdKrw") else FALLBACK_FX
    url = "https://open.er-api.com/v6/latest/USD"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
        rates = payload.get("rates") or {}
        usd_krw = float(rates["KRW"])
        eur_krw = usd_krw / float(rates["EUR"])
        cny_krw = usd_krw / float(rates["CNY"])
        return {
            "usdKrw": round(usd_krw, 2),
            "eurKrw": round(eur_krw, 2),
            "cnyKrw": round(cny_krw, 2),
            "updatedAt": datetime.now(KST).isoformat(),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"환율 조회 실패, 기존/기본값 유지: {exc}")
        return {
            "usdKrw": fallback["usdKrw"],
            "eurKrw": fallback["eurKrw"],
            "cnyKrw": fallback["cnyKrw"],
            "updatedAt": fallback.get("updatedAt", ""),
        }


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_()\[\]/.]", "", (text or "")).lower()


def merge_vehicles(old_vehicles: list, new_vehicles: list) -> list:
    merged: dict[str, dict] = {}
    for v in old_vehicles + new_vehicles:
        key = _norm(v.get("name") or v.get("id") or "")
        if not key:
            continue
        # 동일 차량이면 더 최신 releaseDate를 가진 항목(주로 새로 생성된 쪽)으로 덮어씀
        existing = merged.get(key)
        if existing is None or (v.get("releaseDate") or "") >= (existing.get("releaseDate") or ""):
            merged[key] = v
    result = sorted(merged.values(), key=lambda v: v.get("releaseDate") or "", reverse=True)
    return result[:MAX_VEHICLES]


# A2MAC1 teardown_data.json의 Cell Manufacturer 원문 표기 -> 대시보드 표기 규칙('영문사명 (한글표기)')으로 변환
TEARDOWN_CELL_MAKER_DISPLAY_NAMES = {
    "CATL": "CATL (닝더시대)",
    "CATL-FAW": "CATL-FAW (합작법인)",
    "LG Chem": "LG Energy Solution (LG에너지솔루션)",
    "Samsung SDI": "Samsung SDI (삼성SDI)",
    "SK Innovation": "SK On (SK온)",
    "Panasonic": "Panasonic (파나소닉)",
    "AESC": "AESC (에이이에스씨)",
    "CALB": "CALB (중항리튬)",
    "EVE": "EVE Energy (이브에너지)",
    "Farasis": "Farasis Energy (파라시스)",
    "FinDreams Battery (BYD)": "FinDreams Battery (BYD 자회사)",
    "Gotion": "Gotion High-Tech (궈쉬안)",
    "REPT Saike": "REPT BATTERO (REPT)",
    "SVOLT": "SVOLT (스보르트)",
    "Sunwoda": "Sunwoda (순시다)",
    "Tesla": "Tesla (테슬라 자체)",
    "Ultium Cells": "Ultium Cells (얼티엄셀즈)",
    "Zeekr": "Zeekr (지커 자체)",
}


def _extract_english_name(name: str) -> str:
    """대시보드 vehicle name은 보통 '한글명 (English Name)' 형식이므로 괄호 안 영문명을 추출한다."""
    match = re.search(r"\(([^)]+)\)", name or "")
    return match.group(1) if match else (name or "")


def _model_line_key(english_name: str) -> str:
    """브랜드 + 모델명(숫자/코드)까지만 추출해 트림/버전 차이를 무시하는 매칭 키를 만든다.
    예: 'Tesla Model Y RWD' -> 'tesla model y', 'Kia EV3 GT-Line' -> 'kia ev3',
    'Hyundai Ioniq 9' -> 'hyundai ioniq 9' (Ioniq 5/6/9는 서로 다른 모델이므로 반드시 구분).
    3번째 단어는 그 자체로 모델을 구분짓는 숫자/단일 문자(예: Y, 9)일 때만 포함하고,
    'GT'/'Line' 같은 트림 명칭은 제외한다."""
    words = re.findall(r"[a-zA-Z0-9]+", english_name or "")
    if len(words) < 2:
        return ""
    key_words = words[:2]
    if len(words) > 2 and (words[2].isdigit() or len(words[2]) == 1):
        key_words.append(words[2])
    return " ".join(w.lower() for w in key_words)


def load_teardown_cell_maker_lookup() -> dict:
    """실제 분해(teardown) 데이터로 확인된 Cell Manufacturer를 모델 라인 단위로 조회할 수 있게 로드한다.
    teardown_data.json은 로컬 전용 빌드 산출물이라 없을 수도 있으므로, 없으면 빈 딕셔너리를 반환한다."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "teardown_data.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    lookup = {}
    for v in data.get("vehicles") or []:
        raw_maker = (v.get("cell") or {}).get("Cell Manufacturer")
        if not raw_maker or raw_maker == "-":
            continue
        key = _model_line_key(v.get("name") or "")
        if key:
            lookup.setdefault(key, raw_maker)
    return lookup


def apply_teardown_verified_cell_makers(vehicles: list) -> int:
    """실제 분해 데이터(teardown_data.json)로 확인된 셀 제조사가 있으면, Gemini가 생성한
    cellMaker 추정값을 검증된 값으로 덮어써 모델 오귀속 오류(예: 실제 CATL인데 LG로 잘못 기재)를
    방지한다. 모델 라인(브랜드+모델명)이 일치하는 경우에만 적용해 다른 차량이 잘못 매칭되지 않게 한다."""
    lookup = load_teardown_cell_maker_lookup()
    if not lookup:
        return 0
    updated = 0
    for v in vehicles:
        key = _model_line_key(_extract_english_name(v.get("name") or ""))
        raw_maker = lookup.get(key)
        if not raw_maker:
            continue
        verified = TEARDOWN_CELL_MAKER_DISPLAY_NAMES.get(raw_maker, raw_maker) + " (실측 검증)"
        if v.get("cellMaker") != verified:
            v["cellMaker"] = verified
            updated += 1
    return updated


# Tier1(공식 발표/보도자료 기반) / Tier2(차량 분해 실측 기반, UI에서 미검증 배지 표시) 필드 목록
TIER1_SPEC_FIELDS = [
    "trim", "topSpeed", "zeroToHundred", "maxOutput", "maxChargePower",
    "packVoltage", "packType", "cellType", "coolingMethod",
]
TIER2_SPEC_FIELDS = [
    "packCapacityAh", "packDimensions", "packWeight", "packMinusCellWeight",
    "cellToPackWeightRatio", "packEnergyDensity", "cellConfiguration",
    "cellEnergy", "cellCapacityAh", "cellComposition",
    "cellDimensionsMeasured", "cellWeightMeasured", "cellEnergyDensity",
]
BACKFILL_BATCH_SIZE = 20  # 하루에 보강할 기존 차량 수 (토큰/시간 절약을 위해 점진적으로 진행)
RECHECK_INTERVAL_DAYS = 14  # 이미 확인했지만 여전히 "-"인 필드가 있는 차량을 재확인하는 주기(일)

# teardown 실측 데이터가 없는 신차(예: 아직 분해 전인 신형 모델)의 cellMaker를 무료 Google News
# RSS 검색으로 교차검증하기 위한 설정. 유료 Google Search grounding 없이, 이미 확보한 뉴스 수집
# 인프라(fetch_google_news_rss)를 재사용해 실제 보도에서 언급된 공급사를 찾아낸다.
CELL_MAKER_NEWS_CHECK_BATCH_SIZE = 15  # 하루에 교차검증할 차량 수 (RSS 요청량 제한)
CELL_MAKER_SEARCH_TERMS = [
    ("LG에너지솔루션", "LG Energy Solution (LG에너지솔루션)"),
    ("LG엔솔", "LG Energy Solution (LG에너지솔루션)"),
    ("삼성SDI", "Samsung SDI (삼성SDI)"),
    ("SK온", "SK On (SK온)"),
    ("CATL", "CATL (닝더시대)"),
    ("파나소닉", "Panasonic (파나소닉)"),
    ("비야디", "BYD (비야디)"),
    ("궈쉬안", "Gotion High-Tech (궈쉬안)"),
]


def _extract_korean_prefix(name: str) -> str:
    """대시보드 vehicle name('한글명 (English Name)')에서 괄호 앞 한글 표기를 추출한다."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name or "").strip()


def cross_check_cell_maker_via_news(vehicle_name: str) -> tuple | None:
    """무료 Google News RSS로 '이 차량의 배터리 셀 공급사'를 언급한 실제 보도가 있는지 찾는다.
    검색 결과에 여러 매체가 동일한 공급사를 언급하면 그 값을 반환하고, 결과가 없거나 서로 다른
    공급사가 섞여 나와 확신할 수 없으면 None을 반환해 함부로 덮어쓰지 않는다."""
    korean_name = _extract_korean_prefix(vehicle_name) or vehicle_name
    maker_keywords = " OR ".join(kw for kw, _ in CELL_MAKER_SEARCH_TERMS)
    query = f'"{korean_name}" 배터리 셀 공급 ({maker_keywords})'
    items = fetch_google_news_rss(query, max_items=8)

    found = {}
    source_url = None
    for item in items:
        haystack = f"{item.get('title', '')} {item.get('description', '')}"
        if korean_name not in haystack:
            continue  # 이 차량명이 실제로 언급된 기사만 근거로 인정 (엉뚱한 기사의 오매칭 방지)
        for kw, display in CELL_MAKER_SEARCH_TERMS:
            if kw in haystack:
                found[display] = found.get(display, 0) + 1
                if source_url is None:
                    source_url = item.get("url")
    if len(found) != 1:
        return None  # 결과 없음 또는 서로 다른 공급사가 섞여 나옴 -> 신뢰도 부족, 적용하지 않음
    (only_maker,) = found.keys()
    return only_maker, source_url


def select_cell_maker_check_candidates(vehicles: list, limit: int = CELL_MAKER_NEWS_CHECK_BATCH_SIZE) -> list:
    """teardown 실측으로 이미 확정된("(실측 검증)") 차량과, 최근에 이미 뉴스 교차검증을 시도한
    차량은 제외하고, 아직 한 번도 확인해보지 않은 차량 위주로 하루 batch를 선정한다."""
    candidates = [
        v for v in vehicles
        if "(실측 검증)" not in (v.get("cellMaker") or "")
        and "cellMakerCheckedAt" not in v
    ]
    candidates.sort(key=lambda v: v.get("releaseDate") or "")
    return candidates[:limit]


def apply_news_cross_checked_cell_makers(vehicles: list, today_str: str) -> int:
    """teardown 실측 데이터가 없는 차량에 대해, 무료 뉴스 검색으로 확인 가능한 만큼 cellMaker를
    교차검증한다. 확실한 근거를 찾은 경우에만 "(뉴스 교차검증)" 표시와 함께 갱신한다."""
    updated = 0
    for v in select_cell_maker_check_candidates(vehicles):
        v["cellMakerCheckedAt"] = today_str  # 결과 유무와 관계없이 기록해 매일 재검색하지 않게 함
        result = cross_check_cell_maker_via_news(v.get("name") or "")
        if result is None:
            continue
        maker, source_url = result
        verified = f"{maker} (뉴스 교차검증)"
        if v.get("cellMaker") != verified:
            v["cellMaker"] = verified
            if source_url:
                v["cellMakerSource"] = source_url
            updated += 1
    return updated


def _has_missing_spec_values(v: dict) -> bool:
    """Tier1/Tier2 필드 중 하나라도 아직 미확인(필드 없음 또는 "-")이면 True."""
    for field in TIER1_SPEC_FIELDS + TIER2_SPEC_FIELDS:
        if v.get(field, "-") == "-":
            return True
    return False


def _days_since(date_str: str, today_str: str) -> int:
    try:
        d1 = datetime.strptime(date_str, "%Y-%m-%d")
        d2 = datetime.strptime(today_str, "%Y-%m-%d")
        return (d2 - d1).days
    except Exception:  # noqa: BLE001 - 날짜 형식이 깨져있으면 재확인 대상으로 간주
        return RECHECK_INTERVAL_DAYS


def select_backfill_candidates(vehicles: list, today_str: str, limit: int = BACKFILL_BATCH_SIZE) -> list:
    """우선순위: (1) 한 번도 확인한 적 없는 차량 → (2) 이전에 확인했지만 여전히 "-" 필드가 남아있고
    RECHECK_INTERVAL_DAYS 이상 지난 차량(새로 공개된 정보가 있을 수 있으므로 주기적으로 재확인).
    """
    never_checked = [v for v in vehicles if "specLastCheckedAt" not in v]
    never_checked.sort(key=lambda v: v.get("releaseDate") or "")

    stale_incomplete = [
        v for v in vehicles
        if "specLastCheckedAt" in v
        and _has_missing_spec_values(v)
        and _days_since(v["specLastCheckedAt"], today_str) >= RECHECK_INTERVAL_DAYS
    ]
    stale_incomplete.sort(key=lambda v: v.get("specLastCheckedAt") or "")

    return (never_checked + stale_incomplete)[:limit]


def build_spec_backfill_prompt(candidates: list) -> str:
    slim = [
        {
            "id": v.get("id"),
            "name": v.get("name"),
            "brand": v.get("brand"),
            "type": v.get("type"),
            "batterySpec": v.get("batterySpec"),
            "qcPerformance": v.get("qcPerformance"),
            "rangePerformance": v.get("rangePerformance"),
            "dimensions": v.get("dimensions"),
            "powertrain": v.get("powertrain"),
        }
        for v in candidates
    ]
    return f"""
당신은 글로벌 전기차 스펙 데이터베이스 관리자입니다. 아래는 이미 대시보드에 등록된 차량 목록(간략 정보 포함)입니다.
각 차량에 대해 당신이 알고 있는 지식 범위 내에서 추가 스펙 필드를 채워 JSON 배열로만 응답하세요.

대상 차량 목록:
{json.dumps(slim, ensure_ascii=False, indent=2)}

각 차량에 대해 아래 필드를 채우세요:
- Tier 1 (제조사 공식 발표/보도자료에 흔히 명시되는 정보): trim, topSpeed, zeroToHundred, maxOutput, maxChargePower, packVoltage, packType, cellType, coolingMethod
  실제로 알고 있는 값이면 채우고, 모르면 "-"를 넣으세요.
- Tier 2 (차량 분해 실측 데이터, 공식 자료에 거의 공개되지 않음): packCapacityAh, packDimensions, packWeight, packMinusCellWeight, cellToPackWeightRatio, packEnergyDensity, cellConfiguration, cellEnergy, cellCapacityAh, cellComposition, cellDimensionsMeasured, cellWeightMeasured, cellEnergyDensity
  매우 유명하고 널리 보도된 차량에 대해서만, 실제로 신뢰할 수 있게 알고 있는 값이 있으면 채우고, 조금이라도 불확실하면 절대 임의로 만들지 말고 "-"를 넣으세요.

응답 형식 (배열, 각 원소는 id와 위 필드들만 포함, 원본 name/brand 등은 반복하지 마세요):
[
  {{"id": "차량id", "trim": "...", "topSpeed": "...", "zeroToHundred": "...", "maxOutput": "...", "maxChargePower": "...", "packVoltage": "...", "packType": "...", "cellType": "...", "coolingMethod": "...", "packCapacityAh": "-", "packDimensions": "-", "packWeight": "-", "packMinusCellWeight": "-", "cellToPackWeightRatio": "-", "packEnergyDensity": "-", "cellConfiguration": "-", "cellEnergy": "-", "cellCapacityAh": "-", "cellComposition": "-", "cellDimensionsMeasured": "-", "cellWeightMeasured": "-", "cellEnergyDensity": "-"}}
]

마크다운 코드블록이나 설명 문장 없이 순수 JSON 배열만 응답하세요. 모든 차량의 id를 빠짐없이 포함하세요.
"""


def backfill_tier1_specs(client: "genai.Client", vehicles: list, today_str: str) -> int:
    """아직 확인 안 했거나, 확인했지만 여전히 "-"가 남은 기존 차량들에 한해, 매일 일부씩 스펙을 보강/재확인한다."""
    candidates = select_backfill_candidates(vehicles, today_str)
    if not candidates:
        return 0

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=build_spec_backfill_prompt(candidates),
        )
        filled = extract_json(response.text)
    except Exception as exc:  # noqa: BLE001
        print(f"기존 차량 스펙 보강 실패, 다음 실행에서 재시도합니다: {exc}")
        return 0

    if not isinstance(filled, list):
        return 0

    by_id = {item.get("id"): item for item in filled if isinstance(item, dict) and item.get("id")}
    updated = 0
    for v in candidates:
        patch = by_id.get(v.get("id"))
        if not patch:
            continue
        # 이미 실제 값이 확인된 필드는 절대 덮어쓰지 않고(다운그레이드 방지), "-"/미존재 필드만 새로 발견된 값으로 업그레이드한다.
        for field in TIER1_SPEC_FIELDS + TIER2_SPEC_FIELDS:
            new_val = patch.get(field)
            if new_val and new_val != "-":
                v[field] = new_val
            elif field not in v:
                v[field] = "-"
        v["specLastCheckedAt"] = today_str
        updated += 1
    return updated


def merge_news(old_news: list, new_news: list) -> list:
    # 과거 grounding 실패 시 생성됐던 가짜 검색링크(placeholder, linkType == "search")는
    # 실제 RSS 기사가 아니므로 새 RSS 결과가 있으면 더 이상 유지하지 않고 버린다.
    old_real_news = [n for n in old_news if n.get("linkType") != "search"]
    merged: dict[str, dict] = {}
    for n in old_real_news + new_news:
        key = _norm(n.get("url") or n.get("title") or "")
        if not key:
            continue
        existing = merged.get(key)
        if existing is None or (n.get("date") or "") >= (existing.get("date") or ""):
            merged[key] = n
    result = sorted(merged.values(), key=lambda n: n.get("date") or "", reverse=True)
    # EXCLUDED_SOURCE_NAMES/NON_TECHNICAL_EXCLUDE_KEYWORDS는 그동안 계속 확장되어 왔으므로,
    # 과거에 누적된 기사 중에도 현재 기준으로 제외 대상이 남아있을 수 있다 - 매 실행마다
    # 다시 걸러내 자동으로 정리한다(출처 기준 + 기술 관련성 기준 모두 재검사).
    result = [n for n in result if not _is_excluded_source(n.get("source") or "")]
    result = [n for n in result if is_engineering_relevant({"title": n.get("title", ""), "description": n.get("summary", "")})]
    result = dedup_similar_titles(result)[:MAX_NEWS]
    for idx, item in enumerate(result, start=1):
        item["id"] = idx
    return result


def merge_china_news(old_news: list, new_news: list) -> list:
    """China EV/배터리 뉴스(CnEVPost/CarNewsChina) 병합. 국내 뉴스 파이프라인과 달리 출처가 이미
    2개 사이트로 고정돼 있고 콘텐츠 자체가 EV 산업 전문지라, merge_news처럼 별도의
    출처 제외/엔지니어링 관련성 재검증 없이 url 기준 중복 제거 + 최신순 상한만 적용한다."""
    merged: dict[str, dict] = {}
    for n in old_news + new_news:
        key = _norm(n.get("url") or n.get("title") or "")
        if not key:
            continue
        existing = merged.get(key)
        if existing is None or (n.get("date") or "") >= (existing.get("date") or ""):
            merged[key] = n
    result = sorted(merged.values(), key=lambda n: n.get("date") or "", reverse=True)
    result = dedup_similar_titles(result)[:MAX_CHINA_NEWS]
    for idx, item in enumerate(result, start=1):
        item["id"] = idx
    return result


def generate_vehicles(client: "genai.Client", today_str: str) -> list:
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=build_vehicle_prompt(today_str),
        )
        payload = extract_json(response.text)
        vehicles = payload.get("vehicles") or []
        if not vehicles:
            print("차량 데이터 응답이 비어 있어 차량 목록 갱신을 건너뜁니다.")
        return vehicles
    except Exception as exc:  # noqa: BLE001 - 실패해도 기존 vehicles 유지
        print(f"차량 데이터 생성 실패, 차량 목록 갱신을 건너뜁니다: {exc}")
        return []


def generate_news(client: "genai.Client") -> list:
    print("뉴스 RSS 수집 시작...")
    raw_items = collect_recent_news_raw()
    print(f"뉴스 RSS 수집 완료: 후보 {len(raw_items)}건")
    if not raw_items:
        print("RSS 뉴스 수집 실패(0건) - 뉴스 갱신을 건너뜁니다.")
        return []
    raw_by_key = {_norm(raw["url"]): raw for raw in raw_items}

    selected = []
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=build_news_summary_prompt(raw_items),
        )
        summarized = extract_json(response.text)
        if isinstance(summarized, list):
            for entry in summarized:
                if not isinstance(entry, dict) or not entry.get("url"):
                    continue
                raw = raw_by_key.get(_norm(entry["url"]))
                if raw is None:
                    continue  # 모델이 원본에 없는 url을 만들어낸 경우 방어적으로 제외
                selected.append(
                    {
                        "title": raw["title"],
                        "summary": entry.get("summary") or raw.get("description") or raw["title"],
                        "source": raw["source"],
                        "date": raw["date"],
                        "url": raw["url"],
                        "linkType": "rss",
                    }
                )
    except Exception as exc:  # noqa: BLE001 - 선별/요약 실패 시 키워드 기반 필터로 대체
        print(f"뉴스 선별/요약 생성 실패, 키워드 기반 필터로 대체합니다: {exc}")

    if not selected:
        # Gemini 필터링이 실패했을 때의 최후 안전망: 키워드 기반 엔지니어 관련성 필터만 적용
        for raw in raw_items:
            if not is_engineering_relevant(raw):
                continue
            selected.append(
                {
                    "title": raw["title"],
                    "summary": raw.get("description") or raw["title"],
                    "source": raw["source"],
                    "date": raw["date"],
                    "url": raw["url"],
                    "linkType": "rss",
                }
            )

    selected.sort(key=lambda n: n["date"], reverse=True)
    # Gemini가 선택한 결과라도, 시장점유율/실적/소비자 팁 위주 기사가 섞여 들어올 수 있으므로
    # 제목+요약 기준으로 한 번 더 최종 필터링한다 (Gemini 판단에만 의존하지 않는 방어선).
    selected = [n for n in selected if is_engineering_relevant({"title": n["title"], "description": n.get("summary", "")})]
    return dedup_similar_titles(selected)[:MAX_NEWS]


def generate_news_briefing(client: "genai.Client", news_items: list) -> dict | None:
    """최종 확정된 뉴스 목록을 바탕으로 카테고리별 브리핑(키워드 태깅/시사점 포함)을 생성한다.
    실패 시 None을 반환하며, 호출부에서는 기존 briefing을 유지하거나 프론트가 구버전 문장형으로 대체 표시한다."""
    if not news_items:
        return None
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=build_news_briefing_prompt(news_items),
        )
        payload = extract_json(response.text)
        if not isinstance(payload, dict):
            return None
        categories = payload.get("categories")
        if not isinstance(categories, list) or not categories:
            return None
        # url 기준으로 원본 기사 존재 여부를 검증해, 모델이 지어낸 항목을 방어적으로 제거한다.
        valid_urls = {_norm(n.get("url") or "") for n in news_items}
        cleaned_categories = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            items = cat.get("items")
            if not isinstance(items, list):
                continue
            cleaned_items = [
                item for item in items
                if isinstance(item, dict) and _norm(item.get("url") or "") in valid_urls
            ]
            if cleaned_items:
                cleaned_categories.append({"name": cat.get("name") or "기타", "items": cleaned_items})
        if not cleaned_categories:
            return None
        return {
            "keyTakeaways": [t for t in (payload.get("keyTakeaways") or []) if isinstance(t, str)],
            "categories": cleaned_categories,
            "implications": payload.get("implications") if isinstance(payload.get("implications"), str) else "",
        }
    except Exception as exc:  # noqa: BLE001 - 실패해도 파이프라인은 계속 진행
        print(f"뉴스 브리핑(카테고리/시사점) 생성 실패, 건너뜁니다: {exc}")
        return None


def generate_china_news(client: "genai.Client") -> list:
    """CnEVPost/CarNewsChina 공식 RSS(2개 사이트로 정보 출처 한정)에서 수집한 뒤,
    Gemini로 선별 + 한국어 번역/요약한다."""
    print("China EV 뉴스 RSS 수집 시작...")
    raw_items = collect_china_news_raw()
    print(f"China EV 뉴스 RSS 수집 완료: 후보 {len(raw_items)}건")
    if not raw_items:
        print("China RSS 뉴스 수집 실패(0건) - China 뉴스 갱신을 건너뜁니다.")
        return []
    raw_by_key = {_norm(raw["url"]): raw for raw in raw_items}

    selected = []
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=build_china_news_summary_prompt(raw_items),
        )
        summarized = extract_json(response.text)
        if isinstance(summarized, list):
            for entry in summarized:
                if not isinstance(entry, dict) or not entry.get("url"):
                    continue
                raw = raw_by_key.get(_norm(entry["url"]))
                if raw is None:
                    continue  # 모델이 원본에 없는 url을 만들어낸 경우 방어적으로 제외
                selected.append(
                    {
                        "title": entry.get("title") or raw["title"],
                        "summary": entry.get("summary") or raw.get("description") or raw["title"],
                        "source": raw["source"],
                        "date": raw["date"],
                        "url": raw["url"],
                        "linkType": "rss",
                    }
                )
    except Exception as exc:  # noqa: BLE001 - 실패 시 원문(영어) 그대로 최소한의 결과라도 유지
        print(f"China 뉴스 선별/번역 생성 실패, 원문 기반으로 대체합니다: {exc}")

    if not selected:
        for raw in raw_items[:MAX_CHINA_NEWS]:
            selected.append(
                {
                    "title": raw["title"],
                    "summary": raw.get("description") or raw["title"],
                    "source": raw["source"],
                    "date": raw["date"],
                    "url": raw["url"],
                    "linkType": "rss",
                }
            )

    selected.sort(key=lambda n: n["date"], reverse=True)
    return dedup_similar_titles(selected)[:MAX_CHINA_NEWS]


def generate_china_news_briefing(client: "genai.Client", news_items: list) -> dict | None:
    """China EV/배터리 뉴스 최종 목록을 바탕으로 카테고리별 브리핑(키워드 태깅/시사점 포함)을 생성한다."""
    if not news_items:
        return None
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=build_china_news_briefing_prompt(news_items),
        )
        payload = extract_json(response.text)
        if not isinstance(payload, dict):
            return None
        categories = payload.get("categories")
        if not isinstance(categories, list) or not categories:
            return None
        valid_urls = {_norm(n.get("url") or "") for n in news_items}
        cleaned_categories = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            items = cat.get("items")
            if not isinstance(items, list):
                continue
            cleaned_items = [
                item for item in items
                if isinstance(item, dict) and _norm(item.get("url") or "") in valid_urls
            ]
            if cleaned_items:
                cleaned_categories.append({"name": cat.get("name") or "기타", "items": cleaned_items})
        if not cleaned_categories:
            return None
        return {
            "keyTakeaways": [t for t in (payload.get("keyTakeaways") or []) if isinstance(t, str)],
            "categories": cleaned_categories,
            "implications": payload.get("implications") if isinstance(payload.get("implications"), str) else "",
        }
    except Exception as exc:  # noqa: BLE001 - 실패해도 파이프라인은 계속 진행
        print(f"China 뉴스 브리핑(카테고리/시사점) 생성 실패, 건너뜁니다: {exc}")
        return None


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 가 설정되지 않아 data.json 갱신을 건너뜁니다.")
        return

    now_kst = datetime.now(KST)
    today_str = now_kst.strftime("%Y-%m-%d")

    client = genai.Client(api_key=api_key)

    existing = load_existing_data()
    fx = fetch_fx_rates(existing.get("fx"))

    vehicles = generate_vehicles(client, today_str)
    news = generate_news(client)

    # China EV/배터리 뉴스는 국내 차량/뉴스 파이프라인과 완전히 독립적인 별도 소스(CnEVPost/CarNewsChina)이므로,
    # 여기서 오류가 나더라도 위 vehicles/news 결과 저장을 막지 않도록 별도로 격리한다.
    try:
        china_news = generate_china_news(client)
    except Exception as exc:  # noqa: BLE001
        print(f"China 뉴스 생성 단계에서 예상치 못한 오류, 건너뜁니다: {exc}")
        china_news = []
    merged_china_news = merge_china_news(existing.get("chinaNews") or [], china_news)
    try:
        china_news_briefing = generate_china_news_briefing(client, merged_china_news)
    except Exception as exc:  # noqa: BLE001
        print(f"China 뉴스 브리핑 생성 단계에서 예상치 못한 오류, 건너뜁니다: {exc}")
        china_news_briefing = None
    if china_news_briefing is None:
        china_news_briefing = existing.get("chinaNewsBriefing")

    if not vehicles and not news:
        print("신규로 생성/수집된 차량/뉴스 데이터가 없어 해당 항목은 건너뛰지만, 환율/China 뉴스 정보는 갱신합니다.")
        output = {
            "generatedAt": now_kst.isoformat(),
            "vehicles": existing["vehicles"],
            "news": existing["news"],
            "fx": fx,
            "newsBriefing": existing.get("newsBriefing"),
            "chinaNews": merged_china_news,
            "chinaNewsBriefing": china_news_briefing,
        }
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        return

    merged_vehicles = merge_vehicles(existing["vehicles"], vehicles)
    merged_news = merge_news(existing["news"], news)

    backfilled_count = backfill_tier1_specs(client, merged_vehicles, today_str)

    # 아래 두 단계는 부가적인 "검증 강화" 기능일 뿐이므로, 여기서 예기치 못한 오류가 나더라도
    # 이미 만든 vehicles/news 결과를 data.json에 저장하는 것까지 막지는 않는다 (파이프라인 전체
    # 중단으로 인해 며칠씩 갱신이 멈추는 사고를 방지하기 위한 방어선).
    try:
        verified_count = apply_teardown_verified_cell_makers(merged_vehicles)
    except Exception as exc:  # noqa: BLE001
        print(f"teardown 검증 적용 실패, 건너뜁니다: {exc}")
        verified_count = 0
    try:
        news_checked_count = apply_news_cross_checked_cell_makers(merged_vehicles, today_str)
    except Exception as exc:  # noqa: BLE001
        print(f"뉴스 교차검증 적용 실패, 건너뜁니다: {exc}")
        news_checked_count = 0

    try:
        news_briefing = generate_news_briefing(client, merged_news)
    except Exception as exc:  # noqa: BLE001
        print(f"뉴스 브리핑 생성 단계에서 예상치 못한 오류, 건너뜁니다: {exc}")
        news_briefing = None
    if news_briefing is None:
        # 생성 실패 시 어제 버전을 그대로 유지해 프론트가 갑자기 구도없이 문장형으로 후퇴하는 사태를 최소화한다.
        news_briefing = existing.get("newsBriefing")

    output = {
        "generatedAt": now_kst.isoformat(),
        "vehicles": merged_vehicles,
        "news": merged_news,
        "fx": fx,
        "newsBriefing": news_briefing,
        "chinaNews": merged_china_news,
        "chinaNewsBriefing": china_news_briefing,
    }

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(
        f"data.json 갱신 완료 (신규 vehicles: {len(vehicles)} / 누적 vehicles: {len(merged_vehicles)}, "
        f"신규 news: {len(news)} / 누적 news: {len(merged_news)}, 스펙 보강: {backfilled_count}건, "
        f"teardown 검증 적용: {verified_count}건, 뉴스 교차검증 적용: {news_checked_count}건, "
        f"China 신규 news: {len(china_news)} / 누적 China news: {len(merged_china_news)})"
    )


if __name__ == "__main__":
    main()
