# 크립토 추세추종: 돈치안 앙상블 + 변동성 타기팅

업비트 원화마켓 일봉으로 검증하는 롱-플랫 추세추종 전략.
Zarattini·Pagani·Barbon (2025) *Catching Crypto Trends* (SFI Research Paper 25-80)의 방법론을 현물 환경에 맞게 재구현.

## 전략
| 구성 | 내용 |
|---|---|
| 신호 | 기간 5·10·20·30·60·90·150·250·360일 돈치안 모델 9개 앙상블 |
| 진입 / 청산 | 직전 N일 최고 종가 돌파 시 진입 / 채널 중간값 트레일링 스톱 하회 시 청산 |
| 포지션 크기 | 모델별 min(목표변동성(`config.TARGET_VOL`, 현재 40% · 논문 25%) ÷ 90일 실현변동성, 100%) → 9개 평균 |
| 포트폴리오 | 코인별 자본 1/N 배분, 나머지는 현금 |
| 체결 가정 | t일 종가 신호 → t일 종가 체결 (`--lag 1`로 하루 지연 검증) |
| 비용 | 편도 수수료 0.05% + 슬리피지 5bp |

## 실행
```
pip install requests pandas matplotlib
python run_backtest.py                  # config.py 설정 그대로 백테스트 + 현재 목표 비중
python run_backtest.py --sweep          # 파라미터 민감도
python run_backtest.py --synthetic      # 네트워크 없이 동작 확인
```

## 파일
| 파일 | 역할 |
|---|---|
| `upbit_data.py` | 업비트 일봉 수집·캐시 (진행 중인 당일 캔들 제외) |
| `donchian_trend.py` | 신호·포지션 크기·백테스트 엔진·성과 지표 |
| `run_backtest.py` | 실행, 벤치마크 비교(EMA 10/20, 단순 보유), 민감도 분석 |

## 검증 원칙
- t일 신호는 t일 종가까지의 정보만 사용 (미래 가격을 바꿔도 과거 신호 불변 확인)
- 단일 최적 파라미터 대신 앙상블 + 민감도 표로 과최적화 점검
- 현물 롱-플랫이므로 하락장 수익이 아닌 **낙폭 축소**가 목표

## 페이퍼 트레이딩
매일 09:05(일봉 마감 직후) 실행. 장부 두 개를 동시에 운영해 실행 비용을 측정.

| 장부 | 체결 | 의미 |
|---|---|---|
| 페이퍼 | 실시간 호가 VWAP, 최소주문 5,000원 적용 | 실전이었다면 |
| 섀도 | 일봉 종가, 백테스트와 동일 규칙 | 백테스트였다면 |

두 장부의 차이 = 체결 베이시스 + 최소주문 제약 → 실매매 전환 전 검증 지표.
섀도 장부는 백테스트 엔진과 동일한 값을 내는 것을 확인함.

```
python paper_trader.py --mock     # 동작 확인
python paper_trader.py            # 오늘 실행
python paper_trader.py --report   # 누적 성과
```
설정은 `config.py` (운용 600만 원, 목표 변동성 40%). `STOP` 파일을 만들면 실행 중단.

## 텔레그램 알림
| 시간 | 파일 | 내용 |
|---|---|---|
| 매일 09:05 | `paper_trader.py` | 페이퍼 매매 결과 + 매매 기준가(`trigger_levels.py`) + 7일 내 FOMC·CPI 일정(`events.csv`) |
| 09:10 / 21:10 | `market_watch.py --report` | 김치 프리미엄, 펀딩비, 공포·탐욕 지수, 나스닥 50일선 대비 |
| 매시간 | `market_watch.py` | 위 지표의 상태(🟢/🟡/🔴)가 바뀔 때만 알림 |

- **매매 기준가**: 내일 종가가 어느 가격을 넘거나 깨면 돈치안 모델이 켜지고/꺼지는지 계산. 기준가 ±0.01%로 원 전략 코드를 다시 돌려 정확히 그 가격에서 신호가 바뀌는 것을 검증함
- **시장 지표**는 매매 규칙에 쓰지 않고 `data/market_watch.csv`에 축적만 함 → 충분히 쌓이면 예측력 검증 후 채택 여부 결정

```
python market_watch.py --dry --report   # 전송 없이 미리보기
python trigger_levels.py                # 매매 기준가·일정 미리보기
```
`.env`에 `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` 필요. Windows 예약 작업은 `setup_tasks.ps1`로 한 번에 등록(백그라운드 실행, 배터리·절전 상태에서도 실행).

## 실전 매매 (`live_trader.py`)
페이퍼와 **같은 신호 함수**로 업비트에 실제 시장가 주문. 페이퍼는 나란히 계속 운영해 실전 체결과 비교.

| 안전장치 | 내용 |
|---|---|
| 기본 꺼짐 | `config.LIVE_ENABLED=False`면 아무 주문도 안 냄 |
| 잔고 대조 | 주문 전 실제 계좌가 봇 장부만큼 코인·원화를 갖고 있는지 확인, 부족하면 주문 없이 중단 |
| 1회 주문 상한 | `LIVE_MAX_ORDER_KRW` 초과 주문은 잘라서 실행 |
| 중복 주문 방지 | 주문마다 `ct-날짜-코인-방향` identifier → 같은 날 재실행해도 업비트가 거부 |
| 미확정 주문 복구 | 주문 직후 uuid를 장부에 먼저 기록 → 체결 확인 전에 끊겨도 다음 실행이 조회해 반영 |
| 낙폭 중단 | 기준가가 고점 대비 `LIVE_STOP_DD`(-20%) 이하면 `STOP_LIVE` 생성 → 다음 날부터 정지 |
| 킬스위치 | `STOP`(전체) / `STOP_LIVE`(실전만) |
| 알림 | 체결·오류(IP 변경, 키 만료, 잔고 부족 등) 텔레그램 전송 |

장부는 펀드 기준가 방식: 운용금액을 늘리면 그날 기준가로 좌수를 추가 → 단계적 투입에도 수익률 왜곡 없음.
인증(JWT·SHA512 query hash·HMAC 서명)은 `upbit_client.py`에 표준 라이브러리로 직접 구현.

```
python test_live.py                        # 가짜 거래소로 안전장치 검증 (키 불필요)
python live_trader.py --check              # 잔고·IP·키 권한 확인
python live_trader.py --test-order 10000   # 1만원 매수→매도 왕복 테스트 (봇 장부 밖 여유 원화가 있어야 실행)
python live_trader.py --dry                # 오늘 주문을 주문 테스트 API로만 검증
python live_trader.py --report             # 실전 누적 성과
```
