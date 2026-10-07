# EA Macro Updater

A small Streamlit app to create and update euro-area macroeconomic Excel datasets.

## Mac: easiest installation

1. Unzip the folder.
2. Double-click `run_mac.command`.
3. The launcher checks your Python automatically.
4. If your Mac is using Python 3.9.7 (which modern Streamlit does not support), the launcher will **not alter your system Python**. It will instead:
   - use an existing Python 3.10–3.13 if available;
   - otherwise, if Conda is installed, create a private Python 3.11 environment inside this app folder;
   - otherwise, if Homebrew is installed, install/use `python@3.11`.
5. It then creates an isolated `.venv`, installs the packages and starts the app.

The first launch requires internet access for Python packages and the macro-data downloads. Later launches reuse the environment.

### If macOS blocks the launcher

Right-click `run_mac.command` → **Open** → **Open**.

Or from Terminal:

```bash
cd /Users/pietro/Desktop/ea_macro_updater
chmod +x run_mac.command
./run_mac.command
```

## What the app does

- Creates new EA aggregate monthly, quarterly or annual macro workbooks.
- Creates country-specific workbooks/panels for euro-area members in the app's country list.
- Retrieves the core macro block from Eurostat.

### Variables

- real GDP
- real GDP growth
- log real GDP
- HICP level
- HICP inflation
- unemployment
- industrial production
- log industrial production
- energy HICP index

Monthly GDP is not an official monthly series. The UI therefore lets you choose whether to repeat quarterly GDP within the quarter, interpolate it, or leave it blank.

## Stop the app

Return to the Terminal window and press `Ctrl+C`.

## v2 fixes
- Fixed `une_rt_m`: current headline age code is `Y15-74`; SA/TRE fallbacks are handled automatically.
- Added `EA21` as a current aggregate fallback while retaining EA20 and EA19.
- Monthly GDP defaults to **Chow-Lin (industrial production)**.
- **Linear interpolation** remains selectable.
- Removed the yellow monthly-GDP warning banner.

## v3 unemployment fix
Eurostat `une_rt_m` uses `age=TOTAL` for the headline total unemployment rate.
The downloader now queries `SA + PC_ACT + T + TOTAL` and, for euro-area data,
tries EA21, EA20 and EA19 independently instead of assuming that the aggregate
code selected from another dataset must also be available in unemployment.

## v4 — longest-history source selection
For HICP, unemployment, industrial production and real GDP, the app attempts
both the ECB Data Portal and Eurostat using economically equivalent mappings.
It selects the source with the earliest available observation; ties are broken
by observation count and then the latest endpoint. Exact ties prefer ECB.
Every output variable is followed by a `<variable>_source` column.
Energy HICP remains Eurostat-only until an exactly equivalent ECB classification
is mapped, avoiding silent concept changes.

## v5
- Euro-area downloads now prefer the default/changing-composition euro-area aggregate (`EA` in Eurostat where available; `U2` in ECB) before fixed-composition fallbacks.
- Fixed EA21/EA20/EA19 are fallbacks only.
- Excel provenance columns are compact (`ECB`, `Eurostat`, `Derived (...)`, `Chow-Lin (...)`); full dataset/series IDs remain in the metadata sheet.
- Removed the blue informational box from the home screen.

## v6 — Euro-area query fix
- The Euro Area option downloads a single aggregate observation per period, not member-country rows.
- Eurostat calls now always use a concrete euro-area geo code (EA21/EA20/EA19); the invalid generic `EA` filter was removed.
- Industrial-production (`sts_inpr_m`) calls explicitly filter frequency, geography, seasonal adjustment, unit and NACE aggregate to prevent HTTP 413 bulk extractions.
- ECB uses `U2` for the evolving/default euro area when Euro Area is selected.
- ECB transient 502/503/504 responses are retried once.

## v7 — longest + freshest selection
- Source selection now ranks equivalent ECB/Eurostat series by full calendar span,
  then latest available endpoint, then observation count.
- HICP inflation is selected independently from the HICP level.
- The app first tries Eurostat's direct monthly annual-rate HICP series
  (`prc_hicp_manr`, all-items) rather than forcing inflation to inherit the
  coverage of the chosen index-base series.
- Index-derived year-on-year inflation remains a fallback.

## v8 — frequency consistency and ECB financial series
- The same underlying source series is used across monthly, quarterly and annual transformations.
- Monthly Chow-Lin GDP now benchmarks levels correctly: the average of the three monthly GDP estimates equals the observed quarterly GDP level (not their sum).
- Monthly IP is the base series; quarterly and annual IP are period averages.
- Optional ECB tick-box series: €STR, DFR, MRO, AAA 1-year spot yield and AAA 10-year spot yield.
- Daily/business-day ECB financial observations are averaged within month/quarter/year.

## v9 — sovereign yields in output
- Added `sovereign_1y` and `sovereign_10y` output options.
- Euro-area `sovereign_10y`: ECB harmonised 10Y convergence yield; country output uses the selected country's own ECB IRS 10Y series.
- Euro-area `sovereign_1y`: ECB EA AAA 1Y spot curve.
- For individual countries, the app only accepts an exact ECB country 1Y benchmark series. If ECB does not expose one, the field is left blank and documented in metadata; the EA AAA 1Y curve is never silently substituted.

## v10 — stable results and plotting
- The latest generated Excel workbook is stored in Streamlit session state, so the download button remains available after UI reruns.
- The plotting controls run in a Streamlit fragment, so changing geography or plotted variable no longer reruns/interferes with the downloader.
- The latest successful dataset remains available for plotting until a new successful build replaces it.

## v7 — Greece ECB country-code fix preserved
- The app continues to use `EL` for Greece internally and for Eurostat.
- At the ECB boundary, `EL` is mapped to `GR` before constructing country-specific IRS/FM keys.
- Greek 10Y sovereign yield therefore requests `IRS/M.GR.L.L40.CI.0000.EUR.N.Z`, never the invalid `...M.EL...` key.
- The mapping is centralized so future UI/state/style changes do not overwrite this fix.

## Public edition

The public app focuses on macroeconomic data retrieval, harmonisation, diagnostics, plotting and Excel export. Monetary-policy shock upload/integration is intentionally not included in this edition.
