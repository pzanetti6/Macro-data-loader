from __future__ import annotations

import inspect
from datetime import date

import numpy as np
import pandas as pd
import streamlit as st

import macro_core as mc
from macro_core import (CANONICAL_VARS, COUNTRY_NAMES, EA19, US_VARS, DataError,
                        fetch_country_panel, fetch_macro_dataset, fetch_us_dataset)
from excel_tools import create_workbook, frequency_of_sheet, update_workbook, workbook_sheet_names
from macro_extra import AVAILABILITY, available as series_available

try:
    from macro_core import REFERENCE_EQUITY_INDEX
except ImportError:  # older macro_core
    REFERENCE_EQUITY_INDEX = {}

# Public edition: monetary-policy shock upload is not exposed. A private build can
# collect uploads and call shock_tools.integrate_shocks(datasets, previews, freq).
SHOCK_PREVIEWS: list = []

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

FREQUENCIES = {"Monthly": "M", "Quarterly": "Q", "Annual": "A"}
FREQ_WORD = {"M": "monthly", "Q": "quarterly", "A": "annual"}
GDP_METHODS = [
    "Chow-Lin (industrial production)",
    "Linear interpolation",
    "Quarterly step (repeat within quarter)",
    "Leave blank",
]
GEOGRAPHIES = ["Euro area", "Euro-area countries", "United States"]

EU_LABELS = {
    "real_gdp": "Real GDP", "real_gdp_growth": "Real GDP growth", "log_real_gdp": "Log real GDP",
    "hicp_level": "HICP level", "hicp_rate": "HICP inflation", "unemployment_rate": "Unemployment rate",
    "ip": "Industrial production", "log_ip": "Log industrial production", "energy_index": "Energy prices (HICP)",
    "house_price_index": "House prices",
    "estr": "€STR", "dfr": "Deposit facility rate", "mro": "Main refinancing rate",
    "aaa_1y": "AAA 1Y yield", "aaa_10y": "AAA 10Y yield",
    "sovereign_1y": "Sovereign 1Y yield", "sovereign_10y": "Sovereign 10Y yield",
    "eurostoxx50": "EURO STOXX 50", "country_equity_index": "National equity index",
    "move": "MOVE (rates volatility)",
    "credit_spread": "Credit spread",
    "consumption": "Private consumption", "gcf": "Gross capital formation", "trade_balance": "Trade balance",
    "budget_balance": "Budget balance (% GDP)", "fdi": "FDI inflows (% GDP)",
    "m1": "M1", "m2": "M2", "m3": "M3",
    "loans_households": "New loans to households", "loans_firms": "New loans to firms",
    "ecb_spf_infl_1y": "SPF inflation, 1Y", "ecb_spf_infl_2y": "SPF inflation, 2Y",
    "ecb_spf_infl_lt": "SPF inflation, long term", "ecb_spf_gdp_1y": "SPF GDP growth, 1Y",
    "ecb_spf_gdp_lt": "SPF GDP growth, long term",
    "ecb_proj_gdp_cy": "Projected GDP growth, this year", "ecb_proj_gdp_ny": "Projected GDP growth, next year",
    "ecb_proj_hicp_cy": "Projected HICP inflation, this year", "ecb_proj_hicp_ny": "Projected HICP inflation, next year",
}
US_LABELS = {
    "real_gdp": "Real GDP", "real_gdp_growth": "Real GDP growth", "log_real_gdp": "Log real GDP",
    "cpi_level": "CPI level", "inflation_rate": "CPI inflation", "unemployment_rate": "Unemployment rate",
    "ip": "Industrial production", "log_ip": "Log industrial production", "energy_cpi": "Energy prices (CPI)",
    "house_price_index": "House prices",
    "fed_funds": "Federal funds rate", "sofr": "SOFR",
    "treasury_1y": "Treasury 1Y yield", "treasury_10y": "Treasury 10Y yield",
    "sp500": "S&P 500", "djia": "Dow Jones Industrial Average", "nasdaq": "Nasdaq Composite",
    "vix": "VIX", "credit_spread": "Credit spread",
    "consumption": "Private consumption", "gcf": "Gross private investment", "trade_balance": "Trade balance",
    "budget_balance": "Federal budget balance (% GDP)", "fdi": "FDI inflows (% GDP)",
    "m1": "M1", "m2": "M2",
    "loans_households": "Bank loans to households", "loans_firms": "Bank loans to firms",
    "spf_gdp": "SPF GDP growth, 1Y", "spf_cpi": "SPF CPI inflation, 1Y", "spf_cpi10": "SPF CPI inflation, 10Y",
    "spf_unemp": "SPF unemployment, 1Y",
    "fed_dots_cy": "Fed dots, this year", "fed_dots_ny": "Fed dots, next year", "fed_dots_lr": "Fed dots, longer run",
    "sep_gdp_cy": "SEP GDP growth, this year", "sep_pce_cy": "SEP PCE inflation, this year",
    "sep_unemp_cy": "SEP unemployment, this year",
}
GROUPS = {
    "US": {
        "Macro": ["real_gdp", "real_gdp_growth", "log_real_gdp", "cpi_level",
                  "inflation_rate", "unemployment_rate", "ip", "log_ip"],
        "National accounts": ["consumption", "gcf", "trade_balance", "budget_balance", "fdi"],
        "Rates & money": ["fed_funds", "sofr", "treasury_1y", "treasury_10y", "credit_spread", "m1", "m2"],
        "Financial indicators": ["sp500", "djia", "nasdaq", "vix", "house_price_index", "energy_cpi",
                                 "loans_households", "loans_firms"],
        "Surveys": ["spf_gdp", "spf_cpi", "spf_cpi10", "spf_unemp", "fed_dots_cy", "fed_dots_ny", "fed_dots_lr",
                    "sep_gdp_cy", "sep_pce_cy", "sep_unemp_cy"],
    },
    "EU": {
        "Macro": ["real_gdp", "real_gdp_growth", "log_real_gdp", "hicp_level",
                  "hicp_rate", "unemployment_rate", "ip", "log_ip"],
        "National accounts": ["consumption", "gcf", "trade_balance", "budget_balance", "fdi"],
        "Rates & money": ["estr", "dfr", "mro", "aaa_1y", "aaa_10y",
                          "sovereign_1y", "sovereign_10y", "credit_spread", "m1", "m2", "m3"],
        "Financial indicators": ["eurostoxx50", "country_equity_index", "move", "house_price_index", "energy_index",
                                 "loans_households", "loans_firms"],
        "Surveys": ["ecb_spf_infl_1y", "ecb_spf_infl_2y", "ecb_spf_infl_lt", "ecb_spf_gdp_1y", "ecb_spf_gdp_lt",
                    "ecb_proj_gdp_cy", "ecb_proj_gdp_ny", "ecb_proj_hicp_cy", "ecb_proj_hicp_ny"],
    },
}
GROUP_HELP = {
    "Macro": "Output, prices, labour market and production.",
    "National accounts": "Consumption, investment, trade and budget balances, foreign direct investment.",
    "Rates & money": "Policy and market rates, government yields, credit spreads and monetary aggregates.",
    "Financial indicators": "Equity and volatility indices, house and energy prices, bank lending.",
    "Surveys": "Professional forecasters and central-bank projections, one value per round.",
}
# Series that need the FRED API key itself (ALFRED vintages), not just FRED access.
NEEDS_FRED_KEY = {"fed_dots_cy", "fed_dots_ny", "sep_gdp_cy", "sep_pce_cy", "sep_unemp_cy"}

