from __future__ import annotations

import io
import re
from copy import copy
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

VERSION = "26.3"

# Existing-file aliases observed in the user's annual and quarterly workbooks.
# A header is updated only when it matches one of these exactly (after
# lower-casing and replacing spaces with underscores).
ALIASES = {
    "date": ["date", "year", "quarter", "month", "period"],
    "real_gdp": ["real_gdp", "realgdp", "ea_realgdp", "us_realgdp"],
    "real_gdp_growth": ["real_gdp_growth", "gdp_growth", "ea_gdp_growth", "us_gdp_growth"],
    "log_real_gdp": ["log_real_gdp", "gdp_real_log", "log_realgdp"],
    "hicp_level": ["hicp_level", "ea_hicp", "hicp"],
    "hicp_rate": ["hicp_rate", "ea_inflation", "inflation", "inflation_rate"],
    "cpi_level": ["cpi_level", "us_cpi", "cpi"],
    "inflation_rate": ["inflation_rate", "us_inflation", "cpi_inflation", "inflation"],
    "unemployment_rate": ["unemployment_rate", "ea_unemployment", "us_unemployment", "unemployment"],
    "ip": ["ip", "ea_ip", "us_ip", "industrial_production"],
    "log_ip": ["log_ip", "ea_log_ip", "us_log_ip"],
    "energy_index": ["energy_index", "ea_energy_index", "energy"],
    "energy_cpi": ["energy_cpi", "us_energy_cpi"],
    "house_price_index": ["house_price_index", "house_prices", "hpi", "residential_property_price_index"],
    "eurostoxx50": ["eurostoxx50", "euro_stoxx_50", "stoxx50", "sx5e"],
    "country_equity_index": ["country_equity_index", "equity_index", "share_price_index"],
    "sp500": ["sp500", "s&p500", "s_p_500"],
    "credit_spread": ["credit_spread", "corp_spread", "corporate_spread", "hy_spread", "high_yield_spread",
                      "corporate_hy_spread", "hy_oas", "baa_spread"],
    "nasdaq": ["nasdaq", "nasdaq_composite", "ixic"],
    "vix": ["vix", "vixcls"],
    "move": ["move", "move_index"],
    "djia": ["djia", "dow_jones", "dow_jones_industrial_average"],
    # National accounts. Deficit-named headers are deliberately not matched: the
    # series is a balance (surplus positive), so writing it under "deficit" would flip signs.
    "consumption": ["consumption", "private_consumption", "real_consumption", "household_consumption", "pce_real"],
    "gcf": ["gcf", "gross_capital_formation", "investment", "real_investment", "gpdi"],
    "trade_balance": ["trade_balance", "net_exports", "external_balance"],
    "budget_balance": ["budget_balance", "fiscal_balance", "government_balance", "gov_balance", "net_lending"],
    "fdi": ["fdi", "fdi_inflows", "fdi_gdp", "fdi_net_inflows"],
    # Money and lending
    "m1": ["m1", "money_m1", "m1_stock"],
    "m2": ["m2", "money_m2", "m2_stock"],
    "m3": ["m3", "money_m3", "m3_stock"],
    "loans_households": ["loans_households", "household_loans", "hh_loans", "loans_hh", "new_loans_households"],
    "loans_firms": ["loans_firms", "firm_loans", "nfc_loans", "loans_nfc", "business_loans", "new_loans_firms"],
    # Surveys and projections (exact names only)
    "spf_gdp": ["spf_gdp"], "spf_cpi": ["spf_cpi"], "spf_cpi10": ["spf_cpi10"], "spf_unemp": ["spf_unemp"],
    "fed_dots_cy": ["fed_dots_cy"], "fed_dots_ny": ["fed_dots_ny"], "fed_dots_lr": ["fed_dots_lr"],
    "sep_gdp_cy": ["sep_gdp_cy"], "sep_pce_cy": ["sep_pce_cy"], "sep_unemp_cy": ["sep_unemp_cy"],
    "ecb_spf_infl_1y": ["ecb_spf_infl_1y"], "ecb_spf_infl_2y": ["ecb_spf_infl_2y"],
    "ecb_spf_infl_lt": ["ecb_spf_infl_lt"], "ecb_spf_gdp_1y": ["ecb_spf_gdp_1y"], "ecb_spf_gdp_lt": ["ecb_spf_gdp_lt"],
    "ecb_proj_gdp_cy": ["ecb_proj_gdp_cy"], "ecb_proj_gdp_ny": ["ecb_proj_gdp_ny"],
    "ecb_proj_hicp_cy": ["ecb_proj_hicp_cy"], "ecb_proj_hicp_ny": ["ecb_proj_hicp_ny"],
}

