"""
Source collectors, NSE official CSV downloaders, compilers, and storage.

Shared by app.py (interactive Streamlit dashboard) and scheduler.py (automated,
repeating collection). Keeping all acquisition logic here guarantees both entry
points stay in sync.
"""

from __future__ import annotations

import html
import io
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import dalal
import feedparser
import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

NSE_HOME_COOKIE_URL = "https://www.nseindia.com/option-chain"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:118.0) "
        "Gecko/20100101 Firefox/118.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE",
    "Connection": "keep-alive",
}

INDEX_CSV_URLS: dict[str, str] = {
    "NIFTY 50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY NEXT 50": "https://nsearchives.nseindia.com/content/indices/ind_niftynext50list.csv",
    "NIFTY BANK": "https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv",
    "NIFTY IT": "https://nsearchives.nseindia.com/content/indices/ind_niftyitlist.csv",
    "NIFTY 500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "ALL NSE EQUITIES": "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
}

FALLBACK_STOCKS: list[dict[str, str]] = [
    {"symbol": "RELIANCE", "company_name": "Reliance Industries Limited", "industry": "Oil Gas & Consumable Fuels", "isin": "INE002A01018"},
    {"symbol": "TCS", "company_name": "Tata Consultancy Services Limited", "industry": "Information Technology", "isin": "INE467B01029"},
    {"symbol": "HDFCBANK", "company_name": "HDFC Bank Limited", "industry": "Financial Services", "isin": "INE040A01034"},
    {"symbol": "INFY", "company_name": "Infosys Limited", "industry": "Information Technology", "isin": "INE009A01021"},
    {"symbol": "ICICIBANK", "company_name": "ICICI Bank Limited", "industry": "Financial Services", "isin": "INE090A01021"},
    {"symbol": "BHARTIARTL", "company_name": "Bharti Airtel Limited", "industry": "Telecommunication", "isin": "INE397D01024"},
    {"symbol": "SBIN", "company_name": "State Bank of India", "industry": "Financial Services", "isin": "INE062A01020"},
    {"symbol": "ITC", "company_name": "ITC Limited", "industry": "Fast Moving Consumer Goods", "isin": "INE154A01025"},
    {"symbol": "LT", "company_name": "Larsen & Toubro Limited", "industry": "Construction", "isin": "INE018A01030"},
    {"symbol": "HINDUNILVR", "company_name": "Hindustan Unilever Limited", "industry": "Fast Moving Consumer Goods", "isin": "INE030A01027"},
]

EARNINGS_KEYWORDS = (
    "financial result",
    "unaudited",
    "un-audited",
    "audited",
    "earnings",
    "investor presentation",
    "con. call",
    "conference call",
    "analyst",
    "transcript",
    "media release",
    "press release",
    "outcome of board meeting",
    "quarter ended",
    "half year ended",
)

_SESSION_LOCK = threading.Lock()
_NSE_SESSION: requests.Session | None = None
_COOKIES_PRIMED = False
_LAST_NSE_REQ = 0.0


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _to_float(val: Any) -> float | None:
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _get_nse_session(force_reprime: bool = False) -> requests.Session:
    global _NSE_SESSION, _COOKIES_PRIMED
    with _SESSION_LOCK:
        if _NSE_SESSION is None:
            _NSE_SESSION = requests.Session()
            _NSE_SESSION.headers.update(NSE_HEADERS)
            _COOKIES_PRIMED = False
        if force_reprime or not _COOKIES_PRIMED:
            try:
                _NSE_SESSION.get(NSE_HOME_COOKIE_URL, timeout=15)
            except Exception:
                pass
            _COOKIES_PRIMED = True
        return _NSE_SESSION


def _nse_get(url: str, params: dict | None = None, timeout: int = 20) -> requests.Response:
    global _LAST_NSE_REQ
    with _SESSION_LOCK:
        elapsed = time.monotonic() - _LAST_NSE_REQ
        if elapsed < 0.3:
            time.sleep(0.3 - elapsed)
        _LAST_NSE_REQ = time.monotonic()

    sess = _get_nse_session(force_reprime=False)
    resp = sess.get(url, params=params, timeout=timeout)
    if resp.status_code in (401, 403):
        sess = _get_nse_session(force_reprime=True)
        resp = sess.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


