from __future__ import annotations

import io
import re
from copy import copy
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Existing-file aliases observed in the user's annual and quarterly workbooks.
ALIASES = {
    "date": ["date", "year", "quarter", "month", "period"],
    "real_gdp": ["real_gdp", "realgdp", "ea_realgdp"],
    "real_gdp_growth": ["real_gdp_growth", "gdp_growth", "ea_gdp_growth"],
    "log_real_gdp": ["log_real_gdp", "gdp_real_log", "log_realgdp"],
    "hicp_level": ["hicp_level", "ea_hicp", "hicp"],
    "hicp_rate": ["hicp_rate", "ea_inflation", "inflation", "inflation_rate"],
    "unemployment_rate": ["unemployment_rate", "ea_unemployment", "unemployment"],
    "ip": ["ip", "ea_ip", "industrial_production"],
    "log_ip": ["log_ip", "ea_log_ip"],
    "energy_index": ["energy_index", "ea_energy_index", "energy"],
}

PROTECTED_HINTS = [
    "shock", "fcst", "fcast", "forecast", "ois", "dfr", "estr", "rate", "aaa", "de_rate",
    "cbi", "qe_", "fg_", "gpt_", "rr_", "jk_",
]


def normalize(x) -> str:
    return str(x).strip().lower().replace(" ", "_") if x is not None else ""


def workbook_sheet_names(file_bytes: bytes) -> List[str]:
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=False)
    return list(wb.sheetnames)


def _header_map(ws, header_row: int = 1) -> Dict[str, int]:
    out = {}
    for cell in ws[header_row]:
        if cell.value is not None:
            out[normalize(cell.value)] = cell.column
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
        if v is not None:
            vals.append(str(v))
    joined = " ".join(vals)
    if re.search(r"\d{4}[-/]?\d{2}", joined):
        return "M"
    if re.search(r"\d{4}\s*Q[1-4]|\d{4}-Q[1-4]", joined, re.I):
        return "Q"
    return "A"


