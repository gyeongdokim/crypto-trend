"""
업비트 원화마켓 일봉 수집 + 로컬 캐시

- 업비트 일봉은 매일 09:00 KST(00:00 UTC)에 시작해 다음날 09:00에 마감
- 진행 중인 오늘 캔들은 '아직 확정되지 않은 종가'라 제거 → 미래 정보 혼입 방지
- data/KRW-BTC.csv 형태로 캐시, 다음 실행부터는 새로 생긴 캔들만 추가 수집
"""
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests

URL = "https://api.upbit.com/v1/candles/days"
DATA_DIR = "data"


def _fetch_pages(market, stop_before=None, max_pages=100):
    """최신 → 과거 방향으로 200개씩 페이지 요청. stop_before(Timestamp)보다 과거면 중단"""
    rows, to = [], None
    for _ in range(max_pages):
        params = {"market": market, "count": 200}
        if to:
            params["to"] = to
        r = requests.get(URL, params=params, timeout=10)
        if r.status_code == 429:          # 요청 수 제한 → 잠시 대기 후 재시도
            time.sleep(1)
            continue
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows += data
        oldest = data[-1]["candle_date_time_utc"]
        if len(data) < 200 or (stop_before is not None and pd.Timestamp(oldest) <= stop_before):
            break
        to = oldest + "Z"                 # 'to'는 해당 시각 이전(미포함) 캔들 반환
        time.sleep(0.12)
    return rows


def _to_frame(rows):
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["candle_date_time_utc"]).dt.normalize()
    df = df.rename(columns={"opening_price": "open", "high_price": "high", "low_price": "low",
                            "trade_price": "close", "candle_acc_trade_price": "value"})
    return df[["date", "open", "high", "low", "close", "value"]].set_index("date")


def load_daily(market, refresh=True):
    """확정된 일봉 DataFrame(index=date, columns=open/high/low/close/value) 반환"""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{market}.csv")
    cached = pd.read_csv(path, index_col="date", parse_dates=True) if os.path.exists(path) else None

    if refresh or cached is None:
        last = cached.index.max() if cached is not None else None
        rows = _fetch_pages(market, stop_before=last)
        if rows:
            new = _to_frame(rows)
            df = new if cached is None else pd.concat([cached, new])
        else:
            df = cached
    else:
        df = cached

    df = df[~df.index.duplicated(keep="last")].sort_index()
    today_utc = pd.Timestamp(datetime.now(timezone.utc).date())
    df = df[df.index < today_utc]          # 진행 중인 오늘 캔들 제거
    df.to_csv(path)
    return df


def load_closes(markets, refresh=True):
    """여러 마켓의 종가를 한 표로 (상장 전 구간은 NaN)"""
    closes = {}
    for m in markets:
        closes[m] = load_daily(m, refresh)["close"]
        print(f"  {m}: {closes[m].index.min().date()} ~ {closes[m].index.max().date()} ({len(closes[m])}일)")
    return pd.DataFrame(closes).sort_index()