# Columns the updater must never overwrite: shocks, forecasts and policy/market
# rate columns the user maintains by hand. Matched on whole underscore-separated
# tokens or prefixes, so "unemployment_rate" or "hicp_rate" are NOT caught
# (the previous substring rule matched "rate" and silently skipped them).
PROTECTED_TOKENS = {"shock", "shocks", "fcst", "fcast", "forecast", "ois", "dfr", "estr", "aaa", "cbi", "surprise"}
PROTECTED_PREFIXES = ("qe_", "fg_", "gpt_", "rr_", "jk_", "de_rate")

LEVEL_SHIFT_THRESHOLD = 0.02   # 2% median gap between old and new values
RATE_LIKE = ("rate", "growth", "inflation", "spread", "yield", "ebp", "dfr", "mro", "estr", "sofr",
             "fed_funds", "treasury", "aaa", "sovereign", "balance", "fdi", "spf", "dots", "sep_", "proj_")


def normalize(x) -> str:
    return str(x).strip().lower().replace(" ", "_") if x is not None else ""


def is_protected_header(header) -> bool:
    h = normalize(header)
    tokens = set(re.split(r"[^a-z0-9]+", h))
    return bool(tokens & PROTECTED_TOKENS) or h.startswith(PROTECTED_PREFIXES)


def workbook_sheet_names(file_bytes: bytes) -> List[str]:
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=False)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _header_map(ws, header_row: int = 1) -> Dict[str, int]:
    out = {}
    for cell in ws[header_row]:
        if cell.value is not None:
            out.setdefault(normalize(cell.value), cell.column)
    return out


def detect_frequency(ws, date_col: Optional[int] = None) -> str:
    headers = _header_map(ws)
    if date_col is None:
        for alias in ALIASES["date"]:
            if alias in headers:
                date_col = headers[alias]
                break
    if date_col is None:
        return "A"
    vals = []
    for row in range(2, min(ws.max_row, 15) + 1):
        v = ws.cell(row, date_col).value
        if v is None:
            continue
        if isinstance(v, datetime):
            vals.append(f"{v.year:04d}-{v.month:02d}")
        else:
            vals.append(str(v))
    joined = " ".join(vals)
    if re.search(r"(?:19|20)\d{2}\s*-?\s*Q[1-4]", joined, re.I):
        return "Q"
    if re.search(r"(?:19|20)\d{2}[-/](?:0?[1-9]|1[0-2])(?!\d)", joined):
        return "M"
    return "A"


def frequency_of_sheet(file_bytes: bytes, sheet_name: str) -> Optional[str]:
    """Best guess of an existing sheet's frequency (M/Q/A), or None if unreadable."""
    try:
        wb = load_workbook(io.BytesIO(file_bytes), read_only=False, data_only=True)
        return detect_frequency(wb[sheet_name])
    except Exception:
        return None