def _nse_get_json(url: str, params: dict | None = None) -> Any:
    resp = _nse_get(url, params=params)
    return resp.json()


# ---------------------------------------------------------------------------
# Stock universe / index constituents
# ---------------------------------------------------------------------------

def fetch_index_constituents(index_name: str = "NIFTY 50") -> tuple[pd.DataFrame, str | None]:
    """Fetch official NSE constituent CSV for an index or all NSE equities."""
    url = INDEX_CSV_URLS.get(index_name)
    if not url:
        return pd.DataFrame(FALLBACK_STOCKS), f"Unknown index '{index_name}', using fallback list."
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": NSE_HEADERS["User-Agent"], "Referer": "https://www.nseindia.com/"},
            timeout=15,
        )
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.content.decode("utf-8-sig", errors="replace")))
        df.columns = [c.strip() for c in df.columns]

        if "Symbol" in df.columns:
            out = pd.DataFrame({
                "symbol": df["Symbol"].astype(str).str.strip().str.upper(),
                "company_name": df.get("Company Name", df["Symbol"]).astype(str).str.strip(),
                "industry": df.get("Industry", "Equity").astype(str).str.strip(),
                "isin": df.get("ISIN Code", "").astype(str).str.strip(),
            })
        else:
            # EQUITY_L.csv format: SYMBOL, NAME OF COMPANY, SERIES, ..., ISIN NUMBER
            out = pd.DataFrame({
                "symbol": df["SYMBOL"].astype(str).str.strip().str.upper(),
                "company_name": df.get("NAME OF COMPANY", df["SYMBOL"]).astype(str).str.strip(),
                "industry": df.get("SERIES", "EQ").astype(str).str.strip(),
                "isin": df.get("ISIN NUMBER", "").astype(str).str.strip(),
            })
        out = out[out["symbol"].str.len() > 0].drop_duplicates(subset=["symbol"]).sort_values("symbol").reset_index(drop=True)
        return out, None
    except Exception as e:
        return pd.DataFrame(FALLBACK_STOCKS), f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Live Quote Snapshot
# ---------------------------------------------------------------------------

def collect_quote_snapshot(symbol: str) -> tuple[dict[str, Any], str | None]:
    """Fetch live NSE quote metadata, trade stats, 52W range, PE, industry info."""
    sym = symbol.strip().upper()
    try:
        payload = _nse_get_json(
            "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi",
            params={"functionName": "getSymbolData", "marketType": "N", "series": "EQ", "symbol": sym},
        )
        eq_list = payload.get("equityResponse") or []
        if not eq_list:
            return {}, f"No quote data returned for {sym}"
        eq = eq_list[0]
        meta = eq.get("metaData") or {}
        trade = eq.get("tradeInfo") or {}
        price = eq.get("priceInfo") or {}
        sec = eq.get("secInfo") or {}

        mcap_raw = _to_float(trade.get("totalMarketCap"))
        snapshot = {
            "symbol": meta.get("symbol") or sym,
            "company_name": meta.get("companyName") or sym,
            "isin": meta.get("isinCode") or "",
            "last_price": _to_float(meta.get("closePrice")) or _to_float(trade.get("lastPrice")),
            "previous_close": _to_float(meta.get("previousClose")),
            "change": _to_float(meta.get("change")),
            "p_change": _to_float(meta.get("pChange")),
            "open": _to_float(meta.get("open")),
            "day_high": _to_float(meta.get("dayHigh")),
            "day_low": _to_float(meta.get("dayLow")),
            "vwap": _to_float(meta.get("averagePrice")),
            "week_52_high": _to_float(price.get("yearHigh")),
            "week_52_high_date": price.get("yearHightDt"),
            "week_52_low": _to_float(price.get("yearLow")),
            "week_52_low_date": price.get("yearLowDt"),
            "market_cap_cr": round(mcap_raw / 1e7, 2) if mcap_raw else None,
            "total_traded_volume": _to_float(trade.get("totalTradedVolume")),
            "delivery_pct": _to_float(trade.get("deliveryToTradedQuantity")),
            "face_value": _to_float(trade.get("faceValue")),
            "symbol_pe": _to_float(sec.get("pdSymbolPe")),
            "sector_pe": _to_float(sec.get("pdSectorPe")),
            "macro": sec.get("macro") or "",
            "sector": sec.get("sector") or "",
            "industry": sec.get("industryInfo") or "",
            "basic_industry": sec.get("basicIndustry") or "",
            "listing_date": sec.get("listingDate") or "",
            "last_update_time": eq.get("lastUpdateTime") or "",
            "retrieved_at": now_utc_iso(),
        }
        return snapshot, None
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Earnings & Financial Filings
# ---------------------------------------------------------------------------