def _date_key(v, freq: str):
    if v is None:
        return None
    s = str(v).strip()
    if freq == "A":
        m = re.search(r"(19|20)\d{2}", s)
        return int(m.group(0)) if m else v
    if freq == "Q":
        s = s.upper().replace("-Q", "Q").replace(" ", "")
        m = re.search(r"((?:19|20)\d{2})Q([1-4])", s)
        return f"{m.group(1)}Q{m.group(2)}" if m else s
    # monthly
    if hasattr(v, "year") and hasattr(v, "month"):
        return f"{v.year:04d}-{v.month:02d}"
    m = re.search(r"((?:19|20)\d{2})[-/]?([01]?\d)", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return s[:7]


def _copy_row_style(ws, src_row: int, dst_row: int):
    if src_row < 1 or src_row > ws.max_row:
        return
    for col in range(1, ws.max_column + 1):
        src = ws.cell(src_row, col)
        dst = ws.cell(dst_row, col)
        if src.has_style:
            dst._style = copy(src._style)
        if src.number_format:
            dst.number_format = src.number_format
        dst.alignment = copy(src.alignment)
        dst.font = copy(src.font)
        dst.fill = copy(src.fill)
        dst.border = copy(src.border)
        dst.protection = copy(src.protection)


def update_workbook(file_bytes: bytes, sheet_name: str, new_data: pd.DataFrame,
                    frequency: str = "AUTO", date_header: Optional[str] = None,
                    append_new_periods: bool = True,
                    extra_columns: Optional[Sequence[str]] = None) -> Tuple[bytes, dict]:
    """Update only recognized macro columns. Every other column is preserved untouched."""
    wb = load_workbook(io.BytesIO(file_bytes))
    ws = wb[sheet_name]
    headers = _header_map(ws)

    # Find date column.
    date_col = None
    if date_header:
        date_col = headers.get(normalize(date_header))
    if date_col is None:
        for a in ALIASES["date"]:
            if a in headers:
                date_col = headers[a]
                break
    if date_col is None:
        raise ValueError("Could not identify the date/year/period column. Choose it explicitly in the UI.")

    freq = detect_frequency(ws, date_col) if frequency == "AUTO" else frequency

    # Map canonical variables to existing columns only. This avoids changing the user's schema.
    colmap: Dict[str, int] = {}
    for canonical, aliases in ALIASES.items():
        if canonical == "date":
            continue
        for a in aliases:
            if a in headers:
                colmap[canonical] = headers[a]
                break

    # User-uploaded shock columns are an explicit exception to the normal
    # "never touch shocks" safety rule. Add them if missing, or update the
    # exact same header if already present.
    extra_columns = list(extra_columns or [])
    for name in extra_columns:
        key = normalize(name)
        if key in headers:
            colmap[name] = headers[key]
        else:
            new_col = ws.max_column + 1
            ws.cell(1, new_col).value = name
            if ws.max_column > 1:
                src = ws.cell(1, new_col - 1)
                dst = ws.cell(1, new_col)
                if src.has_style:
                    dst._style = copy(src._style)
            headers[key] = new_col
            colmap[name] = new_col

    existing_rows = {}
    for r in range(2, ws.max_row + 1):
        k = _date_key(ws.cell(r, date_col).value, freq)
        if k is not None:
            existing_rows[k] = r

    updated_cells = 0
    added_rows = 0
    missing_columns = []
    new_cols = [c for c in new_data.columns if c != "date"]
    for c in new_cols:
        if c not in colmap:
            missing_columns.append(c)

    for _, rec in new_data.iterrows():
        key = _date_key(rec["date"], freq)
        if key in existing_rows:
            row = existing_rows[key]
        else:
            if not append_new_periods:
                continue
            row = ws.max_row + 1
            if ws.max_row >= 2:
                _copy_row_style(ws, ws.max_row, row)
            ws.cell(row, date_col).value = rec["date"]
            existing_rows[key] = row
            added_rows += 1
        for canonical, col in colmap.items():
            if canonical not in rec.index:
                continue
            val = rec[canonical]
            if pd.isna(val):
                continue
            # Explicitly protect suspicious headers even if an alias was accidentally added later.
            h = normalize(ws.cell(1, col).value)
            if any(p in h for p in PROTECTED_HINTS) and canonical not in extra_columns:
                continue
            ws.cell(row, col).value = float(val) if isinstance(val, (int, float)) else val
            updated_cells += 1

    # Keep a small provenance sheet, replacing our own previous one only.
    if "_macro_updater_log" in wb.sheetnames:
        del wb["_macro_updater_log"]
    log = wb.create_sheet("_macro_updater_log")
    log.append(["Field", "Value"])
    log.append(["Updated sheet", sheet_name])
    log.append(["Detected frequency", freq])
    log.append(["Rows appended", added_rows])
    log.append(["Macro cells updated", updated_cells])
    log.append(["Recognized macro columns", ", ".join(sorted(colmap.keys()))])
    log.append(["Available variables not present in target sheet", ", ".join(missing_columns)])
    log.append(["Safety rule", "Existing forecast and rate columns remain protected when updating a workbook."])
    log.sheet_state = "hidden"

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue(), {
        "frequency": freq,
        "updated_cells": updated_cells,
        "added_rows": added_rows,
        "recognized_columns": sorted(colmap.keys()),
        "missing_columns": missing_columns,
    }


def _style_sheet(ws):
    fill = PatternFill("solid", fgColor="17365D")
    font = Font(color="FFFFFF", bold=True)
    for cell in ws[1]:
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in range(1, ws.max_column + 1):
        letter = get_column_letter(col)
        width = max(11, min(22, max(len(str(ws.cell(r, col).value or "")) for r in range(1, min(ws.max_row, 100) + 1)) + 2))
        ws.column_dimensions[letter].width = width


def create_workbook(datasets: Dict[str, pd.DataFrame], frequency: str,
                    panel_format: str = "One sheet per country", metadata_rows: Optional[List[dict]] = None) -> bytes:
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
        ws.append(list(panel.columns))
        for row in panel.itertuples(index=False, name=None):
            ws.append(list(row))
        _style_sheet(ws)
    else:
        for geo, df in datasets.items():
            name = "ea_macro" if geo == "EA" else geo
            ws = wb.create_sheet(name[:31])
            ws.append(list(df.columns))
            for row in df.itertuples(index=False, name=None):
                ws.append([None if pd.isna(v) else v for v in row])
            _style_sheet(ws)

    notes = wb.create_sheet("README")
    notes.append(["EA Macro Updater output"])
    notes.append(["Frequency", {"M": "Monthly", "Q": "Quarterly", "A": "Annual"}.get(frequency, frequency)])
    notes.append(["Data source", "ECB Data Portal and Eurostat; the app selects the equivalent series with the longest available history"])
    notes.append(["GDP note", "GDP is natively quarterly. Monthly output uses the treatment selected in the app and is explicitly documented there."])
    notes.append(["Inflation", "12-month percentage change in HICP; quarterly/annual values are averages of monthly rates."])
    notes.append(["Protected fields", "User-supplied auxiliary series and protected fields are intentionally excluded from newly created macro workbooks."])
    notes.append([])
    notes.append(["Source / portal", "URL"])
    notes.append(["ECB Data Portal", "https://data.ecb.europa.eu/"])
    notes.append(["Eurostat HICP / Energy", "https://ec.europa.eu/eurostat/databrowser/view/prc_hicp_midx/default/table"])
    notes.append(["Unemployment", "https://ec.europa.eu/eurostat/databrowser/view/une_rt_m/default/table"])
    notes.append(["Industrial production", "https://ec.europa.eu/eurostat/databrowser/view/sts_inpr_m/default/table"])
    notes.append(["Real GDP", "https://ec.europa.eu/eurostat/databrowser/view/namq_10_gdp/default/table"])
    notes.column_dimensions["A"].width = 24
    notes.column_dimensions["B"].width = 100
    notes["A1"].font = Font(bold=True, size=14)

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
