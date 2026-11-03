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