# How each series is built, keyed by the normalised meta label.
CONSTRUCTION = {
    "real_gdp_growth": "Simple year-on-year % change of real GDP (12 months, 4 quarters or 1 year).",
    "log_real_gdp": "Natural log of real GDP.",
    "hicp_level": "All-items HICP index; monthly values averaged to the output frequency.",
    "hicp_rate": "Published HICP annual rate of change (y/y %). Quarterly and annual values average the monthly rates.",
    "cpi": "CPI-U, seasonally adjusted. Inflation is the 12-month % change; quarterly and annual values average the monthly rates.",
    "unemployment_rate": "Seasonally adjusted unemployment rate; monthly values averaged to the output frequency.",
    "industrial_production": "Industrial production index, seasonally adjusted; monthly values averaged. Log IP is the log of that average.",
    "energy_hicp": "HICP energy aggregate; monthly values averaged to the output frequency.",
    "energy_cpi": "CPI-U energy; monthly values averaged to the output frequency.",
    "house_price_index": "Native quarterly. Monthly output repeats each quarter's value; annual output averages quarters.",
    "dfr": "Rate in force on each day (carried forward between ECB decisions, up to today), averaged over the period.",
    "mro": "Rate in force on each day (carried forward between ECB decisions, up to today), averaged over the period.",
    "estr": "Daily €STR averaged over the period.",
    "aaa_1y": "ECB AAA euro-area government curve, 1Y spot rate; daily values averaged over the period.",
    "aaa_10y": "ECB AAA euro-area government curve, 10Y spot rate; daily values averaged over the period.",
    "ea_sovereign/convergence_10y_yield": "ECB long-term interest rate for convergence purposes (10Y), monthly; averaged for quarterly/annual output.",
    "country_sovereign_10y_yield": "ECB long-term interest rate for convergence purposes (10Y), monthly; averaged for quarterly/annual output.",
    "euro_stoxx_50": "ECB monthly EURO STOXX 50 (average of daily closes); averaged further for quarterly/annual output.",
}


def gdp_construction(freq: str, method: str) -> str:
    if freq == "Q":
        return "Published quarterly real GDP (chain-linked volumes)."
    if freq == "A":
        return "Average of the four published quarterly levels."
    if method.startswith("Chow-Lin"):
        return ("Chow-Lin (maximum likelihood) disaggregation with industrial production and a trend as "
                "indicators; each quarter's three months average exactly to the published quarter.")
    if method.startswith("Linear"):
        return "Quarterly level placed on the quarter's middle month and interpolated linearly."
    if method.startswith("Quarterly step"):
        return "Each quarter's published level repeated for its three months."
    return "Left blank: GDP is not published monthly."


def _norm(label) -> str:
    return str(label or "").strip().lower().replace(" ", "_")


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def canonical_label(v, freq: str):
    p = mc.label_to_period(v, freq)
    if p is None:
        return v
    if freq == "A":
        return f"{p.year:04d}"
    if freq == "Q":
        return f"{p.year:04d}q{p.quarter}"
    return f"{p.year:04d}-{p.month:02d}"


def finalize(datasets: dict, freq: str, start: date, end: date) -> dict:
    """Restrict to the chosen window, drop empty edges and write one date style."""
    out = {}
    for geo, df in datasets.items():
        x = mc.trim_to_window(df, freq, start, end)
        x = x.copy()
        x["date"] = x["date"].map(lambda v: canonical_label(v, freq))
        out[geo] = x
    return out


def value_columns(df: pd.DataFrame) -> list:
    return [c for c in df.columns
            if c not in ("date", "country", "period") and not str(c).endswith("_source")
            and pd.api.types.is_numeric_dtype(df[c])]


def time_index(labels, freq: str) -> pd.DatetimeIndex:
    stamps = []
    for v in labels:
        p = mc.label_to_period(v, freq)
        stamps.append(p.to_timestamp() if p is not None else pd.NaT)
    return pd.DatetimeIndex(stamps)


