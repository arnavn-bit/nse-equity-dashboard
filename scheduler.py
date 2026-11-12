"""
Standalone scheduler — runs collection for a watchlist on a repeating
interval, so you don't need the Streamlit tab open.

Usage:
    python scheduler.py                         # uses watchlist.txt, every 60 min
    python scheduler.py --interval 30            # every 30 min
    python scheduler.py --symbols RELIANCE TCS   # ignore watchlist.txt, use these
    python scheduler.py --once                   # single pass and exit

Each run saves CSV/JSON datasets, compiled news & earnings, quote snapshot,
and official NSE "Download as CSV" files under data/<SYMBOL>/<timestamp>/.
"""

import argparse
import logging
import time
from pathlib import Path

from collectors import (
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
    save_run,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scheduler")

WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"


def load_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        WATCHLIST_FILE.write_text("RELIANCE\nTCS\nHDFCBANK\nINFY\n")
    return [
        line.strip().upper()
        for line in WATCHLIST_FILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def run_once(symbols: list[str], history_days: int = 365) -> None:
    for symbol in symbols:
        log.info("Collecting %s", symbol)
        quote_snapshot, q_err = collect_quote_snapshot(symbol)
        if q_err:
            log.warning("  %s: quote_snapshot failed — %s", symbol, q_err)
        company_name = quote_snapshot.get("company_name") or symbol

        results = {}
        for name, fn in [
            ("earnings_summary", lambda: collect_earnings_summary(symbol)),
            ("integrated_filings", lambda: collect_integrated_filings(symbol)),
            ("financial_results", lambda: collect_financial_results(symbol)),
            ("nse_announcements", lambda: collect_nse_announcements(symbol)),
            ("corporate_actions", lambda: collect_corporate_actions(symbol)),
            ("price_history", lambda: collect_price_history(symbol, history_days)),
            ("shareholding", lambda: collect_shareholding(symbol)),
            ("event_calendar", lambda: collect_event_calendar(symbol)),
            ("google_news", lambda: collect_google_news(company_name, symbol=symbol)),
        ]:
            df, err = fn()
            results[name] = df
            if err:
                log.warning("  %s: %s failed — %s", symbol, name, err)
            else:
                log.info("  %s: %s rows -> %s", symbol, name, len(df))
            time.sleep(0.6)

        compiled_news = compile_news_feed(results)
        if not compiled_news.empty:
            results["compiled_news"] = compiled_news
            log.info("  %s: compiled_news rows -> %s", symbol, len(compiled_news))

        official_csvs, csv_errs = download_nse_official_csvs(symbol, days=history_days)
        log.info("  %s: downloaded %d official NSE CSVs", symbol, len(official_csvs))
        for fname, err in csv_errs.items():
            log.warning("  %s: official CSV %s failed — %s", symbol, fname, err)

        if any(not df.empty for df in results.values()) or official_csvs:
            run_dir = save_run(
                symbol,
                results,
                official_csvs=official_csvs,
                quote_snapshot=quote_snapshot,
            )
            log.info("  saved -> %s", run_dir)
        else:
            log.warning("  no data returned for %s this run", symbol)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60, help="minutes between runs")
    parser.add_argument("--days", type=int, default=365, help="price history window in days")
    parser.add_argument("--symbols", nargs="*", help="override watchlist.txt")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = parser.parse_args()

    symbols = [s.upper() for s in args.symbols] if args.symbols else load_watchlist()
    log.info("Watchlist: %s", ", ".join(symbols))

    if args.once:
        run_once(symbols, history_days=args.days)
        return

    log.info("Running every %d minutes. Ctrl+C to stop.", args.interval)
    while True:
        try:
            run_once(symbols, history_days=args.days)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("Run failed, will retry next cycle")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
