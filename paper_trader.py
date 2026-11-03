"""
페이퍼 트레이더: 매일 09:05 실행 → 확정 일봉으로 목표 비중 계산 → 실시간 호가로 가상 체결

장부 두 개를 동시에 운영
- 페이퍼 장부: 실제 호가(VWAP)로 체결, 최소주문금액 5,000원 적용 → '실전이었다면'
- 섀도 장부:   일봉 종가로 체결, 백테스트와 같은 규칙          → '백테스트였다면'
두 장부의 차이 = 실행 비용(체결 베이시스) + 최소주문 제약 → 실매매 전 반드시 측정할 값

실행
    python paper_trader.py            # 오늘 1회 (이미 처리한 날이면 건너뜀)
    python paper_trader.py --force    # 같은 날 재실행
    python paper_trader.py --mock     # 가짜 데이터로 60일 시뮬레이션 (동작 확인용)
    python paper_trader.py --report   # 누적 성과만 출력
"""
import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import requests

import config as C
from donchian_trend import ensemble_weight, LOOKBACKS


# ───────────────────────── 유틸 ─────────────────────────
def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def notify(text):
    """텔레그램 알림 (.env에 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID가 있을 때만)"""
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=10)
        except Exception as e:
            print(f"[알림 실패] {e}")


def fetch_books(markets):
    r = requests.get("https://api.upbit.com/v1/orderbook",
                     params={"markets": ",".join(markets)}, timeout=10)
    r.raise_for_status()
    out = {}
    for item in r.json():
        u = item["orderbook_units"]
        out[item["market"]] = {
            "bids": [(float(x["bid_price"]), float(x["bid_size"])) for x in u],
            "asks": [(float(x["ask_price"]), float(x["ask_size"])) for x in u],
        }
    return out


def vwap_buy(asks, krw):
    """krw만큼 시장가 매수 → (수량, 평균가)"""
    qty, spent = 0.0, 0.0
    for p, s in asks:
        if spent + p * s >= krw:
            qty += (krw - spent) / p
            return qty, krw / qty
        qty += s; spent += p * s
    raise RuntimeError("호가 부족")


def vwap_sell(bids, qty):
    """qty만큼 시장가 매도 → (대금, 평균가)"""
    left, krw = qty, 0.0
    for p, s in bids:
        q = min(left, s); krw += q * p; left -= q
        if left <= 1e-12:
            return krw, krw / qty
    raise RuntimeError("호가 부족")


def mid(book):
    return (book["bids"][0][0] + book["asks"][0][0]) / 2


# ───────────────────────── 장부 ─────────────────────────
def new_state():
    return {"cash": C.CAPITAL_KRW, "qty": {m: 0.0 for m in C.MARKETS},
            "shadow_equity": 1.0, "shadow_w": {m: 0.0 for m in C.MARKETS},
            "last_date": None, "last_close": {}, "start_date": None}


