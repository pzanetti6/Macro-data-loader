# Macro Data Loader

A Streamlit app that downloads, harmonises, checks and exports macroeconomic data from official sources for the euro area, euro-area member states and the United States. The same code runs on Streamlit Community Cloud, Windows and macOS.

## Quick start

### Streamlit Community Cloud

1. Point the app at `app.py` in this repository.
2. Optional but recommended for US data: in the app's **Settings → Secrets**, add

   ```toml
   FRED_API_KEY = "your32characterlowercasekey000000"
   ```

   A free key is available from your FRED account under *API keys*. Visitors can also paste their own key in the app's **FRED connection** panel; it stays in their browser session and is never written to a workbook.

### Windows

1. Unzip the folder somewhere local (Documents or Desktop).
2. Install 64-bit Python 3.10–3.13 (3.12 recommended) with **Add python.exe to PATH** or the Python Launcher.
3. Double-click **`run_windows.bat`**. The first run creates a private `.venv` and installs the packages; later runs start straight away.

### macOS

1. Unzip the folder and double-click **`run_mac.command`**.
2. If macOS blocks it, right-click → **Open** → **Open**, or run `chmod +x run_mac.command && ./run_mac.command` in Terminal.

The local app runs at http://localhost:8534. Press **Ctrl+C** in the launcher window to stop it.

## Folder contents

```text
Macro Data Loader/
├── app.py               interface
├── macro_core.py        downloads, harmonisation, temporal disaggregation
├── excel_tools.py       workbook creation and safe updating
├── shock_tools.py       shock-upload helpers (not exposed in the public edition)
├── requirements.txt
├── run_windows.bat
├── run_mac.command
├── .streamlit/config.toml   visual theme
├── .gitattributes       keeps Windows line endings in the .bat file
└── README.md
```

Do not copy or commit `.venv`: a virtual environment only works on the machine and operating system that created it. The launchers rebuild it when needed.

## What the app does

- Builds monthly, quarterly or annual workbooks for the euro-area aggregate, a panel of euro-area countries (one sheet per country or a long panel), or the United States.
- Updates an existing workbook in place: only columns whose header matches a known series name are rewritten; shock, forecast and hand-kept rate columns and every formula are left untouched; appended rows copy the sheet's date style; and a large level gap between old and new values (typically a rebased index) is reported.
- Writes a `sources` sheet listing each series' publisher, series ID, native frequency, latest observation and construction.
- Shows a preview, a chart (original units, rebased to 100 or standardised) and automatic checks for duplicate dates, gaps inside a series, placeholder zeros and extreme jumps.
- Keeps downloads for six hours, so rebuilding with a different window or layout does not download again.

## Definitions

- **Dates.** From/To are applied at the output frequency; rows before the first and after the last observation are dropped. Dates are text: `YYYY-MM`, `YYYYqN`, `YYYY`.
- **Growth and inflation.** Simple year-on-year percentage changes (12 months, 4 quarters, 1 year), the convention of published HICP and CPI rates. Quarterly and annual inflation average the monthly rates. Euro-area and US definitions are identical.
- **Monthly GDP.** GDP is quarterly. Chow-Lin (maximum likelihood, AR(1) errors, industrial production and a trend as indicators) is the default; each quarter's three months average exactly to the published level. Linear interpolation centres each quarter on its middle month. Nothing is extrapolated beyond the last published quarter.
- **Daily series.** Rates, spreads and equity indices are averaged over the observations in each period. ECB policy rates are carried forward between decisions, up to today and never into the future.
- **House prices.** Native quarterly: repeated within the quarter for monthly output, averaged for annual output.

## Sources

- **Euro area:** ECB Data Portal and Eurostat (HICP, unemployment, industrial production, GDP, house prices); where both publish an equivalent series the longest and most recent is chosen. OECD harmonised share-price indices for national equity markets. ICE BofA Euro High Yield OAS via FRED.
- **United States:** BEA (GDPC1), BLS (CPIAUCSL, UNRATE, CPIENGSL), Federal Reserve (INDPRO, FEDFUNDS, GS1, GS10), FRBNY (SOFR), FHFA (USSTHPI, from 1975Q1) and ICE BofA (BAMLH0A0HYM2), all through FRED. Moody's Baa–10Y Treasury spread (BAA10YM, from 1953) via FRED, and the Gilchrist–Zakrajšek credit spread and excess bond premium (from 1973) directly from the Federal Reserve Board.
- **Credit spread:** one series per economy, chosen automatically (up to date first, then longest). Since April 2026 FRED publishes only the last three years of every ICE BofA series, so in the US the longer Moody's Baa–Treasury or Gilchrist–Zakrajšek spreads are normally used.
- **Markets:** S&P 500, Dow Jones, Nasdaq Composite and VIX from Yahoo Finance or FRED, whichever is longer and current; MOVE from Yahoo Finance. 