def collect_earnings_summary(symbol: str) -> tuple[pd.DataFrame, str | None]:
    """Fetch quarterly financial status (Income, PBT, PAT, EPS) and compute margins & growth."""
    sym = symbol.strip().upper()
    try:
        rows = _nse_get_json(
            "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi",
            params={"functionName": "getFinancialStatus", "symbol": sym},
        )
        if not isinstance(rows, list) or not rows:
            return pd.DataFrame(), f"No quarterly financial summary available for {sym}"

        records = []
        for r in rows:
            inc_lakhs = _to_float(r.get("totalIncome"))
            pbt_lakhs = _to_float(r.get("reProLossBefTax"))
            pat_lakhs = _to_float(r.get("netProLossAftTax"))
            eps = _to_float(r.get("eps"))

            inc_cr = round(inc_lakhs / 100.0, 2) if inc_lakhs is not None else None
            pbt_cr = round(pbt_lakhs / 100.0, 2) if pbt_lakhs is not None else None
            pat_cr = round(pat_lakhs / 100.0, 2) if pat_lakhs is not None else None
            pat_margin = (
                round((pat_lakhs / inc_lakhs) * 100.0, 2)
                if (pat_lakhs is not None and inc_lakhs and inc_lakhs != 0)
                else None
            )

            records.append({
                "symbol": sym,
                "quarter": r.get("to_date_MonYr") or r.get("to_date"),
                "period_end": r.get("to_date"),
                "total_income_cr": inc_cr,
                "pbt_cr": pbt_cr,
                "net_profit_cr": pat_cr,
                "pat_margin_pct": pat_margin,
                "eps": eps,
                "audited": r.get("audited"),
                "broadcast_timestamp": r.get("re_broadcast_timestamp"),
                "total_income_lakhs": inc_lakhs,
                "pbt_lakhs": pbt_lakhs,
                "net_profit_lakhs": pat_lakhs,
                "retrieved_at": now_utc_iso(),
            })

        df = pd.DataFrame(records)
        df["_sort_dt"] = pd.to_datetime(df["period_end"], format="%d %b %Y", errors="coerce")
        df = df.sort_values("_sort_dt", ascending=True).reset_index(drop=True)
        df["qoq_income_pct"] = (df["total_income_cr"].pct_change() * 100.0).round(2)
        df["qoq_pat_pct"] = (df["net_profit_cr"].pct_change() * 100.0).round(2)
        df["yoy_income_pct"] = (df["total_income_cr"].pct_change(4) * 100.0).round(2)
        df["yoy_pat_pct"] = (df["net_profit_cr"].pct_change(4) * 100.0).round(2)
        df = df.sort_values("_sort_dt", ascending=False).drop(columns=["_sort_dt"]).reset_index(drop=True)
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


def collect_integrated_filings(symbol: str, size: int = 50) -> tuple[pd.DataFrame, str | None]:
    """Fetch NSE Integrated Filing - Financials (latest quarterly XBRL/iXBRL filings)."""
    sym = symbol.strip().upper()
    try:
        payload = _nse_get_json(
            "https://www.nseindia.com/api/integrated-filing-results",
            params={"index": "equities", "symbol": sym, "size": size},
        )
        rows = payload.get("data") if isinstance(payload, dict) else []
        df = pd.DataFrame(rows or [])
        if not df.empty:
            df["retrieved_at"] = now_utc_iso()
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