def meta_rows(meta, freq: str, method: str) -> list:
    rows = []
    for m in meta:
        d = m.__dict__ if hasattr(m, "__dict__") else dict(m)
        key = _norm(d.get("variable"))
        if key == "real_gdp":
            how = gdp_construction(freq, method)
        else:
            how = CONSTRUCTION.get(key) or d.get("note") or "Downloaded directly; averaged to the output frequency where needed."
        rows.append({
            "series": d.get("variable"),
            "source": d.get("source"),
            "series_id": d.get("dataset"),
            "geography": d.get("geography"),
            "native_frequency": d.get("native_frequency"),
            "latest_observation": d.get("latest_period"),
            "construction": how,
            "note": d.get("note", ""),
        })
    return rows


_RATE_LIKE = ("rate", "growth", "inflation", "spread", "yield", "ebp", "dfr", "mro", "estr", "sofr",
              "fed_funds", "treasury", "aaa", "sovereign", "shock", "gpt", "rr_", "jk_", "balance", "fdi",
              "spf", "dots", "sep_", "proj_")


def _rate_like(name: str) -> bool:
    n = str(name).lower()
    return any(k in n for k in _RATE_LIKE)


def run_checks(df: pd.DataFrame, freq: str) -> list:
    """Return (kind, message) pairs: duplicates, interior gaps, odd zeros, extreme jumps."""
    out = []
    if df is None or df.empty:
        return [("warning", "No rows in the selected window.")]
    dates = df["date"].astype(str).reset_index(drop=True)
    dup = dates[dates.duplicated(keep=False)].unique().tolist()
    if dup:
        out.append(("error", "Duplicate dates: " + ", ".join(dup[:8]) + "."))
    gaps, zeros, jumps, empty = [], [], [], []
    for c in value_columns(df):
        s = pd.to_numeric(df[c], errors="coerce").reset_index(drop=True)
        valid = s.notna().to_numpy()
        if not valid.any():
            empty.append(c)
            continue
        first, last = int(np.argmax(valid)), len(valid) - 1 - int(np.argmax(valid[::-1]))
        inner = s.iloc[first:last + 1]
        miss = inner.isna()
        if miss.any():
            gaps.append(f"{c}: {int(miss.sum())} ({', '.join(dates[inner.index[miss]][:3])})")
        rate_like = _rate_like(c)
        if not rate_like and inner.eq(0).any():
            zeros.append(f"{c} ({', '.join(dates[inner.index[inner.eq(0)]][:3])})")
        x = inner.dropna()
        if not rate_like and (x > 0).all():
            x = np.log(x)
        d = x.diff().dropna()
        if len(d) >= 24:
            med = float(d.median())
            mad = float((d - med).abs().median())
            if mad > 0:
                rz = 0.6745 * (d - med) / mad
                big = rz[rz.abs() > 8]
                if not big.empty:
                    top = big.abs().sort_values(ascending=False).index[:3]
                    jumps.append(f"{c}: " + ", ".join(f"{dates[i]} (robust z {rz[i]:.0f})" for i in top))
    if empty:
        out.append(("warning", "No observations in this window for: " + ", ".join(empty) + "."))
    if gaps:
        out.append(("warning", "Missing values inside the sample: " + "; ".join(gaps[:8]) + "."))
    if zeros:
        out.append(("warning", "Exact zeros in level series, usually a placeholder for missing data: "
                    + "; ".join(zeros[:6]) + "."))
    if jumps:
        out.append(("info", "Unusually large period-to-period changes (check, they may be genuine, e.g. 2020): "
                    + "; ".join(jumps[:6]) + "."))
    last_p = mc.label_to_period(dates.iloc[-1], freq)
    if last_p is not None and last_p == pd.Period(date.today(), freq={"M": "M", "Q": "Q", "A": "Y"}[freq]):
        out.append(("info", f"The last period ({dates.iloc[-1]}) is still in progress: averages of daily "
                    "series cover the days published so far."))
    return out


def chart_frame(df: pd.DataFrame, cols: list, freq: str, scale: str):
    x = df[["date"] + cols].copy()
    x.index = time_index(x["date"], freq)
    x = x[cols].apply(pd.to_numeric, errors="coerce")
    x = x[~x.index.isna()]
    skipped = []
    if scale == "Index, first common period = 100" and cols:
        common = x.dropna()
        if common.empty:
            return x.iloc[0:0], cols
        base = common.iloc[0]
        keep = [c for c in cols if base[c] > 0]
        skipped = [c for c in cols if c not in keep]
        x = x[keep].div(base[keep]).mul(100.0)
    elif scale == "Standardised (z-score)":
        x = (x - x.mean()) / x.std(ddof=0)
    return x, skipped


def secret_fred_key() -> str:
    try:
        return str(st.secrets.get("FRED_API_KEY", "") or "")
    except Exception:
        return ""


_SEG_PARAMS = set(inspect.signature(st.segmented_control).parameters)
_PILL_PARAMS = set(inspect.signature(st.pills).parameters)


def segmented(label, options, default, key, **kw):
    """Single-choice segmented control that always returns a value and wraps if narrow."""
    if "required" in _SEG_PARAMS:
        kw["required"] = True
    if "wrap" in _SEG_PARAMS:
        kw["wrap"] = True
    value = st.segmented_control(label, options, default=default, key=key, **kw)
    return value if value is not None else default


def pills(label, options, key, **kw):
    """Multi-select pills. A previous choice survives option changes, minus options that left."""
    if "wrap" in _PILL_PARAMS:
        kw["wrap"] = True
    if key in st.session_state:
        st.session_state[key] = [v for v in (st.session_state[key] or []) if v in options]
    else:
        kw["default"] = []
    return st.pills(label, options, selection_mode="multi", key=key, **kw)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def guilloche_svg(width: int = 1200, height: int = 120, strands: int = 12) -> str:
    """Interlaced engraved sine curves, after the guilloché borders on banknotes."""
    xs = np.linspace(0.0, width, 241)
    envelope = 0.42 + 0.58 * np.sin(np.pi * xs / width) ** 0.8
    paths = []
    for k in range(strands):
        phase = 2.0 * np.pi * k / strands
        y = height / 2 + height * 0.40 * envelope * (
            0.64 * np.sin(2 * np.pi * xs / 310.0 + phase) + 0.36 * np.sin(2 * np.pi * xs / 128.0 - 2 * phase))
        d = "M" + " L".join(f"{a:.1f},{b:.2f}" for a, b in zip(xs, y))
        cls = "g-accent" if k == 3 else "g-line"
        paths.append(f'<path class="{cls}" d="{d}" pathLength="1" style="animation-delay:{k * 55}ms"/>')
    return (f'<svg class="guilloche" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
            f'role="img" aria-label="Engraved line ornament">{"".join(paths)}</svg>')


