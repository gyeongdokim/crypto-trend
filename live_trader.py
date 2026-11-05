"""
실전 트레이더: 페이퍼와 같은 신호로 업비트에 실제 시장가 주문

흐름 (매일 09:07, 페이퍼 실행 직후)
  1. 안전 확인: STOP/STOP_LIVE 파일, LIVE_ENABLED, 오늘 이미 처리했는지
  2. 확정 일봉 → 목표 비중 계산 (페이퍼와 완전히 같은 함수 사용)
  3. 실제 잔고 조회 → 봇 장부와 맞는지 대조 (안 맞으면 주문 없이 중단 + 알림)
  4. 주문 금액 계산 → 1회 상한 적용 → 매도 먼저, 매수 나중
  5. 체결 내역(실제 수량·대금·수수료)으로 장부 갱신 → 텔레그램 알림
  6. 기준가가 최고점 대비 LIVE_STOP_DD 이하로 떨어지면 STOP_LIVE 생성 → 다음 날부터 중단

봇 장부는 '펀드 기준가' 방식: 운용금액을 늘리면 그날 기준가로 좌수를 추가 발행
→ 투입 금액이 바뀌어도 수익률(기준가)이 왜곡되지 않음

실행
    python live_trader.py --check              # 잔고·IP·키 권한 확인 (주문 없음)
    python live_trader.py --test-order 10000   # 1만원 BTC 매수 → 바로 매도 (연결 테스트)
    python live_trader.py --dry                # 오늘 주문을 '주문 테스트 API'로만 검증 (실주문·장부 변경 없음)
    python live_trader.py                      # 실전 (config.LIVE_ENABLED=True일 때만)
    python live_trader.py --report             # 실전 누적 성과
"""
import argparse
import copy
import json
import os
import sys
from datetime import datetime

import pandas as pd
import requests

import config as C
from paper_trader import (load_env, notify, fetch_books, mid, compute_targets,
                          append_csv, vwap_buy, vwap_sell)
from upbit_client import Upbit, UpbitError

TAG = "[크립토 추세 실전]"
NAV0 = 1000.0                       # 시작 기준가


# ───────────────────────── 장부 ─────────────────────────
def new_ledger():
    return {"cash": 0.0, "qty": {m: 0.0 for m in C.MARKETS}, "units": 0.0, "contributed": 0.0,
            "nav_peak": NAV0, "last_date": None, "start_date": None}


