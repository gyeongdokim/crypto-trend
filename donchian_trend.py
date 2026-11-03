"""
돈치안 채널 앙상블 + 변동성 타기팅 (롱-플랫, 현물)

참고: Zarattini, Pagani, Barbon (2025) "Catching Crypto Trends;
      A Tactical Approach for Bitcoin and Altcoins", SFI Research Paper 25-80
※ 논문 요약 공개자료 기반 재구현. 청산 규칙 등 세부는 원문과 다를 수 있으므로
  파라미터(exit_rule 등)로 바꿔 비교할 수 있게 함.

전략 구조
1) 기간 N ∈ {5,10,20,30,60,90,150,250,360}마다 독립된 돈치안 모델 9개
   - 진입: 종가가 직전 N일 최고 종가를 돌파
   - 청산: 트레일링 스톱 = 채널 중간값((최고+최저)/2), 위로만 이동 / 종가가 하회하면 청산
2) 각 모델의 비중 = 보유 시 min(목표변동성 / 90일 실현변동성, 상한)
3) 9개 모델 비중을 동일가중 평균 → 코인별 목표 비중 (0 ~ 상한)
4) 여러 코인이면 자본을 1/N씩 배분
"""
import numpy as np
import pandas as pd

LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
ANN = 365          # 크립토는 365일 거래


# ───────────────────────── 신호 ─────────────────────────
def donchian_position(close: pd.Series, n: int, exit_rule: str = "mid") -> pd.Series:
    """단일 돈치안 모델의 보유 여부(0/1). t일 종가까지의 정보만 사용"""
    upper = close.rolling(n).max().shift(1)      # 오늘 제외 직전 n일 최고 종가
    lower = close.rolling(n).min().shift(1)
    trail = (upper + lower) / 2 if exit_rule == "mid" else lower

    c, up, tr = close.values, upper.values, trail.values
    pos = np.full(len(c), np.nan)
    holding, stop = False, np.nan
    for i in range(len(c)):
        if np.isnan(up[i]) or np.isnan(c[i]):
            continue                              # 워밍업 구간
        if not holding:
            if c[i] > up[i]:
                holding, stop = True, tr[i]
        else:
            stop = max(stop, tr[i])               # 스톱은 위로만
            if c[i] < stop:
                holding = False
        pos[i] = 1.0 if holding else 0.0
    return pd.Series(pos, index=close.index)


def realized_vol(close: pd.Series, window: int = 90) -> pd.Series:
    return close.pct_change().rolling(window).std() * np.sqrt(ANN)


def ensemble_weight(close: pd.Series, target_vol=0.25, vol_window=90, cap=1.0,
                    lookbacks=LOOKBACKS, exit_rule="mid", return_detail=False):
    """코인 하나의 목표 비중 시계열 (0 ~ cap)"""
    vol = realized_vol(close, vol_window)
    size = (target_vol / vol).clip(upper=cap)
    models = pd.DataFrame({n: donchian_position(close, n, exit_rule) for n in lookbacks})
    weight = models.mul(size, axis=0).mean(axis=1, skipna=False)   # 모든 모델 준비돼야 시작
    if return_detail:
        return weight, models, vol
    return weight


def ema_cross_weight(close: pd.Series, fast=10, slow=20) -> pd.Series:
    """비교용 베이스라인: Bitwise Trendwise식 EMA 10/20 롱-플랫 (보유 시 100%)"""
    f = close.ewm(span=fast, adjust=False).mean()
    s = close.ewm(span=slow, adjust=False).mean()
    w = (f > s).astype(float)
    w[close.isna()] = np.nan
    return w


# ───────────────────────── 백테스트 엔진 ─────────────────────────
def backtest(target: pd.DataFrame, closes: pd.DataFrame, fee=0.0005, slip_bp=5.0,
             band=0.0, lag=0):
    """
    target: 날짜×코인 목표 비중(포트폴리오 대비). t일 종가 기준으로 계산된 값
    체결 가정: t일 종가에 목표 비중으로 리밸런싱 → t+1일 수익률을 받음
    lag=1이면 하루 늦게 체결(보수적 가정)
    band: |목표-현재| ≤ band면 매매 생략 (거래비용 절감). 단, 목표 0이면 항상 전량 청산
    현금 이자는 0으로 가정
    """
    tgt = target.shift(lag).fillna(0.0).values
    rets = closes.pct_change().fillna(0.0).values
    cost_rate = fee + slip_bp / 1e4

    T, A = tgt.shape
    w = np.zeros(A)
    equity, eq_hist, w_hist, to_hist, cost_hist = 1.0, [], [], [], []
    for t in range(T):
        # 1) 어제 종가에 잡은 포지션이 오늘 수익률을 받음 + 비중 드리프트
        pr = float(w @ rets[t])
        equity *= 1 + pr
        if 1 + pr > 0:
            w = w * (1 + rets[t]) / (1 + pr)
        # 2) 오늘 종가에 리밸런싱
        diff = tgt[t] - w
        do = (np.abs(diff) > band) | ((tgt[t] == 0) & (w > 0))
        trade = np.where(do, diff, 0.0)
        turnover = np.abs(trade).sum()
        cost = turnover * cost_rate
        equity *= 1 - cost
        w = (w + trade) / (1 - cost) if cost < 1 else w + trade
        eq_hist.append(equity); w_hist.append(w.copy())
        to_hist.append(turnover); cost_hist.append(cost)

    idx = target.index
    return pd.DataFrame({
        "equity": eq_hist,
        "exposure": [x.sum() for x in w_hist],
        "turnover": to_hist,
        "cost": cost_hist,
    }, index=idx), pd.DataFrame(w_hist, index=idx, columns=target.columns)


def buy_and_hold(closes: pd.DataFrame) -> pd.Series:
    """동일가중 매수 후 보유 (시작일 1/N씩 매수, 리밸런싱 없음)"""
    rel = closes / closes.iloc[0]
    return rel.mean(axis=1)


# ───────────────────────── 성과 지표 ─────────────────────────
def metrics(equity: pd.Series, exposure=None, turnover=None, cost=None) -> dict:
    r = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365
    cagr = equity.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    vol = r.std() * np.sqrt(ANN)
    sharpe = r.mean() / r.std() * np.sqrt(ANN) if r.std() > 0 else np.nan
    dd = equity / equity.cummax() - 1
    mdd = dd.min()
    out = {
        "CAGR": cagr,
        "연변동성": vol,
        "샤프": sharpe,
        "최대낙폭": mdd,
        "칼마": cagr / abs(mdd) if mdd < 0 else np.nan,
    }
    if exposure is not None:
        out["평균노출"] = exposure.mean()
    if turnover is not None:
        out["연회전율"] = turnover.sum() / years
    if cost is not None:
        out["연비용"] = cost.sum() / years
    return out


def yearly_returns(equity: pd.Series) -> pd.Series:
    y = equity.resample("YE").last()
    first = equity.iloc[0]
    return y.pct_change().fillna(y.iloc[0] / first - 1)
