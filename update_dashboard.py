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
    "batterySpec": "배터리 용량 & 기술",
    "cellMaker": "셀 제조사",
    "packMaker": "팩 제조사",
    "qcPerformance": "급속충전 성능",
    "rangePerformance": "주행거리 / 성능",
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
오늘 날짜는 {today_str} (KST) 입니다. Google Search 도구를 사용해 최근 7일 이내의
실제 글로벌/중국 전기차 신차 발표 및 EV·배터리 관련 뉴스만 조사하여 아래 JSON 스키마에
맞춰 순수 JSON 한 개만 응답하세요. 마크다운 코드블록이나 설명 문장은 절대 포함하지 마세요.

{{
  "vehicles": [ /* 최근 7일 이내 발표/출시된 신규 전기차 6~10건, 아래 필드 형식 예시 */
    {json.dumps(VEHICLE_SCHEMA_EXAMPLE, ensure_ascii=False)}
  ],
  "news": [ /* 최근 7일 이내 글로벌 EV/배터리 뉴스 헤드라인 정확히 20건, 최신순 정렬 */
    {json.dumps(NEWS_SCHEMA_EXAMPLE, ensure_ascii=False)}
  ]
}}

규칙:
- 추정/허구 데이터 금지. 실제 검색으로 확인되지 않는 수치는 만들지 말고 해당 필드에 "정보 없음"을 넣으세요.
- releaseDate/date 는 실제 발표일(YYYY-MM-DD)로 채우세요.
- url 은 검색으로 확인한 실제 기사 원문 링크를 넣으세요.
- id 값은 모두 서로 달라야 합니다.
"""


def extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```json\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```\s*$", "", cleaned)
    return json.loads(cleaned)


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 가 설정되지 않아 data.json 갱신을 건너뜁니다.")
        return

    now_kst = datetime.now(KST)
    today_str = now_kst.strftime("%Y-%m-%d")

    client = genai.Client(api_key=api_key)

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=build_prompt(today_str),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        payload = extract_json(response.text)
    except Exception as exc:  # noqa: BLE001 - keep the last known good data.json on any failure
        print(f"Gemini 호출/파싱 실패로 data.json 갱신을 건너뜁니다: {exc}")
        return

    vehicles = payload.get("vehicles") or []
    news = payload.get("news") or []

    if not vehicles or not news:
        print("생성된 데이터가 비어 있어 data.json 갱신을 건너뜁니다.")
        return

    output = {
        "generatedAt": now_kst.isoformat(),
        "vehicles": vehicles,
        "news": news,
    }

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"data.json 갱신 완료 (vehicles: {len(vehicles)}, news: {len(news)})")


if __name__ == "__main__":
    main()