## How FRED is reached

For each series the app tries, in order:

1. the official FRED API (`api.stlouisfed.org`) when a key is configured;
2. FRED's public CSV download, requested **without** date-window parameters and filtered locally.

Requests are spaced to respect FRED's rate limit, retried on 429/5xx errors, and run concurrently. If one series still fails, its column is left empty with the reason in its `_source` cell and the rest of the workbook is built. Error messages never contain the API key.

## Troubleshooting

**US series show "Unavailable … HTTP 404".** Open **FRED connection → Check connection**. If the public CSV route fails but the API works, add a key (see Quick start). If both fail, FRED is unreachable from that network; try again later or from another connection.

**"API HTTP 400 (… api_key is not registered)".** The key is mistyped or revoked; a valid key is 32 lowercase letters and digits.

**Windows: the launcher closes immediately.** Run it again; it pauses on errors. Deleting `.venv` and relaunching fixes most environment problems.

**Windows: `python` opens the Microsoft Store.** Install Python from python.org with the Launcher or PATH option.

**macOS: permission denied.** `chmod +x run_mac.command`.

**Packages need refreshing.** Delete `.venv`; the next launch reinstalls everything. Otherwise packages are reinstalled only when `requirements.txt` changes.

## Changelog

### v25.3
- **One credit-spread option per economy.** "Credit spread" compares the available corporate-spread measures and keeps the best: among candidates that are up to date (last observation within three months of the freshest), the longest history wins. US candidates: Moody's Baa minus 10Y Treasury (from 1953), Gilchrist–Zakrajšek credit spread (Federal Reserve Board, from 1973), ICE BofA high-yield OAS. They are never spliced; the source cell names the one used. Euro area: ICE BofA euro high-yield OAS, the only free candidate (three years on FRED since April 2026).
- **New market series:** Nasdaq Composite and VIX (US); MOVE, the ICE BofA US Treasury implied-volatility index (euro-area datasets).
- **Equity and volatility indices** (S&P 500, Dow, Nasdaq, VIX) are compared between Yahoo Finance and FRED with the same up-to-date-then-longest rule.
- Shorter header with a contact link.

### v25
- **US monthly downloads fixed.** The keyless FRED CSV fallback sent `cosd`/`coed` date windows, which that endpoint can ignore or answer with HTTP 404; it now downloads the plain series and slices locally, and validates that it received CSV rather than a maintenance page. FRED API errors now show FRED's own message.
- **Monthly GDP is about 20× faster.** Chow-Lin now computes the quarterly covariance in closed form. Results are identical to the previous implementation on the same ρ grid, followed by a local refinement of ρ. Long US monthly samples previously ran into the download deadline.
- **Each raw series is downloaded once** (INDPRO was fetched twice for monthly GDP) and cached for six hours; ECB, Eurostat, OECD and Yahoo requests share the cache.
- **The FRED key is per session.** It was a module-wide setting shared by every visitor of a hosted app, and could appear in error text; it now stays in the session and is redacted everywhere.
- **The updater now updates rate-named macro columns.** It used to skip `unemployment_rate`, `hicp_rate` and `inflation_rate` because their headers contain "rate".
- **One failing series no longer stops a build.** This applies to the euro area and country panels as well as the US.
- **Consistent definitions:** simple y/y growth and inflation everywhere, period averages of daily data, and S&P 500/Dow as averages of daily closes regardless of provider.
- **Smaller fixes:**
  - ECB policy rates are no longer carried into future months.
  - Linear GDP interpolation is centred on the middle month of each quarter.
  - The From/To dates are applied at month or quarter level.
  - Long-panel workbooks no longer contain NaN cells.
  - The workbook README matches the data source.
  - Eurostat GDP falls back to the 2020/2015 reference years if 2010 is retired.
- **New interface:** theme, step layout, persistent results with preview, chart, checks and sources, FRED connection check.
- **Faster setup:** removed the unused `scipy` and `statsmodels` dependencies; launchers skip reinstalling unchanged packages.

### Earlier
- v23: FRED API key support. v22: concurrent US requests. v21: HICP annual inflation freshness. v15: country HPI selection. v10: house and energy prices.

## Public edition

Monetary-policy shock upload is not exposed in the public interface. The tested helpers live in `shock_tools.py`; a private build can call `shock_tools.integrate_shocks` to attach uploaded shock series.
