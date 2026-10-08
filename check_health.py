"""data.json의 항목별 갱신 상태(status)를 점검해 정체된 항목이 있으면 알림용 리포트를 만든다.

update_dashboard.py는 각 단계가 실패해도 어제 데이터를 유지하도록 설계되어 워크플로우가 항상 "성공"으로 끝나고
generatedAt도 매일 갱신되므로, 특정 카드가 며칠째 멈춰 있어도 겉으로는 드러나지 않는다. 이 스크립트는
data.json의 status[key].lastSuccessAt(마지막으로 실제 갱신에 성공한 시각)을 기준으로 정체를 판정한다.

- 표준 라이브러리만 사용하며 Gemini/외부 API를 호출하지 않는다 (할당량을 소모하지 않음).
- 항상 종료 코드 0으로 끝난다. 알림(GitHub Issue 생성/갱신/종료)은 워크플로우(health_check.yml)가 한다.
- 결과: health_report.md(이슈 본문), 그리고 $GITHUB_OUTPUT에 has_alert / signature / alert_count.

사용법:
    python check_health.py                  # 현재 data.json 점검
    python check_health.py --test-alert     # 실제 상태와 무관하게 테스트용 알림을 만든다 (이메일 수신 확인용)
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# data.json 키 -> 화면에서 부르는 이름
LABELS = {
    "vehicles": "신규 전기차 기본사양&가격 정보",
    "news": "국내 뉴스 목록",
    "chinaNews": "China 뉴스 목록",
    "newsBriefing": "글로벌 EV & 배터리 최신 동향 리포트(카드)",
    "chinaNewsBriefing": "China EV & 배터리 최신 동향 리포트(카드)",
    "benchmarkingPoints": "배터리 벤치마킹 포인트(카드)",
    "patentTrends": "CATL·BYD·Geely 배터리 특허 동향(카드)",
}
# 파이프라인 자체(generatedAt)가 이 시간 넘게 갱신되지 않았다면 워크플로우가 실행되지 않은 것으로 본다.
# GitHub 스케줄 지연으로 실제 실행 간격이 24시간을 넘기는 날이 있어(실측 24시간 45분) 24시간은 오탐이 난다.
PIPELINE_STALE_AFTER_HOURS = 36


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def _fmt(dt):
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M") if dt else "확인 불가"


def _age_text(hours):
    if hours is None:
        return "-"
    if hours >= 48:
        return f"{hours / 24:.1f}일"
    return f"{hours:.0f}시간"


def evaluate(data, now):
    """(정체 항목 리스트, 참고(주의) 항목 리스트, 안내 메시지 리스트)를 반환한다."""
    stale, notes, infos = [], [], []

    generated_at = _parse(data.get("generatedAt"))
    if generated_at is None:
        stale.append({"key": "pipeline", "label": "데이터 생성 파이프라인(generatedAt)", "last": None, "age": None,
                      "limit": PIPELINE_STALE_AFTER_HOURS, "state": "unknown", "reason": "generatedAt을 읽을 수 없음"})
    else:
        age = (now - generated_at).total_seconds() / 3600
        if age > PIPELINE_STALE_AFTER_HOURS:
            stale.append({"key": "pipeline", "label": "데이터 생성 파이프라인(generatedAt)", "last": generated_at, "age": age,
                          "limit": PIPELINE_STALE_AFTER_HOURS, "state": "not-running",
                          "reason": "update 워크플로우가 실행되지 않았거나 data.json이 커밋되지 않음 (Actions 탭 확인)"})

    status = data.get("status")
    if not isinstance(status, dict) or not status:
        infos.append("data.json에 status가 아직 없습니다 (status 기록 기능 배포 후 첫 자동 실행 전). 항목별 점검은 건너뜁니다.")
        return stale, notes, infos

    for key, label in LABELS.items():
        entry = status.get(key)
        if not isinstance(entry, dict):
            continue
        limit = entry.get("staleAfterHours")
        last = _parse(entry.get("lastSuccessAt"))
        state = entry.get("state") or "unknown"
        reason = entry.get("reason") or ""
        age = (now - last).total_seconds() / 3600 if last else None
        row = {"key": key, "label": label, "last": last, "age": age, "limit": limit, "state": state, "reason": reason}
        is_stale = False
        if isinstance(limit, (int, float)) and limit > 0:
            if last is not None:
                is_stale = age > limit
            else:
                is_stale = state == "failed"  # 마지막 성공 시각을 알 수 없고 최근 시도가 실패
        if is_stale:
            stale.append(row)
        elif state in ("failed", "degraded"):
            notes.append(row)  # 아직 정체는 아니지만 최근 실행에서 실패/품질 저하가 있었던 항목
    return stale, notes, infos


def _cell(text):
    """마크다운 표 셀에 넣을 수 있도록 표 구분자/개행을 제거한다."""
    return (text or "-").replace("|", "/").replace("\r", " ").replace("\n", " ")


def build_report(stale, notes, infos, now, test_alert):
    lines = []
    if test_alert:
        lines.append("> **[테스트 알림]** `check_health.py --test-alert`로 만든 테스트입니다. 실제 정체가 아닙니다. 다음 정상 점검 때 자동으로 닫힙니다.\n")
    lines.append(f"점검 시각: {now.astimezone(KST).strftime('%Y-%m-%d %H:%M')} (KST)\n")
    if stale:
        lines.append("### 정체된 항목\n")
        lines.append("| 항목 | 마지막 성공 | 경과 | 정체 기준 | 최근 상태 | 원인 |")
        lines.append("|---|---|---|---|---|---|")
        for r in stale:
            limit = f"{r['limit']}시간" if r["limit"] else "-"
            if r["limit"] and r["limit"] >= 48:
                limit = f"{r['limit'] / 24:.0f}일"
            lines.append(f"| {r['label']} | {_fmt(r['last'])} | {_age_text(r['age'])} | {limit} | {r['state']} | {_cell(r['reason'])} |")
        lines.append("")
    if notes:
        lines.append("### 참고: 정체는 아니지만 최근 실행에서 문제가 있었던 항목\n")
        for r in notes:
            lines.append(f"- {r['label']}: {r['state']} — {r['reason'] or '사유 없음'}")
        lines.append("")
    for msg in infos:
        lines.append(f"- {msg}")
    if stale:
        lines.append("\n### 확인 방법\n")
        lines.append("1. GitHub **Actions** 탭 → *Daily EV Dashboard Auto Update* 최근 실행 로그에서 해당 단계의 실패 메시지를 확인합니다.")
        lines.append("2. 일시적인 서버 오류(503) 등이면 *Run workflow*에서 `force_full_refresh`를 체크해 수동 재실행할 수 있습니다.")
        lines.append("3. 할당량 초과(429)면 재실행하지 말고 다음 날까지 기다립니다 (재실행은 할당량을 더 소모합니다).")
        lines.append("\n정상으로 돌아오면 이 이슈는 자동으로 닫힙니다.")
    return "\n".join(lines) + "\n"


def main():
    # Windows 콘솔(cp949)에서 로컬 실행할 때 한글/특수문자 출력 오류가 나지 않도록 UTF-8로 고정한다.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=os.path.join(BASE_DIR, "data.json"))
    parser.add_argument("--report", default=os.path.join(BASE_DIR, "health_report.md"))
    parser.add_argument("--test-alert", action="store_true", help="실제 상태와 무관하게 테스트용 알림을 만든다")
    args = parser.parse_args()

    now = datetime.now(KST)
    with open(args.data, "r", encoding="utf-8") as f:
        data = json.load(f)

    stale, notes, infos = evaluate(data, now)
    if args.test_alert and not stale:
        stale = [{"key": "test", "label": "테스트 항목", "last": None, "age": None, "limit": 36, "state": "test", "reason": "이메일 수신 확인용 테스트"}]

    report = build_report(stale, notes, infos, now, args.test_alert)
    with open(args.report, "w", encoding="utf-8") as f:
        f.write(report)

    has_alert = bool(stale)
    signature = ",".join(sorted(r["key"] for r in stale))  # 정체 항목 구성이 바뀌었는지 비교하는 용도
    print(report)
    print(f"has_alert={has_alert} signature={signature or '-'}")

    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(f"has_alert={'true' if has_alert else 'false'}\n")
            f.write(f"signature={signature}\n")
            f.write(f"alert_count={len(stale)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
