#!/bin/bash
sleep 30  # 네트워크 안정화 대기
cd /home/odroid/iris
python -c "
import requests
from core.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from datetime import datetime, timezone, timedelta
KST = timezone(timedelta(hours=9))
now = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
requests.post(
    f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
    json={'chat_id': TELEGRAM_CHAT_ID, 'text': f'🔄 IRIS 서버 재시작 완료\n{now}'},
    timeout=10
)
" 2>/dev/null
