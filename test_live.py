"""
live_trader 안전장치 테스트 — 진짜 업비트 대신 '가짜 거래소'로 주문 흐름을 검증 (키·네트워크 불필요)
    python test_live.py
"""
import os
import sys
import tempfile

import numpy as np
import pandas as pd

import config as C
import live_trader as L
from paper_trader import vwap_buy, vwap_sell


class FakeUpbit:
    """업비트처럼 행동하는 가짜 거래소: 호가로 체결, 잔고·수수료 반영"""
    order_params = staticmethod(L.Upbit.order_params)

    def __init__(self, books, krw):
        self.books, self.bal, self.orders, self.ids = books, {"KRW": krw}, {}, set()

    def balances(self):
        return dict(self.bal)

    def _check(self, p):
        if p.get("identifier") in self.ids:
            raise L.UpbitError(400, "duplicate_identifier", "dup")

    def test_place(self, p):
        self._check(p); return {"uuid": "test"}

    def place(self, p):
        self._check(p); self.ids.add(p.get("identifier"))
        m, c = p["market"], p["market"].split("-")[1]
        if p["side"] == "bid":
            krw = float(p["price"]); fee = krw * C.FEE
            assert self.bal["KRW"] >= krw + fee, "insufficient_funds_bid"
            vol, _ = vwap_buy(self.books[m]["asks"], krw)
            self.bal["KRW"] -= krw + fee; self.bal[c] = self.bal.get(c, 0) + vol; funds = krw
        else:
            vol = float(p["volume"]); assert self.bal.get(c, 0) >= vol - 1e-12, "insufficient_funds_ask"
            funds, _ = vwap_sell(self.books[m]["bids"], vol); fee = funds * C.FEE
            self.bal[c] -= vol; self.bal["KRW"] += funds - fee
        u = f"u{len(self.orders)}"; self.orders[u] = (vol, funds, fee); return {"uuid": u}

    def wait_fill(self, u):
        return (*self.orders[u], "done")


def books_at(closes, scale=1.0):
    b = {}
    for m in C.MARKETS:
        c = closes[m].iloc[-1] * scale; t = c * 0.0002
        b[m] = {"bids": [(c - t * (k + 1), 5e7 / c) for k in range(15)],
                "asks": [(c + t * (k + 1), 5e7 / c) for k in range(15)]}
    return b


def uptrend(n=400):
    """모델이 다 켜지도록 꾸준히 오르는 가짜 가격"""
    idx = pd.date_range("2025-01-01", periods=n)
    rng = np.random.default_rng(1)
    r = 0.004 + rng.normal(0, 0.02, (n, len(C.MARKETS)))
    r[-10:] = 0.02                                   # 마지막 열흘은 신고가 행진
    return pd.DataFrame(1e6 * np.exp(np.cumsum(r, axis=0)), idx, C.MARKETS)


ok = 0
def check(cond, name):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        sys.exit(1)
    ok += 1


tmp = tempfile.mkdtemp()
C.LIVE_STATE_DIR = os.path.join(tmp, "state_live")
C.LIVE_KILL_SWITCH = os.path.join(tmp, "STOP_LIVE")
C.LIVE_CAPITAL_KRW = 1_000_000
C.LIVE_MAX_ORDER_KRW = 600_000
L.notify = lambda t: None

closes = uptrend()
print("1) 첫날: 100만원 투입, 상승장 → 매수")
books = books_at(closes)
ex = FakeUpbit(books, krw=1_500_000)            # 계좌엔 봇 몫보다 원화가 더 있음 (다른 돈)
lg = L.new_ledger()
row, trades, msgs = L.run_day(ex, closes, books, lg, "t1")
tgt, _ = L.compute_targets(closes)
check(len(trades) == 2 and all(t["side"] == "매수" for t in trades), "BTC·ETH 둘 다 매수")
for m in C.MARKETS:
    check(abs(row[f"{m}_actual"] - tgt[m]) < 0.01, f"{m} 실제 비중 {row[f'{m}_actual']:.1%} ≈ 목표 {tgt[m]:.1%}")
check(ex.bal["KRW"] >= 500_000 - 1, "봇이 계좌의 '다른 돈' 50만원은 건드리지 않음")
check(abs(lg["contributed"] - 1e6) < 1 and abs(row["nav"] - 1000) < 5, f"기준가 시작 ≈1000 ({row['nav']})")
print(L.summary_text(row, trades, msgs))

print("\n2) 같은 identifier로 다시 주문 → 거래소가 거부 (중복 주문 방지)")
try:
    ex.place(ex.order_params("KRW-BTC", "bid", krw=10000, identifier=f"ct-{row['date']}-BTC-bid"))
    check(False, "중복 거부")
except L.UpbitError as e:
    check(e.name == "duplicate_identifier", "중복 주문 거부됨")

print("\n3) 누가 같은 계정에서 BTC를 직접 팔면 → 주문 없이 중단")
ex2 = FakeUpbit(books, 0); ex2.bal = dict(ex.bal); ex2.bal["BTC"] *= 0.5
try:
    L.run_day(ex2, closes, books, L.copy.deepcopy(lg), "t3"); check(False, "불일치 감지")
except RuntimeError as e:
    check("잔고 불일치" in str(e) and ex2.orders == {}, "불일치 감지, 주문 0건")

