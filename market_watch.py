"""크립토 시장 관찰 지표 → 텔레그램 알림

보는 지표 4개 (모두 무료, API 키 불필요)
  1. 김치 프리미엄  : 업비트 BTC가 해외보다 몇 % 비싼가        → 국내 과열/공포
  2. 펀딩비        : 선물에서 '오른다'에 건 사람이 내는 이용료  → 레버리지 과열
  3. 공포·탐욕 지수 : 시장 분위기 0(공포) ~ 100(탐욕)
  4. 나스닥 추세    : 나스닥이 50일 평균 위인가 아래인가        → 위험자산 분위기

실행
  python market_watch.py --report   # 전체 요약을 텔레그램으로 (하루 2번 추천)
  python market_watch.py            # 상태가 바뀐 지표가 있을 때만 알림 (1시간마다 추천)
  python market_watch.py --dry      # 텔레그램 안 보내고 화면에만 출력 (테스트용)

매 실행마다 data/market_watch.csv 에 값이 쌓임
→ 김프·펀딩비는 과거 데이터를 무료로 구하기 어려워서, 지금부터 직접 모아두면 나중에 백테스트 재료가 됨
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime

import requests

from paper_trader import load_env, notify

UA = {"User-Agent": "Mozilla/5.0"}
LOG_CSV = os.path.join("data", "market_watch.csv")
LAST_STATE = os.path.join("state", "market_watch_last.json")


def get_json(url, **kw):
    r = requests.get(url, headers=UA, timeout=10, **kw)
    r.raise_for_status()
    return r.json()


# ── 1. 김치 프리미엄 ─────────────────────────────────────
def usdkrw():
    """원/달러 환율. 두나무(업비트 운영사) 환율 → 실패 시 야후."""
    try:
        return float(get_json("https://quotation-api-cdn.dunamu.com/v1/forex/recent?codes=FRX.KRWUSD")[0]["basePrice"])
    except Exception:
        return yahoo_closes("KRW=X")[-1]


def global_btc_usdt():
    """해외 BTC 가격(USDT). 바이낸스 → 실패 시 OKX."""
    try:
        return float(get_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")["price"])
    except Exception:
        return float(get_json("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT")["data"][0]["last"])


def kimchi_premium():
    upbit = get_json("https://api.upbit.com/v1/ticker?markets=KRW-BTC")[0]["trade_price"]
    fx = usdkrw()
    glob = global_btc_usdt()
    kp = (upbit / (glob * fx) - 1) * 100
    return {"upbit_btc": upbit, "usdkrw": fx, "global_btc": glob, "kimp_pct": round(kp, 2)}


# ── 2. 펀딩비 ────────────────────────────────────────────
def funding_rate():
    """BTC 무기한 선물 펀딩비(8시간 기준, %). 평상시 약 0.01%."""
    try:
        d = get_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT")
        fr = float(d["lastFundingRate"])
    except Exception:
        d = get_json("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP")
        fr = float(d["data"][0]["fundingRate"])
    return {"funding_pct": round(fr * 100, 4)}


# ── 3. 공포·탐욕 지수 ────────────────────────────────────
def fear_greed():
    d = get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
    return {"fng": int(d["value"])}


# ── 4. 나스닥 추세 ───────────────────────────────────────
def yahoo_closes(symbol, rng="6mo"):
    d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=1d")
    closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    return [c for c in closes if c is not None]


def nasdaq_trend():
    c = yahoo_closes("%5EIXIC")
    ma50 = sum(c[-50:]) / 50
    return {"nasdaq": round(c[-1], 1), "nasdaq_vs_ma50_pct": round((c[-1] / ma50 - 1) * 100, 2)}


# ── 상태 판정: 숫자 → 🟢 / 🟡 / 🔴 ─────────────────────────
# 기준값은 시장에서 흔히 쓰는 경험치. 데이터가 쌓이면 직접 조정할 것.
def judge(v):
    s = {}
    if "kimp_pct" in v:
        k = v["kimp_pct"]
        s["kimp"] = ("🟢 역프(국내 공포)" if k < 0 else "⚪ 정상" if k < 3
                     else "🟡 주의" if k < 5 else "🔴 과열")
    if "funding_pct" in v:
        f = v["funding_pct"]
        s["funding"] = ("🟢 음수(숏 과열·반등 가능)" if f < 0 else "⚪ 정상" if f < 0.03
                        else "🟡 주의" if f < 0.05 else "🔴 레버리지 과열")
    if "fng" in v:
        g = v["fng"]
        s["fng"] = ("🟢 극단적 공포" if g <= 20 else "⚪ 공포" if g < 45 else "⚪ 중립" if g <= 55
                    else "⚪ 탐욕" if g < 80 else "🔴 극단적 탐욕")
    if "nasdaq_vs_ma50_pct" in v:
        s["nasdaq"] = "⚪ 상승 추세" if v["nasdaq_vs_ma50_pct"] >= 0 else "🟡 하락 추세"
    return s


def collect():
    vals, errors = {}, []
    for name, fn in [("김프", kimchi_premium), ("펀딩비", funding_rate),
                     ("공포탐욕", fear_greed), ("나스닥", nasdaq_trend)]:
        try:
            vals.update(fn())
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")
    return vals, errors


def format_msg(v, s, errors, title):
    L = [f"📊 {title}  {datetime.now():%m/%d %H:%M}", ""]
    if "kimp_pct" in v:
        L.append(f"김치 프리미엄  {v['kimp_pct']:+.2f}%  {s['kimp']}")
        L.append(f"   업비트 {v['upbit_btc']:,.0f}원 · 환율 {v['usdkrw']:,.1f}")
    if "funding_pct" in v:
        L.append(f"펀딩비(8h)     {v['funding_pct']:+.4f}%  {s['funding']}")
    if "fng" in v:
        L.append(f"공포·탐욕      {v['fng']}  {s['fng']}")
    if "nasdaq" in v:
        L.append(f"나스닥 vs 50일 {v['nasdaq_vs_ma50_pct']:+.2f}%  {s['nasdaq']}")
    if errors:
        L += ["", "⚠️ 수집 실패: " + ", ".join(errors)]
    return "\n".join(L)


def save_row(v):
    os.makedirs("data", exist_ok=True)
    cols = ["time", "kimp_pct", "upbit_btc", "usdkrw", "global_btc", "funding_pct",
            "fng", "nasdaq", "nasdaq_vs_ma50_pct"]
    new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({"time": datetime.now().isoformat(timespec="minutes"), **v})


def changed(s):
    """지난번과 비교해 상태(색깔)가 바뀐 지표만 골라냄."""
    os.makedirs("state", exist_ok=True)
    last = json.load(open(LAST_STATE, encoding="utf-8")) if os.path.exists(LAST_STATE) else {}
    json.dump(s, open(LAST_STATE, "w", encoding="utf-8"), ensure_ascii=False)
    return [k for k in s if last.get(k) and last[k] != s[k]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="전체 요약 전송")
    ap.add_argument("--dry", action="store_true", help="전송 없이 출력만")
    a = ap.parse_args()

    load_env()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 윈도우 콘솔/로그에서 이모지 오류 방지
    v, errors = collect()
    s = judge(v)
    save_row(v)
    moved = changed(s)

    if a.report:
        msg = format_msg(v, s, errors, "크립토 시장 요약")
    elif moved:
        msg = format_msg(v, s, errors, "⚡ 상태 변화: " + ", ".join(moved))
    else:
        msg = None

    print(msg or f"{datetime.now():%H:%M} 변화 없음 {v}")
    if msg and not a.dry:
        notify(msg)


if __name__ == "__main__":
    main()
