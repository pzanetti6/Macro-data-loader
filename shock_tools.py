"""Monetary-policy shock upload helpers (private-edition feature).

The public edition of the app does not expose shock uploads; this module keeps
the tested code path so a private build can re-enable it with one call to
``integrate_shocks``. Nothing here runs unless shocks are passed in.
"""
from __future__ import annotations

import io
import re
from datetime import datetime

import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils.datetime import to_excel

from macro_core import DataError


SHOCK_SHEET_BY_FREQ = {"M": "monthly_freq", "Q": "quarterly_freq", "A": "annual_freq"}

def _shock_period_key(v, freq: str):
    """
    Normalize heterogeneous shock dates to the selected macro frequency.

    Important rule: slash-formatted dates such as 01/03/99 are interpreted
    day-first (DD/MM/YY), which is the convention used in the supplied European
    shock workbooks. Thus 01/03/99 -> 1999-03, not 1999-01.
    """
    if v is None or (isinstance(v, float) and np.isnan(v)):
        raise ValueError("blank date")

    def _from_timestamp(d):
        d = pd.Timestamp(d)
        if freq == "A":
            return f"{d.year:04d}"
        if freq == "Q":
            return f"{d.year:04d}q{((d.month - 1)//3)+1}"
        return f"{d.year:04d}-{d.month:02d}"

    # Excel/openpyxl dates arrive as datetime objects: never stringify/reparse them.
    if isinstance(v, (datetime, pd.Timestamp)):
        return _from_timestamp(v)

    # Excel serial date fallback.
    if isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool):
        # Four-digit year cells are valid annual dates.
        if freq == "A" and 1900 <= int(v) <= 2100 and float(v).is_integer():
            return f"{int(v):04d}"
        d = pd.Timestamp("1899-12-30") + pd.to_timedelta(float(v), unit="D")
        return _from_timestamp(d)

    s = str(v).strip()
    if not s:
        raise ValueError("blank date")

    # Explicit annual representation.
    if freq == "A" and re.fullmatch(r"(?:19|20)\d{2}(?:\.0)?", s):
        return s[:4]

    # Explicit quarter representations: 2024Q1, 2024-Q1, Q1 2024, etc.
    if freq == "Q":
        compact = re.sub(r"[\s_\-/.]", "", s).upper()
        m = re.fullmatch(r"((?:19|20)\d{2})Q([1-4])", compact)
        if m:
            return f"{m.group(1)}q{m.group(2)}"
        m = re.fullmatch(r"Q([1-4])((?:19|20)\d{2})", compact)
        if m:
            return f"{m.group(2)}q{m.group(1)}"

    # Explicit year-month forms are unambiguous.
    ym = re.fullmatch(r"((?:19|20)\d{2})[-/._](0?[1-9]|1[0-2])", s)
    if ym:
        d = pd.Timestamp(year=int(ym.group(1)), month=int(ym.group(2)), day=1)
        return _from_timestamp(d)
    ym2 = re.fullmatch(r"((?:19|20)\d{2})(0[1-9]|1[0-2])", s)
    if ym2:
        d = pd.Timestamp(year=int(ym2.group(1)), month=int(ym2.group(2)), day=1)
        return _from_timestamp(d)

    # European slash/dash full dates: DD/MM/YYYY or DD/MM/YY.
    # This specifically fixes 01/mm/yy shock sheets.
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", s):
        try:
            return _from_timestamp(pd.to_datetime(s, dayfirst=True, errors="raise"))
        except Exception:
            pass

    # ISO-like full dates YYYY-MM-DD / YYYY/MM/DD.
    if re.fullmatch(r"(?:19|20)\d{2}[/-]\d{1,2}[/-]\d{1,2}", s):
        try:
            return _from_timestamp(pd.to_datetime(s, yearfirst=True, errors="raise"))
        except Exception:
            pass

    # Month-name and other textual dates. Prefer European day-first interpretation.
    try:
        return _from_timestamp(pd.to_datetime(s, dayfirst=True, errors="raise"))
    except Exception:
        raise ValueError(f"'{v}' cannot be interpreted as a { {'M':'monthly','Q':'quarterly','A':'annual'}[freq] } date")