def load_state(d):
    p = os.path.join(d, "ledger.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else new_state()


def save_state(d, st):
    os.makedirs(d, exist_ok=True)
    json.dump(st, open(os.path.join(d, "ledger.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def append_csv(d, name, rows):
    if not rows:
        return
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    new = not os.path.exists(p)
    # 새 파일만 BOM 포함(엑셀 한글), 이어쓰기는 BOM 없이
    pd.DataFrame(rows).to_csv(p, mode="a", header=new, index=False,
                              encoding="utf-8-sig" if new else "utf-8")


# ───────────────────────── 하루 처리 ─────────────────────────
def compute_targets(closes):
    n = len(C.MARKETS)
    tgt, info = {}, {}
    for m in C.MARKETS:
        w, models, vol = ensemble_weight(closes[m].dropna(), C.TARGET_VOL, C.VOL_WINDOW, C.CAP,
                                         LOOKBACKS, C.EXIT_RULE, return_detail=True)
        tgt[m] = float(w.iloc[-1]) / n
        info[m] = {"models_on": int(models.iloc[-1].sum()), "vol": float(vol.iloc[-1])}
    return tgt, info


def update_shadow(st, closes, tgt):
    """백테스트 엔진과 같은 규칙: 직전 처리일 종가 → 오늘 종가 수익 반영 후 종가로 리밸런싱"""
    today = closes.index[-1]
    w = np.array([st["shadow_w"][m] for m in C.MARKETS])
    if st["last_close"]:
        r = np.array([closes[m].iloc[-1] / st["last_close"][m] - 1 for m in C.MARKETS])
        pr = float(w @ r)
        st["shadow_equity"] *= 1 + pr
        if 1 + pr > 0:
            w = w * (1 + r) / (1 + pr)
    t = np.array([tgt[m] for m in C.MARKETS])
    cost = np.abs(t - w).sum() * (C.FEE + C.BT_SLIP_BP / 1e4)
    st["shadow_equity"] *= 1 - cost
    st["shadow_w"] = dict(zip(C.MARKETS, (t / (1 - cost)).tolist()))
    st["last_close"] = {m: float(closes[m].iloc[-1]) for m in C.MARKETS}
    return today


def run_day(closes, books, st, state_dir, now_str):
    date = closes.index[-1].strftime("%Y-%m-%d")
    tgt, info = compute_targets(closes)
    update_shadow(st, closes, tgt)

    # 페이퍼 장부: 현재 평가 (호가 중간값)
    px = {m: mid(books[m]) for m in C.MARKETS}
    equity = st["cash"] + sum(st["qty"][m] * px[m] for m in C.MARKETS)

    # 주문 금액 계산 → 매도 먼저(현금 확보), 매수 나중
    orders = []
    for m in C.MARKETS:
        cur = st["qty"][m] * px[m]
        diff = tgt[m] * equity - cur
        if tgt[m] == 0 and cur > 0:
            diff = -cur                                   # 목표 0이면 전량 청산
        if abs(diff) < C.MIN_ORDER_KRW:
            continue                                      # 최소주문금액 미만 → 생략
        orders.append((m, diff))
    orders.sort(key=lambda x: x[1])

    trades = []
    for m, diff in orders:
        close = float(closes[m].iloc[-1])
        if diff < 0:
            qty = min(st["qty"][m], -diff / px[m]) if tgt[m] > 0 else st["qty"][m]
            krw, avg = vwap_sell(books[m]["bids"], qty)
            fee = krw * C.FEE
            st["qty"][m] -= qty; st["cash"] += krw - fee
            side, amount = "매도", krw
        else:
            krw = min(diff, st["cash"] / (1 + C.FEE))
            if krw < C.MIN_ORDER_KRW:
                continue
            qty, avg = vwap_buy(books[m]["asks"], krw)
            fee = krw * C.FEE
            st["qty"][m] += qty; st["cash"] -= krw + fee
            side, amount = "매수", krw
        trades.append({"time": now_str, "date": date, "market": m, "side": side,
                       "amount_krw": round(amount), "qty": qty, "avg_price": avg,
                       "close": close, "basis_bp": round((avg / close - 1) * 1e4, 2),
                       "fee_krw": round(fee, 1)})

    equity_after = st["cash"] + sum(st["qty"][m] * px[m] for m in C.MARKETS)
    if st["start_date"] is None:
        st["start_date"] = date
    st["last_date"] = date

    row = {"date": date, "run_time": now_str, "equity": round(equity_after),
           "paper_ret": equity_after / C.CAPITAL_KRW - 1,
           "shadow_ret": st["shadow_equity"] - 1, "cash": round(st["cash"])}
    for m in C.MARKETS:
        row[f"{m}_target"] = round(tgt[m], 4)
        row[f"{m}_actual"] = round(st["qty"][m] * px[m] / equity_after, 4)
        row[f"{m}_models"] = info[m]["models_on"]
        row[f"{m}_vol"] = round(info[m]["vol"], 3)
    append_csv(state_dir, "trades.csv", trades)
    append_csv(state_dir, "daily.csv", [row])
    save_state(state_dir, st)
    return row, trades


def summary_text(row, trades):
    lines = [f"[크립토 추세 페이퍼] {row['date']} 종가 기준",
             f"평가액 {row['equity']:,}원 | 누적 페이퍼 {row['paper_ret']:+.2%} / 섀도 {row['shadow_ret']:+.2%}"]
    for m in C.MARKETS:
        lines.append(f"{m}: 모델 {row[f'{m}_models']}/9, 변동성 {row[f'{m}_vol']:.0%}, "
                     f"목표 {row[f'{m}_target']:.1%} / 실제 {row[f'{m}_actual']:.1%}")
    lines.append(f"현금 {row['cash']:,}원")
    for t in trades:
        lines.append(f"  {t['side']} {t['market']} {t['amount_krw']:,}원 @ {t['avg_price']:,.0f}"
                     f" (종가 대비 {t['basis_bp']:+.1f}bp)")
    if not trades:
        lines.append("  주문 없음")
    return "\n".join(lines)


def report(state_dir):
    p = os.path.join(state_dir, "daily.csv")
    if not os.path.exists(p):
        sys.exit("기록 없음")
    d = pd.read_csv(p, encoding="utf-8-sig")
    print(d[["date", "equity", "paper_ret", "shadow_ret"]].tail(10).to_string(index=False))
    gap = (d["paper_ret"] - d["shadow_ret"]).iloc[-1]
    print(f"\n운영 {len(d)}일 | 페이퍼 {d['paper_ret'].iloc[-1]:+.2%} vs 섀도 {d['shadow_ret'].iloc[-1]:+.2%}"
          f" → 실행 괴리 {gap * 1e4:+.0f}bp")
    tp = os.path.join(state_dir, "trades.csv")
    if os.path.exists(tp):
        t = pd.read_csv(tp, encoding="utf-8-sig")
        print(f"체결 {len(t)}건 | 종가 대비 평균 체결 베이시스 매수 "
              f"{t.loc[t.side == '매수', 'basis_bp'].mean():+.1f}bp / 매도 "
              f"{t.loc[t.side == '매도', 'basis_bp'].mean():+.1f}bp")


# ───────────────────────── 실행 ─────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--report", action="store_true")
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 윈도우 콘솔/로그에서 이모지 오류 방지
    load_env()
    state_dir = C.STATE_DIR + ("_mock" if a.mock else "")

    if a.report:
        return report(state_dir)
    if os.path.exists(C.KILL_SWITCH):
        print(f"킬스위치({C.KILL_SWITCH} 파일) 감지 → 실행 중단"); return

    if a.mock:
        return mock_run(state_dir)

    st = load_state(state_dir)
    from upbit_data import load_closes
    closes = load_closes(C.MARKETS, refresh=True)
    date = closes.index[-1].strftime("%Y-%m-%d")
    if st["last_date"] == date and not a.force:
        print(f"{date}는 이미 처리됨 (재실행: --force)"); return
    if a.force and st["last_date"] == date:
        print("※ --force: 같은 날 재처리 (섀도 장부는 수익 0일로 처리됨)")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        books = fetch_books(C.MARKETS)
        row, trades = run_day(closes, books, st, state_dir, now_str)
    except Exception as e:
        notify(f"[크립토 추세 페이퍼] 오류: {e}"); raise
    from trigger_levels import extra_text   # 매매 기준가 + 주요 일정
    msg = summary_text(row, trades) + extra_text(closes)
    print(msg); notify(msg)


def mock_run(state_dir, days=60):
    """가짜 가격으로 60일 운영 시뮬레이션 → 장부·기록 생성 확인"""
    import shutil
    from run_backtest import synthetic_closes
    shutil.rmtree(state_dir, ignore_errors=True)
    closes_all = synthetic_closes(C.MARKETS)
    st = new_state()
    rng = np.random.default_rng(0)
    for i in range(days, 0, -1):
        closes = closes_all.iloc[:-i] if i > 0 else closes_all
        books = {}
        for m in C.MARKETS:
            c = closes[m].iloc[-1] * (1 + rng.normal(0, 0.002))   # 09:05 가격 ≈ 종가 ± 노이즈
            tick = c * 0.0002
            books[m] = {"bids": [(c - tick * (k + 1), 3e7 / c) for k in range(15)],
                        "asks": [(c + tick * (k + 1), 3e7 / c) for k in range(15)]}
        row, trades = run_day(closes, books, st, state_dir, "mock")
    print(summary_text(row, trades), "\n")
    report(state_dir)


if __name__ == "__main__":
    main()
