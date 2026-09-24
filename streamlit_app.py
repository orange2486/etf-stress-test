"""台股 ETF 配置壓力測試器（Streamlit）。

本機執行：streamlit run web_tool/app/streamlit_app.py
計算全部在 core.py；這個檔只負責介面。
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

import core

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
FREQ = {"每年": "Y", "每季": "Q", "每月": "M"}
ALL_START = pd.Timestamp("2010-01-01")

st.set_page_config(page_title="台股 ETF 配置壓力測試器", page_icon="📉", layout="wide")


@st.cache_data
def prices() -> dict[str, pd.DataFrame]:
    return core.load_prices()


@st.cache_data
def run(weights: tuple, freq: str, start: pd.Timestamp, end: pd.Timestamp):
    p = prices()
    w = dict(weights)
    codes = [c for c, v in w.items() if v > 0]
    days = core.common_days(p, codes, start, end)
    port = core.simulate(p, w, days, freq=freq)
    ref = core.simulate(p, {"0050": 1.0}, days, freq=freq)
    ev_w = core.equal_vol_weight(port, ref)
    ev = core.simulate(p, {"0050": ev_w}, days, freq=freq)
    return port, ref, ev, ev_w


def pct(x: float, signed: bool = True) -> str:
    return f"{x:+.1%}" if signed else f"{x:.1%}"


# --- 側欄：輸入 ---------------------------------------------------------------------

p = prices()
latest = min(df.index.max() for df in p.values())
with st.sidebar:
    st.header("你的配置")
    st.caption("各 ETF 的比例（%），剩下的放現金（利息以 0 計）")
    qp = st.query_params
    has_mix = any(c in qp for c in core.ETF_INFO)

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return min(max(int(qp.get(key, default)), lo), hi)
        except ValueError:
            return default

    weights = {}
    for code, name in core.ETF_INFO.items():
        default = _int(code, 0, 0, 100) if has_mix else (75 if code == "0050" else 0)
        weights[code] = st.number_input(f"{code} {name}", min_value=0, max_value=100, step=5,
                                        value=default, key=code) / 100
    total = sum(weights.values())
    if total > 1 + 1e-9:
        st.error(f"合計 {total:.0%}，超過 100%，請調低。")
        st.stop()
    st.metric("現金", f"{1 - total:.0%}")
    freq_keys = list(FREQ)
    freq_default = next((i for i, k in enumerate(freq_keys) if FREQ[k] == qp.get("f")), 0)
    freq_label = st.radio("多久調回原比例", freq_keys, index=freq_default, horizontal=True)
    selected = [c for c, v in weights.items() if v > 0]
    earliest = max([ALL_START] + [p[c].index.min() for c in selected])
    years = list(range(earliest.year + (earliest.month > 1), latest.year - 1))
    if not years:
        st.error("所選 ETF 的歷史太短（不到 2 年），無法做壓力測試。")
        st.stop()
    y_default = _int("y", years[0], years[0], years[-1])
    start_year = st.selectbox("從哪一年開始", years, index=years.index(y_default))
    amount = st.number_input("投入金額（元）", min_value=10_000, step=100_000,
                             value=_int("amt", 1_000_000, 10_000, 10**10))
    # 把目前設定寫回網址：複製網址就能分享同一個組合
    st.query_params.from_dict({**{c: int(round(v * 100)) for c, v in weights.items() if v > 0},
                               "f": FREQ[freq_label], "y": start_year, "amt": int(amount)})
    st.caption("網址會記住目前的設定，複製網址就能分享這個組合。")

start = max(earliest, pd.Timestamp(start_year, 1, 1))

# --- 主畫面 -------------------------------------------------------------------------

st.title("台股 ETF 配置壓力測試器")
st.markdown("看清楚**最壞情況**再決定比例：用 2010 年以來的真實價格（含配息再投入、已扣交易成本），"
            "測試你的配置在歷次大跌中會怎樣。")
st.caption("在左側設定你的 ETF 比例（手機請點左上角「»」打開設定）。")

if total == 0:
    st.info("目前全部放現金：資產不會變動。請在左側輸入至少一檔 ETF 的比例。")
    st.stop()

port, ref, ev, ev_w = run(tuple(weights.items()), FREQ[freq_label], start, latest)
m, m_ref, m_ev = core.metrics(port), core.metrics(ref), core.metrics(ev)
mix_text = "＋".join(f"{c} {v:.0%}" for c, v in weights.items() if v > 0) + (f"＋現金 {1 - total:.0%}" if total < 1 else "")
st.caption(f"組合：{mix_text}｜{freq_label}調回比例｜期間 {port.index[1].date()} ~ {port.index[-1].date()}"
           + ("（受限於所選 ETF 的上市日）" if start > pd.Timestamp(start_year, 1, 31) else ""))

c1, c2, c3, c4 = st.columns(4)
c1.metric("最大跌幅", pct(m.mdd), help="從任一高點到之後最低點的最大跌幅")
c2.metric(f"投入 {amount:,.0f} 元最多少掉", f"{-m.mdd * amount:,.0f} 元")
c3.metric("最長沒回到前高", f"{m.longest_underwater_years:.1f} 年")
c4.metric("年化報酬", pct(m.cagr), help="這段歷史的平均年報酬，不代表未來")
if (port.index[-1] - port.index[1]).days < 365 * 5:
    st.warning("這段期間不到 5 年，只經歷過很少次大跌，結果的參考價值有限。")

# 資產曲線
only_0050 = [c for c, v in weights.items() if v > 0] == ["0050"]
ev_name = f"同波動的 0050 {ev_w:.0%}＋現金"
series = [("你的組合", port, ORANGE), ("100% 0050", ref, BLUE)] + ([] if only_0050 else [(ev_name, ev, AQUA)])
lines = pd.concat([pd.DataFrame({"日期": s.index, "資產": s.values * amount / 10_000, "組合": n}) for n, s, _ in series])
color = alt.Scale(domain=[n for n, _, _ in series], range=[c for _, _, c in series])
base = alt.Chart(lines).encode(
    x=alt.X("日期:T", title=None, axis=alt.Axis(format="%Y", tickCount="year", labelAngle=0)),
    color=alt.Color("組合:N", scale=color, legend=alt.Legend(orient="top", title=None)),
    tooltip=[alt.Tooltip("日期:T", format="%Y-%m-%d"), "組合:N", alt.Tooltip("資產:Q", format=",.1f", title="資產（萬元）")],
)
st.subheader(f"投入 {amount:,.0f} 元後的資產變化")
st.altair_chart(base.mark_line(strokeWidth=2).encode(y=alt.Y("資產:Q", title="萬元", axis=alt.Axis(format=",.0f"))),
                width="stretch")

# 回撤
dd = pd.concat([
    pd.DataFrame({"日期": port.index, "跌幅": (port / port.cummax() - 1).values, "組合": "你的組合"}),
    pd.DataFrame({"日期": ref.index, "跌幅": (ref / ref.cummax() - 1).values, "組合": "100% 0050"}),
])
st.subheader("距離前一次高點跌了多少")
st.altair_chart(alt.Chart(dd).mark_line(strokeWidth=1.6).encode(
    x=alt.X("日期:T", title=None, axis=alt.Axis(format="%Y", tickCount="year", labelAngle=0)), y=alt.Y("跌幅:Q", title=None, axis=alt.Axis(format="%")),
    color=alt.Color("組合:N", scale=alt.Scale(domain=["你的組合", "100% 0050"], range=[ORANGE, BLUE]),
                    legend=alt.Legend(orient="top", title=None)),
    tooltip=[alt.Tooltip("日期:T", format="%Y-%m-%d"), "組合:N", alt.Tooltip("跌幅:Q", format=".1%")],
), width="stretch")

# 同波動對照
st.subheader("跟「同樣波動的 0050＋現金」比")
st.markdown(f"把 0050 放 **{ev_w:.0%}**、其餘放現金，波動會跟你的組合一樣。如果你的組合沒有比它好，"
            "代表你得到的「穩」主要來自「股票放少一點」，而不是選到更好的 ETF。")
cmp = pd.DataFrame({
    "": ["你的組合", f"0050 {ev_w:.0%}＋現金", "100% 0050"],
    "年化報酬": [pct(x.cagr) for x in (m, m_ev, m_ref)],
    "最大跌幅": [pct(x.mdd) for x in (m, m_ev, m_ref)],
    "Sharpe": [f"{x.sharpe:.2f}" for x in (m, m_ev, m_ref)],
})
if not only_0050:
    st.dataframe(cmp, hide_index=True, width="stretch")
better_ret, better_dd = m.cagr > m_ev.cagr, m.mdd > m_ev.mdd
if only_0050:
    st.info("你的組合本身就是「0050＋現金」，不需要這個比較。試試加入 0056、00878 或 00679B，"
            "看看換成其他 ETF 是否真的比「0050 少放一點」好。")
elif better_ret and better_dd:
    st.success("這段歷史中，你的組合報酬較高、跌幅也較淺——比單純「少放一點 0050」好。")
elif not better_ret and not better_dd:
    st.info("這段歷史中，同波動的 0050＋現金報酬較高、跌幅也較淺——你的組合沒有帶來額外好處。")
else:
    st.info("兩者各有勝負（一邊報酬較高、另一邊跌幅較淺），差異不明顯。")

# 歷次大跌
st.subheader("歷次大跌（0050 從前高跌超過 15% 的每一段）")
ep = core.episode_table(port, ref)
if len(ep):
    show = ep.copy()
    for col in ("你的組合 跌幅", "100% 0050 跌幅"):
        show[col] = show[col].map(lambda x: f"{x:+.1%}")
    st.dataframe(show, hide_index=True, width="stretch")
else:
    st.write("這段期間沒有 0050 跌超過 15% 的段落。")

# 持有期間
st.subheader("如果只持有一段時間：虧錢的機率")
st.caption("每個交易日都當作一次買進日，看持有 N 後的結果。相鄰起點高度重疊，機率是歷史頻率，不是保證。")
ht = core.holding_table(port)
if len(ht):
    show = ht.copy()
    show["虧錢機率"] = show["虧錢機率"].map(lambda x: f"{x:.0%}")
    for col in ("最慘", "中位數"):
        show[col] = show[col].map(lambda x: f"{x:+.1%}")
    st.dataframe(show[["持有", "虧錢機率", "最慘", "中位數"]], hide_index=True, width="stretch")

with st.expander("方法、資料來源與限制"):
    st.markdown(f"""
- **價格**：還原價（配息視為再投入、分割已調整），由公開的每日成交資料推算，並與 TEJ 資料交叉驗證。
- **成本**：手續費 0.05%／邊、ETF 證交稅 0.1%（賣出）、滑價 1 檔／邊；現金利息以 0 計（保守）。
- **調回比例**：{freq_label}第一個交易日開盤調回原比例。
- **限制**：只有 2010 年以來一段台股歷史，期間整體偏多頭；2008 年金融海嘯等級的下跌不在資料內。
  00878、00679B 的歷史較短。過去不代表未來。
- **背後的研究**：這個工具來自《台股 ETF 投資迷思實證》研究報告，所有比較方法都事先鎖定、只執行一次。
""")

st.divider()
st.caption(f"資料來源：臺灣證券交易所、證券櫃檯買賣中心（政府資料開放授權條款）。資料截至 {latest.date()}。"
           "本工具為歷史回測，**不構成任何投資建議**。")
