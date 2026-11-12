# NSE Equity Research & Earnings Dashboard

Interactive Streamlit dashboard + automated scheduler for collecting and compiling
NSE stock data, live quotes, price history, quarterly earnings & XBRL filings,
compiled news articles (Google News RSS + NSE Press Releases), corporate
announcements, corporate actions, shareholding patterns, and official NSE
"Download as CSV" files.

## Setup

```bash
pip install -r requirements.txt
```

## Run the Interactive Dashboard

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`.

1. **Select an Index / Stock Universe** (`NIFTY 50`, `NIFTY NEXT 50`, `NIFTY BANK`, `NIFTY IT`, `NIFTY 500`, `ALL NSE EQUITIES`, or `SAVED LOCAL STOCKS`) from the first dropdown.
2. **Pick a Stock** from the searchable `SYMBOL — Company Name` dropdown (or type any NSE symbol directly).
3. Browse **6 dashboard tabs**:
   - **Overview & Price**: Interactive price & volume chart (`1M`, `3M`, `6M`, `1Y`, `ALL`) with optional 20D/50D SMA overlays, plus latest headlines & filings.
   - **Earnings & Financials**: Compiled quarterly financials (Revenue, PBT, PAT, EPS, PAT Margin %, QoQ growth), interactive Altair charts, Integrated Filings (`iXBRL` / `XBRL`), Historical Financial Results, and earnings-tagged NSE releases.
   - **News Articles**: Compiled feed merging Google News RSS (company + earnings queries) and NSE official press/investor updates, with category/source/keyword filters.
   - **Announcements & Events**: Filterable NSE corporate announcements (with PDF links) and Board Meetings / Event Calendar.
   - **Actions & Shareholding**: Dividends, splits, bonuses, rights, demergers + quarterly promoter/public shareholding trend chart.
   - **NSE CSVs & Downloads**: One-click ZIP bundle + individual downloads for all **official NSE "Download as CSV"** (`?csv=true`) files and compiled CSV/JSON datasets.

Every live fetch auto-saves under `data/<SYMBOL>/<timestamp>/` (including `nse_official_csv/` and `combined.json`), and you can also switch to any past saved run directly from the sidebar.

## Run the Scheduler (Automated Background Collection)

```bash
python scheduler.py                       # every 60 min, uses watchlist.txt
python scheduler.py --interval 30          # every 30 min
python scheduler.py --symbols RELIANCE TCS # one-off list, ignores watchlist.txt
python scheduler.py --once                 # single pass and exit
```

Edit `watchlist.txt` (one NSE symbol per line) to change what gets tracked.
