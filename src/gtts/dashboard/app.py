"""Local read-only research dashboard."""

import os
from datetime import date
from typing import Any

import httpx
import pandas as pd  # type: ignore[import-untyped]
import streamlit as st

API = os.environ.get("GTTS_API_URL", "http://127.0.0.1:8000")
st.title("GTTS 研究儀表板")
st.warning("研究結果未證實；樣本不足時不可推論績效。")
try:
    run_ids = httpx.get(f"{API}/runs", timeout=5).raise_for_status().json()
    if not run_ids:
        st.info("沒有 run；請先執行離線 demo。")
        st.stop()
    run_id = st.sidebar.selectbox("run_id", run_ids)
    result = httpx.get(f"{API}/runs/{run_id}/results", timeout=5).raise_for_status().json()
    trades = httpx.get(f"{API}/runs/{run_id}/trades", timeout=5).raise_for_status().json()
    features = httpx.get(f"{API}/runs/{run_id}/features", timeout=5).raise_for_status().json()
except (httpx.HTTPError, ValueError) as exc:
    st.error(f"無法讀取研究資料：{exc}")
    st.stop()
report = result["report"]
st.write("研究狀態：", report.get("claim_status", "未證實"))
st.warning("外部交易 ledger 尚未逐筆對帳；請確認報告限制。")
for limitation in report.get("limitations", []):
    st.warning(limitation)
data_gaps = result.get("data_gaps", [])
if any("合成" in gap for gap in data_gaps):
    st.info("此 run 使用合成資料，僅供離線介面示範。")
comparison = report.get("comparison")
interval = comparison.get("interval") if isinstance(comparison, dict) else None
if isinstance(interval, dict):
    st.write(
        "OOS 樣本狀態：",
        interval.get("status", "未提供"),
        "；配對日觀測：",
        interval.get("observations", "未提供"),
        "；已核對交易：",
        comparison.get("completed_trades", "未提供"),
    )
else:
    st.write("OOS 樣本狀態：未執行比較。")

date_values = [
    str(row[field])[:10]
    for rows, field in (
        (result.get("daily_equity", []), "date"),
        (result.get("baseline_daily_equity", []), "date"),
        (trades.get("completed_trades") or [], "trade_date"),
        (trades.get("fills", []), "trade_date"),
        (features.get("features", []), "available_at"),
    )
    for row in rows
    if row.get(field)
]
available_dates: list[date] = []
for value in date_values:
    try:
        available_dates.append(date.fromisoformat(value))
    except ValueError:
        continue
minimum_date = min(available_dates, default=date(1970, 1, 1))
maximum_date = max(available_dates, default=date(2100, 12, 31))
start = st.sidebar.date_input(
    "起始日期", minimum_date, min_value=minimum_date, max_value=maximum_date
)
end = st.sidebar.date_input(
    "結束日期", maximum_date, min_value=minimum_date, max_value=maximum_date
)
if start > end:
    st.error("起始日期不可晚於結束日期。")
    st.stop()


def filter_rows(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    return [row for row in rows if not row.get(field) or str(start) <= str(row[field])[:10] <= str(end)]


st.subheader("OOS 淨值與回撤")
if result.get("evaluation_period") != "oos" or report.get("evaluation_period") != "oos":
    st.warning("run 與報告必須一致標記為 OOS，才繪製曲線。")
else:
    series = [
        (
            "strategy",
            f"{report.get('name', '策略')}（策略）",
            "daily_equity",
            "initial_equity",
        ),
        (
            "baseline",
            f"{result.get('baseline_name', 'baseline')}（基準）",
            "baseline_daily_equity",
            "baseline_initial_equity",
        ),
    ]
    labels = {role: label for role, label, _, _ in series}
    selected_series = st.sidebar.multiselect(
        "策略／基準",
        [role for role, _, _, _ in series],
        default=[role for role, _, _, _ in series],
        format_func=labels.__getitem__,
    )
    for role, label, key, initial_key in series:
        if role not in selected_series:
            continue
        rows = result.get(key, [])
        if not rows:
            st.info(f"{label}：缺少日終權益。")
            continue
        frame = pd.DataFrame(rows)
        frame["equity"] = pd.to_numeric(frame["equity"], errors="coerce")
        initial = pd.to_numeric(result.get(initial_key), errors="coerce")
        peak = float(initial) if pd.notna(initial) else float(frame["equity"].iloc[0])
        drawdowns: list[float | None] = []
        for equity in frame["equity"]:
            if pd.isna(equity):
                drawdowns.append(None)
                continue
            peak = max(peak, float(equity))
            drawdowns.append(float(equity) / peak - 1 if peak > 0 else None)
        frame["drawdown"] = drawdowns
        frame = frame[(frame["date"] >= str(start)) & (frame["date"] <= str(end))]
        st.write(label)
        st.line_chart(frame.set_index("date")[["equity"]])
        st.line_chart(frame.set_index("date")[["drawdown"]])
st.subheader("交易與成本拆解")
metrics = report.get("metrics", {})
st.write("全 run 指標（不受日期篩選）", metrics)
cost_columns = st.columns(4)
for column, (key, label) in zip(
    cost_columns,
    (("broker_fee", "券商費用"), ("tax", "交易稅"), ("slippage", "滑價"), ("total_cost", "總成本")),
    strict=True,
):
    column.metric(label, metrics.get(key, "缺少資料"))
rows = trades.get("completed_trades")
if rows:
    st.dataframe(filter_rows(rows, "trade_date"))
else:
    st.info("缺少完成交易 ledger。")
if trades.get("fills"):
    st.write("成交與費用明細")
    st.dataframe(filter_rows(trades["fills"], "trade_date"))
st.subheader("訊號信心、決策依據與來源時間")
if features.get("features"):
    st.dataframe(filter_rows(features["features"], "available_at"))
else:
    st.info("缺少 feature／來源時間資料。")
st.subheader("換月與資料缺口")
for key, label in [("rolls", "換月"), ("data_gaps", "資料缺口")]:
    st.write(label, result.get(key) or "未提供")
