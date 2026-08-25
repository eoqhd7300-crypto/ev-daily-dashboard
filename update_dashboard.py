"""
Daily EV & Battery Dashboard Data Updater
------------------------------------------
매일 실행되어 Gemini(Google Search grounding)로 최신 글로벌 EV 신차 정보와
뉴스 헤드라인을 수집한 뒤 data.json 을 재생성합니다.
index.html 은 이 data.json 을 fetch 하여 화면을 갱신합니다.
(GEMINI_API_KEY 가 없거나 호출이 실패하면 기존 data.json 을 그대로 두고 종료합니다.)
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types

KST = timezone(timedelta(hours=9))
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

MAX_VEHICLES = 80  # 누적 상한 (최근 1년치 데이터를 충분히 보유)
MAX_NEWS = 20       # 항상 최신 20건만 유지 (기존 대시보드 사양과 동일)

VEHICLE_SCHEMA_EXAMPLE = {
    "id": "brand_model_slug",
    "selected": True,
    "releaseDate": "YYYY-MM-DD",
    "name": "모델명 (영문/현지어 병기)",
    "brand": "브랜드명",
    "type": "차종/체급",
    "timeline": "출시/예상시점 (예: 2026년 08월 출시)",
    "priceLocal": "현지 통화 가격",
    "priceKRW": "원화 환산 가격",
    "batterySpec": "배터리 용량 & 기술 (예: 800V SiC Ultra-Fast NCM (98 kWh))",
    "cellMaker": "셀 제조사 - 반드시 '영문사명 (한글표기)' 형식 (예: CATL (닝더시대))",
    "packMaker": "팩 제조사 - 반드시 '영문사명 (한글표기)' 형식 (예: Hyundai Mobis (현대모비스))",
    "qcPerformance": "급속충전 성능 - 반드시 'X.XC Peak (최대 XXXkW) | SOC 10% → 80% (약 XX분)' 형식",
    "rangePerformance": "주행거리/성능 - CLTC/EPA/WLTP 수치와 가속성능(0-100km/h)/모터 출력 등 추가 정보 포함 (예: CLTC 700km+ (9분 충전시 450km) / 800V SiC 듀얼모터)",
    "overview": "한글 개요 2~3문장",
    "adMessage": "마케팅 슬로건",
    "dimensions": "제원 (전장×전폭×전고 / 휠베이스)",
    "powertrain": "파워트레인 요약",
    "packInfo": "배터리 팩 구조 설명",
    "cellInfo": "셀 케미스트리/형태 설명",
    "chargingSafety": "충전/안전 관련 특징",
}

NEWS_SCHEMA_EXAMPLE = {
    "id": 1,
    "title": "영문 원제목",
    "summary": "한글 요약 1~2문장",
    "source": "출처 매체명",
    "date": "YYYY-MM-DD",
    "url": "원본 기사 URL",
}


def build_prompt(today_str: str) -> str:
    return f"""
당신은 글로벌 전기차(EV) 및 배터리 산업 전문 애널리스트입니다.
오늘 날짜는 {today_str} (KST) 입니다. (검색 도구가 제공되면 이를 활용해) 아래 JSON 스키마에
맞춰 순수 JSON 한 개만 응답하세요. 마크다운 코드블록이나 설명 문장은 절대 포함하지 마세요.

{{
  "vehicles": [ /* 최근 12개월(1년) 이내에 발표/출시된 주요 글로벌/중국 신규 전기차를 발표일이 고르게 분산되도록 15~25건, 아래 필드 형식 예시 */
    {json.dumps(VEHICLE_SCHEMA_EXAMPLE, ensure_ascii=False)}
  ],
  "news": [ /* 최근 7일 이내 글로벌 EV/배터리 뉴스 헤드라인 정확히 20건, 최신순 정렬 */
    {json.dumps(NEWS_SCHEMA_EXAMPLE, ensure_ascii=False)}
  ]
}}

