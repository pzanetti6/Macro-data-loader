# Macro Data Loader — Universal Edition

A local Streamlit app for downloading, harmonising, inspecting and exporting macroeconomic data from official sources. The same application code runs on **Windows and macOS**; each operating system has its own double-click launcher.

## Quick start

### Windows

1. Unzip the folder to a normal local folder (for example, Documents or Desktop).
2. Make sure a **64-bit Python 3.10–3.13** installation is available. Python 3.11 or 3.12 is a good default.
3. Double-click **`run_windows.bat`**.
4. On the first run, the launcher creates a private `.venv`, installs the required packages, and opens the app in your browser.
5. Later runs reuse the same environment.

If Windows says Python is missing, install Python from the official Python website, enable **Add python.exe to PATH** (or install the Python Launcher), then run `run_windows.bat` again.

### macOS

1. Unzip the folder.
2. Double-click **`run_mac.command`**.
3. The launcher finds a compatible Python automatically. If necessary, it can use Conda or Homebrew without modifying the system Python.
4. It creates a private `.venv`, installs the packages, and starts the app.

If macOS blocks the launcher, right-click `run_mac.command` → **Open** → **Open**. Alternatively, from Terminal run:

```bash
chmod +x run_mac.command
./run_mac.command
```

## Important portability rule

Do **not** copy or distribute the `.venv` folder. A virtual environment is operating-system-specific. The launchers create the correct environment locally on each computer.

The distributable folder should contain only:

```text
Macro Data Loader/
├── app.py
├── macro_core.py
├── excel_tools.py
├── requirements.txt
├── run_windows.bat
├── run_mac.command
└── README.md
```

## What the app does

- Creates euro-area aggregate monthly, quarterly or annual macro workbooks.
- Creates country-specific workbooks/panels for euro-area members in the app's country list.
- Retrieves the core macro block from Eurostat and equivalent ECB series where mapped.
- Chooses among equivalent sources using the app's longest/freshest-history logic.
- Supports optional ECB policy rates, money-market rates and yield series.
- Adds equity benchmarks: EURO STOXX 50, harmonised OECD national share-price indices for country output, and S&P 500 / Dow Jones for US output; all are aligned to the selected M/Q/A frequency.
- Provides plotting and diagnostics before Excel export.
- Keeps the latest successful workbook in Streamlit session state so it remains downloadable after UI reruns.

### Core variables

- real GDP
- real GDP growth
- log real GDP
- HICP level
- HICP inflation
- unemployment
- industrial production
- log industrial production
- energy HICP index

Monthly GDP is not an official monthly series. The UI lets the user choose the monthly treatment implemented by the app, including Chow-Lin using industrial production, linear interpolation, repeated quarterly GDP, or blank monthly GDP where applicable.

## Excel output

Excel files are generated directly by Python with `openpyxl`; Microsoft Excel does not need to be installed for the app to create `.xlsx` files.

## Internet access

Internet access is required to:

- install/update Python packages on the first run (and when dependencies change);
- download macroeconomic data from the official online sources.

## Stop the app

Return to the launcher/Terminal window and press **Ctrl+C**. The local Streamlit server runs on port **8534**.

## Troubleshooting

### Windows: launcher opens and immediately closes
Run `run_windows.bat` again. The launcher pauses when setup/startup fails so the error remains visible. A common fix is to delete `.venv` and launch again.

### Windows: `python` opens the Microsoft Store
Install Python from the official Python website with the Python Launcher or PATH option enabled. The launcher prefers `py` when available.

### macOS: permission denied
Run:

```bash
chmod +x run_mac.command
```

### A `.venv` came from another computer or operating system
Delete `.venv`. The launcher will rebuild it correctly.

## Data/source behaviour retained from the current app

- Headline Eurostat unemployment uses the app's current filters and euro-area fallbacks.
- Euro-area downloads use concrete Eurostat EA aggregate codes and ECB `U2` where appropriate.
- Industrial-production calls are constrained to avoid accidental bulk extractions.
- ECB transient 502/503/504 responses are retried by the application logic.
- Equivalent ECB/Eurostat series are ranked by the app's coverage/freshness logic.
- HICP inflation can be selected independently from the HICP level.
- Monthly Chow-Lin GDP benchmarks monthly estimates to observed quarterly GDP levels.
- Daily/business-day ECB financial observations are aggregated to the selected output frequency.
- Sovereign-yield handling and the Greece `EL` → ECB `GR` mapping are preserved.

## Public edition

This public edition focuses on macroeconomic data retrieval, harmonisation, diagnostics, plotting and Excel export. Monetary-policy shock upload/integration is intentionally not included.

## For maintainers

The application logic is shared across operating systems. Keep OS-specific behaviour in the launcher files rather than adding Windows/macOS branches to `app.py`, `macro_core.py`, or `excel_tools.py` unless genuinely necessary.

When releasing a new version, distribute a fresh ZIP **without** `.venv`, `.conda-python311`, `__pycache__`, `.DS_Store`, generated Excel files, or local logs.

## v10 — house prices and energy prices

The variable picker now has a dedicated **Prices & real estate** category.

- **EA aggregate and EA countries — House price index:** Eurostat `prc_hpi_q`, total residential-property purchases, index 2015=100, native quarterly frequency.
- **United States — House price index:** FHFA All-Transactions House Price Index (`USSTHPI`, distributed through FRED), native quarterly frequency.
- **EA aggregate and EA countries — Energy price index:** Eurostat HICP energy aggregate (`NRG`). The loader now tries the current `prc_hicp_minr` ECOICOP v2 table first, with a legacy-table fallback.
- **United States — Energy price index:** BLS Energy CPI (`CPIENGSL`, distributed through FRED).

Frequency harmonisation for house prices is deliberately conservative: quarterly data are used directly for quarterly output, averaged across quarters for annual output, and repeated within each quarter for monthly output. The monthly representation therefore does not invent intra-quarter house-price movements.