CSS = """
<style>
:root { --ink:#15302E; --ink-2:#4B6461; --intaglio:#0F5B52; --line:#9DBAB2; --bronze:#A07C00; --rule:#D3DDD9; }
.block-container, [data-testid="stMainBlockContainer"] { max-width: 1240px; padding-top: 2.6rem; padding-bottom: 5rem; }
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display: none; }
header[data-testid="stHeader"] { background: transparent; }
.hero h1 { font-family: Spectral, Georgia, serif; font-weight: 400; font-size: clamp(2.5rem, 6vw, 3.9rem);
           letter-spacing: -0.025em; line-height: 1.0; margin: 0; padding: 0; color: var(--ink); }
.hero a { color: var(--intaglio); text-decoration: underline; text-underline-offset: 2px; }
.hero p { max-width: none; color: var(--ink-2); font-size: 1.06rem; line-height: 1.6; margin: .9rem 0 0 0; }
.guilloche { width: 100%; height: 112px; display: block; margin: 1.6rem 0 .5rem 0; overflow: visible; }
.guilloche path { fill: none; stroke-width: .75; vector-effect: non-scaling-stroke;
                  stroke-dasharray: 1; stroke-dashoffset: 1; animation: draw 2.4s cubic-bezier(.3,.6,.2,1) forwards; }
.guilloche .g-line { stroke: var(--line); }
.guilloche .g-accent { stroke: var(--bronze); stroke-width: 1.1; }
@keyframes draw { to { stroke-dashoffset: 0; } }
@media (prefers-reduced-motion: reduce) { .guilloche path { animation: none; stroke-dashoffset: 0; } }
.sources-line { color: var(--ink-2); font-size: .86rem; margin: 0 0 .4rem 0; }
.step { display: flex; align-items: baseline; gap: .8rem; margin: 2.6rem 0 0 0; }
.step-n { font-family: Spectral, Georgia, serif; font-size: 2.1rem; color: var(--intaglio); line-height: 1; min-width: 1.1ch; }
.step-title { font-family: Spectral, Georgia, serif; font-weight: 500; font-size: 1.55rem; line-height: 1.1; color: var(--ink); }
.step-sub { color: var(--ink-2); font-size: .95rem; margin: .25rem 0 1.1rem 2.05rem; max-width: 70ch; }
.group-name { font-weight: 600; color: var(--ink); margin-top: .3rem; }
.group-help { color: var(--ink-2); font-size: .84rem; line-height: 1.45; margin-bottom: .6rem; }
.group-gap { height: .35rem; }
.country-tools { color: var(--ink-2); font-size: .88rem; white-space: nowrap; transform: translateY(-4px); }
.st-key-countries_all button, .st-key-countries_none button, .st-key-countries_all p, .st-key-countries_none p { color: var(--intaglio) !important; font-weight: 500; }
.group-more { color: var(--ink-2); font-size: .84rem; line-height: 1.45; margin: .15rem 0 0 .1rem; }
.result-title { font-family: Spectral, Georgia, serif; font-weight: 500; font-size: 1.9rem; color: var(--ink); margin: 3rem 0 .2rem 0; }
.result-sub { color: var(--ink-2); font-size: .95rem; margin-bottom: 1rem; }
.footnote { color: var(--ink-2); font-size: .84rem; }
@media (max-width: 640px) { .step-sub { margin-left: 0; } .guilloche { height: 80px; } }
</style>
"""

