"""
돈치안 앙상블 + 변동성 타기팅 백테스트 (업비트 원화마켓 일봉)

실행 예
    python run_backtest.py                                   # BTC·ETH 기본 설정
    python run_backtest.py --markets KRW-BTC                 # BTC 단독
    python run_backtest.py --band 0.05 --lag 1               # 거래비용 절감 밴드, 하루 늦은 체결
    python run_backtest.py --sweep                           # 파라미터 민감도 점검
    python run_backtest.py --synthetic                       # 네트워크 없이 가짜 데이터로 동작 확인
"""
import argparse
import os

import numpy as np
import pandas as pd

import config as C
from donchian_trend import (backtest, buy_and_hold, ema_cross_weight, ensemble_weight,
                            metrics, yearly_returns, LOOKBACKS)

OUT_DIR = "outputs"


def synthetic_closes(markets, seed=7):
    """국면 전환(상승·하락·횡보)이 있는 가짜 가격 — 코드 동작 확인용"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2017-10-01", pd.Timestamp.today().normalize() - pd.Timedelta(days=1))
    out = {}
    for k, m in enumerate(markets):
        regime = np.repeat(rng.choice([0.004, -0.003, 0.0], size=len(idx) // 60 + 1), 60)[:len(idx)]
        r = regime + rng.normal(0, 0.035 + 0.01 * k, len(idx))
        out[m] = 1e7 / (10 ** k) * np.exp(np.cumsum(r))
    return pd.DataFrame(out, index=idx)


def build_targets(closes, a):
    n = closes.shape[1]
    trend = pd.DataFrame({m: ensemble_weight(closes[m].dropna(), a.target_vol, a.vol_window, a.cap,
                                             LOOKBACKS, a.exit_rule) for m in closes}) / n
    ema = pd.DataFrame({m: ema_cross_weight(closes[m].dropna()) for m in closes}) / n
    return trend.reindex(closes.index), ema.reindex(closes.index)


def fmt(d):
    pct = {"CAGR", "연변동성", "최대낙폭", "평균노출", "연회전율", "연비용"}
    return {k: (f"{v:.1%}" if k in pct else f"{v:.2f}") if pd.notna(v) else "-" for k, v in d.items()}


def run(closes, a, verbose=True):
    trend_t, ema_t = build_targets(closes, a)
    start = a.start or trend_t.apply(lambda s: s.first_valid_index()).max()
    start = pd.Timestamp(start)
    c, tt, et = closes.loc[start:], trend_t.loc[start:], ema_t.loc[start:]

    res_tr, w_tr = backtest(tt, c, a.fee, a.slip_bp, a.band, a.lag)
    res_ema, _ = backtest(et, c, a.fee, a.slip_bp, 0.0, a.lag)
    bh = buy_and_hold(c)

    table = pd.DataFrame({
        "돈치안 앙상블": fmt(metrics(res_tr["equity"], res_tr["exposure"], res_tr["turnover"], res_tr["cost"])),
        "EMA 10/20": fmt(metrics(res_ema["equity"], res_ema["exposure"], res_ema["turnover"], res_ema["cost"])),
        "단순 보유": fmt(metrics(bh)),
    })
    if not verbose:
        return metrics(res_tr["equity"])

    print(f"\n■ 성과 비교  {c.index[0].date()} ~ {c.index[-1].date()}"
          f"  | 목표변동성 {a.target_vol:.0%}, 상한 {a.cap:.0%}, 밴드 {a.band:.0%}, "
          f"수수료 {a.fee:.2%}+슬리피지 {a.slip_bp:.0f}bp, 체결지연 {a.lag}일, 청산 {a.exit_rule}")
    print(table.to_string())

    yr = pd.DataFrame({
        "돈치안 앙상블": yearly_returns(res_tr["equity"]),
        "EMA 10/20": yearly_returns(res_ema["equity"]),
        "단순 보유": yearly_returns(bh),
    })
    yr.index = yr.index.year
    print("\n■ 연도별 수익률")
    print(yr.map(lambda v: f"{v:+.1%}").to_string())

    # 현재 신호 (마지막 확정 일봉 기준) → 실매매 단계에서 그대로 쓰는 값
    print(f"\n■ 현재 목표 비중 ({c.index[-1].date()} 종가 기준, 다음 리밸런싱 시 적용)")
    n = closes.shape[1]
    for m in closes:
        w, models, vol = ensemble_weight(closes[m].dropna(), a.target_vol, a.vol_window, a.cap,
                                         LOOKBACKS, a.exit_rule, return_detail=True)
        on = int(models.iloc[-1].sum())
        print(f"  {m}: 보유 모델 {on}/{len(LOOKBACKS)}개, 90일 변동성 {vol.iloc[-1]:.0%}"
              f" → 코인 비중 {w.iloc[-1]:.1%} / 포트폴리오 비중 {w.iloc[-1] / n:.1%}")
    print(f"  현금 비중: {1 - trend_t.iloc[-1].sum():.1%}")

    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame({"donchian": res_tr["equity"], "ema": res_ema["equity"], "buyhold": bh,
                  "exposure": res_tr["exposure"]}).to_csv(f"{OUT_DIR}/equity.csv", encoding="utf-8-sig")
    w_tr.to_csv(f"{OUT_DIR}/weights.csv", encoding="utf-8-sig")
    plot(res_tr, res_ema, bh)
    print(f"\n저장: {OUT_DIR}/equity.csv, {OUT_DIR}/weights.csv, {OUT_DIR}/backtest.png")


def plot(res_tr, res_ema, bh):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import platform
    except ImportError:
        print("(matplotlib 미설치 → 차트 생략: pip install matplotlib)")
        return
    
    plt.rcParams["font.family"] = {"Windows": "Malgun Gothic", "Darwin": "AppleGothic"}.get(platform.system(), "NanumGothic")
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(3, 1, figsize=(11, 9), sharex=True,
                           gridspec_kw={"height_ratios": [3, 1.3, 1]})
    for s, lab in [(res_tr["equity"], "돈치안 앙상블"), (res_ema["equity"], "EMA 10/20"), (bh, "단순 보유")]:
        ax[0].plot(s, label=lab, lw=1.3 if lab == "돈치안 앙상블" else 1)
        ax[1].plot(s / s.cummax() - 1, lw=1)
    ax[0].set_yscale("log"); ax[0].legend(); ax[0].set_title("누적 수익 (로그)")
    ax[1].set_title("낙폭")
    ax[2].fill_between(res_tr.index, res_tr["exposure"], step="post", alpha=0.5)
    ax[2].set_title("돈치안 앙상블 총 노출 비중"); ax[2].set_ylim(0, 1.05)
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/backtest.png", dpi=120)


def sweep(closes, a):
    """파라미터를 바꿔도 결과가 버티는지 (특정 값에서만 좋으면 과최적화 신호)"""
    rows = []
    for exit_rule in ["mid", "lower"]:
        for tv in [0.15, 0.25, 0.40]:
            for band in [0.0, 0.05]:
                for lag in [0, 1]:
                    b = argparse.Namespace(**{**vars(a), "target_vol": tv, "band": band,
                                              "lag": lag, "exit_rule": exit_rule})
                    m = run(closes, b, verbose=False)
                    rows.append({"청산": exit_rule, "목표변동성": f"{tv:.0%}", "밴드": f"{band:.0%}",
                                 "지연": lag, "CAGR": f"{m['CAGR']:.1%}", "샤프": f"{m['샤프']:.2f}",
                                 "최대낙폭": f"{m['최대낙폭']:.1%}"})
    print("\n■ 파라미터 민감도")
    print(pd.DataFrame(rows).to_string(index=False))


def main():
    p = argparse.ArgumentParser()
    # 기본값은 config.py(실제 페이퍼·실전 운용 설정)와 같게 → 그냥 돌리면 '지금 운용 중인 전략'을 검증
    p.add_argument("--markets", nargs="+", default=C.MARKETS)
    p.add_argument("--target-vol", type=float, default=C.TARGET_VOL,
                   help=f"연 목표 변동성 (운용 {C.TARGET_VOL * 100:.0f}%%, 논문 25%%)")
    p.add_argument("--vol-window", type=int, default=C.VOL_WINDOW)
    p.add_argument("--cap", type=float, default=C.CAP, help="코인별 비중 상한 (현물이라 100%%)")
    p.add_argument("--fee", type=float, default=C.FEE, help="편도 수수료 (업비트 원화 0.05%%)")
    p.add_argument("--slip-bp", type=float, default=C.BT_SLIP_BP, help="편도 슬리피지 가정(bp)")
    p.add_argument("--band", type=float, default=0.0, help="리밸런싱 생략 밴드 (예: 0.05)")
    p.add_argument("--lag", type=int, default=0, help="체결 지연 일수 (1 = 보수적)")
    p.add_argument("--exit-rule", choices=["mid", "lower"], default=C.EXIT_RULE)
    p.add_argument("--start", default=None, help="평가 시작일 YYYY-MM-DD")
    p.add_argument("--no-refresh", action="store_true", help="캐시만 사용")
    p.add_argument("--synthetic", action="store_true")
    p.add_argument("--sweep", action="store_true")
    a = p.parse_args()

    if a.synthetic:
        print("※ 가짜 데이터로 실행 (동작 확인용, 결과 해석 금지)")
        closes = synthetic_closes(a.markets)
    else:
        from upbit_data import load_closes
        print("데이터 로드")
        closes = load_closes(a.markets, refresh=not a.no_refresh)

    if a.sweep:
        sweep(closes, a)
    else:
        run(closes, a)


if __name__ == "__main__":
    main()
