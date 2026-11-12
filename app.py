"""
Equity Research & Earnings Dashboard — Streamlit App.

Interactive dashboard with NSE index/stock dropdowns, live quote KPIs,
interactive price charts, compiled quarterly earnings & filings, compiled news
articles (Google News + NSE Press Releases), corporate announcements, corporate
actions, shareholding pattern, and one-click downloads for official NSE
"Download as CSV" files + compiled datasets.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import importlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

import collectors as _collectors_mod
if not hasattr(_collectors_mod, "NSE_CSV_CATEGORIES"):
    importlib.reload(_collectors_mod)

from collectors import (
    INDEX_CSV_URLS,
    NSE_CSV_CATEGORIES,
    batch_extract_nse_csvs,
    collect_bse_announcements,
    collect_corporate_actions,
    collect_earnings_summary,
    collect_event_calendar,
    collect_financial_results,
    collect_google_news,
    collect_integrated_filings,
    collect_nse_announcements,
    collect_price_history,
    collect_quote_snapshot,
    collect_shareholding,
    compile_news_feed,
    download_nse_official_csvs,
    fetch_index_constituents,
    list_saved_runs,
    list_saved_symbols,
    load_saved_run,
    save_run,
)

st.set_page_config(
    page_title="NSE Equity & Earnings Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Subtle styling for metric cards & clean header
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    div[data-testid="stMetric"] {
        background-color: rgba(128, 128, 128, 0.07);
        border: 1px solid rgba(128, 128, 128, 0.16);
        border-radius: 10px;
        padding: 0.75rem 0.9rem;
    }
    .stock-banner {
        padding: 0.9rem 1.15rem;
        border-radius: 12px;
        background: linear-gradient(135deg, rgba(37, 99, 235, 0.12), rgba(16, 185, 129, 0.08));
        border: 1px solid rgba(37, 99, 235, 0.22);
        margin-bottom: 1rem;
    }
    .stock-badge {
        display: inline-block;
        padding: 0.15rem 0.55rem;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
        background: rgba(37, 99, 235, 0.15);
        margin-right: 0.4rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Cached universe loader & live collector
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def get_cached_constituents(index_name: str) -> tuple[pd.DataFrame, str | None]:
    return fetch_index_constituents(index_name)


def run_live_collection(
    symbol: str,
    company_name: str,
    history_days: int,
    download_official_csv: bool,
    include_bse: bool,
) -> dict[str, Any]:
    """Collect all datasets for a symbol, compile news & earnings, and save to disk."""
    errors: dict[str, str] = {}
    results: dict[str, pd.DataFrame] = {}

    progress = st.progress(0.0, text=f"Fetching live quote for {symbol}…")
    quote_snapshot, q_err = collect_quote_snapshot(symbol)
    if q_err:
        errors["quote_snapshot"] = q_err
    resolved_name = quote_snapshot.get("company_name") or company_name or symbol

    steps: list[tuple[str, Any]] = [
        ("earnings_summary", lambda: collect_earnings_summary(symbol)),
        ("integrated_filings", lambda: collect_integrated_filings(symbol)),
        ("financial_results", lambda: collect_financial_results(symbol)),
        ("nse_announcements", lambda: collect_nse_announcements(symbol)),
        ("corporate_actions", lambda: collect_corporate_actions(symbol)),
        ("price_history", lambda: collect_price_history(symbol, history_days)),
        ("shareholding", lambda: collect_shareholding(symbol)),
        ("event_calendar", lambda: collect_event_calendar(symbol)),
        ("google_news", lambda: collect_google_news(resolved_name, symbol=symbol)),
    ]
    if include_bse:
        steps.append(("bse_announcements", lambda: collect_bse_announcements(symbol)))

    total_steps = len(steps) + (1 if download_official_csv else 0) + 1
    for idx, (name, fn) in enumerate(steps, start=1):
        progress.progress(idx / total_steps, text=f"Collecting {name.replace('_', ' ')}…")
        df, err = fn()
        results[name] = df
        if err:
            errors[name] = err

    compiled_news = compile_news_feed(results)
    if not compiled_news.empty:
        results["compiled_news"] = compiled_news

    official_csvs: dict[str, bytes] = {}
    if download_official_csv:
        progress.progress((len(steps) + 1) / total_steps, text="Downloading official NSE CSVs…")
        official_csvs, csv_errs = download_nse_official_csvs(symbol, days=history_days)
        for fname, err in csv_errs.items():
            errors[f"csv:{fname}"] = err

    run_dir: Path | None = None
    if any(df is not None and not df.empty for df in results.values()) or official_csvs:
        run_dir = save_run(
            symbol,
            results,
            official_csvs=official_csvs,
            quote_snapshot=quote_snapshot,
        )

    progress.progress(1.0, text="Ready!")
    progress.empty()

    return {
        "symbol": symbol,
        "company_name": resolved_name,
        "quote_snapshot": quote_snapshot,
        "results": results,
        "official_csvs": official_csvs,
        "errors": errors,
        "run_dir": str(run_dir) if run_dir else "",
        "mode": "Live",
    }


def build_zip_bundle(
    symbol: str,
    results: dict[str, pd.DataFrame],
    official_csvs: dict[str, bytes],
    quote_snapshot: dict[str, Any],
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        combined: dict[str, Any] = {}
        for name, df in results.items():
            if df is None or df.empty:
                continue
            zf.writestr(f"compiled/{symbol}_{name}.csv", df.to_csv(index=False))
            records = json.loads(df.to_json(orient="records"))
            zf.writestr(f"compiled/{symbol}_{name}.json", json.dumps(records, indent=2))
            combined[name] = records
        if quote_snapshot:
            zf.writestr(f"compiled/{symbol}_quote_snapshot.json", json.dumps(quote_snapshot, indent=2, default=str))
            combined["quote_snapshot"] = quote_snapshot
        zf.writestr(f"compiled/{symbol}_combined.json", json.dumps(combined, indent=2, default=str))
        for fname, raw_bytes in official_csvs.items():
            zf.writestr(f"nse_official_csv/{symbol}_{fname}", raw_bytes)
    buf.seek(0)
    return buf.read()


def fmt_inr(val: Any, decimals: int = 2) -> str:
    if val is None or pd.isna(val):
        return "—"
    return f"₹{float(val):,.{decimals}f}"


def fmt_cr(val: Any) -> str:
    if val is None or pd.isna(val):
        return "—"
    return f"₹{float(val):,.0f} Cr"


# ---------------------------------------------------------------------------
# Sidebar — Stock Dropdowns & Controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("📊 Equity Dashboard")
    st.caption("Select an index & stock to compile live NSE data, earnings, news & official CSVs.")

    saved_symbols = list_saved_symbols()
    universe_options = list(INDEX_CSV_URLS.keys()) + (["SAVED LOCAL STOCKS"] if saved_symbols else [])
    index_choice = st.selectbox("1. Index / Stock Universe", universe_options, index=0)

    if index_choice == "SAVED LOCAL STOCKS":
        universe_df = pd.DataFrame([
            {"symbol": s, "company_name": s, "industry": "Saved Local Data", "isin": ""}
            for s in saved_symbols
        ])
        idx_err = None
    else:
        universe_df, idx_err = get_cached_constituents(index_choice)

    if idx_err:
        st.caption(f"⚠️ Index fallback active ({idx_err})")

    # Build formatted labels for the stock dropdown
    symbol_list = universe_df["symbol"].tolist()
    label_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    industry_map: dict[str, str] = {}
    for _, row in universe_df.iterrows():
        sym = str(row["symbol"])
        cname = str(row.get("company_name") or sym)
        ind = str(row.get("industry") or "").strip()
        label_map[sym] = f"{sym} — {cname}" + (f" ({ind})" if ind and ind != sym else "")
        name_map[sym] = cname
        industry_map[sym] = ind

    default_idx = symbol_list.index("RELIANCE") if "RELIANCE" in symbol_list else 0
    selected_from_dropdown = st.selectbox(
        "2. Select Stock",
        options=symbol_list,
        index=default_idx if symbol_list else None,
        format_func=lambda s: label_map.get(s, s),
    )

    custom_symbol = st.text_input(
        "Or type any NSE symbol (optional)",
        placeholder="e.g. ZOMATO, TATAMOTORS, BAJFINANCE",
    ).strip().upper()

    active_symbol = custom_symbol or (selected_from_dropdown or "RELIANCE")
    active_company_name = name_map.get(active_symbol, active_symbol) if not custom_symbol else active_symbol

    st.divider()
    st.subheader("Data Source & Options")

    saved_runs = list_saved_runs(active_symbol)
    source_options = ["Live NSE + News (Fetch & Save)"] + [
        f"Saved Run: {p.name}" for p in saved_runs
    ]
    data_mode = st.selectbox("Source Mode", source_options, index=0)

    is_live_mode = data_mode.startswith("Live")
    history_days = st.select_slider(
        "Price History Window (days)",
        options=[90, 180, 365, 540, 730],
        value=365,
        disabled=not is_live_mode,
    )
    download_official_csv = st.checkbox(
        "Download official NSE CSVs (&csv=true)",
        value=True,
        disabled=not is_live_mode,
    )
    include_bse = st.checkbox(
        "Also try BSE announcements",
        value=False,
        disabled=not is_live_mode,
        help="BSE API may block automated requests with HTTP 403 depending on network.",
    )
    auto_fetch = st.checkbox(
        "Auto-fetch when switching stock",
        value=True,
        disabled=not is_live_mode,
    )

    refresh_clicked = st.button(
        "🔄 Fetch / Refresh Live Data" if is_live_mode else "📂 Load Saved Run",
        type="primary",
        width="stretch",
    )


# ---------------------------------------------------------------------------
# Load or Fetch Active Bundle
# ---------------------------------------------------------------------------

if "dashboard_cache" not in st.session_state:
    st.session_state["dashboard_cache"] = {}

cache: dict[str, dict[str, Any]] = st.session_state["dashboard_cache"]

if is_live_mode:
    cache_key = f"LIVE::{active_symbol}::{history_days}::{download_official_csv}::{include_bse}"
    if refresh_clicked or (auto_fetch and cache_key not in cache):
        bundle = run_live_collection(
            symbol=active_symbol,
            company_name=active_company_name,
            history_days=history_days,
            download_official_csv=download_official_csv,
            include_bse=include_bse,
        )
        cache[cache_key] = bundle
    elif cache_key not in cache:
        # Fallback to latest saved run if auto_fetch is unchecked
        if saved_runs:
            r_dir = saved_runs[0]
            res, ocsv, qsnap = load_saved_run(r_dir)
            if "compiled_news" not in res:
                cn = compile_news_feed(res)
                if not cn.empty:
                    res["compiled_news"] = cn
            cache[cache_key] = {
                "symbol": active_symbol,
                "company_name": qsnap.get("company_name") or active_company_name,
                "quote_snapshot": qsnap,
                "results": res,
                "official_csvs": ocsv,
                "errors": {},
                "run_dir": str(r_dir),
                "mode": f"Saved ({r_dir.name})",
            }
        else:
            bundle = run_live_collection(
                symbol=active_symbol,
                company_name=active_company_name,
                history_days=history_days,
                download_official_csv=download_official_csv,
                include_bse=include_bse,
            )
            cache[cache_key] = bundle
    active_bundle = cache[cache_key]
else:
    run_stamp = data_mode.replace("Saved Run: ", "").strip()
    cache_key = f"SAVED::{active_symbol}::{run_stamp}"
    if refresh_clicked or cache_key not in cache:
        matching = [p for p in saved_runs if p.name == run_stamp]
        if matching:
            r_dir = matching[0]
            res, ocsv, qsnap = load_saved_run(r_dir)
            if "compiled_news" not in res:
                cn = compile_news_feed(res)
                if not cn.empty:
                    res["compiled_news"] = cn
            cache[cache_key] = {
                "symbol": active_symbol,
                "company_name": qsnap.get("company_name") or active_company_name,
                "quote_snapshot": qsnap,
                "results": res,
                "official_csvs": ocsv,
                "errors": {},
                "run_dir": str(r_dir),
                "mode": f"Saved ({run_stamp})",
            }
    active_bundle = cache.get(cache_key, {
        "symbol": active_symbol,
        "company_name": active_company_name,
        "quote_snapshot": {},
        "results": {},
        "official_csvs": {},
        "errors": {},
        "run_dir": "",
        "mode": "Empty",
    })


# ---------------------------------------------------------------------------
# Extract Active Data
# ---------------------------------------------------------------------------

sym: str = active_bundle["symbol"]
q: dict[str, Any] = active_bundle.get("quote_snapshot") or {}
results: dict[str, pd.DataFrame] = active_bundle.get("results") or {}
official_csvs: dict[str, bytes] = active_bundle.get("official_csvs") or {}
errors: dict[str, str] = active_bundle.get("errors") or {}
run_dir_str: str = active_bundle.get("run_dir") or ""

display_name = q.get("company_name") or active_bundle.get("company_name") or sym
sector_label = q.get("sector") or industry_map.get(sym, "")
industry_label = q.get("basic_industry") or q.get("industry") or ""
isin_label = q.get("isin") or ""

earnings_df = results.get("earnings_summary", pd.DataFrame())
integrated_df = results.get("integrated_filings", pd.DataFrame())
fin_results_df = results.get("financial_results", pd.DataFrame())
nse_ann_df = results.get("nse_announcements", pd.DataFrame())
bse_ann_df = results.get("bse_announcements", pd.DataFrame())
actions_df = results.get("corporate_actions", pd.DataFrame())
price_df = results.get("price_history", pd.DataFrame())
shareholding_df = results.get("shareholding", pd.DataFrame())
events_df = results.get("event_calendar", pd.DataFrame())
gnews_df = results.get("google_news", pd.DataFrame())
compiled_news_df = results.get("compiled_news", pd.DataFrame())


# ---------------------------------------------------------------------------
# Header Banner & Top KPI Strip
# ---------------------------------------------------------------------------

badges_html = f"<span class='stock-badge'>NSE: {sym}</span>"
if isin_label:
    badges_html += f"<span class='stock-badge'>ISIN: {isin_label}</span>"
if sector_label:
    badges_html += f"<span class='stock-badge'>{sector_label}</span>"
if industry_label and industry_label != sector_label:
    badges_html += f"<span class='stock-badge'>{industry_label}</span>"

meta_line = f"Mode: **{active_bundle.get('mode', 'Live')}**"
if q.get("last_update_time"):
    meta_line += f" · Market Update: {q['last_update_time']}"
if run_dir_str:
    meta_line += f" · Saved to `{Path(run_dir_str).relative_to(Path(__file__).parent) if Path(run_dir_str).is_relative_to(Path(__file__).parent) else run_dir_str}`"

st.markdown(
    f"""
    <div class="stock-banner">
        <div style="font-size: 1.55rem; font-weight: 700; margin-bottom: 0.25rem;">
            {display_name}
        </div>
        <div>{badges_html}</div>
    </div>
    """,
    unsafe_allow_html=True,
)
st.caption(meta_line)

if errors:
    with st.expander(f"⚠️ {len(errors)} source notice(s)", expanded=False):
        for k, msg in errors.items():
            st.warning(f"**{k}**: `{msg}`")

# KPI metrics row
k1, k2, k3, k4, k5, k6 = st.columns(6)

last_px = q.get("last_price")
chg = q.get("change")
pchg = q.get("p_change")
delta_px = f"{chg:+.2f} ({pchg:+.2f}%)" if (chg is not None and pchg is not None) else None
k1.metric("Last Price", fmt_inr(last_px), delta=delta_px)

w52_h = q.get("week_52_high")
w52_l = q.get("week_52_low")
if last_px and w52_h and w52_l and w52_h > w52_l:
    pos_pct = ((last_px - w52_l) / (w52_h - w52_l)) * 100
    k2.metric("52W Range", f"{fmt_inr(w52_l, 0)} – {fmt_inr(w52_h, 0)}", delta=f"{pos_pct:.0f}% of 52W range", delta_color="off")
else:
    k2.metric("52W High / Low", f"{fmt_inr(w52_h, 0)} / {fmt_inr(w52_l, 0)}")

k3.metric("Market Cap", fmt_cr(q.get("market_cap_cr")))

sym_pe = q.get("symbol_pe")
sec_pe = q.get("sector_pe")
k4.metric(
    "P/E Ratio",
    f"{sym_pe:.2f}" if sym_pe is not None else "—",
    delta=f"Sector P/E: {sec_pe:.2f}" if sec_pe is not None else None,
    delta_color="off",
)

if not earnings_df.empty:
    latest_q = earnings_df.iloc[0]
    q_label = latest_q.get("quarter") or "Latest Qtr"
    q_inc = latest_q.get("total_income_cr")
    q_pat = latest_q.get("net_profit_cr")
    q_margin = latest_q.get("pat_margin_pct")
    q_eps = latest_q.get("eps")
    k5.metric(
        f"Revenue ({q_label})",
        fmt_cr(q_inc),
        delta=f"PAT: {fmt_cr(q_pat)} ({q_margin:.1f}%)" if q_pat is not None and q_margin is not None else None,
        delta_color="off",
    )
    deliv = q.get("delivery_pct")
    k6.metric(
        f"EPS ({q_label})",
        fmt_inr(q_eps),
        delta=f"Delivery: {deliv:.1f}%" if deliv is not None else None,
        delta_color="off",
    )
else:
    k5.metric("Latest Qtr Revenue", "—")
    deliv = q.get("delivery_pct")
    k6.metric("Delivery %", f"{deliv:.1f}%" if deliv is not None else "—")


# ---------------------------------------------------------------------------
# Main Dashboard Tabs
# ---------------------------------------------------------------------------

tab_overview, tab_earnings, tab_news, tab_announcements, tab_actions, tab_downloads = st.tabs([
    "📈 Overview & Price",
    "💰 Earnings & Financials",
    f"📰 News Articles ({len(compiled_news_df)})",
    f"📢 Announcements ({len(nse_ann_df)})",
    "🏛️ Actions & Shareholding",
    f"📥 NSE CSVs & Downloads ({len(official_csvs)})",
])


# ===========================================================================
# TAB 1: OVERVIEW & PRICE
# ===========================================================================
with tab_overview:
    if not price_df.empty:
        p_df = price_df.copy()
        p_df["dt"] = pd.to_datetime(p_df.get("date") if "date" in p_df.columns else p_df.get("mtimestamp"), errors="coerce")
        p_df["close"] = pd.to_numeric(p_df.get("chClosingPrice", p_df.get("close")), errors="coerce")
        p_df["open"] = pd.to_numeric(p_df.get("chOpeningPrice", p_df.get("open")), errors="coerce")
        p_df["high"] = pd.to_numeric(p_df.get("chTradeHighPrice", p_df.get("high")), errors="coerce")
        p_df["low"] = pd.to_numeric(p_df.get("chTradeLowPrice", p_df.get("low")), errors="coerce")
        p_df["vwap"] = pd.to_numeric(p_df.get("vwap"), errors="coerce")
        p_df["volume"] = pd.to_numeric(p_df.get("chTotTradedQty", p_df.get("volume")), errors="coerce")
        p_df = p_df.dropna(subset=["dt", "close"]).sort_values("dt").reset_index(drop=True)

        p_df["SMA_20"] = p_df["close"].rolling(20).mean()
        p_df["SMA_50"] = p_df["close"].rolling(50).mean()

        ctrl1, ctrl2, ctrl3 = st.columns([2, 2, 3])
        range_choice = ctrl1.radio(
            "Chart Range",
            ["1M", "3M", "6M", "1Y", "ALL"],
            index=3,
            horizontal=True,
        )
        show_sma = ctrl2.checkbox("Overlay 20D & 50D SMA", value=True)

        max_dt = p_df["dt"].max()
        days_back = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365}.get(range_choice)
        plot_df = p_df[p_df["dt"] >= (max_dt - pd.Timedelta(days=days_back))] if days_back else p_df

        if not plot_df.empty:
            start_px = float(plot_df["close"].iloc[0])
            end_px = float(plot_df["close"].iloc[-1])
            ret_pct = ((end_px - start_px) / start_px) * 100 if start_px else 0.0
            ctrl3.metric(f"{range_choice} Period Return", f"{ret_pct:+.2f}%", delta=f"{fmt_inr(start_px)} → {fmt_inr(end_px)}")

            y_min = float(plot_df["low"].min() if plot_df["low"].notna().any() else plot_df["close"].min()) * 0.985
            y_max = float(plot_df["high"].max() if plot_df["high"].notna().any() else plot_df["close"].max()) * 1.015

            base = alt.Chart(plot_df).encode(
                x=alt.X("dt:T", title=None, axis=alt.Axis(format="%d %b %y"))
            )
            price_area = base.mark_area(
                line={"color": "#2563eb", "width": 2},
                color=alt.Gradient(
                    gradient="linear",
                    stops=[
                        alt.GradientStop(color="rgba(37, 99, 235, 0.30)", offset=0),
                        alt.GradientStop(color="rgba(37, 99, 235, 0.02)", offset=1),
                    ],
                    x1=1, x2=1, y1=1, y2=0,
                ),
            ).encode(
                y=alt.Y("close:Q", title="Close Price (₹)", scale=alt.Scale(domain=[y_min, y_max])),
                tooltip=[
                    alt.Tooltip("dt:T", title="Date", format="%d %b %Y"),
                    alt.Tooltip("open:Q", title="Open", format=",.2f"),
                    alt.Tooltip("high:Q", title="High", format=",.2f"),
                    alt.Tooltip("low:Q", title="Low", format=",.2f"),
                    alt.Tooltip("close:Q", title="Close", format=",.2f"),
                    alt.Tooltip("vwap:Q", title="VWAP", format=",.2f"),
                    alt.Tooltip("volume:Q", title="Volume", format=","),
                ],
            )
            layers = [price_area]
            if show_sma:
                if plot_df["SMA_20"].notna().any():
                    layers.append(
                        base.mark_line(color="#f59e0b", strokeDash=[4, 3], strokeWidth=1.6).encode(
                            y="SMA_20:Q"
                        )
                    )
                if plot_df["SMA_50"].notna().any():
                    layers.append(
                        base.mark_line(color="#10b981", strokeDash=[2, 2], strokeWidth=1.6).encode(
                            y="SMA_50:Q"
                        )
                    )

            price_chart = alt.layer(*layers).properties(height=320).interactive()
            vol_chart = base.mark_bar(color="#64748b", opacity=0.55).encode(
                y=alt.Y("volume:Q", title="Volume"),
                tooltip=[
                    alt.Tooltip("dt:T", title="Date", format="%d %b %Y"),
                    alt.Tooltip("volume:Q", title="Volume", format=","),
                ],
            ).properties(height=110)

            st.altair_chart(alt.vconcat(price_chart, vol_chart).resolve_scale(x="shared"), width="stretch")
    else:
        st.info("No historical price data loaded yet for this symbol.")

    st.divider()
    col_l, col_r = st.columns(2)

    with col_l:
        st.subheader("🔥 Latest Headlines")
        if not compiled_news_df.empty:
            top_news = compiled_news_df.head(6)[["published_at", "title", "publisher", "link"]]
            st.dataframe(
                top_news,
                width="stretch",
                hide_index=True,
                column_config={
                    "published_at": st.column_config.TextColumn("Published", width="small"),
                    "title": st.column_config.TextColumn("Headline", width="large"),
                    "publisher": st.column_config.TextColumn("Source", width="small"),
                    "link": st.column_config.LinkColumn("Open", display_text="Read ↗", width="small"),
                },
            )
        else:
            st.caption("No news headlines collected.")

    with col_r:
        st.subheader("📄 Latest Earnings & Material Filings")
        if not nse_ann_df.empty:
            cols_want = [c for c in ["an_dt", "desc", "attchmntText", "attchmntFile"] if c in nse_ann_df.columns]
            top_ann = nse_ann_df.head(6)[cols_want]
            st.dataframe(
                top_ann,
                width="stretch",
                hide_index=True,
                column_config={
                    "an_dt": st.column_config.TextColumn("Broadcast", width="small"),
                    "desc": st.column_config.TextColumn("Category", width="medium"),
                    "attchmntText": st.column_config.TextColumn("Summary", width="large"),
                    "attchmntFile": st.column_config.LinkColumn("PDF", display_text="Open ↗", width="small"),
                },
            )
        else:
            st.caption("No NSE announcements collected.")


# ===========================================================================
# TAB 2: EARNINGS & FINANCIALS
# ===========================================================================
with tab_earnings:
    st.subheader("Quarterly Financial Summary")
    if not earnings_df.empty:
        disp_e = earnings_df.copy()
        chart_e = disp_e.copy()
        chart_e["_dt"] = pd.to_datetime(chart_e["period_end"], format="%d %b %Y", errors="coerce")
        chart_e = chart_e.sort_values("_dt")

        c_left, c_right = st.columns(2)
        with c_left:
            melted = chart_e.melt(
                id_vars=["quarter", "_dt"],
                value_vars=["total_income_cr", "net_profit_cr"],
                var_name="Metric",
                value_name="Amount_Cr",
            )
            melted["Metric"] = melted["Metric"].replace({
                "total_income_cr": "Total Income (₹ Cr)",
                "net_profit_cr": "Net Profit / PAT (₹ Cr)",
            })
            rev_pat_chart = (
                alt.Chart(melted)
                .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                .encode(
                    x=alt.X("quarter:N", sort=alt.SortField("_dt"), title="Quarter"),
                    y=alt.Y("Amount_Cr:Q", title="₹ Crores"),
                    xOffset="Metric:N",
                    color=alt.Color(
                        "Metric:N",
                        scale=alt.Scale(range=["#2563eb", "#10b981"]),
                        legend=alt.Legend(orient="top", title=None),
                    ),
                    tooltip=[
                        alt.Tooltip("quarter:N", title="Quarter"),
                        alt.Tooltip("Metric:N", title="Metric"),
                        alt.Tooltip("Amount_Cr:Q", title="₹ Cr", format=",.2f"),
                    ],
                )
                .properties(height=280, title="Quarterly Revenue vs Net Profit (₹ Cr)")
            )
            st.altair_chart(rev_pat_chart, width="stretch")

        with c_right:
            eps_bars = (
                alt.Chart(chart_e)
                .mark_bar(color="#6366f1", cornerRadiusTopLeft=4, cornerRadiusTopRight=4, opacity=0.85)
                .encode(
                    x=alt.X("quarter:N", sort=alt.SortField("_dt"), title="Quarter"),
                    y=alt.Y("eps:Q", title="Diluted / Basic EPS (₹)"),
                    tooltip=[
                        alt.Tooltip("quarter:N", title="Quarter"),
                        alt.Tooltip("eps:Q", title="EPS (₹)", format=",.2f"),
                        alt.Tooltip("pat_margin_pct:Q", title="PAT Margin %", format=",.2f"),
                    ],
                )
            )
            eps_text = eps_bars.mark_text(dy=-8, fontWeight="bold").encode(
                text=alt.Text("eps:Q", format=".2f")
            )
            st.altair_chart(
                (eps_bars + eps_text).properties(height=280, title="Quarterly EPS (₹)"),
                width="stretch",
            )

        show_cols = [
            "quarter",
            "period_end",
            "total_income_cr",
            "pbt_cr",
            "net_profit_cr",
            "pat_margin_pct",
            "eps",
            "qoq_income_pct",
            "qoq_pat_pct",
            "audited",
            "broadcast_timestamp",
        ]
        avail_cols = [c for c in show_cols if c in disp_e.columns]
        st.dataframe(
            disp_e[avail_cols],
            width="stretch",
            hide_index=True,
            column_config={
                "quarter": "Quarter",
                "period_end": "Period End",
                "total_income_cr": st.column_config.NumberColumn("Total Income (₹ Cr)", format="₹%,.2f"),
                "pbt_cr": st.column_config.NumberColumn("PBT (₹ Cr)", format="₹%,.2f"),
                "net_profit_cr": st.column_config.NumberColumn("Net Profit (₹ Cr)", format="₹%,.2f"),
                "pat_margin_pct": st.column_config.NumberColumn("PAT Margin %", format="%.2f%%"),
                "eps": st.column_config.NumberColumn("EPS (₹)", format="₹%.2f"),
                "qoq_income_pct": st.column_config.NumberColumn("QoQ Rev %", format="%+.2f%%"),
                "qoq_pat_pct": st.column_config.NumberColumn("QoQ PAT %", format="%+.2f%%"),
                "audited": "Audit Status",
                "broadcast_timestamp": "Filed At",
            },
        )
        d1, d2 = st.columns(2)
        d1.download_button(
            "⬇ Download Compiled Earnings CSV",
            disp_e.to_csv(index=False),
            file_name=f"{sym}_earnings_summary.csv",
            mime="text/csv",
            key="dl_earnings_csv",
        )
        d2.download_button(
            "⬇ Download Compiled Earnings JSON",
            disp_e.to_json(orient="records", indent=2),
            file_name=f"{sym}_earnings_summary.json",
            mime="application/json",
            key="dl_earnings_json",
        )
    else:
        st.info("No quarterly financial summary returned by NSE for this symbol.")

    st.divider()
    st.subheader("Official NSE Earnings Filings (Integrated Filings & XBRL Archive)")

    consol_filter = st.radio(
        "Filter filings by basis",
        ["All", "Consolidated", "Standalone / Non-Consolidated"],
        horizontal=True,
    )

    f_col1, f_col2 = st.columns(2)
    with f_col1:
        st.markdown("**Integrated Filing — Financials (Latest Format)**")
        if not integrated_df.empty:
            idf = integrated_df.copy()
            if consol_filter == "Consolidated" and "consolidated" in idf.columns:
                idf = idf[idf["consolidated"].astype(str).str.contains("Consolidated", case=False, na=False) & ~idf["consolidated"].astype(str).str.contains("Non-", case=False, na=False)]
            elif consol_filter.startswith("Standalone") and "consolidated" in idf.columns:
                idf = idf[idf["consolidated"].astype(str).str.contains("Standalone|Non-", case=False, na=False)]
            cols = [c for c in ["qe_Date", "consolidated", "audited", "type_Sub", "broadcast_Date", "ixbrl", "xbrl"] if c in idf.columns]
            st.dataframe(
                idf[cols],
                width="stretch",
                hide_index=True,
                column_config={
                    "qe_Date": "Quarter End",
                    "consolidated": "Basis",
                    "audited": "Audit",
                    "type_Sub": "Submission",
                    "broadcast_Date": "Broadcast",
                    "ixbrl": st.column_config.LinkColumn("iXBRL", display_text="View iXBRL ↗"),
                    "xbrl": st.column_config.LinkColumn("XBRL", display_text="XML ↗"),
                },
            )
        else:
            st.caption("No Integrated Filing financials available.")

    with f_col2:
        st.markdown("**Historical Financial Results Archive**")
        if not fin_results_df.empty:
            fdf = fin_results_df.copy()
            if consol_filter == "Consolidated" and "consolidated" in fdf.columns:
                fdf = fdf[fdf["consolidated"].astype(str).str.strip().str.lower() == "consolidated"]
            elif consol_filter.startswith("Standalone") and "consolidated" in fdf.columns:
                fdf = fdf[fdf["consolidated"].astype(str).str.contains("Non-Consolidated|Standalone", case=False, na=False)]
            cols = [c for c in ["toDate", "relatingTo", "consolidated", "audited", "filingDate", "xbrl"] if c in fdf.columns]
            st.dataframe(
                fdf[cols],
                width="stretch",
                hide_index=True,
                column_config={
                    "toDate": "Period End",
                    "relatingTo": "Quarter",
                    "consolidated": "Basis",
                    "audited": "Audit",
                    "filingDate": "Filing Date",
                    "xbrl": st.column_config.LinkColumn("XBRL", display_text="XML ↗"),
                },
            )
        else:
            st.caption("No historical financial results archive available.")

    st.divider()
    st.subheader("Earnings Releases, Investor Presentations & Call Transcripts")
    if not nse_ann_df.empty and "is_earnings_related" in nse_ann_df.columns:
        earn_ann = nse_ann_df[nse_ann_df["is_earnings_related"] == True]
        if not earn_ann.empty:
            cols = [c for c in ["an_dt", "desc", "attchmntText", "attFileSize", "attchmntFile"] if c in earn_ann.columns]
            st.dataframe(
                earn_ann[cols],
                width="stretch",
                hide_index=True,
                column_config={
                    "an_dt": "Broadcast Time",
                    "desc": "Category",
                    "attchmntText": st.column_config.TextColumn("Description", width="large"),
                    "attFileSize": "Size",
                    "attchmntFile": st.column_config.LinkColumn("Attachment", display_text="PDF ↗"),
                },
            )
        else:
            st.caption("No earnings-tagged announcements found.")
    else:
        st.caption("No NSE announcements available to filter for earnings updates.")


# ===========================================================================
# TAB 3: NEWS ARTICLES (COMPILED)
# ===========================================================================
with tab_news:
    st.subheader(f"Compiled News & Press Releases — {display_name}")
    if not compiled_news_df.empty:
        nf1, nf2, nf3 = st.columns([2, 2, 2])
        cat_opts = sorted(compiled_news_df["category"].dropna().unique().tolist())
        sel_cats = nf1.multiselect("Category", cat_opts, default=cat_opts)

        src_opts = sorted(compiled_news_df["source_type"].dropna().unique().tolist())
        sel_srcs = nf2.multiselect("Feed Source", src_opts, default=src_opts)

        news_query = nf3.text_input("Search headlines / summary", placeholder="e.g. Q2, dividend, margin, acquisition")

        filt_news = compiled_news_df[
            compiled_news_df["category"].isin(sel_cats) & compiled_news_df["source_type"].isin(sel_srcs)
        ]
        if news_query.strip():
            mask = (
                filt_news["title"].astype(str).str.contains(news_query, case=False, na=False)
                | filt_news["summary"].astype(str).str.contains(news_query, case=False, na=False)
                | filt_news["publisher"].astype(str).str.contains(news_query, case=False, na=False)
            )
            filt_news = filt_news[mask]

        st.caption(f"Showing **{len(filt_news)}** of {len(compiled_news_df)} compiled articles & official updates.")

        st.dataframe(
            filt_news[["published_at", "category", "publisher", "title", "summary", "link"]],
            width="stretch",
            hide_index=True,
            height=520,
            column_config={
                "published_at": st.column_config.TextColumn("Published", width="small"),
                "category": st.column_config.TextColumn("Category", width="medium"),
                "publisher": st.column_config.TextColumn("Publisher", width="small"),
                "title": st.column_config.TextColumn("Headline", width="large"),
                "summary": st.column_config.TextColumn("Summary", width="large"),
                "link": st.column_config.LinkColumn("Link", display_text="Read ↗", width="small"),
            },
        )

        nd1, nd2 = st.columns(2)
        nd1.download_button(
            "⬇ Download Compiled News CSV",
            filt_news.to_csv(index=False),
            file_name=f"{sym}_compiled_news.csv",
            mime="text/csv",
            key="dl_compiled_news_csv",
        )
        nd2.download_button(
            "⬇ Download Compiled News JSON",
            filt_news.to_json(orient="records", indent=2),
            file_name=f"{sym}_compiled_news.json",
            mime="application/json",
            key="dl_compiled_news_json",
        )
    else:
        st.info("No news articles compiled for this stock yet.")


# ===========================================================================
# TAB 4: ANNOUNCEMENTS & EVENT CALENDAR
# ===========================================================================
with tab_announcements:
    st.subheader("NSE Corporate Announcements")
    if not nse_ann_df.empty:
        af1, af2, af3 = st.columns([2, 2, 1])
        desc_vals = sorted(nse_ann_df["desc"].dropna().astype(str).unique().tolist()) if "desc" in nse_ann_df.columns else []
        sel_descs = af1.multiselect("Filter by Category", desc_vals, default=[])
        ann_kw = af2.text_input("Keyword search", placeholder="e.g. Board Meeting, Press Release, Credit Rating")
        earnings_only = af3.checkbox("Earnings-only", value=False)

        ann_view = nse_ann_df.copy()
        if sel_descs and "desc" in ann_view.columns:
            ann_view = ann_view[ann_view["desc"].astype(str).isin(sel_descs)]
        if earnings_only and "is_earnings_related" in ann_view.columns:
            ann_view = ann_view[ann_view["is_earnings_related"] == True]
        if ann_kw.strip():
            blob = (
                ann_view.get("desc", pd.Series("", index=ann_view.index)).astype(str)
                + " "
                + ann_view.get("attchmntText", pd.Series("", index=ann_view.index)).astype(str)
            )
            ann_view = ann_view[blob.str.contains(ann_kw, case=False, na=False)]

        st.caption(f"Showing **{len(ann_view)}** of {len(nse_ann_df)} NSE announcements.")
        cols = [c for c in ["an_dt", "desc", "attchmntText", "attFileSize", "attchmntFile"] if c in ann_view.columns]
        st.dataframe(
            ann_view[cols],
            width="stretch",
            hide_index=True,
            height=450,
            column_config={
                "an_dt": "Broadcast Time",
                "desc": "Category",
                "attchmntText": st.column_config.TextColumn("Details", width="large"),
                "attFileSize": "File Size",
                "attchmntFile": st.column_config.LinkColumn("Attachment", display_text="Open PDF ↗"),
            },
        )
        for fname, raw_bytes in official_csvs.items():
            if fname.startswith("CF-AN-") or "announcements" in fname:
                st.download_button(
                    f"⬇ Download Official NSE CSV ({fname})",
                    data=raw_bytes,
                    file_name=fname,
                    mime="text/csv",
                    key=f"tab4_dl_{fname}",
                )
    else:
        st.info("No NSE corporate announcements available.")

    st.divider()
    st.subheader("Board Meetings & Event Calendar")
    if not events_df.empty:
        cols = [c for c in ["date", "purpose", "bm_desc", "symbol", "company"] if c in events_df.columns]
        st.dataframe(events_df[cols], width="stretch", hide_index=True)
    else:
        st.caption("No event calendar records returned.")

    if not bse_ann_df.empty:
        st.divider()
        st.subheader("BSE Corporate Announcements")
        st.dataframe(bse_ann_df, width="stretch", hide_index=True)


# ===========================================================================
# TAB 5: CORPORATE ACTIONS & SHAREHOLDING
# ===========================================================================
with tab_actions:
    ac1, ac2 = st.columns([1.15, 1])

    with ac1:
        st.subheader("Corporate Actions (Dividends / Splits / Bonuses)")
        if not actions_df.empty:
            act_view = actions_df.copy()
            cols = [c for c in ["exDate", "recDate", "subject", "series", "faceVal"] if c in act_view.columns]
            st.dataframe(
                act_view[cols],
                width="stretch",
                hide_index=True,
                column_config={
                    "exDate": "Ex-Date",
                    "recDate": "Record Date",
                    "subject": st.column_config.TextColumn("Purpose / Action", width="large"),
                    "series": "Series",
                    "faceVal": "Face Value",
                },
            )
            for fname, raw_bytes in official_csvs.items():
                if fname.startswith("CF-CA-") or "corporate_actions" in fname:
                    st.download_button(
                        f"⬇ Download Official NSE CSV ({fname})",
                        data=raw_bytes,
                        file_name=fname,
                        mime="text/csv",
                        key=f"tab5_dl_{fname}",
                    )
        else:
            st.info("No corporate actions returned.")

    with ac2:
        st.subheader("Quarterly Shareholding Pattern")
        if not shareholding_df.empty and "promoter_pct" in shareholding_df.columns:
            sh_plot = shareholding_df.copy()
            sh_plot["_dt"] = pd.to_datetime(sh_plot["date"], format="%d-%b-%Y", errors="coerce")
            sh_plot = sh_plot.dropna(subset=["_dt"]).sort_values("_dt").tail(12)
            sh_melt = sh_plot.melt(
                id_vars=["date", "_dt"],
                value_vars=["promoter_pct", "public_pct"],
                var_name="Holder",
                value_name="Pct",
            )
            sh_melt["Holder"] = sh_melt["Holder"].replace({
                "promoter_pct": "Promoter & Promoter Group",
                "public_pct": "Public",
            })
            sh_chart = (
                alt.Chart(sh_melt)
                .mark_line(point=True, strokeWidth=2.5)
                .encode(
                    x=alt.X("date:N", sort=alt.SortField("_dt"), title="Quarter End"),
                    y=alt.Y("Pct:Q", title="Holding (%)", scale=alt.Scale(zero=False)),
                    color=alt.Color(
                        "Holder:N",
                        scale=alt.Scale(range=["#2563eb", "#f59e0b"]),
                        legend=alt.Legend(orient="top", title=None),
                    ),
                    tooltip=[
                        alt.Tooltip("date:N", title="As on"),
                        alt.Tooltip("Holder:N", title="Group"),
                        alt.Tooltip("Pct:Q", title="%", format=".2f"),
                    ],
                )
                .properties(height=250)
            )
            st.altair_chart(sh_chart, width="stretch")

            cols = [c for c in ["date", "promoter_pct", "public_pct", "employee_trusts_pct", "submissionDate", "xbrl"] if c in shareholding_df.columns]
            st.dataframe(
                shareholding_df[cols],
                width="stretch",
                hide_index=True,
                column_config={
                    "date": "As On Date",
                    "promoter_pct": st.column_config.NumberColumn("Promoter %", format="%.2f%%"),
                    "public_pct": st.column_config.NumberColumn("Public %", format="%.2f%%"),
                    "employee_trusts_pct": st.column_config.NumberColumn("Emp. Trusts %", format="%.2f%%"),
                    "submissionDate": "Submitted",
                    "xbrl": st.column_config.LinkColumn("XBRL", display_text="XML ↗"),
                },
            )
        else:
            st.info("No shareholding data returned.")


# ===========================================================================
# TAB 6: OFFICIAL NSE CSV DOWNLOADS & EXPORTS
# ===========================================================================
with tab_downloads:
    st.subheader("📦 Complete Export Bundle")
    if results or official_csvs:
        zip_bytes = build_zip_bundle(sym, results, official_csvs, q)
        st.download_button(
            label=f"⬇ Download All Data for {sym} (.zip — Official NSE CSVs + Compiled CSV/JSON)",
            data=zip_bytes,
            file_name=f"{sym}_complete_bundle.zip",
            mime="application/zip",
            type="primary",
            key="dl_zip_bundle",
        )

    st.divider()
    st.subheader("🏛️ Official NSE Website 'Download as CSV' Files")
    st.caption(
        "Downloaded directly from nseindia.com's official CSV endpoints (`?csv=true`) "
        "and saved in `nse_official_csv/`."
    )

    if official_csvs:
        def _friendly_nse_label(fname: str) -> str:
            if fname.startswith("CF-CA-") or "corporate_actions" in fname:
                return "NSE Corporate Actions CSV"
            if fname.startswith("CF-AN-") or "announcements" in fname:
                return "NSE Corporate Announcements CSV"
            if fname.startswith("CF-FR-") or "financial_results" in fname:
                return "NSE Quarterly Financial Results CSV"
            if fname.startswith("CF-Integrated-Filing-") or "integrated_filings" in fname:
                return "NSE Integrated Filing Financials CSV"
            if fname.startswith("CF-Event-") or "event_calendar" in fname:
                return "NSE Board Meetings & Event Calendar CSV"
            if fname.startswith("CF-Shareholding-") or "shareholding" in fname:
                return "NSE Shareholding Pattern CSV"
            if fname.startswith("Quote-Equity-") or "price_history" in fname:
                return "NSE Historical Price Data CSV"
            return fname

        for fname, raw_bytes in official_csvs.items():
            title_lbl = _friendly_nse_label(fname)
            size_kb = len(raw_bytes) / 1024.0
            try:
                preview_df = pd.read_csv(io.BytesIO(raw_bytes))
                row_count = len(preview_df)
            except Exception:
                preview_df = pd.DataFrame()
                row_count = 0

            c1, c2, c3 = st.columns([3, 1.5, 1.5])
            c1.markdown(f"**{title_lbl}** (`{fname}`)")
            c2.caption(f"{row_count:,} rows · {size_kb:,.1f} KB")
            c3.download_button(
                "⬇ Download CSV",
                data=raw_bytes,
                file_name=fname if fname.startswith(("CF-", "Quote-")) else f"{sym}_{fname}",
                mime="text/csv",
                key=f"official_dl_{fname}",
                width="stretch",
            )
            if not preview_df.empty:
                with st.expander(f"Preview {fname} (top 20 rows)"):
                    st.dataframe(preview_df.head(20), width="stretch", hide_index=True)
    else:
        st.info("Official NSE CSVs were not downloaded for this run. Enable 'Download official NSE CSVs' in the sidebar and click Refresh.")

    st.divider()
    st.subheader("🗂️ Batch Extract Official NSE CSVs for Multiple Stocks")
    st.caption(
        "Extract the exact NSE website CSVs (such as `CF-CA-equities-<SYMBOL>-<DATE>.csv`, "
        "`CF-AN-equities-<SYMBOL>-<DATE>.csv`, etc.) for multiple stocks at once into "
        "`data/nse_csv_exports/<SYMBOL>/` and download as a single ZIP."
    )

    wl_file = Path(__file__).parent / "watchlist.txt"
    default_wl = (
        [l.strip().upper() for l in wl_file.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
        if wl_file.exists()
        else ["RELIANCE", "TCS", "HDFCBANK", "INFY"]
    )
    batch_options = sorted(set(symbol_list) | set(default_wl) | {sym})
    default_sel = [s for s in default_wl if s in batch_options][:10]

    b1, b2 = st.columns([2, 1.5])
    batch_stocks = b1.multiselect(
        "Select stocks to extract CSVs for",
        options=batch_options,
        default=default_sel,
        key="batch_stocks_select",
    )
    batch_cats = b2.multiselect(
        "Which NSE CSVs to download",
        options=list(NSE_CSV_CATEGORIES.keys()),
        default=["corporate_actions", "announcements", "financial_results"],
        format_func=lambda k: NSE_CSV_CATEGORIES[k],
        key="batch_cats_select",
    )

    if st.button("⬇ Extract Official NSE CSVs for Selected Stocks", key="btn_batch_extract", disabled=not (batch_stocks and batch_cats)):
        with st.spinner(f"Extracting official NSE CSVs for {len(batch_stocks)} stocks…"):
            extracted, batch_errs, export_root = batch_extract_nse_csvs(
                batch_stocks, categories=batch_cats, days=history_days
            )
        zbuf = io.BytesIO()
        total_files = 0
        with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for b_sym, files_dict in extracted.items():
                for fname, raw_b in files_dict.items():
                    zf.writestr(f"{b_sym}/{fname}", raw_b)
                    total_files += 1
        zbuf.seek(0)
        st.session_state["batch_zip_bytes"] = zbuf.read()
        st.session_state["batch_summary"] = f"Extracted {total_files} official NSE CSVs across {len(extracted)} stocks → `{export_root}`"
        if batch_errs:
            st.warning(f"Some files had notices: {batch_errs}")

    if "batch_zip_bytes" in st.session_state and st.session_state["batch_zip_bytes"]:
        st.success(st.session_state.get("batch_summary", "Batch extraction complete!"))
        st.download_button(
            "📦 Download Batch Official NSE CSVs (.zip)",
            data=st.session_state["batch_zip_bytes"],
            file_name="nse_official_csvs_batch.zip",
            mime="application/zip",
            type="primary",
            key="dl_batch_zip",
        )

    st.divider()
    st.subheader("📊 Compiled & Enriched Datasets")
    for name, df in results.items():
        if df is None or df.empty:
            continue
        r1, r2, r3, r4 = st.columns([3, 1.2, 1.2, 1.2])
        r1.markdown(f"**{name}**")
        r2.caption(f"{len(df):,} rows · {len(df.columns)} cols")
        r3.download_button(
            "⬇ CSV",
            data=df.to_csv(index=False),
            file_name=f"{sym}_{name}.csv",
            mime="text/csv",
            key=f"comp_csv_{name}",
            width="stretch",
        )
        r4.download_button(
            "⬇ JSON",
            data=df.to_json(orient="records", indent=2),
            file_name=f"{sym}_{name}.json",
            mime="application/json",
            key=f"comp_json_{name}",
            width="stretch",
        )