def collect_financial_results(symbol: str, period: str = "Quarterly") -> tuple[pd.DataFrame, str | None]:
    """Fetch NSE Corporate Financial Results archive (Quarterly/Annual XBRL filings)."""
    sym = symbol.strip().upper()
    try:
        rows = _nse_get_json(
            "https://www.nseindia.com/api/corporates-financial-results",
            params={"index": "equities", "symbol": sym, "period": period},
        )
        df = pd.DataFrame(rows if isinstance(rows, list) else [])
        if not df.empty:
            df["retrieved_at"] = now_utc_iso()
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Announcements, Actions, Price History, Shareholding, Event Calendar
# ---------------------------------------------------------------------------

def collect_nse_announcements(symbol: str) -> tuple[pd.DataFrame, str | None]:
    sym = symbol.strip().upper()
    try:
        try:
            rows = dalal.announcements(sym, exchange="NSE")
        except Exception:
            rows = _nse_get_json(
                "https://www.nseindia.com/api/corporate-announcements",
                params={"index": "equities", "symbol": sym},
            )
        df = pd.DataFrame(rows if isinstance(rows, list) else [])
        if not df.empty:
            df["retrieved_at"] = now_utc_iso()
            df["source"] = "NSE"
            text_blob = (
                df.get("desc", pd.Series("", index=df.index)).fillna("").astype(str)
                + " "
                + df.get("attchmntText", pd.Series("", index=df.index)).fillna("").astype(str)
            ).str.lower()
            df["is_earnings_related"] = text_blob.apply(
                lambda s: any(kw in s for kw in EARNINGS_KEYWORDS)
            )
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


def collect_bse_announcements(symbol: str) -> tuple[pd.DataFrame, str | None]:
    try:
        matches = dalal.lookup(symbol, exchange="BSE")
        if not matches:
            return pd.DataFrame(), f"No BSE scrip code found for '{symbol}'"
        scripcode = matches[0].get("scripcode") or matches[0].get("symbol")
        rows = dalal.announcements(scripcode, exchange="BSE")
        table = rows.get("Table", []) if isinstance(rows, dict) else rows
        df = pd.DataFrame(table or [])
        if not df.empty:
            df["retrieved_at"] = now_utc_iso()
            df["source"] = "BSE"
            df["bse_scripcode"] = scripcode
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


def collect_corporate_actions(symbol: str) -> tuple[pd.DataFrame, str | None]:
    sym = symbol.strip().upper()
    try:
        try:
            rows = dalal.actions(sym, exchange="NSE")
        except Exception:
            rows = _nse_get_json(
                "https://www.nseindia.com/api/corporates-corporateActions",
                params={"index": "equities", "symbol": sym},
            )
        df = pd.DataFrame(rows if isinstance(rows, list) else [])
        if not df.empty:
            df["retrieved_at"] = now_utc_iso()
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


def collect_price_history(symbol: str, days: int = 365) -> tuple[pd.DataFrame, str | None]:
    sym = symbol.strip().upper()
    try:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = dalal.history(sym, start, end)
        df = pd.DataFrame(rows if isinstance(rows, list) else [])
        if not df.empty:
            df["retrieved_at"] = now_utc_iso()
            if "mtimestamp" in df.columns:
                df["date"] = pd.to_datetime(df["mtimestamp"], format="%d-%b-%Y", errors="coerce").dt.strftime("%Y-%m-%d")
                df = df.sort_values("date").reset_index(drop=True)
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