st.set_page_config(page_title="Macro Data Loader", page_icon=":material/stacked_line_chart:", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
st.markdown(
    '<div class="hero"><h1>Macro Data Loader</h1>'
    "<p>Official macroeconomic series downloader for US, Euro Area average and single countries.<br>"
    "Public sources are ranked by reliability and available time length and are pooled across FRED, ECB, "
    "Eurostat, OECD, World Bank.<br>"
    'Report any bug or suggestion at <a href="mailto:pzanetti@luiss.it">pzanetti@luiss.it</a></p></div>'
    + guilloche_svg(),
    unsafe_allow_html=True,
)


def step(n: int, title: str, sub: str = ""):
    st.markdown(f'<div class="step"><span class="step-n">{n}</span><span class="step-title">{title}</span></div>'
                + (f'<div class="step-sub">{sub}</div>' if sub else '<div style="height:.9rem"></div>'),
                unsafe_allow_html=True)


# --- 1 Output ---------------------------------------------------------------
step(1, "Output", "What to build, for which economy, and over which period.")
c1, c2 = st.columns([1.65, 1], gap="medium")
with c1:
    geography = segmented("Economy", GEOGRAPHIES, "Euro area", "geography")
with c2:
    freq_label = segmented("Frequency", list(FREQUENCIES), "Monthly", "freq_label")
freq = FREQUENCIES[freq_label]
is_us = geography == "United States"

countries = list(EA19)
panel_format = "One sheet per country"
COUNTRY_ORDER = sorted(EA19, key=lambda c: COUNTRY_NAMES.get(c, c))


def _set_countries(value):
    st.session_state["country_pick"] = list(value)


if geography == "Euro-area countries":
    pick_kw = {"wrap": True} if "wrap" in _PILL_PARAMS else {}
    if "country_pick" not in st.session_state:
        pick_kw["default"] = COUNTRY_ORDER
    countries = st.pills("Countries", COUNTRY_ORDER, selection_mode="multi", key="country_pick",
                         format_func=lambda c: COUNTRY_NAMES.get(c, c), **pick_kw) or []
    countries = [c for c in EA19 if c in countries]          # keep the usual code order in the output
    left, right = st.columns([1.65, 1], gap="medium", vertical_alignment="center")
    with left:
        k1, k2, k3 = st.columns([.09, .11, 1], gap="small", vertical_alignment="center")
        with k1:
            st.button("All", type="tertiary", key="countries_all", on_click=_set_countries, args=(COUNTRY_ORDER,))
        with k2:
            st.button("None", type="tertiary", key="countries_none", on_click=_set_countries, args=([],))
        with k3:
            st.markdown(f'<div class="country-tools">{len(countries)} of {len(EA19)} selected</div>',
                        unsafe_allow_html=True)
    with right:
        panel_format = segmented("Layout", ["One sheet per country", "Long panel"],
                                 "One sheet per country", "panel_format", label_visibility="collapsed")

c3, d1, d2 = st.columns([1.3, 1, 1], gap="medium")
with c3:
    action = segmented("Task", ["New workbook", "Update a workbook"], "New workbook", "action")
with d1:
    start_date = st.date_input("From", value=date(1948, 1, 1) if is_us else date(1996, 1, 1),
                               min_value=date(1913, 1, 1), max_value=date.today(), format="DD/MM/YYYY",
                               key=f"start_{'us' if is_us else 'eu'}")
with d2:
    end_date = st.date_input("To", value=date.today(), min_value=date(1913, 1, 1), max_value=date.today(),
                             format="DD/MM/YYYY", key="end")

uploaded, sheet_name, sheet_freq = None, None, None
if action == "Update a workbook":
    u1, u2 = st.columns([1.6, 1], gap="medium")
    with u1:
        uploaded = st.file_uploader("Workbook to update", type=["xlsx"], key="update_workbook")
    if uploaded is not None:
        try:
            sheets = workbook_sheet_names(uploaded.getvalue())
            with u2:
                sheet_name = st.selectbox("Sheet", sheets)
            sheet_freq = frequency_of_sheet(uploaded.getvalue(), sheet_name)
        except Exception as exc:
            st.error(f"This file could not be opened as an Excel workbook ({type(exc).__name__}).")
    st.caption("Only columns whose header matches a known series name are rewritten. Shock, forecast and "
               "hand-kept rate columns, and any formula, are left as they are.")

# --- 2 Series ---------------------------------------------------------------
step(2, "Series", "Pick what to include. Every series gets a neighbouring column naming its source.")
labels = US_LABELS if is_us else EU_LABELS
kind = "US" if is_us else ("EA" if geography == "Euro area" else "CTRY")
if is_us:
    offered = list(US_VARS)
elif geography == "Euro area":
    # sovereign_1y has no harmonised euro-area series and would always be empty.
    offered = [v for v in CANONICAL_VARS if v not in ("country_equity_index", "sovereign_1y")]
else:
    # No comparable country-level corporate spread or survey exists; the columns would be empty.
    offered = [v for v in CANONICAL_VARS if v != "credit_spread"]
offered = [v for v in offered if v not in AVAILABILITY or kind in AVAILABILITY[v]]
# Quarterly or annual series are not offered at a higher frequency.
available = [v for v in offered if series_available(v, kind, freq)]


def hidden_note(hidden: list, any_offered: bool) -> str:
    """Say where the series missing at this frequency can be found."""
    if not any_offered:
        lowest = "annually" if all(AVAILABILITY[v][kind] == "A" for v in hidden) else "quarterly or annually"
        target = "Annual" if lowest == "annually" else "Quarterly or Annual"
        return f"Published {lowest}: switch to {target} to choose from {len(hidden)} series."
    name = lambda v: labels.get(v, v).replace("% GDP", "%\u00a0GDP")
    quarterly = [name(v) for v in hidden if "Q" in AVAILABILITY[v][kind]]
    annual = [name(v) for v in hidden if AVAILABILITY[v][kind] == "A"]
    parts = ([f"Also offered quarterly: {', '.join(quarterly)}."] if quarterly else []) + \
            ([f"Annual only: {', '.join(annual)}."] if annual else [])
    return " ".join(parts)


selected: list = []
for group, members in GROUPS["US" if is_us else "EU"].items():
    options = [v for v in members if v in available]
    hidden = [v for v in members if v in offered and v not in available]
    if not options and not hidden:
        continue
    g1, g2 = st.columns([1, 3.1], gap="large")
    with g1:
        st.markdown(f'<div class="group-name">{group}</div><div class="group-help">{GROUP_HELP[group]}</div>',
                    unsafe_allow_html=True)
    with g2:
        if options:
            picked = pills(group, options, key=f"vars_{geography}_{group}",
                           format_func=lambda v: labels.get(v, v.replace("_", " ").title()),
                           label_visibility="collapsed")
            selected.extend(picked or [])
        if hidden:
            st.markdown(f'<div class="group-more">{hidden_note(hidden, bool(options))}</div>',
                        unsafe_allow_html=True)
    st.markdown('<div class="group-gap"></div>', unsafe_allow_html=True)

st.caption(f"{len(selected)} of {len(available)} series selected." if selected else "Nothing selected yet.")

monthly_gdp_method = GDP_METHODS[0]
needs_gdp_method = freq == "M" and any(v in selected for v in ("real_gdp", "real_gdp_growth", "log_real_gdp"))
needs_fred = is_us or "credit_spread" in selected

if needs_gdp_method:
    g1, _ = st.columns([1.65, 1], gap="medium")
    with g1:
        monthly_gdp_method = st.selectbox(
            "Monthly GDP", GDP_METHODS,
            help="GDP is published quarterly. Chow-Lin uses monthly industrial production to distribute "
                 "each quarter across its months; the three months always average to the published quarter.")

fred_key = secret_fred_key()
needs_key = bool(NEEDS_FRED_KEY & set(selected))
if needs_fred:
    with st.expander("FRED connection", expanded=needs_key and not fred_key, icon=":material/key:"):
        st.markdown(
            "US series and high-yield spreads come from FRED. Without a key the app uses FRED's public CSV "
            "download; with a free [FRED API key](https://fredaccount.stlouisfed.org/apikeys) it uses the "
            "official API first, which is faster and more reliable. The key stays in this browser session "
            "and is never written to the workbook.")
        k1, k2 = st.columns([2.2, 1], gap="medium", vertical_alignment="bottom")
        with k1:
            typed = st.text_input("FRED API key", type="password", key="fred_key_input",
                                  placeholder="Using the app's configured key" if fred_key else "32 lowercase letters and digits")
        problem = mc.fred_key_problem(typed)
        if typed and problem:
            st.error(problem)
        elif typed:
            fred_key = typed.strip()
        if needs_key and not fred_key:
            st.info("Fed dots and SEP medians are rebuilt from FRED's archive of past releases (ALFRED), "
                    "which only answers with an API key. Without one those columns stay empty; the longer-run "
                    "dot works either way.", icon=":material/info:")
        with k2:
            if st.button("Check connection", width="stretch"):
                with st.spinner("Contacting FRED"):
                    res = mc.check_fred_connection(fred_key)
                st.session_state["fred_check"] = res
        res = st.session_state.get("fred_check")
        if res:
            for route, name in (("api", "Official API"), ("csv", "Public CSV")):
                msg = res.get(route, "")
                (st.success if msg == "OK" else st.warning)(f"{name}: {msg}")

# --- 3 Build ----------------------------------------------------------------
step(3, "Build")
blockers = []
if not selected:
    blockers.append("pick at least one series")
if end_date < start_date:
    blockers.append("set a 'To' date after the 'From' date")
if geography == "Euro-area countries" and not countries:
    blockers.append("choose at least one country")
if action == "Update a workbook":
    if geography == "Euro-area countries":
        blockers.append("switch to Euro area or United States: updating works on a single sheet, not a country panel")
    elif uploaded is None:
        blockers.append("upload the workbook to update")
    elif sheet_freq and sheet_freq != freq:
        blockers.append(f"match the frequency: the sheet looks {FREQ_WORD[sheet_freq]}, "
                        f"but {FREQ_WORD[freq]} is selected")

run_label = "Build workbook" if action == "New workbook" else "Update workbook"
run = st.button(run_label, type="primary", width="stretch", disabled=bool(blockers), icon=":material/table_view:")
if blockers:
    st.caption("To continue, " + "; ".join(blockers) + ".")
else:
    st.caption("Downloads are kept for six hours, so changing the period or layout and building again is quick.")

if run and not blockers:
    status = st.status("Downloading series", expanded=False)
    bar = status.progress(0.0, text="Starting")

    def progress(frac, text):
        bar.progress(float(min(max(frac, 0.0), 1.0)) * 0.9, text=text)

    try:
        sy, ey = int(start_date.year), int(end_date.year)
        if geography == "Euro area":
            progress(0.1, "Comparing ECB and Eurostat series")
            df, meta = fetch_macro_dataset("EA_AUTO", freq, sy, ey, selected, monthly_gdp_method,
                                           fred_api_key=fred_key)
            datasets = {"EA": df}
        elif is_us:
            df, meta = fetch_us_dataset(freq, sy, ey, selected, monthly_gdp_method,
                                        fred_api_key=fred_key, progress=progress)
            datasets = {"US": df}
        else:
            datasets, meta = fetch_country_panel(countries, freq, sy, ey, selected, monthly_gdp_method,
                                                 progress=progress, fred_api_key=fred_key)

        if SHOCK_PREVIEWS:
            from shock_tools import integrate_shocks
            datasets, _, _ = integrate_shocks(datasets, SHOCK_PREVIEWS, freq)

        datasets = finalize(datasets, freq, start_date, end_date)
        rows = meta_rows(meta, freq, monthly_gdp_method)
        if all(d.empty for d in datasets.values()):
            reasons = [f"{r['series']}: {r['note']}" for r in rows if str(r["source"]).startswith("Unavailable")]
            raise DataError("Nothing to write: none of the selected series has observations between "
                            f"{start_date:%d/%m/%Y} and {end_date:%d/%m/%Y}."
                            + ("\n\n" + "\n\n".join(reasons[:8]) if reasons else ""))
        bar.progress(0.95, text="Writing the workbook")

        if action == "New workbook":
            book = datasets
            if geography == "Euro-area countries" and "country_equity_index" in selected:
                book = {}
                for geo, x in datasets.items():
                    name = REFERENCE_EQUITY_INDEX.get(geo, "National equity index")
                    book[geo] = x.rename(columns={"country_equity_index": name,
                                                  "country_equity_index_source": f"{name}_source"})
            out = create_workbook(book, freq, panel_format=panel_format, metadata_rows=rows,
                                  geography="US" if is_us else "EA")
            slug = "ea" if geography == "Euro area" else ("us" if is_us else "countries")
            filename = f"macro_{slug}_{freq_label.lower()}.xlsx"
            summary = None
        else:
            out, summary = update_workbook(uploaded.getvalue(), sheet_name, datasets["US" if is_us else "EA"],
                                           frequency=freq, append_new_periods=True)
            filename = f"{uploaded.name.rsplit('.', 1)[0]}_updated.xlsx"

        st.session_state["result"] = {
            "datasets": datasets, "freq": freq, "bytes": out, "filename": filename,
            "meta": rows, "summary": summary, "action": action, "geography": geography,
            "labels": labels, "built": pd.Timestamp.now().strftime("%d %b %Y, %H:%M"),
        }
        bar.progress(1.0, text="Done")
        status.update(label="Workbook ready", state="complete")
    except DataError as exc:
        status.update(label="Download stopped", state="error")
        st.error(f"{mc.redact(exc)}")
        st.caption("Each source is tried with fallbacks before stopping. If one provider is down, "
                   "deselect its series and build again.")
    except Exception as exc:
        status.update(label="Something went wrong", state="error")
        st.error(f"Unexpected {type(exc).__name__}: {mc.redact(exc)}")
        with st.expander("Technical details"):
            st.exception(exc)

# --- Results ----------------------------------------------------------------
res = st.session_state.get("result")
if res:
    rfreq = res["freq"]
    datasets = res["datasets"]
    rlabels = res["labels"]
    first_df = next(iter(datasets.values()))
    noun = "Workbook updated" if res["action"] == "Update a workbook" else "Your workbook"
    st.markdown(f'<div class="result-title">{noun}</div>'
                f'<div class="result-sub">{res["geography"]}, {FREQ_WORD[rfreq]}. Built {res["built"]}.</div>',
                unsafe_allow_html=True)
    st.download_button(f"Download {res['filename']}", data=res["bytes"], file_name=res["filename"],
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       type="primary", width="stretch", icon=":material/download:", key="download")

    meta_df = pd.DataFrame(res["meta"])
    unavailable = meta_df[meta_df["source"].astype(str).str.startswith("Unavailable")] if not meta_df.empty else meta_df
    ends = [d["date"].iloc[-1] for d in datasets.values() if not d.empty]
    latest = max(ends, key=lambda v: mc.label_to_period(v, rfreq) or pd.Period("1900", "Y").asfreq(
        {"M": "M", "Q": "Q", "A": "Y"}[rfreq])) if ends else "None"
    n_rows = sum(len(d) for d in datasets.values())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Latest observation", latest,
              help="Latest period with any data. For daily series the current period can still be in progress.")
    m2.metric("Series", len(value_columns(first_df)))
    m3.metric("Rows", f"{n_rows:,}")
    m4.metric("Unavailable", len(unavailable))
    if len(unavailable):
        st.warning("Some series could not be downloaded; their columns are empty and the source cell says why.\n\n"
                   + "\n".join(f"- **{r.series}** ({r.series_id}): {r.note}" for r in unavailable.itertuples()))

    summary = res.get("summary")
    if summary:
        lines = [f"{summary['updated_cells']:,} cells updated and {summary['added_rows']} rows appended.",
                 "Columns updated: " + (", ".join(summary["recognized_columns"]) or "none") + "."]
        if summary.get("missing_columns"):
            lines.append("Downloaded but not in the sheet (no matching header): "
                         + ", ".join(summary["missing_columns"]) + ".")
        if summary.get("protected_columns"):
            lines.append("Protected, left untouched: " + ", ".join(summary["protected_columns"]) + ".")
        if summary.get("formula_cells_kept"):
            lines.append(f"{summary['formula_cells_kept']} formula cells left untouched.")
        st.info("\n\n".join(lines))
        for w in summary.get("level_warnings", []):
            st.warning(w)

    t_prev, t_chart, t_checks, t_src = st.tabs(["Preview", "Chart", "Checks", "Sources"])

    with t_prev:
        geo = st.selectbox("Sheet", list(datasets), key="preview_geo") if len(datasets) > 1 else next(iter(datasets))
        pdf = datasets[geo]
        st.dataframe(pdf[[c for c in pdf.columns if not str(c).endswith("_source")]],
                     hide_index=True, height=420, width="stretch")
        st.caption("Source columns are hidden here; they sit beside each series in the workbook and are listed "
                   "under Sources.")

    with t_chart:
        @st.fragment
        def chart_panel():
            if len(datasets) > 1:
                cc1, cc2 = st.columns([1, 3], gap="medium")
                with cc1:
                    cgeo = st.selectbox("Sheet", list(datasets), key="chart_geo",
                                        format_func=lambda g: COUNTRY_NAMES.get(g, g))
            else:
                cc2 = st.container()
                cgeo = next(iter(datasets))
            cdf = datasets[cgeo]
            options = value_columns(cdf)
            with cc2:
                picks = st.multiselect("Series (up to six)", options, default=options[:1], max_selections=6,
                                       format_func=lambda c: rlabels.get(c, c), key="chart_vars")
            scale = segmented("Scale", ["Original units", "Index, first common period = 100", "Standardised (z-score)"],
                              "Original units", "chart_scale")
            if not picks:
                st.caption("Choose one or more series to plot.")
                return
            frame, skipped = chart_frame(cdf, picks, rfreq, scale)
            frame = frame.rename(columns=lambda c: rlabels.get(c, c))
            if frame.dropna(how="all").empty:
                st.caption("These series have no overlapping observations to plot.")
                return
            st.line_chart(frame, height=380, width="stretch")
            span = frame.dropna(how="all").index
            st.caption(f"{span.min():%b %Y} to {span.max():%b %Y}. "
                       + ("Series in different units share one axis here; switch the scale to compare their shape. "
                          if scale == "Original units" and len(picks) > 1 else "")
                       + (f"Not indexed (non-positive base value): {', '.join(skipped)}." if skipped else ""))

        chart_panel()

    with t_checks:
        any_issue = False
        for geo, df in datasets.items():
            issues = run_checks(df, rfreq)
            if not issues:
                continue
            any_issue = True
            if len(datasets) > 1:
                st.markdown(f"**{COUNTRY_NAMES.get(geo, geo)}**")
            for kind, msg in issues:
                {"error": st.error, "warning": st.warning, "info": st.info}[kind](msg)
        if not any_issue:
            st.success("No duplicate dates, gaps inside the sample, placeholder zeros or extreme jumps.")
        st.caption("Leading and trailing blanks are normal: series start at different dates and are published "
                   "with different lags. The checks look only inside each series' own span.")

    with t_src:
        if not meta_df.empty:
            show = meta_df[["series", "source", "series_id", "geography", "native_frequency",
                            "latest_observation", "construction"]]
            st.dataframe(show, hide_index=True, width="stretch",
                         column_config={"construction": st.column_config.TextColumn("construction", width="large")})
            st.caption("The same table is saved in the workbook's 'sources' sheet.")