def _macro_period_key(v, freq: str):
    """Return canonical output dates: YYYY-MM, YYYYqQ, or YYYY."""
    if isinstance(v, pd.Period):
        if freq == "A":
            return f"{v.year:04d}"
        if freq == "Q":
            return f"{v.year:04d}q{v.quarter}"
        return f"{v.year:04d}-{v.month:02d}"
    if isinstance(v, (datetime, pd.Timestamp)):
        if freq == "A":
            return f"{v.year:04d}"
        if freq == "Q":
            return f"{v.year:04d}q{((v.month-1)//3)+1}"
        return f"{v.year:04d}-{v.month:02d}"
    return _shock_period_key(v, freq)

def _canonicalize_dataset_dates(datasets, freq: str):
    """Force the final Excel date column into one Stata-friendly text convention."""
    out={}
    for geo, df in datasets.items():
        x=df.copy()
        date_col="date" if "date" in x.columns else ("period" if "period" in x.columns else x.columns[0])
        x[date_col]=x[date_col].map(lambda v:_macro_period_key(v,freq))
        out[geo]=x
    return out


def _read_shock_sheet(file_bytes: bytes, freq: str):
    """
    Read the requested frequency sheet. If it is absent but a meeting-level
    sheet exists, aggregate meeting shocks by SUM into the requested frequency.
    """
    wanted = SHOCK_SHEET_BY_FREQ[freq]
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)

    # Accept common sheet-name variants, case-insensitively.
    # Examples: monthly_freq, monthly, month; quarterly_freq, quarterly, quarter;
    # annual_freq, annual, yearly, year.
    aliases = {
        "M": ["monthly_freq", "monthly", "month", "monthly frequency", "monthly-frequency"],
        "Q": ["quarterly_freq", "quarterly", "quarter", "quarterly frequency", "quarterly-frequency"],
        "A": ["annual_freq", "annual", "yearly", "year", "annual frequency", "annual-frequency"],
    }
    def _norm_sheet_name(s):
        return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")

    normalized_sheets = {_norm_sheet_name(s): s for s in wb.sheetnames}
    matched_sheet = None
    for alias in aliases[freq]:
        key = _norm_sheet_name(alias)
        if key in normalized_sheets:
            matched_sheet = normalized_sheets[key]
            break

    def _sheet_to_df(ws):
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            raise DataError(f"Shock sheet '{ws.title}' is empty.")
        headers = [str(x).strip() if x is not None else "" for x in rows[0]]
        date_candidates = [
            i for i,h in enumerate(headers)
            if h.lower() in ("date","meetingdate","meeting_date","year","period","quarter","month")
        ]
        date_idx = date_candidates[0] if date_candidates else 0
        if not headers[date_idx]:
            headers[date_idx] = "date"
        data = pd.DataFrame(rows[1:], columns=headers).dropna(how="all")
        return data, headers[date_idx]

    if matched_sheet is not None:
        data, date_col = _sheet_to_df(wb[matched_sheet])
        return data, date_col, matched_sheet

    # Flexible fallback for raw meeting-level shock workbooks.
    meeting_aliases = {"meeting", "meetings", "meeting_freq", "meeting_level", "meeting_frequency"}
    meeting_names = [s for s in wb.sheetnames if _norm_sheet_name(s) in meeting_aliases]
    if not meeting_names:
        raise DataError(
            f"Shock workbook has no recognized { {'M':'monthly','Q':'quarterly','A':'annual'}[freq] } sheet "
            f"(accepted examples: {', '.join(aliases[freq])}) and no meeting-level sheet from which "
            "the requested frequency can be constructed."
        )

    meeting_name = meeting_names[0]
    raw, date_col = _sheet_to_df(wb[meeting_name])

    # Convert meeting dates flexibly. Excel/openpyxl normally gives datetime objects;
    # text dates are handled by pandas.
    parsed_dates = []
    bad_dates = []
    for v in raw[date_col].tolist():
        try:
            if isinstance(v, (datetime, pd.Timestamp)):
                d = pd.Timestamp(v)
            elif isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool):
                # Excel serial date fallback.
                d = pd.Timestamp("1899-12-30") + pd.to_timedelta(float(v), unit="D")
            else:
                d = pd.to_datetime(str(v).strip(), errors="raise")
            parsed_dates.append(d)
        except Exception:
            parsed_dates.append(pd.NaT)
            bad_dates.append(str(v))
    if bad_dates:
        raise DataError(
            f"Could not interpret meeting dates in '{meeting_name}'. Examples: "
            + ", ".join(bad_dates[:3])
        )

    # Identify usable value columns. Metadata such as Year/Month/source/rates are not
    # treated as shocks by default; shock/surprise-named columns are preferred.
    candidate_cols = [c for c in raw.columns if c != date_col and str(c).strip()]
    preferred = [
        c for c in candidate_cols
        if any(k in str(c).lower() for k in ("shock","surprise"))
    ]
    value_cols = preferred if preferred else candidate_cols

    converted = {}
    for c in value_cols:
        vals = raw[c].map(_shock_numeric)
        # Keep a column only if every nonblank observation is numeric and at least one exists.
        bad = raw[c].notna() & vals.isna()
        if not bad.any() and vals.notna().any():
            converted[c] = vals.astype(float)
    if not converted:
        raise DataError(
            f"No numeric shock/surprise columns could be identified in meeting-level sheet '{meeting_name}'."
        )

    temp = pd.DataFrame({"_meeting_date": parsed_dates, **converted})
    if freq == "M":
        temp["date"] = temp["_meeting_date"].map(lambda d: f"{d.year:04d}-{d.month:02d}")
    elif freq == "Q":
        temp["date"] = temp["_meeting_date"].map(lambda d: f"{d.year:04d}q{((d.month-1)//3)+1}")
    else:
        temp["date"] = temp["_meeting_date"].map(lambda d: f"{d.year:04d}")

    # Monetary-policy meeting shocks aggregate by summation within the lower frequency.
    agg = temp.groupby("date", as_index=False)[list(converted.keys())].sum(min_count=1)
    return agg, "date", f"{meeting_name} → derived {wanted}"