def _date_key(v, freq: str):
    if v is None:
        return None
    if isinstance(v, datetime):
        if freq == "A":
            return v.year
        if freq == "Q":
            return f"{v.year}Q{(v.month - 1) // 3 + 1}"
        return f"{v.year:04d}-{v.month:02d}"
    s = str(v).strip()
    if freq == "A":
        m = re.search(r"(19|20)\d{2}", s)
        return int(m.group(0)) if m else v
    if freq == "Q":
        s = s.upper().replace("-Q", "Q").replace(" ", "")
        m = re.search(r"((?:19|20)\d{2})Q([1-4])", s)
        return f"{m.group(1)}Q{m.group(2)}" if m else s
    m = re.search(r"((?:19|20)\d{2})[-/]?([01]?\d)", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return s[:7]


def _format_date_like(sample, key, freq: str):
    """Write an appended period in the same style as the sheet's existing dates."""
    if key is None:
        return None
    if freq == "A":
        year = int(key)
        return year if (sample is None or isinstance(sample, (int, float))) else str(year)
    if isinstance(sample, datetime):
        if freq == "Q":
            y, q = str(key).split("Q")
            return datetime(int(y), 3 * int(q) - 2, 1)
        y, m = str(key).split("-")
        return datetime(int(y), int(m), 1)
    if freq == "Q":
        y, q = str(key).split("Q")
        s = str(sample or "")
        if re.search(r"\d{4}-Q\d", s, re.I):
            out = f"{y}-Q{q}"
        elif re.search(r"\d{4}\s+Q\d", s, re.I):
            out = f"{y} Q{q}"
        else:
            out = f"{y}Q{q}"
        return out.lower() if re.search(r"\dq\d", s) else out
    y, m = str(key).split("-")
    s = str(sample or "")
    if re.fullmatch(r"\d{4}/\d{2}", s):
        return f"{y}/{m}"
    if re.fullmatch(r"\d{4}m\d{1,2}", s, re.I):
        return f"{y}{'m' if 'm' in s else 'M'}{int(m)}"
    return f"{y}-{m}"


def _copy_row_style(ws, src_row: int, dst_row: int):
    if src_row < 1 or src_row > ws.max_row:
        return
    for col in range(1, ws.max_column + 1):
        src = ws.cell(src_row, col)
        if src.has_style:
            ws.cell(dst_row, col)._style = copy(src._style)


def update_workbook(file_bytes: bytes, sheet_name: str, new_data: pd.DataFrame,
                    frequency: str = "AUTO", date_header: Optional[str] = None,
                    append_new_periods: bool = True,
                    extra_columns: Optional[Sequence[str]] = None) -> Tuple[bytes, dict]:
    """Update only recognized macro columns. Every other column is preserved untouched.

    Safety rules: protected columns (shocks, forecasts, hand-kept rates) are never
    written; cells holding formulas are left alone; a large level gap between the
    old and new values of a column (typically an index rebasing or a different
    unit) is reported so the user can check the series before relying on it.
    """
    wb = load_workbook(io.BytesIO(file_bytes))
    ws = wb[sheet_name]
    headers = _header_map(ws)

    date_col = None
    if date_header:
        date_col = headers.get(normalize(date_header))
    if date_col is None:
        for a in ALIASES["date"]:
            if a in headers:
                date_col = headers[a]
                break
    if date_col is None:
        raise ValueError("Could not find a date column. The first row needs a header named "
                         "date, year, quarter, month or period.")

    freq = detect_frequency(ws, date_col) if frequency == "AUTO" else frequency

    colmap: Dict[str, int] = {}
    for canonical, aliases in ALIASES.items():
        if canonical == "date" or canonical not in new_data.columns:
            continue
        for a in aliases:
            if a in headers and headers[a] not in colmap.values():
                colmap[canonical] = headers[a]
                break

    extra_columns = list(extra_columns or [])
    for name in extra_columns:
        key = normalize(name)
        if key in headers:
            colmap[name] = headers[key]
        else:
            new_col = ws.max_column + 1
            ws.cell(1, new_col).value = name
            src = ws.cell(1, new_col - 1)
            if new_col > 1 and src.has_style:
                ws.cell(1, new_col)._style = copy(src._style)
            headers[key] = new_col
            colmap[name] = new_col

    protected_skipped = sorted(
        normalize(ws.cell(1, col).value) for canonical, col in colmap.items()
        if canonical not in extra_columns and is_protected_header(ws.cell(1, col).value)
    )
    colmap = {k: v for k, v in colmap.items()
              if k in extra_columns or not is_protected_header(ws.cell(1, v).value)}

    existing_rows: Dict[object, int] = {}
    last_date_sample = None
    last_data_row = 1
    for r in range(2, ws.max_row + 1):
        v = ws.cell(r, date_col).value
        k = _date_key(v, freq)
        if k is not None:
            existing_rows[k] = r
            last_date_sample = v
            last_data_row = r

    updated_cells = 0
    added_rows = 0
    formula_cells_kept = 0
    overlap: Dict[str, List[Tuple[float, float]]] = {}
    missing_columns = [c for c in new_data.columns
                       if c != "date" and not str(c).endswith("_source") and c not in colmap]

    for _, rec in new_data.iterrows():
        key = _date_key(rec["date"], freq)
        values = {c: rec[c] for c in colmap if c in rec.index and not pd.isna(rec[c])}
        if key in existing_rows:
            row = existing_rows[key]
        else:
            if not append_new_periods or not values:
                continue
            row = max(ws.max_row, last_data_row) + 1
            _copy_row_style(ws, last_data_row, row)
            ws.cell(row, date_col).value = _format_date_like(last_date_sample, key, freq)
            existing_rows[key] = row
            last_data_row = row
            added_rows += 1
        for canonical, val in values.items():
            cell = ws.cell(row, colmap[canonical])
            old = cell.value
            if isinstance(old, str) and old.startswith("="):
                formula_cells_kept += 1
                continue
            if isinstance(val, (np.floating, float, np.integer, int)) and not isinstance(val, bool):
                val = float(val)
                if isinstance(old, (int, float)) and not isinstance(old, bool) and old != 0:
                    overlap.setdefault(canonical, []).append((float(old), val))
            cell.value = val
            updated_cells += 1

    level_warnings = []
    for canonical, pairs in overlap.items():
        # Only levels and indices can be "rebased"; relative gaps in rates near zero mean nothing.
        if len(pairs) < 4 or any(k in str(canonical).lower() for k in RATE_LIKE):
            continue
        rel = np.array([abs(new / old - 1.0) for old, new in pairs])
        med = float(np.median(rel))
        if med > LEVEL_SHIFT_THRESHOLD:
            level_warnings.append(
                f"{canonical}: new values differ from the old ones by a median {med:.1%} "
                f"over {len(pairs)} overlapping periods. This usually means a rebased index or a "
                "different unit; check the column before mixing old and new rows."
            )

    if "_macro_updater_log" in wb.sheetnames:
        del wb["_macro_updater_log"]
    log = wb.create_sheet("_macro_updater_log")
    log.append(["Field", "Value"])
    log.append(["Updated sheet", sheet_name])
    log.append(["Updated at", datetime.now().strftime("%Y-%m-%d %H:%M")])
    log.append(["Frequency", freq])
    log.append(["Rows appended", added_rows])
    log.append(["Cells updated", updated_cells])
    log.append(["Columns updated", ", ".join(sorted(colmap.keys()))])
    log.append(["Downloaded variables with no matching column", ", ".join(missing_columns)])
    log.append(["Protected columns left untouched", ", ".join(protected_skipped)])
    log.append(["Formula cells left untouched", formula_cells_kept])
    for w in level_warnings:
        log.append(["Level check", w])
    log.sheet_state = "hidden"

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue(), {
        "frequency": freq,
        "updated_cells": updated_cells,
        "added_rows": added_rows,
        "recognized_columns": sorted(colmap.keys()),
        "missing_columns": missing_columns,
        "protected_columns": protected_skipped,
        "formula_cells_kept": formula_cells_kept,
        "level_warnings": level_warnings,
    }


HEADER_FILL = PatternFill("solid", fgColor="0F3B3A")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _cell_value(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)) and np.isnan(v):
        return None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    if isinstance(v, pd.Period):
        return str(v)
    if v is pd.NaT:
        return None
    return v


