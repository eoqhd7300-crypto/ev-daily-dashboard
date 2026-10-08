/*
 * 카드/목록의 "정체" 배지 표시 (index.html, news.html 공용).
 *
 * update_dashboard.py가 data.json의 status[key]에 항목별 lastSuccessAt(마지막으로 실제 갱신에 성공한 시각),
 * staleAfterHours(정체로 보는 기준 시간), state/reason을 기록한다. 이 값을 브라우저의 현재 시각과 비교해
 * 기준 시간을 넘긴 항목에만 "마지막 갱신: YYYY-MM-DD (정체 중)" 배지를 보여준다. 정상일 때는 아무것도 표시하지 않는다.
 * 백엔드가 아예 멈추면 data.json 자체가 갱신되지 않아 서버 쪽에서는 정체를 표시할 수 없으므로, 판정은 브라우저에서 한다.
 * status가 없는 예전 data.json이면 배지를 표시하지 않는다(하위 호환).
 */
(function () {
    var formatKstDate = function (ms) {
        try {
            // 'sv-SE' 로케일은 YYYY-MM-DD 형식을 돌려준다.
            return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul' }).format(new Date(ms));
        } catch (e) {
            return new Date(ms).toISOString().slice(0, 10);
        }
    };

    // entry: data.json의 status[key]. 정체가 아니면 null, 정체면 { label, tooltip }을 반환한다.
    window.evaluateStale = function (entry, nowMs) {
        if (!entry || typeof entry !== 'object') return null;
        var hours = Number(entry.staleAfterHours);
        if (!isFinite(hours) || hours <= 0) return null;
        var last = entry.lastSuccessAt ? Date.parse(entry.lastSuccessAt) : NaN;
        var reason = entry.reason ? ' / 사유: ' + entry.reason : '';
        if (isFinite(last)) {
            if (nowMs - last <= hours * 3600 * 1000) return null;
            return {
                label: '⚠ 마지막 갱신: ' + formatKstDate(last) + ' (정체 중)',
                tooltip: '마지막으로 갱신에 성공한 시각: ' + formatKstDate(last) + reason,
            };
        }
        // 마지막 성공 시각을 알 수 없지만 최근 시도가 실패한 경우
        if (entry.state === 'failed') {
            return {
                label: '⚠ 최근 갱신 실패 (마지막 갱신일 확인 불가)',
                tooltip: '최근 갱신 시도가 실패했습니다' + reason,
            };
        }
        return null;
    };

    // el에 배지를 그리거나(정체) 숨긴다(정상). 텍스트는 textContent로만 넣어 data.json 값이 HTML로 해석되지 않게 한다.
    window.applyStaleBadge = function (el, entry, theme) {
        if (!el) return;
        var info = window.evaluateStale(entry, Date.now());
        if (!info) {
            el.className = 'hidden';
            el.textContent = '';
            el.removeAttribute('title');
            return;
        }
        var palette = theme === 'light'
            ? 'bg-amber-50 text-amber-800 border border-amber-300'
            : 'bg-amber-400/20 text-amber-200 border border-amber-300/40';
        el.className = palette + ' text-[10px] font-bold px-2 py-0.5 rounded-full whitespace-nowrap flex-shrink-0';
        el.textContent = info.label;
        el.title = info.tooltip;
    };

    // data-stale-key="newsBriefing" 같은 속성을 가진 모든 요소에 배지를 적용한다 (data-stale-theme="light"면 밝은 배경용).
    window.renderStaleBadges = function (status) {
        document.querySelectorAll('[data-stale-key]').forEach(function (el) {
            var entry = status && typeof status === 'object' ? status[el.getAttribute('data-stale-key')] : null;
            window.applyStaleBadge(el, entry, el.getAttribute('data-stale-theme'));
        });
    };
})();
