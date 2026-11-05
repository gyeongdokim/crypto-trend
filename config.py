"""운용 설정 — 실매매 전환 시에도 이 파일만 수정"""
MARKETS = ["KRW-BTC", "KRW-ETH"]
CAPITAL_KRW = 6_000_000      # 운용 금액
TARGET_VOL = 0.40            # 목표 변동성
VOL_WINDOW = 90
CAP = 1.0                    # 코인별 비중 상한 (현물)
EXIT_RULE = "mid"

FEE = 0.0005                 # 업비트 원화마켓 편도 수수료
BT_SLIP_BP = 5.0             # 섀도(백테스트 기준) 장부의 슬리피지 가정
MIN_ORDER_KRW = 5_000        # 업비트 최소 주문금액

STATE_DIR = "state"          # 장부·기록 저장 폴더
KILL_SWITCH = "STOP"         # 이 이름의 파일이 있으면 아무것도 하지 않음

# ───────────── 실전 매매 (live_trader.py) ─────────────
LIVE_ENABLED = False             # True로 바꿔야 매일 자동 실주문이 나감 (그 전엔 아무것도 안 함)
LIVE_CAPITAL_KRW = 1_000_000     # 봇에 맡기는 금액. 단계: 100만(2주) → 300만(2주) → 600만
LIVE_MAX_ORDER_KRW = 600_000     # 1회 주문 상한. 넘으면 이 금액으로 잘라서 주문 + 알림 (운용금액 × 0.6 권장)
LIVE_STOP_DD = -0.20             # 기준가가 최고점 대비 -20%면 실매매 자동 중단 (보유분은 그대로, 판단은 사람이)
LIVE_STATE_DIR = "state_live"    # 실전 장부 폴더 (페이퍼 장부와 분리)
LIVE_KILL_SWITCH = "STOP_LIVE"   # 이 파일이 있으면 실전만 중단 (STOP은 페이퍼+실전 모두 중단)