def _write_frame(ws, df: pd.DataFrame):
    ws.append([str(c) for c in df.columns])
    for row in df.itertuples(index=False, name=None):
        ws.append([_cell_value(v) for v in row])


def _style_sheet(ws, number_format: Optional[str] = None):
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False)
    ws.freeze_panes = "B2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for col in range(1, ws.max_column + 1):
        letter = get_column_letter(col)
        sample = [len(str(ws.cell(r, col).value or "")) for r in range(1, min(ws.max_row, 60) + 1)]
        ws.column_dimensions[letter].width = max(11, min(28, max(sample or [0]) + 2))
        header = str(ws.cell(1, col).value or "")
        if number_format and col > 1 and not header.endswith("_source") and header != "country":
            for r in range(2, ws.max_row + 1):
                c = ws.cell(r, col)
                if isinstance(c.value, float):
                    c.number_format = number_format


_NOTES = {
    "EA": [
        ("Data sources", "ECB Data Portal, Eurostat, OECD and World Bank. Where several publish an equivalent series, "
                         "up-to-date sources are ranked by history length, then reliability; each *_source column "
                         "names the series chosen."),
        ("Inflation", "Published HICP annual rate of change (y/y %). Quarterly and annual values average the monthly rates."),
    ],
    "US": [
        ("Data sources", "BEA, BLS, Federal Reserve, FRBNY, FHFA, OMB and FOMC series distributed through FRED/ALFRED; "
                         "Philadelphia Fed SPF; World Bank FDI; equity indices from Yahoo Finance or FRED, whichever "
                         "is longer and up to date."),
        ("Inflation", "12-month % change of CPI-U (CPIAUCSL). Quarterly and annual values average the monthly rates."),
    ],
}
_COMMON_NOTES = [
    ("Growth rates", "Simple year-on-year percentage changes: 12 months, 4 quarters or 1 year."),
    ("Monthly GDP", "GDP is published quarterly. Monthly values follow the method chosen in the app; "
                    "Chow-Lin and the step method keep each quarter's monthly average equal to the published level."),
    ("Daily series", "Rates, spreads and equity indices are averaged over the observations in each period. "
                     "The latest period may still be in progress."),
    ("Dates", "Monthly YYYY-MM, quarterly YYYYqN, annual YYYY (Stata-friendly text)."),
    ("Surveys", "Each survey round or projection exercise is dated by the quarter it was released; annual values "
                "average the year's rounds."),
    ("Flows and stocks", "New loans are flows summed over the period; money and loan stocks are averaged. National "
                         "accounts, money and lending are reported only for complete quarters and years."),
]