규칙:
- 추정/허구 데이터 금지. 실제 검색으로 확인되지 않는 수치는 만들지 말고 해당 필드에 "정보 없음"을 넣으세요.
- releaseDate/date 는 실제 발표일(YYYY-MM-DD)로 채우세요.
- vehicles는 최근 1개월에만 몰리지 않고 지난 12개월 전체 기간에 골고루 분포되도록 구성하세요 (예: 각 월마다 1~2건씩).
- cellMaker/packMaker는 반드시 '영문사명 (한글표기)' 형식으로 상세히 표기하세요 (예: 'CATL (닝더시대)', 'LG 에너지솔루션'). 간략화나 생략 금지.
- qcPerformance는 반드시 'X.XC Peak (최대 XXXkW) | SOC 10% → 80% (약 XX분)' 형식으로 C-rate, 최대 출력(kW), 충전시간을 모두 포함해 상세히 작성하세요.
- rangePerformance는 주행거리 수치 외에 가속성능/모터 출력/충전방식 등 추가 기술 정보를 함께 포함해 상세히 작성하세요. 단순 수치 한 개만 쓰는 요약형 문장은 금지.
- url 은 검색으로 확인한 실제 기사 원문 링크를 넣으세요. 검색 도구를 쓸 수 없다면 네가 학습한 지식 중 가장 최근 정보로 채우고 url은 해당 매체의 대표 도메인 URL을 넣으세요.
- id 값은 모두 서로 달라야 합니다.
- 어떤 경우에도 사과, 거절, 설명 문구를 출력하지 말고 위 JSON 구조만 응답하세요. 실시간 검색이 불가능하더라도 거부하지 말고 보유한 지식 중 가장 최근 정보로 추론해서 채우세요.
"""


def extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```json\s*", "", (text or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```\s*$", "", cleaned)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 모델이 설명/거절 문구를 섞어 보낸 경우, 첫 { 부터 마지막 } 까지만 추출해 재시도
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


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


def merge_news(old_news: list, new_news: list) -> list:
    merged: dict[str, dict] = {}
    for n in old_news + new_news:
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


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 가 설정되지 않아 data.json 갱신을 건너뜁니다.")
        return

    now_kst = datetime.now(KST)
    today_str = now_kst.strftime("%Y-%m-%d")

    client = genai.Client(api_key=api_key)
    prompt = build_prompt(today_str)

    # 1차 시도: Google Search grounding 사용 (무료 티어는 별도의 낮은 할당량이 적용될 수 있음)
    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        payload = extract_json(response.text)
    except Exception as exc:  # noqa: BLE001
        print(f"Grounding 호출 실패({exc}), 검색 도구 없이 재시도합니다.")
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
            )
            raw_text = response.text or ""
            payload = extract_json(raw_text)
        except Exception as exc2:  # noqa: BLE001 - keep the last known good data.json on any failure
            print(f"Gemini 호출/파싱 실패로 data.json 갱신을 건너뜁니다: {exc2}")
            print(f"응답 원문(디버깅용, 최대 500자): {raw_text[:500] if 'raw_text' in locals() else '(응답 없음)'}")
            return

    vehicles = payload.get("vehicles") or []
    news = payload.get("news") or []

    if not vehicles or not news:
        print("생성된 데이터가 비어 있어 data.json 갱신을 건너뜁니다.")
        return

    existing = load_existing_data()
    merged_vehicles = merge_vehicles(existing["vehicles"], vehicles)
    merged_news = merge_news(existing["news"], news)

    output = {
        "generatedAt": now_kst.isoformat(),
        "vehicles": merged_vehicles,
        "news": merged_news,
    }

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(
        f"data.json 갱신 완료 (신규 vehicles: {len(vehicles)} / 누적 vehicles: {len(merged_vehicles)}, "
        f"news: {len(merged_news)})"
    )


if __name__ == "__main__":
    main()