print("\n4) 운용금액 100만 → 300만 증액: 기준가 유지, 1회 주문 상한 적용")
C.LIVE_CAPITAL_KRW = 3_000_000
ex.bal["KRW"] += 2_000_000
nav_before = L.nav(lg, {m: L.mid(books[m]) for m in C.MARKETS})
closes2 = closes.copy(); closes2.loc[closes.index[-1] + pd.Timedelta(days=1)] = closes.iloc[-1]
books2 = books_at(closes2); ex.books = books2
row, trades, msgs = L.run_day(ex, closes2, books2, lg, "t4")
check(abs(row["nav"] - nav_before) < 3, f"증액 후 기준가 {nav_before:.1f} → {row['nav']:.1f} (거의 그대로)")
check(all(t["amount_krw"] <= C.LIVE_MAX_ORDER_KRW + 1 for t in trades), "모든 주문 ≤ 60만원")
check(any("상한" in t["note"] for t in trades), "상한에 걸린 주문에 메모 표시")
print(L.summary_text(row, trades, msgs))

print("\n5) 폭락(-40%) → 추세 이탈 전량 매도 + 낙폭 중단 장치 작동")
crash = closes2.copy()
for i in range(1, 6):
    crash.loc[closes2.index[-1] + pd.Timedelta(days=i)] = closes2.iloc[-1] * (1 - 0.1 * i)
books3 = books_at(crash); ex.books = books3
C.LIVE_MAX_ORDER_KRW = 10_000_000
row, trades, msgs = L.run_day(ex, crash, books3, lg, "t5")
tgt, _ = L.compute_targets(crash)
print(L.summary_text(row, trades, msgs))
check(all(t["side"] == "매도" for t in trades), "매도만 발생")
check(all(tgt[m] > 0 or lg["qty"][m] * L.mid(books3[m]) < C.MIN_ORDER_KRW for m in C.MARKETS),
      "목표 0인 코인은 전량 청산")
check(os.path.exists(C.LIVE_KILL_SWITCH), "STOP_LIVE 생성됨 (다음 날부터 실전 중단)")

print("\n6) 장부 = 실제 계좌 (봇 몫 + 다른 돈 50만원)")
for m in C.MARKETS:
    c = m.split("-")[1]
    check(abs(ex.bal.get(c, 0) - lg["qty"][m]) < 1e-9, f"{c} 계좌 = 장부")
check(abs(ex.bal["KRW"] - 500_000 - lg["cash"]) < 1, "원화: 계좌 - 50만 = 장부 현금")

print("\n7) 주문은 나갔는데 체결 확인 중 끊김 → 다음 실행이 조회해서 장부에 반영")
C.LIVE_STATE_DIR = os.path.join(tmp, "state_live7")
C.LIVE_CAPITAL_KRW, C.LIVE_MAX_ORDER_KRW = 1_000_000, 600_000
books7 = books_at(closes)
ex7 = FakeUpbit(books7, krw=1_000_000)               # 여유 원화 없이 봇 몫만 있는 계좌
real_wait, calls = ex7.wait_fill, []
def flaky_wait(u):
    calls.append(u)
    if len(calls) == 1:
        raise ConnectionError("Read timed out")       # 첫 주문 직후 네트워크 끊김
    return real_wait(u)
ex7.wait_fill = flaky_wait
try:
    L.run_day(ex7, closes, books7, L.new_ledger(), "t7"); check(False, "끊김 재현")
except ConnectionError:
    pass
lg7 = L.load_ledger()                                 # 파일에서 다시 읽기 = 프로그램 재시작
check(len(lg7.get("pending", [])) == 1 and len(ex7.orders) == 1, "주문 1건 나감 → 장부에 미확정으로 저장됨")
row, trades, msgs = L.run_day(ex7, closes, books7, lg7, "t7b")
check(not lg7["pending"] and any("미확정" in t["note"] for t in trades), "재실행 시 미확정분 반영 (잔고 불일치로 안 멈춤)")
for m in C.MARKETS:
    c = m.split("-")[1]
    check(abs(ex7.bal.get(c, 0) - lg7["qty"][m]) < 1e-9, f"{c} 계좌 = 장부")
check(abs(ex7.bal["KRW"] - lg7["cash"]) < 1, "원화 계좌 = 장부")
tgt, _ = L.compute_targets(closes)
check(all(abs(row[f"{m}_actual"] - tgt[m]) < 0.01 for m in C.MARKETS), "두 코인 모두 목표 비중 도달")

print("\n8) --test-order: 봇 장부 밖 여유 원화가 없으면 주문 안 냄")
placed = []
ex7.place = lambda p: placed.append(p) or {"uuid": "x"}
L.test_order(ex7, 10_000)
check(placed == [], "여유 원화 0 → 테스트 주문 거부")

print("\n9) 알림 전송 실패 시 로그에 봇 토큰이 안 남음")
import contextlib, io, requests
import paper_trader as P
os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "123456:SECRET-TOKEN", "1"
orig_post = requests.post
def boom(url, **kw):
    raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
requests.post = boom
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    sent = P.notify("x")
requests.post = orig_post
check(sent is False and "SECRET" not in buf.getvalue() and "<TOKEN>" in buf.getvalue(),
      f"토큰 가려짐: {buf.getvalue().strip()}")
print(f"\n전체 {ok}개 통과")
