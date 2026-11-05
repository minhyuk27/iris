#!/bin/bash
# iris v1 봇 시작 스크립트 (iris-v1.service → iris_v1_bot.py)
# 거래일 확인 후 휴장일이면 조용히 종료. 봇 자체(run())도 내부적으로 한 번 더
# 확인하지만, 여기서 먼저 거르면 불필요한 systemd 기동을 피할 수 있다.

cd /home/odroid/iris

TODAY=$(TZ=Asia/Seoul date +%Y%m%d)
LOG=logs/iris_v1_bot.log

IS_TRADING=$(timeout 45 python3 -c "
from core.kis_api import is_trading_day
import sys
try:
    print('Y' if is_trading_day('${TODAY}') else 'N')
except Exception as e:
    print('ERR:' + str(e), file=sys.stderr)
    print('Y')
" 2>> $LOG)
[ -z "$IS_TRADING" ] && IS_TRADING="Y"

if [ "$IS_TRADING" = "N" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${TODAY} 휴장일 — iris v1 미실행" >> $LOG
    exit 0
fi

MANUAL_STOP=$(timeout 10 python3 -c "
from core.db import state_get
v = state_get('iris_v1_manual_stop', 'false')
print(str(v).lower())
" 2>/dev/null)
if [ "$MANUAL_STOP" = "true" ] || [ "$MANUAL_STOP" = "1" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${TODAY} 수동 정지 예약됨 — iris v1 미실행" >> $LOG
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${TODAY} 거래일 확인 — iris v1 시작" >> $LOG
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user start iris-v1
