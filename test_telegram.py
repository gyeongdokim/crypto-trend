import os
import sys

from paper_trader import load_env, notify

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_env()
tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
print("토큰 읽음:", bool(tok), "/ 채팅ID 읽음:", bool(chat))
print("전송 성공" if notify("크립토 추세 페이퍼 알림 테스트 ✅") else "전송 실패 (위 [알림 실패] 메시지 확인)")
