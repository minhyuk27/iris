"""
iris v2 — 텔레그램 인바운드 명령어 처리 (2026-07-24 신규, v2.5.4).

포지션 사이징 모드(잔고전액 vs 고정금액)를 텔레그램 메시지로 전환할 수 있게 한다.
iris v2 전체가 "1회성 스크립트+cron, 상시루프 데몬 아님" 원칙을 지키므로, 이것도
별도 리스너 프로세스를 띄우지 않는다 — bot/iris_v2_entry.py가 매일 09:29 실행
시작 직후(정밀대기 진입 전) getUpdates를 한 번 짧게 폴링해 그 사이 도착한 명령을
처리한다. 즉 그날 반영되려면 09:29 이전에 메시지를 보내야 하고, 그 이후엔 다음
거래일부터 적용된다.

명령어:
  /mode              현재 모드 조회
  /mode full         잔고전액 모드로 전환(매매당 가용현금 전부 사용, 기본값)
  /mode fixed <원>   고정금액 모드로 전환(예: /mode fixed 3000000)

보안: `.env`의 TELEGRAM_CHAT_ID(운영봇 소유자 채팅)와 정확히 일치하는 발신자의
메시지만 처리 — 그 외는 무시하고 로그만 남긴다. 텔레그램 API 자체가 실패해도
매매판단 로직에 영향 주지 않도록 전부 예외를 흡수한다(조회 실패=명령 없음과 동일 취급).
"""
import requests

from core.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from core.db import state_get, state_set
from core.logger import get_logger

logger = get_logger("telegram_commands", "IRIS_V2")

MODE_KEY = "iris_v2_position_size_mode"
FIXED_AMOUNT_KEY = "iris_v2_position_size_fixed_amount"
OFFSET_KEY = "iris_v2_telegram_update_offset"

MODE_FULL = "full_balance"
MODE_FIXED = "fixed"

MIN_FIXED_AMOUNT = 100_000       # 2026-07-24: 50만원->10만원(최소금액 스모크테스트용,
                                  # 175건 실측 최고가 32,550원 기준 10만원이면 1주는
                                  # 확실히 사고 부분익절 로직(2주 이상 필요)도 테스트 가능
MAX_FIXED_AMOUNT = 50_000_000    # 5,000만원 — 자릿수 오타 방지용 상한

_API = "https://api.telegram.org/bot{token}/getUpdates"


def get_position_size_mode() -> tuple[str, int]:
    """현재 (모드, 고정금액값) 반환. fixed 모드가 아닐 때도 값 자체는 항상 반환."""
    from strategy.iris_v2_params import CAPITAL_PER_TRADE
    mode = state_get(MODE_KEY, MODE_FULL)
    amount = state_get(FIXED_AMOUNT_KEY, CAPITAL_PER_TRADE)
    return mode, int(amount)


def _reply(text: str):
    from core.notify import send_ops, TAG_INFO
    send_ops(text, TAG_INFO)


def _handle_command(text: str) -> str | None:
    """명령 텍스트 처리, 응답 메시지 반환(iris v2 관련 명령이 아니면 None)."""
    parts = text.strip().split()
    if not parts or parts[0] != "/mode":
        return None

    if len(parts) == 1:
        mode, amount = get_position_size_mode()
        label = "잔고전액" if mode == MODE_FULL else f"고정금액({amount:,}원)"
        return f"현재 포지션 사이징 모드: {label}"

    sub = parts[1].lower()
    if sub in ("full", "전액"):
        state_set(MODE_KEY, MODE_FULL)
        return "✅ 포지션 사이징 → 잔고전액 모드로 전환됨(다음 진입판단부터 적용)"

    if sub in ("fixed", "고정"):
        if len(parts) < 3:
            return "사용법: /mode fixed <금액>  (예: /mode fixed 3000000)"
        try:
            amount = int(parts[2].replace(",", ""))
        except ValueError:
            return "금액을 숫자로 입력해줘: /mode fixed <금액>"
        if not (MIN_FIXED_AMOUNT <= amount <= MAX_FIXED_AMOUNT):
            return (f"금액은 {MIN_FIXED_AMOUNT:,}원~{MAX_FIXED_AMOUNT:,}원 범위여야 함"
                    f"(입력값 {amount:,}원)")
        state_set(MODE_KEY, MODE_FIXED)
        state_set(FIXED_AMOUNT_KEY, amount)
        return f"✅ 포지션 사이징 → 고정금액 {amount:,}원 모드로 전환됨(다음 진입판단부터 적용)"

    return f"모르는 명령: {sub}. 사용법: /mode | /mode full | /mode fixed <금액>"


def process_pending_commands():
    """getUpdates로 새 메시지를 짧게 폴링해 명령 처리. 미설정/네트워크실패시 조용히 스킵
    (텔레그램 장애가 매매판단을 막으면 안 됨 — halt와는 다른 클래스의 실패)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    offset = state_get(OFFSET_KEY, 0)
    try:
        r = requests.get(
            _API.format(token=TELEGRAM_TOKEN),
            params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"텔레그램 명령어 폴링 실패(무시하고 계속 진행): {e}")
        return

    if not data.get("ok"):
        return

    max_update_id = offset - 1
    for update in data.get("result", []):
        max_update_id = max(max_update_id, update["update_id"])
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "")
        if not text:
            continue
        if chat_id != str(TELEGRAM_CHAT_ID):
            logger.warning(f"미승인 채팅({chat_id})의 명령 무시: {text[:50]}")
            continue
        reply = _handle_command(text)
        if reply is not None:
            logger.info(f"텔레그램 명령 처리: {text!r} → {reply}")
            _reply(reply)

    if max_update_id >= offset:
        state_set(OFFSET_KEY, max_update_id + 1)