def load_ledger():
    p = os.path.join(C.LIVE_STATE_DIR, "ledger.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else new_ledger()


def save_ledger(lg):
    os.makedirs(C.LIVE_STATE_DIR, exist_ok=True)
    p = os.path.join(C.LIVE_STATE_DIR, "ledger.json")
    json.dump(lg, open(p + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(p + ".tmp", p)       # 쓰다가 꺼져도 장부가 깨지지 않게 (임시파일 → 교체)


def equity(lg, px):
    return lg["cash"] + sum(lg["qty"][m] * px[m] for m in C.MARKETS)


def nav(lg, px):
    return equity(lg, px) / lg["units"] if lg["units"] > 0 else NAV0


def sync_capital(lg, px):
    """config의 LIVE_CAPITAL_KRW와 장부 투입금 차이를 오늘 기준가로 입금/출금 처리"""
    diff = C.LIVE_CAPITAL_KRW - lg["contributed"]
    if abs(diff) < 1:
        return None
    n = nav(lg, px)
    if diff < 0 and -diff > lg["cash"]:
        return f"⚠️ 운용금액 축소 {diff:,.0f}원 요청 — 장부 현금 {lg['cash']:,.0f}원 부족해 이번엔 보류"
    lg["units"] += diff / n
    lg["cash"] += diff
    lg["contributed"] += diff
    return f"💰 운용금액 {'증액' if diff > 0 else '감액'} {diff:+,.0f}원 (기준가 {n:,.1f}에 반영)"


def coin(m):
    return m.split("-")[1]


# ───────────────────────── 하루 처리 ─────────────────────────
def reconcile(lg, bal):
    """실제 계좌가 봇 장부만큼 갖고 있는지 확인. 부족하면 누가 손으로 거래했거나 출금한 것"""
    probs = []
    for m in C.MARKETS:
        have = bal.get(coin(m), 0.0)
        if have + 1e-8 < lg["qty"][m] * (1 - 1e-6):
            probs.append(f"{coin(m)} 계좌 {have:.8f} < 장부 {lg['qty'][m]:.8f}")
    if bal.get("KRW", 0.0) + 1 < lg["cash"]:
        probs.append(f"원화 계좌 {bal.get('KRW', 0):,.0f} < 장부 {lg['cash']:,.0f}")
    return probs


def plan_orders(lg, tgt, px, bal):
    """목표 비중 → 주문 목록. 반환: [(market, side, 금액 또는 수량, 메모)]"""
    eq = equity(lg, px)
    sells, buys = [], []
    for m in C.MARKETS:
        cur = lg["qty"][m] * px[m]
        diff = tgt[m] * eq - cur
        if tgt[m] == 0 and cur > 0:
            diff = -cur
        if abs(diff) < C.MIN_ORDER_KRW:
            continue
        note = ""
        if abs(diff) > C.LIVE_MAX_ORDER_KRW:
            note = f"상한 적용({diff:+,.0f}→{C.LIVE_MAX_ORDER_KRW:,}원)"
            diff = C.LIVE_MAX_ORDER_KRW * (1 if diff > 0 else -1)
        if diff < 0:
            q = lg["qty"][m] if (tgt[m] == 0 and not note) else -diff / px[m]
            q = min(q, lg["qty"][m], bal.get(coin(m), 0.0))
            if q * px[m] >= C.MIN_ORDER_KRW:
                sells.append((m, "ask", q, note))
        else:
            buys.append((m, "bid", diff, note))
    return sells + buys


def apply_fill(lg, m, side, vol, funds, fee):
    if side == "bid":
        lg["qty"][m] += vol; lg["cash"] -= funds + fee
    else:
        lg["qty"][m] -= vol; lg["cash"] += funds - fee


def settle_pending(ex, lg, now_str, dry=False):
    """지난 실행에서 주문은 나갔지만 체결 확인 전에 끊긴 주문을 조회해 장부에 반영.
    이걸 안 하면 실제 계좌와 장부가 어긋나서 다음 대조에서 매매가 멈춤"""
    msgs, trades = [], []
    for p in list(lg.get("pending", [])):
        vol, funds, fee, state = ex.wait_fill(p["uuid"])
        if state not in ("done", "cancel"):
            raise RuntimeError(f"미확정 주문 {p['market']} {p['side']} (uuid={p['uuid']}, state={state}) "
                               "→ 업비트에서 확인 후 재실행")
        if vol > 0:
            apply_fill(lg, p["market"], p["side"], vol, funds, fee)
            trades.append({"time": now_str, "date": p["date"], "market": p["market"],
                           "side": "매수" if p["side"] == "bid" else "매도", "amount_krw": round(funds),
                           "qty": vol, "avg_price": funds / vol, "close": p["close"],
                           "basis_bp": round((funds / vol / p["close"] - 1) * 1e4, 2),
                           "fee_krw": round(fee, 1), "state": state, "note": "이전 실행 미확정분 반영"})
        lg["pending"].remove(p)
        msgs.append(f"  ↩️ 미확정 주문 반영: {p['market']} {p['side']} {vol:.8f}개 / {funds:,.0f}원 ({state})")
    if msgs and not dry:
        save_ledger(lg)
    return msgs, trades


def run_day(ex, closes, books, lg, now_str, dry=False, suffix=""):
    date = closes.index[-1].strftime("%Y-%m-%d")
    px = {m: mid(books[m]) for m in C.MARKETS}
    msgs = []
    if lg["start_date"] is None:
        lg["start_date"] = date
    msgs, trades = settle_pending(ex, lg, now_str, dry)      # 대조보다 먼저
    cap_msg = sync_capital(lg, px)
    if cap_msg:
        msgs.append(cap_msg)

    bal = ex.balances()
    probs = reconcile(lg, bal)
    if probs:
        raise RuntimeError("잔고 불일치 → 주문 중단\n  " + "\n  ".join(probs) +
                           "\n(봇 운영 중엔 같은 계정에서 BTC·ETH 직접 거래 금지)")

    tgt, info = compute_targets(closes)
    for m, side, amt, note in plan_orders(lg, tgt, px, bal):
        close = float(closes[m].iloc[-1])
        if side == "bid":                                     # 매수 직전 원화 다시 확인
            krw_av = ex.balances().get("KRW", 0.0) if not dry else bal.get("KRW", 0.0)
            amt = min(amt, lg["cash"] / (1 + C.FEE), krw_av / (1 + C.FEE))
            if amt < C.MIN_ORDER_KRW:
                msgs.append(f"  {m} 매수 생략: 원화 부족"); continue
            params = ex.order_params(m, "bid", krw=amt, identifier=f"ct-{date}-{coin(m)}-bid{suffix}")
        else:
            params = ex.order_params(m, "ask", volume=amt, identifier=f"ct-{date}-{coin(m)}-ask{suffix}")

        if dry:                                               # 실주문 대신 주문 테스트 API + 호가로 가상 체결
            ex.test_place(params)
            if side == "bid":
                vol, avg = vwap_buy(books[m]["asks"], int(amt)); funds = int(amt)
            else:
                vol = float(params["volume"]); funds, avg = vwap_sell(books[m]["bids"], vol)
            fee = funds * C.FEE; state = "dry"
        else:
            o = ex.place(params)
            # 체결 확인 전에 먼저 '미확정'으로 기록 → 여기서 끊겨도 다음 실행이 조회해서 반영
            lg.setdefault("pending", []).append({"uuid": o["uuid"], "market": m, "side": side,
                                                 "date": date, "close": close})
            save_ledger(lg)
            vol, funds, fee, state = ex.wait_fill(o["uuid"])
            if state not in ("done", "cancel"):              # 아직 체결 중 → 미확정으로 남겨 둠
                msgs.append(f"  ⚠️ {m} {side} 체결 확인 지연 (state={state}) → 다음 실행 때 반영, "
                            "장부 현금이 불확실하므로 오늘 남은 주문은 중단")
                break
            lg["pending"] = [p for p in lg["pending"] if p["uuid"] != o["uuid"]]
            if vol <= 0:
                save_ledger(lg)
                msgs.append(f"  ⚠️ {m} {side} 미체결 (state={state}, uuid={o['uuid']})"); continue
            avg = funds / vol

        apply_fill(lg, m, side, vol, funds, fee)
        if not dry:
            save_ledger(lg)                                   # 주문 하나마다 저장 (중간에 꺼져도 기록 보존)
        trades.append({"time": now_str, "date": date, "market": m,
                       "side": "매수" if side == "bid" else "매도", "amount_krw": round(funds),
                       "qty": vol, "avg_price": avg, "close": close,
                       "basis_bp": round((avg / close - 1) * 1e4, 2), "fee_krw": round(fee, 1),
                       "state": state, "note": note})

    eq = equity(lg, px)
    n = nav(lg, px)
    lg["nav_peak"] = max(lg["nav_peak"], n)
    dd = n / lg["nav_peak"] - 1
    lg["last_date"] = date
    row = {"date": date, "run_time": now_str, "equity": round(eq), "contributed": round(lg["contributed"]),
           "nav": round(n, 2), "ret": n / NAV0 - 1, "dd": round(dd, 4), "cash": round(lg["cash"])}
    for m in C.MARKETS:
        row[f"{m}_target"] = round(tgt[m], 4)
        row[f"{m}_actual"] = round(lg["qty"][m] * px[m] / eq, 4) if eq > 0 else 0
        row[f"{m}_models"] = info[m]["models_on"]
    if dd <= C.LIVE_STOP_DD:
        msgs.append(f"🛑 기준가 최고점 대비 {dd:.1%} ≤ {C.LIVE_STOP_DD:.0%} → 실매매 중단 (STOP_LIVE 생성). "
                    "보유분은 그대로 둠, 판단 후 STOP_LIVE 삭제하면 재개")
        if not dry:
            open(C.LIVE_KILL_SWITCH, "w", encoding="utf-8").write(f"{now_str} 낙폭 {dd:.1%}\n")
    if not dry:
        append_csv(C.LIVE_STATE_DIR, "trades.csv", trades)
        append_csv(C.LIVE_STATE_DIR, "daily.csv", [row])
        save_ledger(lg)
    return row, trades, msgs


def paper_basis(date):
    """같은 날 페이퍼 체결 베이시스 (실전과 비교용)"""
    p = os.path.join(C.STATE_DIR, "trades.csv")
    if not os.path.exists(p):
        return {}
    t = pd.read_csv(p, encoding="utf-8-sig")
    t = t[t["date"] == date]
    return {(r.market, r.side): r.basis_bp for r in t.itertuples()}


def summary_text(row, trades, msgs, dry=False):
    pb = paper_basis(row["date"])
    lines = [f"{TAG}{' [DRY 테스트]' if dry else ''} {row['date']} 종가 기준",
             f"평가액 {row['equity']:,}원 (투입 {row['contributed']:,}) | 기준가 {row['nav']:,.1f} "
             f"({row['ret']:+.2%}) | 고점대비 {row['dd']:.1%}"]
    for m in C.MARKETS:
        lines.append(f"{m}: 모델 {row[f'{m}_models']}/9, 목표 {row[f'{m}_target']:.1%} / 실제 {row[f'{m}_actual']:.1%}")
    lines.append(f"현금 {row['cash']:,}원")
    for t in trades:
        pp = pb.get((t["market"], t["side"]))
        cmp_ = f", 페이퍼 {pp:+.1f}bp" if pp is not None else ""
        lines.append(f"  {t['side']} {t['market']} {t['amount_krw']:,}원 @ {t['avg_price']:,.0f} "
                     f"(종가 대비 {t['basis_bp']:+.1f}bp{cmp_}, 수수료 {t['fee_krw']:,.0f}원) {t['note']}")
    if not trades:
        lines.append("  주문 없음")
    return "\n".join(lines + msgs)


# ───────────────────────── 점검 명령 ─────────────────────────
def check(ex):
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text
        print(f"현재 이 PC의 공인 IP: {ip}  (업비트 허용 IP와 같아야 함)")
    except Exception:
        pass
    bal = ex.balances()
    print("잔고:", {k: v for k, v in bal.items() if v > 0})
    print("자산조회 권한 OK")
    try:
        ex.test_place(ex.order_params("KRW-BTC", "bid", krw=C.MIN_ORDER_KRW))
        print("주문하기 권한 OK (주문 테스트 API, 실제 주문 없음)")
    except UpbitError as e:
        print(f"주문 테스트 실패: {e}")
    need = C.LIVE_CAPITAL_KRW - load_ledger()["contributed"]
    if need > 0:
        ok = bal.get("KRW", 0) >= need * (1 + C.FEE)
        print(f"첫 투입 필요 원화 {need:,.0f}원 → {'충분' if ok else '부족! 원화 입금 필요'}")


def test_order(ex, krw):
    # 왕복 비용은 계좌 원화에서 빠지지만 봇 장부엔 안 잡힘 → 봇 몫이 아닌 '여유 원화'로만 테스트해야
    # 다음 날 잔고 대조(계좌 ≥ 장부)가 깨지지 않음
    lg = load_ledger()
    spare = ex.balances().get("KRW", 0.0) - lg["cash"]
    need = krw * (1 + C.FEE) * 1.01
    if spare < need:
        return print(f"여유 원화 {spare:,.0f}원 < 필요 {need:,.0f}원 → 테스트 안 함\n"
                     f"(장부 현금 {lg['cash']:,.0f}원은 봇 몫. 테스트 비용이 여기서 빠지면 다음 날 잔고 불일치로 실전이 멈춤)")
    if input(f"KRW-BTC {krw:,}원 시장가 매수 후 바로 매도합니다. 진행하려면 yes: ").strip() != "yes":
        return print("취소")
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    o = ex.place(ex.order_params("KRW-BTC", "bid", krw=krw, identifier=f"ct-test-{stamp}-bid"))
    vb, fb, feeb, sb = ex.wait_fill(o["uuid"])
    print(f"매수 체결: {vb:.8f} BTC, {fb:,.0f}원, 수수료 {feeb:.2f}원, state={sb}")
    if vb <= 0:
        return print("매수 미체결 → 매도 생략")
    o = ex.place(ex.order_params("KRW-BTC", "ask", volume=vb, identifier=f"ct-test-{stamp}-ask"))
    vs, fs, fees, ss = ex.wait_fill(o["uuid"])
    print(f"매도 체결: {vs:.8f} BTC, {fs:,.0f}원, 수수료 {fees:.2f}원, state={ss}")
    cost = fb + feeb - (fs - fees)
    msg = (f"{TAG} 연결 테스트 OK\n매수 {fb:,.0f}원 → 매도 {fs:,.0f}원, 왕복 비용 {cost:,.1f}원 "
           f"({cost / fb * 1e4:.1f}bp)")
    print(msg); notify(msg)


def report():
    p = os.path.join(C.LIVE_STATE_DIR, "daily.csv")
    if not os.path.exists(p):
        sys.exit("실전 기록 없음")
    d = pd.read_csv(p, encoding="utf-8-sig")
    print(d[["date", "equity", "contributed", "nav", "ret", "dd"]].tail(15).to_string(index=False))


# ───────────────────────── 실행 ─────────────────────────
def make_exchange():
    return Upbit(os.environ.get("UPBIT_ACCESS_KEY"), os.environ.get("UPBIT_SECRET_KEY"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--test-order", type=int)
    p.add_argument("--dry", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--report", action="store_true")
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env()

    if a.report:
        return report()
    for f in (C.KILL_SWITCH, C.LIVE_KILL_SWITCH):
        if os.path.exists(f):
            print(f"킬스위치({f}) 감지 → 실전 실행 안 함"); return
    try:
        ex = make_exchange()
        if a.check:
            return check(ex)
        if a.test_order:
            return test_order(ex, max(a.test_order, C.MIN_ORDER_KRW))
        if not C.LIVE_ENABLED and not a.dry:
            print("LIVE_ENABLED=False → 실전 주문 비활성 (테스트는 --dry)"); return

        lg = load_ledger()
        if a.dry:
            lg = copy.deepcopy(lg)
        from upbit_data import load_closes
        closes = load_closes(C.MARKETS, refresh=True)
        date = closes.index[-1].strftime("%Y-%m-%d")
        if lg["last_date"] == date and not (a.force or a.dry):
            print(f"{date}는 이미 처리됨 (재실행: --force)"); return
        now = datetime.now()
        suffix = f"-r{now:%H%M%S}" if a.force else ""     # 재실행 시 identifier 중복 방지
        books = fetch_books(C.MARKETS)
        row, trades, msgs = run_day(ex, closes, books, lg, now.strftime("%Y-%m-%d %H:%M:%S"),
                                    dry=a.dry, suffix=suffix)
    except Exception as e:
        notify(f"{TAG} ❌ 오류 — 오늘 실전 주문이 완료되지 않았을 수 있음\n{e}")
        raise
    msg = summary_text(row, trades, msgs, dry=a.dry)
    print(msg); notify(msg)


if __name__ == "__main__":
    main()