def collect_shareholding(symbol: str) -> tuple[pd.DataFrame, str | None]:
    sym = symbol.strip().upper()
    try:
        try:
            rows = dalal.shareholding(sym)
        except Exception:
            rows = _nse_get_json(
                "https://www.nseindia.com/api/corporate-share-holdings-master",
                params={"index": "equities", "symbol": sym},
            )
        df = pd.DataFrame(rows if isinstance(rows, list) else [])
        if not df.empty:
            df["promoter_pct"] = pd.to_numeric(df.get("pr_and_prgrp"), errors="coerce")
            df["public_pct"] = pd.to_numeric(df.get("public_val"), errors="coerce")
            df["employee_trusts_pct"] = pd.to_numeric(df.get("employeeTrusts"), errors="coerce").fillna(0.0)
            df["_dt"] = pd.to_datetime(df.get("date"), format="%d-%b-%Y", errors="coerce")
            df = df.sort_values("_dt", ascending=False).drop(columns=["_dt"]).reset_index(drop=True)
            df["retrieved_at"] = now_utc_iso()
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


def collect_event_calendar(symbol: str) -> tuple[pd.DataFrame, str | None]:
    sym = symbol.strip().upper()
    try:
        rows = _nse_get_json(
            "https://www.nseindia.com/api/event-calendar",
            params={"index": "equities", "symbol": sym},
        )
        df = pd.DataFrame(rows if isinstance(rows, list) else [])
        if not df.empty:
            df["retrieved_at"] = now_utc_iso()
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Google News RSS + Compiled News
# ---------------------------------------------------------------------------

