"""網頁工具的計算核心（自給自足，部署時不依賴研究倉庫）。

邏輯跟研究倉庫的 src/allocation.py（固定比例＋再平衡）與報告用的回撤／歷次大跌定義相同；
tests/test_web_core.py 用同一份資料逐日比對兩邊權益線，保證網頁跟報告算出一樣的數字。

成本（跟報告相同）：手續費 0.05%／邊、ETF 證交稅 0.1%（賣出）、滑價 1 檔／邊
（檔位看未調整價：未滿 50 元 0.01、50 元以上 0.05）。現金利息 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"

ETF_INFO = {
    "0050": "元大台灣50",
    "006208": "富邦台50",
    "0056": "元大高股息",
    "00878": "國泰永續高股息",
    "00679B": "元大美債20年",
}
COMMISSION = 0.0005
TAX_SELL = 0.001
SLIP_TICKS = 1
TRADING_DAYS = 252
EPISODE_MIN_DD = 0.15
HORIZONS = [("1 個月", 21), ("3 個月", 63), ("6 個月", 126), ("1 年", 252), ("2 年", 504), ("3 年", 756)]


def tick(raw_price: float) -> float:
    return 0.01 if raw_price < 50 else 0.05


def load_prices(data_dir: Path = DATA_DIR) -> dict[str, pd.DataFrame]:
    """每檔：index=date，欄位 raw_open, adj_open, adj_close。"""
    out = {}
    for code in ETF_INFO:
        df = pd.read_csv(data_dir / f"{code}.csv", parse_dates=["date"]).set_index("date").sort_index()
        df["adj_open"] = df["raw_open"] * df["adj_factor"]
        df["adj_close"] = df["raw_close"] * df["adj_factor"]
        out[code] = df[["raw_open", "adj_open", "adj_close"]]
    return out


def common_days(prices: dict[str, pd.DataFrame], codes: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """所選 ETF 都有資料的交易日（以 0050 的交易日為準，0050 一定載入）。"""
    days = prices["0050"].index
    for c in codes:
        days = days.intersection(prices[c].index)
    return days[(days >= start) & (days <= end)]


def simulate(prices: dict[str, pd.DataFrame], weights: dict[str, float], days: pd.DatetimeIndex,
             freq: str = "Y", initial: float = 1.0) -> pd.Series:
    """固定比例；每 freq（"M"/"Q"/"Y"）第一個交易日開盤再平衡，第一天開盤建倉。
    回傳權益線（index=days，第一個值之前再補一個「前一天＝initial」的基準點）。"""
    codes = [c for c, w in weights.items() if w > 0]
    if sum(weights.values()) > 1 + 1e-9 or any(w < 0 for w in weights.values()):
        raise ValueError("weights must be >= 0 and sum to <= 1")
    rebalance = set(pd.Series(days, index=days).groupby(days.to_period(freq)).head(1)) | {days[0]}
    op = {c: prices[c]["adj_open"].reindex(days).to_numpy() for c in codes}
    rop = {c: prices[c]["raw_open"].reindex(days).to_numpy() for c in codes}
    cl = {c: prices[c]["adj_close"].reindex(days).to_numpy() for c in codes}
    cash, units = float(initial), {c: 0.0 for c in codes}
    equity = np.empty(len(days))
    for i, d in enumerate(days):
        if d in rebalance and codes:
            px = {c: op[c][i] for c in codes}
            slip = {c: SLIP_TICKS * tick(rop[c][i]) / rop[c][i] for c in codes}
            value = cash + sum(units[c] * px[c] for c in codes)
            diffs = {c: weights[c] * value - units[c] * px[c] for c in codes}
            for c in sorted(codes):
                if diffs[c] < 0:
                    su = -diffs[c] / px[c]
                    units[c] -= su
                    cash += su * px[c] * (1 - slip[c]) * (1 - COMMISSION - TAX_SELL)
            total_buy = sum(v for v in diffs.values() if v > 0)
            scale = min(1.0, cash / total_buy) if total_buy > 0 else 0.0
            for c in sorted(codes):
                if diffs[c] > 0:
                    spend = diffs[c] * scale
                    units[c] += spend / (px[c] * (1 + slip[c]) * (1 + COMMISSION))
                    cash -= spend
        equity[i] = cash + sum(units[c] * cl[c][i] for c in codes)
    base_day = days[0] - pd.Timedelta(days=1)
    return pd.concat([pd.Series([float(initial)], index=[base_day]), pd.Series(equity, index=days)])


# --- 指標 -------------------------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    cagr: float
    vol: float
    sharpe: float
    mdd: float
    longest_underwater_years: float


def metrics(eq: pd.Series) -> Metrics:
    r = eq.pct_change().dropna()
    n = len(eq) - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (TRADING_DAYS / n) - 1
    vol = r.std(ddof=1) * np.sqrt(TRADING_DAYS)
    sharpe = r.mean() / r.std(ddof=1) * np.sqrt(TRADING_DAYS) if r.std(ddof=1) > 0 else float("nan")
    mdd = (eq / eq.cummax() - 1).min()
    under = (eq < eq.cummax()).to_numpy()
    longest = run = 0
    for u in under:
        run = run + 1 if u else 0
        longest = max(longest, run)
    return Metrics(float(cagr), float(vol), float(sharpe), float(mdd), longest / TRADING_DAYS)


def drawdown_episodes(eq: pd.Series, min_dd: float = EPISODE_MIN_DD) -> list[dict]:
    """從前高跌超過 min_dd 的段落：peak、trough、recovery（回到前高那天，沒回到就是 None）。"""
    peak_val, peak_date = eq.iloc[0], eq.index[0]
    episodes, cur = [], None
    for d, v in eq.items():
        if v >= peak_val:
            if cur is not None:
                cur["recovery"] = d
                episodes.append(cur)
                cur = None
            peak_val, peak_date = v, d
            continue
        dd = v / peak_val - 1
        if cur is None and dd <= -min_dd:
            cur = {"peak": peak_date, "trough": d, "dd": dd, "recovery": None}
        elif cur is not None and dd < cur["dd"]:
            cur["trough"], cur["dd"] = d, dd
    if cur is not None:
        episodes.append(cur)
    return episodes


def episode_table(portfolio: pd.Series, reference: pd.Series) -> pd.DataFrame:
    """以 reference（100% 0050）的大跌段落為準，看兩條權益線在同一組日期跌多少、多久回本。"""
    rows = []
    for ep in drawdown_episodes(reference):
        row = {"高點": ep["peak"].date(), "低點": ep["trough"].date()}
        for name, s in (("你的組合", portfolio), ("100% 0050", reference)):
            peak_v = s.loc[ep["peak"]]
            row[f"{name} 跌幅"] = s.loc[ep["peak"]:ep["trough"]].min() / peak_v - 1
            hit = s.loc[ep["trough"]:]
            hit = hit[hit >= peak_v]
            row[f"{name} 回本"] = f"{(hit.index[0] - ep['peak']).days / 30.44:.0f} 個月" if len(hit) else "尚未回到"
        rows.append(row)
    return pd.DataFrame(rows)


def holding_table(eq: pd.Series) -> pd.DataFrame:
    rows = []
    for label, n in HORIZONS:
        r = (eq.shift(-n) / eq - 1).dropna()
        if len(r) < 20:
            continue
        rows.append({"持有": label, "虧錢機率": (r < 0).mean(), "最慘": r.min(), "中位數": r.median(), "樣本數": len(r)})
    return pd.DataFrame(rows)


def equal_vol_weight(portfolio: pd.Series, zero_zero_fifty: pd.Series) -> float:
    """讓「0050＋現金」的波動跟你的組合一樣所需的 0050 比例（上限 100%）。"""
    v_p = portfolio.pct_change().dropna().std()
    v_0 = zero_zero_fifty.pct_change().dropna().std()
    return float(min(1.0, v_p / v_0)) if v_0 > 0 else 1.0