def create_workbook(datasets: Dict[str, pd.DataFrame], frequency: str,
                    panel_format: str = "One sheet per country",
                    metadata_rows: Optional[List[dict]] = None,
                    geography: str = "EA") -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    if panel_format == "Long panel" and len(datasets) > 1:
        pieces = []
        for geo, df in datasets.items():
            x = df.copy()
            x.insert(1, "country", geo)
            pieces.append(x)
        panel = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
        ws = wb.create_sheet("panel")
        _write_frame(ws, panel)
        _style_sheet(ws)
    else:
        for geo, df in datasets.items():
            name = {"EA": "ea_macro", "US": "us_macro"}.get(geo, geo)
            ws = wb.create_sheet(name[:31])
            _write_frame(ws, df)
            _style_sheet(ws)

    if metadata_rows:
        meta = pd.DataFrame(metadata_rows)
        ws = wb.create_sheet("sources")
        _write_frame(ws, meta)
        _style_sheet(ws)
        for col in range(1, ws.max_column + 1):
            ws.column_dimensions[get_column_letter(col)].width = 18
        if "construction" in meta.columns:
            ws.column_dimensions[get_column_letter(list(meta.columns).index("construction") + 1)].width = 90
        if "note" in meta.columns:
            ws.column_dimensions[get_column_letter(list(meta.columns).index("note") + 1)].width = 70

    notes = wb.create_sheet("README")
    region = "US" if geography == "US" else "EA"
    notes.append(["Macro Data Loader output"])
    notes.append(["Created", datetime.now().strftime("%Y-%m-%d %H:%M")])
    notes.append(["Frequency", {"M": "Monthly", "Q": "Quarterly", "A": "Annual"}.get(frequency, frequency)])
    for label, text in _NOTES[region] + _COMMON_NOTES:
        notes.append([label, text])
    notes.append([])
    notes.append(["Source / portal", "URL"])
    links = ([("FRED", "https://fred.stlouisfed.org/"), ("BEA", "https://www.bea.gov/"),
              ("BLS", "https://www.bls.gov/"), ("Federal Reserve H.15", "https://www.federalreserve.gov/releases/h15/")]
             if region == "US" else
             [("ECB Data Portal", "https://data.ecb.europa.eu/"),
              ("Eurostat HICP", "https://ec.europa.eu/eurostat/databrowser/view/prc_hicp_midx/default/table"),
              ("Eurostat unemployment", "https://ec.europa.eu/eurostat/databrowser/view/une_rt_m/default/table"),
              ("Eurostat industrial production", "https://ec.europa.eu/eurostat/databrowser/view/sts_inpr_m/default/table"),
              ("Eurostat real GDP", "https://ec.europa.eu/eurostat/databrowser/view/namq_10_gdp/default/table")])
    for label, url in links:
        notes.append([label, url])
    notes.column_dimensions["A"].width = 24
    notes.column_dimensions["B"].width = 110
    notes["A1"].font = Font(bold=True, size=14)
    for row in notes.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
        if len(row) > 1:
            row[1].alignment = Alignment(wrap_text=True, vertical="top")

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