def _parse_pub_date(pub_str: str | None) -> str:
    if not pub_str:
        return ""
    try:
        dt = parsedate_to_datetime(pub_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M IST")
    except Exception:
        return str(pub_str)


def collect_google_news(
    company_name: str,
    symbol: str | None = None,
    max_items: int = 40,
) -> tuple[pd.DataFrame, str | None]:
    """Fetch Google News RSS via requests (avoids macOS urllib SSL issues) for both general & earnings news."""
    sym = (symbol or company_name).strip().upper()
    clean_name = re.sub(r"\b(Limited|Ltd\.?)\b", "", company_name, flags=re.IGNORECASE).strip() or sym
    if clean_name.upper() != sym:
        base_term = f'"{clean_name}" OR {sym}'
    else:
        base_term = f"{sym} NSE stock"

    queries = [
        ("Market & Company News", f"{base_term} stock"),
        ("Earnings & Results", f"{base_term} quarterly results OR earnings OR profit"),
    ]

    all_items: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    errors: list[str] = []

    for category, q_str in queries:
        try:
            url = (
                f"https://news.google.com/rss/search?q={quote_plus(q_str)}"
                "&hl=en-IN&gl=IN&ceid=IN:en"
            )
            resp = requests.get(
                url,
                headers={"User-Agent": NSE_HEADERS["User-Agent"]},
                timeout=15,
            )
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
            for entry in feed.entries[:max_items]:
                title = _strip_html(entry.get("title"))
                link = (entry.get("link") or "").strip()
                dedup_key = title.lower()[:90]
                if not title or dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                src_obj = entry.get("source")
                publisher = src_obj.get("title") if isinstance(src_obj, dict) else None
                if not publisher and " - " in title:
                    publisher = title.rsplit(" - ", 1)[-1].strip()

                raw_pub = entry.get("published") or ""
                all_items.append({
                    "published_ist": _parse_pub_date(raw_pub),
                    "published_raw": raw_pub,
                    "title": title,
                    "publisher": publisher or "Google News",
                    "category": category,
                    "summary": _strip_html(entry.get("summary")),
                    "link": link,
                    "query": q_str,
                    "retrieved_at": now_utc_iso(),
                    "pit_safe": False,
                })
        except Exception as e:
            errors.append(f"{category}: {type(e).__name__}: {e}")

    df = pd.DataFrame(all_items)
    if not df.empty:
        df["_sort"] = pd.to_datetime(df["published_raw"], errors="coerce", utc=True)
        df = df.sort_values("_sort", ascending=False).drop(columns=["_sort"]).reset_index(drop=True)
    err_msg = "; ".join(errors) if (errors and df.empty) else None
    return df, err_msg


def compile_news_feed(results: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Compile Google News articles + NSE Press/Media Releases & Investor Updates into one feed."""
    rows: list[dict[str, Any]] = []

    gnews = results.get("google_news")
    if gnews is not None and not gnews.empty:
        for _, r in gnews.iterrows():
            rows.append({
                "published_at": r.get("published_ist") or r.get("published_raw") or "",
                "title": r.get("title") or "",
                "publisher": r.get("publisher") or "Google News",
                "category": r.get("category") or "Market & Company News",
                "source_type": "Google News RSS",
                "summary": r.get("summary") or "",
                "link": r.get("link") or "",
            })

    nse_ann = results.get("nse_announcements")
    if nse_ann is not None and not nse_ann.empty:
        news_like_descs = {
            "press release",
            "investor presentation",
            "analysts/institutional investor meet/con. call updates",
            "outcome of board meeting",
            "updates",
            "general updates",
        }
        for _, r in nse_ann.iterrows():
            desc = str(r.get("desc") or "").strip()
            if desc.lower() in news_like_descs or bool(r.get("is_earnings_related")):
                bdate = str(r.get("an_dt") or r.get("sort_date") or "")
                rows.append({
                    "published_at": bdate,
                    "title": f"[NSE {desc}] {str(r.get('attchmntText') or desc)[:160]}",
                    "publisher": "NSE Official Filing",
                    "category": "NSE Press / Earnings Release" if r.get("is_earnings_related") else "NSE Corporate Update",
                    "source_type": "NSE Filing",
                    "summary": str(r.get("attchmntText") or ""),
                    "link": str(r.get("attchmntFile") or ""),
                })

    df = pd.DataFrame(rows)
    if not df.empty:
        # Attempt unified datetime sorting
        cleaned_dt = (
            df["published_at"]
            .astype(str)
            .str.replace(" IST", "", regex=False)
        )
        df["_dt"] = pd.to_datetime(cleaned_dt, errors="coerce")
        df = df.sort_values("_dt", ascending=False).drop(columns=["_dt"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Official NSE "Download as CSV" endpoints (&csv=true)
# ---------------------------------------------------------------------------

def download_nse_official_csvs(
    symbol: str,
    days: int = 365,
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Download the official 'Download as CSV' files directly from nseindia.com."""
    sym = symbol.strip().upper()
    end_dmy = datetime.now().strftime("%d-%m-%Y")
    start_dmy = (datetime.now() - timedelta(days=days)).strftime("%d-%m-%Y")

    endpoints: dict[str, tuple[str, dict[str, Any]]] = {
        "nse_official_announcements.csv": (
            "https://www.nseindia.com/api/corporate-announcements",
            {"index": "equities", "symbol": sym, "csv": "true"},
        ),
        "nse_official_corporate_actions.csv": (
            "https://www.nseindia.com/api/corporates-corporateActions",
            {"index": "equities", "symbol": sym, "csv": "true"},
        ),
        "nse_official_financial_results.csv": (
            "https://www.nseindia.com/api/corporates-financial-results",
            {"index": "equities", "symbol": sym, "period": "Quarterly", "csv": "true"},
        ),
        "nse_official_integrated_filings.csv": (
            "https://www.nseindia.com/api/integrated-filing-results",
            {"index": "equities", "symbol": sym, "csv": "true"},
        ),
        "nse_official_event_calendar.csv": (
            "https://www.nseindia.com/api/event-calendar",
            {"index": "equities", "symbol": sym, "csv": "true"},
        ),
        "nse_official_shareholding.csv": (
            "https://www.nseindia.com/api/corporate-share-holdings-master",
            {"index": "equities", "symbol": sym, "csv": "true"},
        ),
        "nse_official_price_history.csv": (
            "https://www.nseindia.com/api/historicalOR/cm/equity",
            {"symbol": sym, "series": '["EQ"]', "from": start_dmy, "to": end_dmy, "csv": "true"},
        ),
    }

    csv_files: dict[str, bytes] = {}
    errors: dict[str, str] = {}

    for filename, (url, params) in endpoints.items():
        try:
            resp = _nse_get(url, params=params, timeout=25)
            content = resp.content
            # Normalize BOM cleanly to UTF-8 with BOM or plain UTF-8 so Excel & pandas both open cleanly
            text = content.decode("utf-8-sig", errors="replace").strip()
            if text and not text.lower().startswith("<html") and not text.lower().startswith("<!doctype"):
                csv_files[filename] = text.encode("utf-8-sig")
            else:
                errors[filename] = "Empty or non-CSV response from NSE"
        except Exception as e:
            errors[filename] = f"{type(e).__name__}: {e}"

    return csv_files, errors


# ---------------------------------------------------------------------------
# Registry + Persistence
# ---------------------------------------------------------------------------

SOURCES = {
    "earnings_summary": lambda symbol, **kw: collect_earnings_summary(symbol),
    "integrated_filings": lambda symbol, **kw: collect_integrated_filings(symbol),
    "financial_results": lambda symbol, **kw: collect_financial_results(symbol),
    "nse_announcements": lambda symbol, **kw: collect_nse_announcements(symbol),
    "bse_announcements": lambda symbol, **kw: collect_bse_announcements(symbol),
    "corporate_actions": lambda symbol, **kw: collect_corporate_actions(symbol),
    "price_history": lambda symbol, days=365, **kw: collect_price_history(symbol, days),
    "shareholding": lambda symbol, **kw: collect_shareholding(symbol),
    "event_calendar": lambda symbol, **kw: collect_event_calendar(symbol),
    "google_news": lambda symbol, company_name=None, **kw: collect_google_news(company_name or symbol, symbol=symbol),
}


def save_run(
    symbol: str,
    results: dict[str, pd.DataFrame],
    official_csvs: dict[str, bytes] | None = None,
    quote_snapshot: dict[str, Any] | None = None,
) -> Path:
    sym = symbol.strip().upper()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = DATA_DIR / sym / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    combined_json: dict[str, Any] = {}
    for name, df in results.items():
        if df is None or df.empty:
            continue
        df.to_csv(run_dir / f"{name}.csv", index=False)
        records = json.loads(df.to_json(orient="records"))
        (run_dir / f"{name}.json").write_text(json.dumps(records, indent=2))
        combined_json[name] = records

    if quote_snapshot:
        (run_dir / "quote_snapshot.json").write_text(
            json.dumps(quote_snapshot, indent=2, default=str)
        )
        combined_json["quote_snapshot"] = quote_snapshot

    if official_csvs:
        csv_dir = run_dir / "nse_official_csv"
        csv_dir.mkdir(exist_ok=True)
        for fname, raw_bytes in official_csvs.items():
            (csv_dir / fname).write_bytes(raw_bytes)

    (run_dir / "combined.json").write_text(json.dumps(combined_json, indent=2, default=str))
    return run_dir


def list_saved_symbols() -> list[str]:
    if not DATA_DIR.exists():
        return []
    return sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))


def list_saved_runs(symbol: str) -> list[Path]:
    sym_dir = DATA_DIR / symbol.strip().upper()
    if not sym_dir.exists():
        return []
    return sorted(
        (p for p in sym_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name,
        reverse=True,
    )


def load_saved_run(run_dir: Path) -> tuple[dict[str, pd.DataFrame], dict[str, bytes], dict[str, Any]]:
    results: dict[str, pd.DataFrame] = {}
    official_csvs: dict[str, bytes] = {}
    quote_snapshot: dict[str, Any] = {}

    if not run_dir.exists():
        return results, official_csvs, quote_snapshot

    for csv_path in sorted(run_dir.glob("*.csv")):
        try:
            results[csv_path.stem] = pd.read_csv(csv_path)
        except Exception:
            pass

    q_path = run_dir / "quote_snapshot.json"
    if q_path.exists():
        try:
            quote_snapshot = json.loads(q_path.read_text())
        except Exception:
            pass

    official_dir = run_dir / "nse_official_csv"
    if official_dir.exists():
        for csv_path in sorted(official_dir.glob("*.csv")):
            official_csvs[csv_path.name] = csv_path.read_bytes()

    return results, official_csvs, quote_snapshot
