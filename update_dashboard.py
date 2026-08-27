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
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from google import genai

KST = timezone(timedelta(hours=9))
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

MAX_VEHICLES = 80  # 누적 상한 (최근 1년치 데이터를 충분히 보유)
MAX_NEWS = 20       # 항상 최신 20건만 유지 (기존 대시보드 사양과 동일)

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
]

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
아래는 RSS로 수집한 실제 전기차/배터리 관련 뉴스 원본 목록입니다 (title, url, date, source, description 포함).
이 목록의 각 항목에 대해 한글 요약(summary)을 1~2문장으로 작성해서 JSON 배열로만 응답하세요.

- title, url, date, source 값은 절대 변경하지 말고 원본 그대로 유지하세요.
- summary는 description 내용을 바탕으로 자연스러운 한글 뉴스 요약 문장으로 작성하세요 (직역이 아니라 핵심 내용 요약).
- description이 비어있거나 정보가 부족하면 title을 근거로 합리적으로 요약하세요.
- 마크다운 코드블록이나 설명 문장 없이 순수 JSON 배열만 응답하세요.

원본 목록:
{items_json}

응답 형식 (배열, 각 원소는 아래 5개 필드만 포함):
[
  {{"title": "...", "summary": "...", "source": "...", "date": "YYYY-MM-DD", "url": "..."}}
]
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


def collect_recent_news_raw(max_total: int = MAX_NEWS) -> list:
    collected = []
    seen = set()
    for query in GOOGLE_NEWS_QUERIES:
        for item in fetch_google_news_rss(query, max_items=15):
            key = _norm(item["url"])
            if not key or key in seen:
                continue
            seen.add(key)
            collected.append(item)
    collected.sort(key=lambda x: x["date"], reverse=True)
    return collected[:max_total]


def load_existing_data() -> dict:
    if not os.path.exists(DATA_PATH):
        return {"vehicles": [], "news": []}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "vehicles": data.get("vehicles") or [],
            "news": data.get("news") or [],
        }
    except Exception:  # noqa: BLE001 - 손상된 파일이면 빈 값으로 시작
        return {"vehicles": [], "news": []}


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
    result = result[:MAX_NEWS]
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
    raw_items = collect_recent_news_raw(MAX_NEWS)
    if not raw_items:
        print("RSS 뉴스 수집 실패(0건) - 뉴스 갱신을 건너뜁니다.")
        return []

    summaries_by_key = {}
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=build_news_summary_prompt(raw_items),
        )
        summarized = extract_json(response.text)
        if isinstance(summarized, list):
            for entry in summarized:
                if isinstance(entry, dict) and entry.get("url"):
                    summaries_by_key[_norm(entry["url"])] = entry.get("summary")
    except Exception as exc:  # noqa: BLE001 - 요약 실패해도 원문 설명으로 대체
        print(f"뉴스 요약 생성 실패, 원문 설명을 그대로 사용합니다: {exc}")

    news = []
    for raw in raw_items:
        key = _norm(raw["url"])
        summary = summaries_by_key.get(key) or raw.get("description") or raw["title"]
        news.append(
            {
                "title": raw["title"],
                "summary": summary,
                "source": raw["source"],
                "date": raw["date"],
                "url": raw["url"],
                "linkType": "rss",
            }
        )
    return news


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 가 설정되지 않아 data.json 갱신을 건너뜁니다.")
        return

    now_kst = datetime.now(KST)
    today_str = now_kst.strftime("%Y-%m-%d")

    client = genai.Client(api_key=api_key)

    vehicles = generate_vehicles(client, today_str)
    news = generate_news(client)

    if not vehicles and not news:
        print("신규로 생성/수집된 데이터가 없어 data.json 갱신을 건너뜁니다.")
        return

    existing = load_existing_data()
    merged_vehicles = merge_vehicles(existing["vehicles"], vehicles)
    merged_news = merge_news(existing["news"], news)

    backfilled_count = backfill_tier1_specs(client, merged_vehicles, today_str)

    output = {
        "generatedAt": now_kst.isoformat(),
        "vehicles": merged_vehicles,
        "news": merged_news,
    }

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(
        f"data.json 갱신 완료 (신규 vehicles: {len(vehicles)} / 누적 vehicles: {len(merged_vehicles)}, "
        f"신규 news: {len(news)} / 누적 news: {len(merged_news)}, 스펙 보강: {backfilled_count}건)"
    )


if __name__ == "__main__":
    main()