def _shock_numeric(v):
    """Recover numeric shock values even when Excel mistakenly formats them as dates/times."""
    if v is None:
        return np.nan
    if isinstance(v, (datetime, pd.Timestamp)):
        return float(to_excel(v))
    if isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).strip())
    except Exception:
        return np.nan

def _validate_and_prepare_shocks(file_bytes: bytes, freq: str, value_columns, macro_dates):
    raw, date_col, sheet = _read_shock_sheet(file_bytes, freq)
    if not value_columns:
        raise DataError("Select at least one shock series from the uploaded shock workbook.")

    keys=[]
    bad_dates=[]
    for v in raw[date_col].tolist():
        try:
            keys.append(_shock_period_key(v, freq))
        except Exception as exc:
            keys.append(None)
            bad_dates.append(str(exc))
    if bad_dates:
        sample="; ".join(bad_dates[:3])
        raise DataError(f"Shock dates do not match the selected {freq} frequency. {sample}")

    out=pd.DataFrame({"_period_key":keys})
    if out["_period_key"].duplicated().any():
        dups=out.loc[out["_period_key"].duplicated(False),"_period_key"].unique()[:5]
        raise DataError(
            "Shock file contains duplicate periods after date normalization: "
            + ", ".join(map(str, dups))
            + ". Check whether the source really has multiple observations in the same period."
        )

    for c in value_columns:
        if c == date_col or c not in raw.columns:
            continue
        vals=raw[c].map(_shock_numeric)
        bad=raw[c].notna() & vals.isna()
        if bad.any():
            examples=", ".join(map(str, raw.loc[bad,c].head(3).tolist()))
            raise DataError(f"Shock column '{c}' contains non-numeric values: {examples}")
        out[c]=vals.astype(float)

    macro_keys=[_macro_period_key(v,freq) for v in macro_dates]
    macro_set=set(macro_keys)
    shock_set=set(out["_period_key"].dropna())
    outside=sorted(shock_set-macro_set)
    if outside:
        sample=", ".join(outside[:6])
        raise DataError(
            f"Shock dates do not align with the selected macro sample ({macro_keys[0]} to {macro_keys[-1]}). "
            f"Outside dates include: {sample}"
        )

    # Partial coverage is legitimate (e.g. a shock series ending before the macro data).
    aligned=pd.DataFrame({"_period_key":macro_keys}).merge(out,on="_period_key",how="left")

    # GPT/ChatGPT meeting shocks are defined as zero in periods with no meeting.
    # Fill only BETWEEN the first and last observed period of that uploaded
    # series; do not invent zeros before the series begins or after it ends.
    shock_period_order = {k:i for i,k in enumerate(macro_keys)}
    observed_keys = [k for k in out["_period_key"].dropna() if k in shock_period_order]
    if observed_keys:
        lo = min(shock_period_order[k] for k in observed_keys)
        hi = max(shock_period_order[k] for k in observed_keys)
        inside = aligned.index.to_series().between(lo, hi)
        for c in value_columns:
            cname = str(c).lower().replace("_","").replace("-","")
            if c in aligned.columns and ("gpt" in cname or "chatgpt" in cname):
                aligned.loc[inside & aligned[c].isna(), c] = 0.0

    coverage={c:int(aligned[c].notna().sum()) for c in value_columns if c in aligned.columns}
    return aligned, coverage, sheet