# --- Reference --------------------------------------------------------------
st.markdown('<div style="height:3rem"></div>', unsafe_allow_html=True)
with st.expander("Definitions and sources"):
    st.markdown("""
**Dates.** The From and To dates are applied at the output frequency: 15 March 2001 means 2001-03, 2001q1 or 2001.
Rows before the first and after the last available observation are dropped. Dates are written as text:
`YYYY-MM`, `YYYYqN` or `YYYY`.

**Growth and inflation.** Simple year-on-year percentage changes (12 months, 4 quarters or 1 year), the same
convention as the published HICP and CPI rates. Quarterly and annual inflation are averages of the monthly rates.

**Monthly GDP.** GDP is published quarterly. Chow-Lin estimates the monthly path from industrial production and a
trend by maximum likelihood; the three months of each quarter average exactly to the published level. Linear
interpolation centres each quarter on its middle month. No months are filled beyond the last published quarter.

**Daily series.** Policy rates, money-market rates, yields, spreads and equity indices are averaged over the
observations in each period. ECB policy rates are carried forward between decisions, up to today.

**Source ranking.** When several public sources publish the same concept, the app keeps those that are up to date
(last observation within three months, or one quarter or year, of the freshest), then those within 5% of the longest
history, and among them prefers official statistics and central banks, then international organisations and
official mirrors, then market data vendors. The winner is named in the source column; series are never spliced.

**Frequencies.** A series is offered only at its publication frequency or lower: quarterly national accounts and
quarterly surveys are not interpolated to months. Stocks and levels are averaged over the period, flows (new loans)
are summed, and for the national-accounts, money and lending series a quarter or year is reported only once all of
its months or quarters are published.

**Euro area.** ECB Data Portal and Eurostat; where both publish an equivalent series the one with the longest and
most recent history is used. National equity indices are OECD harmonised share-price indices, labelled with the
familiar market name (DAX, CAC 40, FTSE MIB). No harmonised euro-area 1-year sovereign yield, country-level
high-yield spread or country SPF exists, so those are not offered.

**United States.** BEA real GDP (GDPC1), BLS CPI-U (CPIAUCSL) and unemployment (UNRATE), Federal Reserve
industrial production (INDPRO), federal funds (FEDFUNDS) and Treasury yields (GS1, GS10), FRBNY SOFR, FHFA house
prices (USSTHPI, from 1975), all through FRED.

**National accounts.** Euro area and countries: household and NPISH consumption and gross capital formation in
chain-linked volumes, and the external balance of goods and services at current prices. Eurostat quarterly
accounts, ECB main national accounts and OECD quarterly national accounts compete, and at annual frequency so do
World Bank series (often from the 1960s; annual totals divided by four so they share the quarterly-average scale of
real GDP). Government net lending in % of GDP comes from Eurostat quarterly government finance statistics, with the
EDP notification and the World Bank competing at annual frequency. Reference years and currencies of
chain-linked volumes differ across publishers, which is why sources are never spliced. United States: BEA real PCE (monthly from 2007, quarterly from 1947), real gross private
domestic investment (government investment excluded), NIPA net exports or the monthly BOP trade balance at an annual
rate, and the federal surplus or deficit in % of GDP by fiscal year. FDI is net inflows in % of GDP from the World
Bank, annual everywhere.

**Money and lending.** Euro-area M1, M2 and M3 are ECB outstanding amounts (seasonally adjusted); for countries
they are national contributions, which exclude currency in circulation. New loans to households (house purchase,
consumer credit, other lending) and to firms are ECB new-business volumes, from 2003. The US publishes M1 and M2
(M3 was discontinued in 2006) and no gross new-lending statistic, so US bank loans are H.8 outstanding stocks:
consumer plus residential real-estate loans for households, commercial and industrial loans for firms. M1 includes
savings deposits from May 2020.

**Surveys.** Each survey round is dated by the quarter it was released; annual values average the rounds. US SPF
(Philadelphia Fed) medians: GDP growth, CPI inflation and unemployment four quarters ahead, and ten-year CPI
inflation. Fed dots and SEP: FOMC median projections for the end of this year and next year and the longer run,
rebuilt from FRED/ALFRED vintages (an API key is required except for the longer-run dot). ECB SPF: average point
forecasts at one-year, two-year and longer-term horizons. ECB/Eurosystem staff projections: annual growth and HICP
inflation for this year and next year; country values come from the June and December Eurosystem exercises.

**Credit spread.** One series per economy, chosen automatically: among the candidates that are up to date (last
observation within three months of the freshest), the one with the longest history is used and named in its source
cell. US candidates: Moody's Baa minus 10Y Treasury (from 1953), the Gilchrist-Zakrajšek credit spread (Federal
Reserve Board, from 1973) and the ICE BofA high-yield OAS. They cover different bonds and are never spliced. Since
April 2026 FRED publishes only three years of ICE BofA data; for the euro area that index is the only free candidate.

**Market indices.** S&P 500, Dow Jones, Nasdaq Composite and VIX are taken from Yahoo Finance or FRED, whichever
gives the longer up-to-date history (FRED licenses only ten years of S&P 500 and Dow). MOVE, the ICE BofA index of
US Treasury implied volatility, comes from Yahoo Finance and is offered with the euro-area datasets as the global
rates-volatility gauge. 
""")
    if st.button("Clear downloaded data", help="Forget the downloads kept for six hours and fetch fresh copies next time."):
        mc.clear_download_cache()
        st.toast("Download cache cleared.")
