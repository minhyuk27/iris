"""
IRIS - 텔레그램 알림 (공통)
────────────────────────────────────────────────────────────
- HTML 특수문자 자동 이스케이프 (종목명에 &, <, > 포함 대비)
- 발송 재시도 (네트워크 일시 오류 흡수)
- 토큰 미설정 시 조용히 로그만
- 봇 2개 지원(2026-07-22): 운영봇(기존, 기본값) / 리포트봇(성과요약 전용)

운영봇 메시지 종류 태그(2026-08-08 정리 — "[IRIS-v2🟡]" 하나로 뭉뚱그리던 걸
메시지 종류별로 구분): 진입/청산(손익부호별)/부분익절/후보예고/주의/긴급/일반
7종으로 나눠 알림 목록만 봐도 무슨 일인지 바로 구분되게 한다. send_ops()로
발송하면 접두어가 자동으로 붙는다.

사용:
  from core.notify import send_ops, send_telegram_report, TAG_ENTRY
  send_ops("진입: ...", TAG_ENTRY)                              # 운영봇
  send_telegram_report("오늘 성과 요약...", prefix="[IRIS-리포트📊] ")  # 리포트봇
"""
import html
import time
import requests

from core.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_TOKEN_REPORT, TELEGRAM_CHAT_ID_REPORT
from core.logger import get_logger

logger = get_logger("notify", "SYSTEM")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_RETRY = 3

# 운영봇 메시지 태그 — 종류별로 한눈에 구분되도록 접두어 이모지를 고정한다.
TAG_CANDIDATE = "\U0001F319"  # 🌙 저녁 후보 예고
TAG_ENTRY     = "\U0001F7E2"  # 🟢 진입(매수) 확정
TAG_EXIT_WIN  = "\U0001F4C8"  # 📈 청산(매도) 확정, 손익 플러스
TAG_EXIT_LOSS = "\U0001F4C9"  # 📉 청산(매도) 확정, 손익 마이너스
TAG_PARTIAL   = "\U0001F3AF"  # 🎯 1차 부분익절(포지션 일부만 청산, 계속 보유)
TAG_WARN      = "⚠️"  # ⚠️ 주의 — 매매는 계속 진행됨
TAG_ALERT     = "\U0001F6A8"  # 🚨 긴급 — halt 등 자동매매 중단, 수동 확인 필요
TAG_INFO      = "\U0001F4AC"  # 💬 명령 응답 등 일반 안내


def esc(text) -> str:
    """HTML parse_mode용 이스케이프. <b> 등 의도한 태그는 호출부에서 직접 작성."""
    return html.escape(str(text), quote=False)


def exit_tag(pnl_pct: float) -> str:
    """청산 손익부호에 맞는 태그 반환 — 열지 않고도 익절/손절인지 구분되도록."""
    return TAG_EXIT_WIN if pnl_pct >= 0 else TAG_EXIT_LOSS


def send_telegram(text: str, prefix: str = "", parse_mode: str = "HTML") -> bool:
    """운영봇으로 발송(기존 TELEGRAM_TOKEN/CHAT_ID). 성공 True.
    새 호출부는 send_ops()를 쓸 것 — 이 함수는 prefix를 직접 조립해야 하는
    특수 경우(리포트봇 등 다른 prefix 관례)나 하위호환용으로 남겨둠."""
    return _send(text, prefix, parse_mode, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)


def send_ops(text: str, tag: str = TAG_INFO, parse_mode: str = "HTML", label: str = "IRIS-v2") -> bool:
    """운영봇 발송 — 메시지 종류 태그를 접두어로 자동 부착(위 TAG_* 상수 참조).
    send_telegram()을 그대로 통해서 나간다 — 기존 테스트 전부가
    patch("core.notify.send_telegram")로 발송을 가로채는 관례라, 여기서 우회하면
    그 목(mock) 전체가 조용히 무력화된다(2026-08-08 정리 중 발견, 반드시 지킬 것).
    2026-08-18: label 선택인자 추가(기본값 "IRIS-v2"라 기존 호출부는 100%
    하위호환) — iris v3는 send_ops(msg, tag, label="IRIS-v3")로 구분."""
    return send_telegram(text, prefix=f"[{label}{tag}] ", parse_mode=parse_mode)


def send_telegram_report(text: str, prefix: str = "", parse_mode: str = "HTML") -> bool:
    """리포트봇으로 발송(TELEGRAM_TOKEN_REPORT/CHAT_ID_REPORT, 성과요약 전용). 성공 True.
    새 호출부는 send_report()를 쓸 것(접두어 자동 부착, send_ops()와 대칭)."""
    return _send(text, prefix, parse_mode, TELEGRAM_TOKEN_REPORT, TELEGRAM_CHAT_ID_REPORT)


def send_report(text: str, parse_mode: str = "HTML") -> bool:
    """리포트봇 발송 — 고정 접두어 자동 부착(운영봇 send_ops()와 대칭 관례)."""
    return send_telegram_report(text, prefix="[IRIS-리포트\U0001F4CA] ", parse_mode=parse_mode)


def _send(text: str, prefix: str, parse_mode: str, token: str, chat_id: str) -> bool:
    """
    텔레그램 발송 공통 로직.
    text 안의 동적 값(종목명 등)은 호출부에서 esc()로 감싸야 안전하다.
    prefix는 그대로 앞에 붙는다(태그 미포함 권장).
    """
    if not token or not chat_id:
        logger.info(f"[텔레그램 미설정] {prefix}{text[:80]}")
        return False

    body = {
        "chat_id": chat_id,
        "text":    f"{prefix}{text}" if prefix else text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    url = _API.format(token=token)

    for i in range(_RETRY):
        try:
            r = requests.post(url, json=body, timeout=8)
            if r.status_code == 200:
                return True
            # HTML 파싱 오류면 plain text로 1회 폴백
            if r.status_code == 400 and parse_mode:
                body2 = {**body, "parse_mode": ""}
                # 태그 제거
                import re
                body2["text"] = re.sub(r"<[^>]+>", "", body2["text"])
                r2 = requests.post(url, json=body2, timeout=8)
                if r2.status_code == 200:
                    return True
                logger.warning(f"텔레그램 400 폴백 실패: {r2.text[:120]}")
                return False
            logger.warning(f"텔레그램 {r.status_code}: {r.text[:120]}")
        except Exception as e:
            if i < _RETRY - 1:
                time.sleep(1.5 * (i + 1))
            else:
                logger.error(f"텔레그램 발송 실패: {e}")
    return False
