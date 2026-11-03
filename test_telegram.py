import os
import requests
from paper_trader import load_env

load_env()
tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
print("토큰 읽음:", bool(tok), "/ 채팅ID 읽음:", bool(chat))
r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                  data={"chat_id": chat, "text": "크립토 추세 페이퍼 알림 테스트 ✅"})
print(r.status_code, r.text[:200])