def _attach_shocks(datasets, shock_aligned, shock_columns, freq):
    result={}
    for geo,df in datasets.items():
        x=df.copy()
        date_col="date" if "date" in x.columns else ("period" if "period" in x.columns else x.columns[0])
        x["_period_key"]=x[date_col].map(lambda v:_macro_period_key(v,freq))
        add=shock_aligned[["_period_key"]+[c for c in shock_columns if c in shock_aligned.columns]]
        x=x.merge(add,on="_period_key",how="left").drop(columns="_period_key")
        result[geo]=x
    return result


def integrate_shocks(datasets, shock_file_previews, freq):
    """Attach every value column of every uploaded shock workbook to each dataset.

    ``shock_file_previews`` holds tuples (uploaded_file, preview_df, date_col,
    sheet, value_columns) as built by the private edition's upload widget.
    Returns (datasets, shock_columns, coverage).
    """
    if not shock_file_previews:
        return datasets, [], {}
    all_names = [str(c) for *_, cols in shock_file_previews for c in cols]
    dups = sorted({c for c in all_names if all_names.count(c) > 1})
    if dups:
        raise DataError("Duplicate shock column names across uploaded workbooks: " + ", ".join(dups)
                        + ". Rename them so every imported shock series has a unique header.")
    base_df = next(iter(datasets.values()))
    base_date_col = "date" if "date" in base_df.columns else base_df.columns[0]
    macro_dates = base_df[base_date_col].tolist()
    combined = pd.DataFrame({"_period_key": [_macro_period_key(v, freq) for v in macro_dates]})
    shock_columns, coverage = [], {}
    for sf, _preview, _date_col, _sheet, value_cols in shock_file_previews:
        if not value_cols:
            continue
        aligned, cov, _used = _validate_and_prepare_shocks(sf.getvalue(), freq, value_cols, macro_dates)
        combined = combined.merge(aligned[["_period_key"] + list(value_cols)], on="_period_key",
                                  how="left", validate="one_to_one")
        shock_columns.extend(value_cols)
        for c, n in cov.items():
            coverage[f"{sf.name} · {c}"] = n
    return _attach_shocks(datasets, combined, shock_columns, freq), shock_columns, coverage
