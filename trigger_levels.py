"""매일 아침 알림에 붙는 두 가지
  1) 매매 기준가: 내일 종가(오전 9시)가 어디를 넘거나 깨면 모델이 켜지고/꺼지는지
  2) 주요 일정: 앞으로 7일 안의 FOMC·미국 CPI 발표 (events.csv, 한국시간)

단독 실행:  python trigger_levels.py   → 매매 없이 화면에 미리보기
"""
import csv
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config as C
from donchian_trend import LOOKBACKS, realized_vol

EVENTS_CSV = "events.csv"
WEEKDAY = "월화수목금토일"


# ── 1. 매매 기준가 ───────────────────────────────────────
def model_state(close: pd.Series, n: int, exit_rule: str = "mid"):
    """donchian_trend.donchian_position과 같은 규칙을 끝까지 돌려서
    (오늘 보유 여부, 내일 기준가)를 돌려줌.
      - 미보유 모델 → 기준가 = 진입가 (최근 n일 최고 종가, 이 위로 마감하면 매수)
      - 보유 모델   → 기준가 = 청산가 (트레일링 스톱, 이 아래로 마감하면 매도)
    """
    upper = close.rolling(n).max().shift(1)
    lower = close.rolling(n).min().shift(1)
    trail = (upper + lower) / 2 if exit_rule == "mid" else lower

    holding, stop = False, np.nan
    for c, up, tr in zip(close.values, upper.values, trail.values):
        if np.isnan(up) or np.isnan(c):
            continue
        if not holding:
            if c > up:
                holding, stop = True, tr
        else:
            stop = max(stop, tr)
            if c < stop:
                holding = False

    last = close.iloc[-n:]                       # 내일 기준 '직전 n일' = 오늘 포함 최근 n일
    if holding:
        next_trail = (last.max() + last.min()) / 2 if exit_rule == "mid" else last.min()
        return True, max(stop, next_trail)
    return False, last.max()


def _ladder(levels, px, step, arrow, verb, sign):
    """같은 방향 기준가들을 가까운 순으로 2단계까지 누적해서 보여줌."""
    if not levels:
        return [f"  {arrow} 없음"]
    out, done = [], 0
    for lv in sorted(set(levels), key=lambda x: abs(x - px))[:2]:
        k = sum(1 for x in levels if (x <= lv if sign > 0 else x >= lv)) - done
        done += k
        out.append(f"  {arrow} {lv:,.0f}원 ({lv / px - 1:+.1%}) {verb} → 모델 {sign * done:+d}개 "
                   f"(비중 {sign * done * step:+.1%})")
    return out


def levels_text(closes: pd.DataFrame) -> str:
    L = ["", "📍 매매 기준가 (내일 오전 9시 종가 기준)"]
    for m in C.MARKETS:
        close = closes[m].dropna()
        px = float(close.iloc[-1])
        vol = float(realized_vol(close, C.VOL_WINDOW).iloc[-1])
        # 모델 1개가 바뀔 때 포트폴리오 비중 변화
        step = min(C.TARGET_VOL / vol, C.CAP) / len(LOOKBACKS) / len(C.MARKETS)
        ups, downs, on = [], [], 0
        for n in LOOKBACKS:
            holding, lv = model_state(close, n, C.EXIT_RULE)
            on += holding
            (downs if holding else ups).append(lv)
        L.append(f"{m} {px:,.0f}원 · 모델 {on}/9")
        L += _ladder(ups, px, step, "▲", "위로 마감", +1)
        L += _ladder(downs, px, step, "▼", "아래로 마감", -1)
    return "\n".join(L)


# ── 2. 주요 일정 ─────────────────────────────────────────
def events_text(days: int = 7, now: datetime = None) -> str:
    if not os.path.exists(EVENTS_CSV):
        return ""
    now = now or datetime.now()
    rows = []
    with open(EVENTS_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = datetime.strptime(r["kst"], "%Y-%m-%d %H:%M")
            if now <= t <= now + timedelta(days=days):
                rows.append((t, r["event"]))
    if not rows:
        return "\n\n🗓 7일 안 주요 일정: 없음"
    L = ["", "🗓 7일 안 주요 일정 (한국시간, 변동성 주의)"]
    for t, ev in sorted(rows):
        L.append(f"  {t:%m/%d}({WEEKDAY[t.weekday()]}) {t:%H:%M} {ev}")
    return "\n".join(L)


def extra_text(closes: pd.DataFrame) -> str:
    """paper_trader 알림 뒤에 붙일 내용. 여기서 오류가 나도 매매는 멈추지 않게 함."""
    parts = []
    for fn in (lambda: levels_text(closes), events_text):
        try:
            parts.append(fn())
        except Exception as e:
            parts.append(f"\n(추가 정보 오류: {e})")
    return "".join(parts)


if __name__ == "__main__":
    from upbit_data import load_closes
    print(extra_text(load_closes(C.MARKETS, refresh=True)))
