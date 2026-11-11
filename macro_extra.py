"""National accounts, money, bank lending and survey series.

Each variable has one or more candidate sources. Candidates are converted to
the output frequency and ranked with ``macro_core.pick_best_source``: up to date
first, then the longest sample, then the more reliable publisher. A variable is
offered only at frequencies where some candidate is published natively at that
frequency or higher (``available``), so quarterly or annual series never appear
in a monthly selection.
"""
from __future__ import annotations

import io
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import macro_core as mc

VERSION = "26.3"
from macro_core import DataError, FetchMeta, redact

# ---------------------------------------------------------------------------
# What exists where
# ---------------------------------------------------------------------------
# Geography kinds: "US", "EA" (euro-area aggregate), "CTRY" (one euro-area member).

AVAILABILITY: Dict[str, Dict[str, str]] = {
    # National accounts
    "consumption":      {"US": "MQA", "EA": "QA", "CTRY": "QA"},
    "gcf":              {"US": "QA",  "EA": "QA", "CTRY": "QA"},
    "trade_balance":    {"US": "MQA", "EA": "QA", "CTRY": "QA"},
    "budget_balance":   {"US": "A",   "EA": "QA", "CTRY": "QA"},
    "fdi":              {"US": "A",   "EA": "A",  "CTRY": "A"},
    # Money and bank lending
    "m1":               {"US": "MQA", "EA": "MQA", "CTRY": "MQA"},
    "m2":               {"US": "MQA", "EA": "MQA", "CTRY": "MQA"},
    "m3":               {"EA": "MQA", "CTRY": "MQA"},
    "loans_households": {"US": "MQA", "EA": "MQA", "CTRY": "MQA"},
    "loans_firms":      {"US": "MQA", "EA": "MQA", "CTRY": "MQA"},
    # Surveys and projections (quarterly releases at most)
    "spf_gdp":          {"US": "QA"},
    "spf_cpi":          {"US": "QA"},
    "spf_cpi10":        {"US": "QA"},
    "spf_unemp":        {"US": "QA"},
    "fed_dots_cy":      {"US": "QA"},
    "fed_dots_ny":      {"US": "QA"},
    "fed_dots_lr":      {"US": "QA"},
    "sep_gdp_cy":       {"US": "QA"},
    "sep_pce_cy":       {"US": "QA"},
    "sep_unemp_cy":     {"US": "QA"},
    "ecb_spf_infl_1y":  {"EA": "QA"},
    "ecb_spf_infl_2y":  {"EA": "QA"},
    "ecb_spf_infl_lt":  {"EA": "QA"},
    "ecb_spf_gdp_1y":   {"EA": "QA"},
    "ecb_spf_gdp_lt":   {"EA": "QA"},
    "ecb_proj_gdp_cy":  {"EA": "QA", "CTRY": "QA"},
    "ecb_proj_gdp_ny":  {"EA": "QA", "CTRY": "QA"},
    "ecb_proj_hicp_cy": {"EA": "QA", "CTRY": "QA"},
    "ecb_proj_hicp_ny": {"EA": "QA", "CTRY": "QA"},
}

NEW_US_VARS = [v for v, a in AVAILABILITY.items() if "US" in a]
NEW_EU_VARS = [v for v, a in AVAILABILITY.items() if "EA" in a or "CTRY" in a]


def available(var: str, kind: str, freq: str) -> bool:
    """True when ``var`` exists for this geography kind at frequency M/Q/A."""
    a = AVAILABILITY.get(var)
    return True if a is None else freq in a.get(kind, "")


LABELS = {
    "consumption": "Private consumption", "gcf": "Gross capital formation",
    "trade_balance": "Trade balance", "budget_balance": "Budget balance (% GDP)",
    "fdi": "FDI inflows (% GDP)", "m1": "M1", "m2": "M2", "m3": "M3",
    "loans_households": "Bank loans to households", "loans_firms": "Bank loans to firms",
    "spf_gdp": "SPF GDP growth, 1Y", "spf_cpi": "SPF CPI inflation, 1Y", "spf_cpi10": "SPF CPI inflation, 10Y",
    "spf_unemp": "SPF unemployment, 1Y", "fed_dots_cy": "Fed dots, this year",
    "fed_dots_ny": "Fed dots, next year", "fed_dots_lr": "Fed dots, longer run",
    "sep_gdp_cy": "SEP GDP growth, this year", "sep_pce_cy": "SEP PCE inflation, this year",
    "sep_unemp_cy": "SEP unemployment, this year",
    "ecb_spf_infl_1y": "ECB SPF inflation, 1Y", "ecb_spf_infl_2y": "ECB SPF inflation, 2Y",
    "ecb_spf_infl_lt": "ECB SPF inflation, long term", "ecb_spf_gdp_1y": "ECB SPF GDP growth, 1Y",
    "ecb_spf_gdp_lt": "ECB SPF GDP growth, long term",
    "ecb_proj_gdp_cy": "Projected GDP growth, this year", "ecb_proj_gdp_ny": "Projected GDP growth, next year",
    "ecb_proj_hicp_cy": "Projected HICP inflation, this year", "ecb_proj_hicp_ny": "Projected HICP inflation, next year",
}

TIER_OFFICIAL, TIER_INTERNATIONAL, TIER_MARKET = mc.TIER_OFFICIAL, mc.TIER_INTERNATIONAL, mc.TIER_MARKET

# One download per resource even when several variables need it at once
# (e.g. Fed dots for this year and next year share one ALFRED request).
_LOCKS: Dict[tuple, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _single_flight(key: tuple) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


_tl = threading.local()


def _thread_clients():
    """Per-thread Eurostat and ECB clients (their HTTP sessions are not shared across threads)."""
    if getattr(_tl, "clients", None) is None:
        _tl.clients = (mc.EurostatClient(), mc.ECBClient())
    return _tl.clients

# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

_PFREQ = {"M": "M", "Q": "Q", "A": "Y"}
_RANK = {"M": 0, "Q": 1, "A": 2, "Y": 2}


def _period_index(labels, native: str) -> pd.PeriodIndex:
    out = []
    for v in labels:
        if isinstance(v, (pd.Timestamp, np.datetime64)) or (hasattr(v, "year") and not isinstance(v, str)):
            out.append(pd.Period(pd.Timestamp(v), freq=_PFREQ[native]))
            continue
        s = str(v).strip().upper().replace(" ", "")
        if native == "Q":
            m = re.match(r"(\d{4})-?Q([1-4])", s)
            out.append(pd.Period(f"{m.group(1)}Q{m.group(2)}", freq="Q") if m else
                       pd.Period(pd.Timestamp(s[:10]), freq="Q"))
        elif native == "M":
            out.append(pd.Period(s[:7], freq="M"))
        else:
            out.append(pd.Period(s[:4], freq="Y"))
    return pd.PeriodIndex(out, freq=_PFREQ[native])


def _series(values, labels, native: str) -> pd.Series:
    s = pd.Series(pd.to_numeric(pd.Series(list(values)), errors="coerce").to_numpy(dtype=float),
                  index=_period_index(labels, native)).dropna()
    return s.groupby(level=0).last().sort_index()


def convert(s: pd.Series, freq: str, how: str = "mean", complete: bool = True) -> pd.Series:
    """Aggregate a Period-indexed series to M/Q/A.

    ``how`` is ``mean`` for levels, rates and stocks, ``sum`` for flows such as
    new loans, ``last`` for survey releases. With ``complete`` a lower-frequency
    period is produced only when all its sub-periods are observed, so a year
    with two published quarters is not reported as an annual value.
    """
    s = s.dropna()
    if s.empty:
        return pd.Series(dtype=float)
    src = s.index.freqstr[0]
    tgt = _PFREQ[freq]
    if _RANK[src] > _RANK[freq]:
        raise DataError("source is published less often than the requested frequency")
    if _RANK[src] == _RANK[freq]:
        return s
    keys = s.index.asfreq(tgt)
    g = s.groupby(keys)
    out = getattr(g, how)()
    if complete and how != "last":
        need = {("M", "Q"): 3, ("M", "Y"): 12, ("Q", "Y"): 4}[(src, tgt)]
        out = out[g.count() >= need]
    return out


def _from_dated(s: pd.Series, native: str) -> pd.Series:
    """DatetimeIndex series to its native period frequency."""
    return _series(s.to_numpy(), s.index, native)


def _cand(label, sid, publisher, series, native, tier=TIER_OFFICIAL, note=""):
    return {"label": label, "sid": sid, "publisher": publisher, "series": series,
            "native": native, "tier": tier, "note": note}


def _best(cands: List[dict], freq: str) -> Tuple[Optional[dict], str]:
    return mc.pick_best_source(cands, freq)


# ---------------------------------------------------------------------------
# Generic external sources
# ---------------------------------------------------------------------------

WORLD_BANK_URL = "https://api.worldbank.org/v2/country/{iso3}/indicator/{indicator}"
WB_ISO3 = {"EA": "EMU", "US": "USA", **mc._OECD_COUNTRY3}


def world_bank(indicator: str, iso3: str) -> pd.Series:
    """Annual World Bank WDI series (cached)."""
    key = ("worldbank", indicator, iso3)
    with _single_flight(key):
        return _world_bank(indicator, iso3, key)


def _world_bank(indicator, iso3, key):
    cached = mc._CACHE.get(key)
    if cached is not None:
        return cached.copy()
    r = mc._http_get(WORLD_BANK_URL.format(iso3=iso3, indicator=indicator),
                     params={"format": "json", "per_page": 20000}, timeout=(6, 30), retries=2, label="World Bank")
    if r.status_code != 200:
        raise DataError(f"World Bank {indicator}/{iso3} returned HTTP {r.status_code}")
    try:
        js = r.json()
    except ValueError:
        raise DataError("World Bank returned a non-JSON response")
    if not isinstance(js, list) or len(js) < 2 or not js[1]:
        msg = js[0].get("message") if isinstance(js, list) and js and isinstance(js[0], dict) else None
        raise DataError(f"World Bank {indicator}/{iso3}: no data" + (f" ({msg})" if msg else ""))
    rows = [x for x in js[1] if x.get("value") is not None]
    s = _series([x["value"] for x in rows], [x["date"] for x in rows], "A")
    if s.empty:
        raise DataError(f"World Bank {indicator}/{iso3}: no observations")
    mc._CACHE.set(key, s)
    return s.copy()


def fred_period(sid: str, key: Optional[str], native: str) -> pd.Series:
    return _from_dated(mc.fred_series(sid, key), native)


def fred_vintages(series_id: str, key: Optional[str]) -> pd.DataFrame:
    """Every ALFRED vintage of a FRED series: realtime_start, realtime_end, date, value."""
    k = mc._resolve_fred_key(key)
    if not k:
        raise DataError("past projections are stored as ALFRED vintages, which need a FRED API key")
    ck = ("alfred", series_id)
    with _single_flight(ck):
        return _fred_vintages(series_id, k, ck)


def _fred_vintages(series_id, k, ck):
    cached = mc._CACHE.get(ck)
    if cached is not None:
        return cached.copy()
    r = mc._http_get(mc.FRED_API_URL,
                     params={"series_id": series_id, "api_key": k, "file_type": "json",
                             "realtime_start": "1776-07-04", "realtime_end": "9999-12-31"},
                     timeout=(6, 40), retries=2, limiter=mc._FRED_API_LIMITER, label="FRED API")
    if r.status_code != 200:
        raise DataError(f"ALFRED {series_id}: HTTP {r.status_code} {mc._fred_error_text(r)}".strip())
    rows = r.json().get("observations") or []
    df = pd.DataFrame(rows)
    if df.empty:
        raise DataError(f"ALFRED {series_id}: no vintages")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    for c in ("realtime_start", "realtime_end", "date"):
        df[c] = pd.to_datetime(df[c].replace("9999-12-31", "2262-04-11"), errors="coerce")
    df = df.dropna(subset=["value", "realtime_start", "date"])
    mc._CACHE.set(ck, df)
    return df.copy()


def projection_by_release(vint: pd.DataFrame, horizon: int) -> pd.Series:
    """Value projected for (release year + horizon), one point per release date."""
    out = {}
    for v in sorted(vint["realtime_start"].unique()):
        live = vint[(vint["realtime_start"] <= v) & (vint["realtime_end"] >= v)]
        hit = live[live["date"].dt.year == pd.Timestamp(v).year + horizon]
        if not hit.empty:
            out[pd.Timestamp(v)] = float(hit["value"].iloc[-1])
    return pd.Series(out, dtype=float).sort_index()


def _releases_to_freq(s: pd.Series, freq: str) -> pd.Series:
    """Release-dated values to quarters (last release in the quarter) or years (mean)."""
    if s.empty:
        return s
    q = _series(s.to_numpy(), s.index, "Q")         # last release per quarter
    return q if freq == "Q" else convert(q, "A", "mean", complete=False)


OECD_QNA_URL = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA,1.1/{key}"
# FREQ.ADJUSTMENT.REF_AREA.SECTOR.COUNTERPART_SECTOR.TRANSACTION.INSTR_ASSET.ACTIVITY.
# EXPENDITURE.UNIT_MEASURE.PRICE_BASE.TRANSFORMATION.TABLE_IDENTIFIER
_OECD_PREFER = {"UNIT_MEASURE": ("XDC", "EUR"), "TRANSFORMATION": ("N",), "EXPENDITURE": ("_T", "_Z"),
                "INSTR_ASSET": ("N1G", "_Z"), "ACTIVITY": ("_T", "_Z"), "COUNTERPART_SECTOR": ("S1",)}


def oecd_qna(iso3: str, sector: str, transaction: str, price_base: str, start_year: int) -> Tuple[pd.Series, str]:
    """One OECD quarterly national-accounts series, seasonally adjusted, national currency (million).

    The key wildcards every dimension except frequency, adjustment, area,
    sector, transaction and price base; the CSV is then narrowed to the
    preferred codes (national currency, non-transformed, totals) and the
    longest remaining series is kept.
    """
    key = ".".join(["Q", "Y", iso3, sector, "", transaction, "", "", "", "", price_base, "", ""])
    ck = ("oecd-qna", key, start_year)
    with _single_flight(ck):
        cached = mc._CACHE.get(ck)
        if cached is not None:
            return cached[0].copy(), cached[1]
        r = mc._http_get(OECD_QNA_URL.format(key=key),
                         params={"startPeriod": f"{start_year - 1}-Q1", "dimensionAtObservation": "AllDimensions",
                                 "format": "csvfile"},
                         headers={"Accept": "text/csv,application/vnd.sdmx.data+csv;q=0.9,*/*;q=0.1"},
                         timeout=(8, 40), retries=2, label=f"OECD QNA {iso3}")
        if r.status_code == 404 or not r.content.strip():
            raise DataError(f"OECD QNA {iso3} {transaction}: no matching series")
        if r.status_code != 200:
            raise DataError(f"OECD QNA {iso3} {transaction} returned HTTP {r.status_code}")
        try:
            df = pd.read_csv(io.BytesIO(r.content), dtype=str)
        except Exception:
            raise DataError(f"OECD QNA {iso3} {transaction}: response could not be parsed")
        cols = {str(c).upper(): c for c in df.columns}
        if "TIME_PERIOD" not in cols or "OBS_VALUE" not in cols:
            raise DataError(f"OECD QNA {iso3} {transaction}: unexpected columns")
        for dim, prefs in _OECD_PREFER.items():
            if dim in cols:
                present = set(df[cols[dim]].dropna())
                keep = next((p for p in prefs if p in present), None)
                if keep is not None:
                    df = df[df[cols[dim]] == keep]
        dims = [cols[c] for c in cols if c not in ("TIME_PERIOD", "OBS_VALUE", "OBS_STATUS", "UNIT_MULT",
                                                   "DECIMALS", "CONF_STATUS", "OBS_STATUS_LABEL")
                and not c.endswith("LABEL") and df[cols[c]].nunique() > 1]
        if dims:
            best = df.groupby(dims)[cols["OBS_VALUE"]].count().idxmax()
            best = best if isinstance(best, tuple) else (best,)
            for d, v in zip(dims, best):
                df = df[df[d] == v]
        s = _series(df[cols["OBS_VALUE"]], df[cols["TIME_PERIOD"]], "Q")
        if "UNIT_MULT" in cols and not df.empty:                 # report in millions
            mult = pd.to_numeric(df[cols["UNIT_MULT"]], errors="coerce").dropna()
            if not mult.empty and int(mult.iloc[0]) != 6:
                s = s * 10.0 ** (int(mult.iloc[0]) - 6)
        if s.empty:
            raise DataError(f"OECD QNA {iso3} {transaction}: no observations")
        order = ["FREQ", "ADJUSTMENT", "REF_AREA", "SECTOR", "COUNTERPART_SECTOR", "TRANSACTION", "INSTR_ASSET",
                 "ACTIVITY", "EXPENDITURE", "UNIT_MEASURE", "PRICE_BASE", "TRANSFORMATION", "TABLE_IDENTIFIER"]
        parts = key.split(".")
        for i, dim in enumerate(order):          # name the exact series kept, not the wildcard query
            if dim in cols and not df.empty and df[cols[dim]].nunique() == 1:
                parts[i] = str(df[cols[dim]].iloc[0])
        sid = "QNA." + ".".join(parts)
        mc._CACHE.set(ck, (s, sid))
        return s.copy(), sid


def ecb_mna(ecb, area: str, key_tail: str, prefer: str = "") -> Tuple[pd.Series, str]:
    """ECB main national accounts (MNA). ``key_tail`` may contain empty (wildcard) positions."""
    df = ecb.frame("MNA", f"Q.Y.{area}.{key_tail}")
    keys = list(df["KEY"].unique())
    if prefer and any(prefer in k for k in keys):
        keys = [k for k in keys if prefer in k]
    counts = df[df["KEY"].isin(keys)].groupby("KEY")["TIME_PERIOD"].count()
    key = counts.idxmax()
    sub = df[df["KEY"] == key]
    s = _series(sub["OBS_VALUE"], sub["TIME_PERIOD"], "Q")
    if s.empty:
        raise DataError(f"ECB {key}: no observations")
    return s, key


# ---------------------------------------------------------------------------
# Philadelphia Fed SPF
# ---------------------------------------------------------------------------

SPF_LEVEL_URLS = [
    "https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/"
    "historical-data/medianLevel.xlsx",
    "https://www.philadelphiafed.org/-/media/frbp/assets/surveys-and-data/survey-of-professional-forecasters/"
    "historical-data/medianlevel.xlsx",
]


def philly_spf_sheets() -> Dict[str, pd.DataFrame]:
    with _single_flight(("philly", "medianLevel")):
        return _philly_spf_sheets()


def _philly_spf_sheets() -> Dict[str, pd.DataFrame]:
    cached = mc._CACHE.get(("philly", "medianLevel"))
    if cached is not None:
        return {k: v.copy() for k, v in cached.items()}
    errors = []
    for url in SPF_LEVEL_URLS:
        try:
            r = mc._http_get(url, timeout=(6, 40), retries=1, label="Philadelphia Fed")
        except DataError as exc:
            errors.append(str(exc))
            continue
        if r.status_code != 200 or r.content[:2] != b"PK":
            errors.append(f"HTTP {r.status_code}" + ("" if r.status_code != 200 else ", not an Excel file"))
            continue
        sheets = pd.read_excel(io.BytesIO(r.content), sheet_name=None)
        sheets = {str(k).strip().upper(): v.rename(columns=lambda c: str(c).strip().upper())
                  for k, v in sheets.items()}
        mc._CACHE.set(("philly", "medianLevel"), sheets)
        return sheets
    raise DataError("Philadelphia Fed SPF file: " + "; ".join(errors))


def _spf_frame(sheets, name: str) -> pd.DataFrame:
    if name not in sheets:
        raise DataError(f"SPF sheet {name} not found (sheets: {', '.join(list(sheets)[:12])})")
    df = sheets[name].copy()
    if "YEAR" not in df.columns or "QUARTER" not in df.columns:
        raise DataError(f"SPF sheet {name} has no YEAR/QUARTER columns")
    df = df[pd.to_numeric(df["YEAR"], errors="coerce").notna() & pd.to_numeric(df["QUARTER"], errors="coerce").notna()]
    df.index = pd.PeriodIndex([f"{int(y)}Q{int(q)}" for y, q in zip(df["YEAR"], df["QUARTER"])], freq="Q")
    return df.apply(pd.to_numeric, errors="coerce")


def spf_series(var: str) -> Tuple[pd.Series, str]:
    sheets = philly_spf_sheets()
    if var == "spf_gdp":
        d = _spf_frame(sheets, "RGDP")
        return (d["RGDP6"] / d["RGDP2"] - 1.0) * 100.0, "RGDP2, RGDP6"
    if var == "spf_cpi":
        d = _spf_frame(sheets, "CPI")
        cols = ["CPI3", "CPI4", "CPI5", "CPI6"]
        grow = np.prod([(1.0 + d[c] / 100.0) ** 0.25 for c in cols], axis=0)
        return pd.Series((grow - 1.0) * 100.0, index=d.index), "CPI3-CPI6"
    if var == "spf_cpi10":
        d = _spf_frame(sheets, "CPI10")
        return d["CPI10"], "CPI10"
    if var == "spf_unemp":
        d = _spf_frame(sheets, "UNEMP")
        return d["UNEMP6"], "UNEMP6"
    raise KeyError(var)


SPF_NOTES = {
    "spf_gdp": "Median forecast of real GDP growth over the next four quarters (level forecast four quarters "
               "ahead relative to the current-quarter forecast), percent; by survey quarter.",
    "spf_cpi": "Median forecast of headline CPI inflation over the next four quarters (compounded quarterly "
               "annualised rates), percent; by survey quarter. Starts 1981Q3.",
    "spf_cpi10": "Median forecast of average CPI inflation over the next ten years, percent; by survey quarter.",
    "spf_unemp": "Median forecast of the unemployment rate four quarters ahead, percent; by survey quarter.",
}

# ---------------------------------------------------------------------------
# ECB SPF and staff projections
# ---------------------------------------------------------------------------

_SEASON_Q = {"W": 1, "G": 2, "S": 3, "A": 4}   # March, June, September, December


def ecb_spf(ecb, topic: str, horizon: str) -> Tuple[pd.Series, str]:
    """Average SPF point forecast by survey quarter.

    ``horizon`` is ``LT`` (longer term) or a rolling horizon in months such as
    ``P12M``. Rolling horizons come from one wildcard request: the exact code
    when published, otherwise the closest rolling horizon within six months.
    """
    if horizon == "LT":
        df = ecb.frame("SPF", f"Q.U2.{topic}.POINT.LT.Q.AVG")
        key = df.groupby("KEY")["TIME_PERIOD"].count().idxmax()
    else:
        target = int(re.sub(r"\D", "", horizon))
        with _single_flight(("ecb-spf", topic)):        # 1Y and 2Y share this request
            df = ecb.frame("SPF", f"Q.U2.{topic}.POINT..Q.AVG")
        codes = {}
        for k in df["KEY"].unique():
            m = re.search(r"\.POINT\.P(\d+)M\.", k)
            if m:
                codes[k] = int(m.group(1))
        near = {k: h for k, h in codes.items() if abs(h - target) <= 6}
        if not near:
            raise DataError(f"ECB SPF {topic}: no rolling horizon near {target} months "
                            f"(found {sorted(set(codes.values()))})")
        key = min(near, key=lambda k: (abs(near[k] - target), near[k]))
    sub = df[df["KEY"] == key]
    s = _series(sub["OBS_VALUE"], sub["TIME_PERIOD"], "Q")
    if s.empty:
        raise DataError(f"ECB SPF {key}: no observations")
    return s, key


def information_set(exercises: Dict[pd.Period, Dict[int, float]], horizon: int, max_carry: int = 3) -> pd.Series:
    """Quarterly series of the latest projection available in each quarter.

    ``exercises`` maps the release quarter of each projection round to its
    {target year: value}. Quarter q shows the most recent round released in or
    before q, for target year q.year + horizon. A round is carried forward for
    at most ``max_carry`` quarters and never beyond the current quarter, so
    country projections (June and December only) leave no gaps and a December
    round correctly supplies "this year" for the following first quarter.
    """
    if not exercises:
        return pd.Series(dtype=float)
    rounds = sorted(exercises)
    now = pd.Period(pd.Timestamp.today(), freq="Q")
    last = min(rounds[-1] + max_carry, now)
    out = {}
    i = 0
    for q in pd.period_range(rounds[0], max(last, rounds[-1]), freq="Q"):
        while i + 1 < len(rounds) and rounds[i + 1] <= q:
            i += 1
        if q.ordinal - rounds[i].ordinal > max_carry:
            continue
        v = exercises[rounds[i]].get(q.year + horizon)
        if v is not None and np.isfinite(v):
            out[q] = float(v)
    return pd.Series(out, dtype=float).sort_index()


def ecb_exercises(ecb, area: str, item: str) -> Tuple[Dict[pd.Period, Dict[int, float]], str]:
    """Every ECB/Eurosystem projection round: release quarter -> {target year: value}."""
    with _single_flight(("ecb-mpd", area, item)):     # this year and next year share this request
        df = ecb.frame("MPD", f"A.{area}.{item}.A..0000")
    out: Dict[pd.Period, Dict[int, float]] = {}
    for key, sub in df.groupby("KEY"):
        m = re.search(r"\.([WGSA])(\d{2})\.0000$", key)
        if not m:
            continue
        yy = int(m.group(2))
        year = 1900 + yy if yy >= 90 else 2000 + yy
        q = pd.Period(f"{year}Q{_SEASON_Q[m.group(1)]}", freq="Q")
        out[q] = {int(str(tp)[:4]): float(v) for tp, v in zip(sub["TIME_PERIOD"], sub["OBS_VALUE"])}
    if not out:
        raise DataError(f"ECB projections {item} for {area}: no exercises found")
    return out, f"MPD.A.{area}.{item}.A.<exercise>.0000"


def ecb_projection(ecb, area: str, item: str, horizon: int) -> Tuple[pd.Series, str]:
    ex, sid = ecb_exercises(ecb, area, item)
    return information_set(ex, horizon), sid


def sep_exercises(vint: pd.DataFrame) -> Dict[pd.Period, Dict[int, float]]:
    """ALFRED vintages of an SEP median: release quarter -> {target year: value}."""
    out: Dict[pd.Period, Dict[int, float]] = {}
    for v in sorted(vint["realtime_start"].unique()):
        live = vint[(vint["realtime_start"] <= v) & (vint["realtime_end"] >= v)]
        out[pd.Period(pd.Timestamp(v), freq="Q")] = {int(d.year): float(x) for d, x in zip(live["date"], live["value"])}
    return out


# ---------------------------------------------------------------------------
# Tasks: one callable per variable, run inside the callers' download pools
# ---------------------------------------------------------------------------
# A task returns (series at the output frequency, source text, FetchMeta) or
# raises DataError explaining why every candidate failed.

Task = Tuple[str, Callable[[], Tuple[pd.Series, str, FetchMeta]]]
_FREQ_WORD = {"M": "monthly", "Q": "quarterly", "A": "annual"}
_NATIVE_WORD = {"MQA": "monthly", "QA": "quarterly", "A": "annual"}


def _make_task(var: str, kind: str, geo_label: str, freq: str, builders: List[Tuple[str, Callable]],
               start_year: Optional[int] = None) -> Task:
    """``start_year``: candidates are compared on the requested window only (a source
    that happens to return earlier history is not 'longer' where it matters)."""
    sid_all = " / ".join(sid for sid, _ in builders)

    def run():
        if not available(var, kind, freq):
            native = _NATIVE_WORD.get(AVAILABILITY.get(var, {}).get(kind, ""), "")
            raise DataError(f"published at {native} frequency at most, not {_FREQ_WORD[freq]}" if native
                            else "not published for this geography")
        cands, errs = [], []
        for sid, build in builders:
            try:
                c = build()
                if c is not None and start_year is not None:
                    c["series"] = c["series"][c["series"].index.year >= start_year]
                if c is not None and not c["series"].dropna().empty:
                    cands.append(c)
            except Exception as exc:
                errs.append(f"{sid}: {redact(exc)}")
        best, summary = _best(cands, freq)
        if best is None:
            raise DataError("; ".join(errs) or "no observations returned")
        s = best["series"]
        note = best["note"] + (f" Compared: {summary}." if ";" in summary else "")
        meta = FetchMeta(LABELS[var], best["publisher"], best["sid"], geo_label, best["native"],
                         str(s.dropna().index.max()), note.strip())
        return s, f"{best['publisher']} · {best['sid']}", meta

    return sid_all, run


def unavailable_meta(var: str, sid: str, geo_label: str, reason: str) -> FetchMeta:
    return FetchMeta(LABELS.get(var, var), "Unavailable", sid, geo_label, "", None, redact(reason))


# ---------------------------------------------------------------------------
# United States
# ---------------------------------------------------------------------------

def us_tasks(vars_set, freq: str, key: Optional[str], start_year: Optional[int] = None) -> Dict[str, Task]:
    """Tasks for the new US variables in ``vars_set``."""
    tasks: Dict[str, Task] = {}

    def add(var, builders):
        if var in vars_set:
            tasks[var] = _make_task(var, "US", "US", freq, builders, start_year)

    def fred(sid, native, how="mean", scale=1.0, label="", note="", tier=TIER_OFFICIAL, publisher="FRED"):
        def build():
            s = fred_period(sid, key, native) * scale
            return _cand(label or publisher, sid, publisher, convert(s, freq, how), native, tier, note)
        return sid, build

    add("consumption", [
        fred("PCECC96", "Q", publisher="BEA via FRED",
             note="Real personal consumption expenditures, billions of chained 2017 dollars, SAAR. "
                  "Annual = average of quarters."),
        fred("PCEC96", "M", publisher="BEA via FRED",
             note="Real personal consumption expenditures, billions of chained 2017 dollars, SAAR, monthly "
                  "(from 2007)."),
    ])
    add("gcf", [fred("GPDIC1", "Q", publisher="BEA via FRED",
                     note="Real gross private domestic investment (fixed investment plus inventories), billions "
                          "of chained 2017 dollars, SAAR. Government investment is not included.")])
    add("trade_balance", [
        fred("NETEXP", "Q", publisher="BEA via FRED",
             note="Net exports of goods and services (NIPA), billions of dollars, SAAR."),
        fred("BOPGSTB", "M", scale=12 / 1000.0, publisher="Census/BEA via FRED",
             note="Trade balance in goods and services (balance-of-payments basis), converted to billions of "
                  "dollars at an annual rate (x12/1000) so it is comparable with the quarterly NIPA series. "
                  "From 1992."),
    ])
    add("budget_balance", [fred("FYFSGDA188S", "A", publisher="OMB/Treasury via FRED",
                                note="Federal surplus (+) or deficit (-), percent of GDP, fiscal years "
                                     "(October to September). Annual only.")])

    def wb_fdi():
        s = world_bank("BX.KLT.DINV.WD.GD.ZS", "USA")
        return _cand("World Bank", "BX.KLT.DINV.WD.GD.ZS", "World Bank", convert(s, freq), "A", TIER_INTERNATIONAL,
                     "Foreign direct investment, net inflows, percent of GDP (World Bank WDI, from IMF "
                     "balance-of-payments data). Annual only.")
    add("fdi", [("BX.KLT.DINV.WD.GD.ZS", wb_fdi)])

    add("m1", [fred("M1SL", "M", publisher="Federal Reserve H.6 via FRED",
                    note="M1 money stock, billions of dollars, seasonally adjusted; period average. Savings "
                         "deposits enter M1 from May 2020, so the level jumps at that date (definitional break).")])
    add("m2", [fred("M2SL", "M", publisher="Federal Reserve H.6 via FRED",
                    note="M2 money stock, billions of dollars, seasonally adjusted; period average.")])
    add("loans_firms", [fred("BUSLOANS", "M", publisher="Federal Reserve H.8 via FRED",
                             note="Commercial and industrial loans outstanding, all commercial banks, billions of "
                                  "dollars, seasonally adjusted; period average. The US publishes no gross "
                                  "new-lending statistic, so this is the outstanding stock.")])

    def hh():
        c = fred_period("CONSUMER", key, "M")
        r = fred_period("RREACBM027SBOG", key, "M")
        s = pd.concat([c, r], axis=1).dropna().sum(axis=1)
        return _cand("H.8", "CONSUMER + RREACBM027SBOG", "Federal Reserve H.8 via FRED", convert(s, freq), "M",
                     TIER_OFFICIAL, "Consumer loans plus residential real-estate loans outstanding, all commercial "
                     "banks, billions of dollars, seasonally adjusted; period average. Outstanding stock: the US "
                     "publishes no gross new-lending statistic.")
    add("loans_households", [("CONSUMER + RREACBM027SBOG", hh)])

    for var in ("spf_gdp", "spf_cpi", "spf_cpi10", "spf_unemp"):
        def build(var=var):
            s, cols = spf_series(var)
            return _cand("Philadelphia Fed SPF", f"SPF medianLevel {cols}", "Federal Reserve Bank of Philadelphia",
                         convert(s.dropna(), freq, "mean", complete=False), "Q", TIER_OFFICIAL,
                         SPF_NOTES[var] + " Annual = average of the year's four surveys.")
        add(var, [("SPF", build)])

    sep = {"fed_dots_cy": ("FEDTARMD", 0, "Median FOMC projection of the federal funds rate at the end of the "
                                          "current year"),
           "fed_dots_ny": ("FEDTARMD", 1, "Median FOMC projection of the federal funds rate at the end of next "
                                          "year"),
           "sep_gdp_cy": ("GDPC1MD", 0, "Median FOMC projection of current-year real GDP growth (Q4 over Q4)"),
           "sep_pce_cy": ("PCECTPIMD", 0, "Median FOMC projection of current-year PCE inflation (Q4 over Q4)"),
           "sep_unemp_cy": ("UNRATEMD", 0, "Median FOMC projection of the Q4 unemployment rate, current year")}
    for var, (sid, h, text) in sep.items():
        def build(sid=sid, h=h, text=text):
            s = information_set(sep_exercises(fred_vintages(sid, key)), h)
            return _cand("FOMC SEP", f"{sid} (ALFRED vintages)", "FOMC via ALFRED",
                         convert(s, freq, "mean", complete=False), "Q", TIER_OFFICIAL,
                         f"{text}, percent. Each quarter shows the latest SEP released by then (four a year), for "
                         "the target year counted from that quarter; annual = average of the four quarters. Rebuilt "
                         "from ALFRED vintages, so it starts with the first SEP FRED stored.")
        add(var, [(sid, build)])

    def lr():
        s = mc.fred_series("FEDTARMDLR", key)
        return _cand("FOMC SEP", "FEDTARMDLR", "FOMC via FRED", _releases_to_freq(s, freq), "Q", TIER_OFFICIAL,
                     "Median FOMC projection of the longer-run federal funds rate, percent; one value per SEP "
                     "release from 2012.")
    add("fed_dots_lr", [("FEDTARMDLR", lr)])
    return tasks


# ---------------------------------------------------------------------------
# Euro area and member states
# ---------------------------------------------------------------------------

def _eurostat_q(client, dataset: str, filters: List[dict], start_year: int, end_year: int) -> pd.Series:
    df = client.first_working(dataset, filters, f"{start_year - 1}-Q1", f"{end_year}-Q4")
    s = mc._time_value(df)
    return _series(s.to_numpy(), s.index, "Q")


def _eurostat_a(client, dataset: str, filters: List[dict], start_year: int, end_year: int) -> pd.Series:
    df = client.first_working(dataset, filters, str(start_year - 1), str(end_year))
    s = mc._time_value(df)
    return _series(s.to_numpy(), s.index, "A")


def _na_filters(geo: str, item: str, units: List[str]) -> List[dict]:
    return ([{"geo": geo, "na_item": item, "unit": u, "s_adj": "SCA"} for u in units]
            + [{"geo": geo, "na_item": item, "unit": units[0]}])


def _ecb_monthly(ecb, flow: str, key: str) -> pd.Series:
    s = ecb.series(flow, key)
    return _series(s.to_numpy(), s.index, "M")


# Partitions of new loans to firms that add up to the total (MIR codes). Amount categories:
# 0 = up to EUR 1 million, 1 = over EUR 1 million, 2 = up to EUR 0.25 million, 3 = EUR 0.25-1 million.
# Initial rate fixation: D up to 3 months, Q 3 months-1 year, F floating and up to 1 year, R 1-3 years,
# S 3-5 years, O 5-10 years, P over 10 years, J over 5 years, I over 1 year.
_AMOUNT_PARTS = [("0", "1"), ("2", "3", "1")]
_FIXATION_PARTS = [("F", "I"), ("F", "R", "S", "J"), ("D", "Q", "R", "S", "J"), ("F", "R", "S", "O", "P"),
                   ("D", "Q", "R", "S", "O", "P")]


def mir_firms_total(ecb, area: str) -> Tuple[pd.Series, str]:
    """Total new loans to firms for one country.

    National MIR data have no total series; they are split by loan size and by
    initial rate fixation. The total is the published total if any, otherwise
    the sum of the size classes, otherwise the sum of the fixation buckets.
    Months where any component is missing are left empty.
    """
    df = ecb.frame("MIR", f"M.{area}.B.A2A..B..2240.EUR.N")
    parts = df["KEY"].str.split(".")
    df = df.assign(fix=parts.str[5], amt=parts.str[7])
    have = {(f, a): g for (f, a), g in df.groupby(["fix", "amt"])}

    def series(f, a):
        g = have[(f, a)]
        return _series(g["OBS_VALUE"], g["TIME_PERIOD"], "M")

    if ("A", "A") in have:
        return series("A", "A"), "published total"
    for combo in _AMOUNT_PARTS:
        if all(("A", a) in have for a in combo):
            s = pd.concat([series("A", a) for a in combo], axis=1).dropna().sum(axis=1)
            return s, "sum of loan-size classes " + "+".join(combo)
    for combo in _FIXATION_PARTS:
        if all((f, "A") in have for f in combo):
            s = pd.concat([series(f, "A") for f in combo], axis=1).dropna().sum(axis=1)
            return s, "sum of rate-fixation buckets " + "+".join(combo)
    raise DataError(f"MIR {area}: no complete breakdown of new loans to firms "
                    f"(published: {sorted(have)[:12]})")


def eu_tasks(geo: str, requested_ea: bool, vars_set, freq: str, start_year: int, end_year: int,
             client=None, ecb=None) -> Dict[str, Task]:
    """Tasks for the new euro-area or member-state variables in ``vars_set``.

    ``geo`` is the Eurostat code (EA20, DE, EL...). ECB keys use U2 for the
    aggregate and GR for Greece.
    """
    kind = "EA" if requested_ea or str(geo).startswith("EA") else "CTRY"
    area = "U2" if kind == "EA" else mc._ecb_country_code(geo)
    tasks: Dict[str, Task] = {}

    def add(var, builders):
        if var in vars_set:
            tasks[var] = _make_task(var, kind, geo, freq, builders, start_year)

    def es():
        return client if client is not None else _thread_clients()[0]

    def ec():
        return ecb if ecb is not None else _thread_clients()[1]

    iso3 = WB_ISO3["EA"] if kind == "EA" else WB_ISO3.get(geo, geo)
    oecd3 = "EA20" if kind == "EA" else mc._OECD_COUNTRY3.get(geo, geo)
    mna_area = {"EA21": "I10", "EA20": "I9", "EA19": "I8"}.get(geo, "I9") if kind == "EA" else area
    annual_note = (" Annual = average of quarters, so annual values are on the same quarterly scale as real GDP.")

    def na(item, units, note):
        def build():
            s = _eurostat_q(es(), "namq_10_gdp", _na_filters(geo, item, units), start_year, end_year)
            return _cand("Eurostat", f"namq_10_gdp {item}", "Eurostat", convert(s, freq), "Q", TIER_OFFICIAL,
                         note + annual_note)
        return f"Eurostat namq_10_gdp {item}", build

    def mna(key_tail, prefer, note):
        def build():
            s, sid = ecb_mna(ec(), mna_area, key_tail, prefer)
            return _cand("ECB", f"MNA.{sid.split('MNA.', 1)[-1]}", "ECB", convert(s, freq), "Q", TIER_OFFICIAL,
                         note + " ECB main national accounts (Eurostat source data)." + annual_note)
        return "ECB MNA", build

    def oecd(sector, transaction, price_base, note):
        def build():
            s, sid = oecd_qna(oecd3, sector, transaction, price_base, start_year)
            return _cand("OECD", sid, "OECD", convert(s, freq), "Q", TIER_INTERNATIONAL,
                         note + " OECD Quarterly National Accounts, national currency." + annual_note)
        return "OECD QNA", build

    def wb_level(indicator, note, scale=1e-6 / 4):
        def build():
            if freq != "A":
                return None                       # annual only
            s = world_bank(indicator, iso3) * scale
            return _cand("World Bank", f"{indicator} ({iso3})", "World Bank", s, "A", TIER_INTERNATIONAL,
                         note + " World Bank WDI annual total in million euro divided by four, so it is on the "
                         "same quarterly-average scale as the other candidates and real GDP.")
        return f"World Bank {indicator}", build

    volumes = ["CLV10_MEUR", "CLV20_MEUR", "CLV15_MEUR"]
    cons = "Household and NPISH final consumption expenditure, chain-linked volumes, million euro, seasonally " \
           "and calendar adjusted."
    add("consumption", [
        na("P31_S14_S15", volumes, cons),
        mna("W0.S1M.S1.D.P31._Z._Z._T.EUR.LR.N", "", cons),
        oecd("S1M", "P31", "LR", "Household and NPISH final consumption expenditure, chain-linked volumes, "
                                 "seasonally adjusted."),
        wb_level("NE.CON.PRVT.KN", "Household and NPISH final consumption expenditure, constant prices."),
    ])
    gcf = "Gross capital formation (fixed investment plus inventories, private and public), chain-linked volumes, " \
          "million euro, seasonally and calendar adjusted."
    add("gcf", [
        na("P5G", volumes, gcf),
        mna("W0.S1.S1.D.P5....EUR.LR.N", ".P5.N1G.", gcf),
        oecd("S1", "P5", "LR", "Gross capital formation, chain-linked volumes, seasonally adjusted."),
        wb_level("NE.GDI.TOTL.KN", "Gross capital formation, constant prices."),
    ])
    trade = "External balance of goods and services (exports minus imports), current prices, million euro, " \
            "seasonally and calendar adjusted."
    add("trade_balance", [
        na("B11", ["CP_MEUR"], trade),
        mna(".S1.S1.B.B11._Z._Z._Z.EUR.V.N", "", trade),
        oecd("S1", "B11", "V", "External balance of goods and services, current prices, seasonally adjusted."),
        wb_level("NE.RSB.GNFS.CN", "External balance on goods and services, current prices."),
    ])

    def gov_q():
        s = _eurostat_q(es(), "gov_10q_ggnfa",
                        [{"geo": geo, "unit": "PC_GDP", "sector": "S13", "na_item": "B9", "s_adj": a}
                         for a in ("SCA", "NSA")], start_year, end_year)
        return _cand("Eurostat quarterly", "gov_10q_ggnfa B9", "Eurostat", convert(s, freq), "Q", TIER_OFFICIAL,
                     "General government net lending (+) / net borrowing (-), percent of GDP, quarterly "
                     "government finance statistics (seasonally adjusted where published). Annual = average of "
                     "quarters.")

    def gov_a():
        if freq != "A":
            return None
        s = _eurostat_a(es(), "gov_10dd_edpt1",
                        [{"geo": geo, "unit": "PC_GDP", "sector": "S13", "na_item": "B9"}], start_year, end_year)
        return _cand("Eurostat EDP", "gov_10dd_edpt1 B9", "Eurostat", s, "A", TIER_OFFICIAL,
                     "General government net lending (+) / net borrowing (-), percent of GDP, annual "
                     "excessive-deficit-procedure notification.")

    def gov_wb():
        if freq != "A":
            return None
        s = world_bank("GC.NLD.TOTL.GD.ZS", iso3)
        return _cand("World Bank", f"GC.NLD.TOTL.GD.ZS ({iso3})", "World Bank", s, "A", TIER_INTERNATIONAL,
                     "Net lending (+) / net borrowing (-), percent of GDP (World Bank WDI, IMF government finance "
                     "statistics; central government for some countries). Annual only.")
    def gov_ecb():
        # ECB government finance statistics: quarterly ratio to GDP, or the four-quarter moving sum
        # ratio (CY) when the quarterly ratio is not published.
        df = ec().frame("GFS", f"Q.N.{area}.W0.S13.S1._Z.B.B9._Z._Z._Z.XDC_R_B1GQ+XDC_R_B1GQ_CY._Z.S.V.N+CY._T")
        counts = df.groupby("KEY")["TIME_PERIOD"].count()
        key_ = sorted(counts.index, key=lambda k: ("_CY." in k, -counts[k]))[0]
        sub = df[df["KEY"] == key_]
        s = _series(sub["OBS_VALUE"], sub["TIME_PERIOD"], "Q")
        rolling = "_CY." in key_
        return _cand("ECB GFS", key_, "ECB", convert(s, freq), "Q", TIER_OFFICIAL,
                     "General government net lending (+) / net borrowing (-), percent of GDP, ECB government "
                     "finance statistics, not seasonally adjusted" +
                     ("; four-quarter moving sum, so each quarter shows the deficit of the past year" if rolling
                      else "") + ". Annual = average of quarters.")
    add("budget_balance", [("Eurostat gov_10q_ggnfa B9", gov_q), ("ECB GFS B9", gov_ecb),
                           ("Eurostat gov_10dd_edpt1 B9", gov_a), ("World Bank GC.NLD.TOTL.GD.ZS", gov_wb)])


    def wb_fdi():
        s = world_bank("BX.KLT.DINV.WD.GD.ZS", iso3)
        return _cand("World Bank", f"BX.KLT.DINV.WD.GD.ZS ({iso3})", "World Bank", convert(s, freq), "A",
                     TIER_INTERNATIONAL, "Foreign direct investment, net inflows, percent of GDP (World Bank WDI, "
                     "from IMF balance-of-payments data). Annual only." +
                     (" The euro-area value includes flows between member states." if kind == "EA" else ""))
    add("fdi", [("BX.KLT.DINV.WD.GD.ZS", wb_fdi)])

    for var, item in (("m1", "M10"), ("m2", "M20"), ("m3", "M30")):
        def build(item=item, var=var):
            if kind == "EA":
                k = f"M.U2.Y.V.{item}.X.1.U2.2300.Z01.E"
                s = _ecb_monthly(ec(), "BSI", k)
                sid = f"BSI.{k}"
                note = (f"Euro-area {var.upper()}, outstanding amounts at month end, million euro, seasonally and "
                        "working-day adjusted; period average.")
            else:
                # National contributions are published under MFIs (U) or MFIs plus central government (V),
                # for the aggregate or its variant excluding currency in circulation (e.g. M30A).
                df = ec().frame("BSI", f"M.{area}..U+V.{item}+{item}A.X.1..2300.Z01.E")
                counts = df.groupby("KEY")["TIME_PERIOD"].count()
                keys = sorted(counts.index, key=lambda k: (f".{area}.Y." not in k, f".{item}." not in k,
                                                           ".V." not in k, -counts[k]))
                sid = keys[0]
                sub = df[df["KEY"] == sid]
                s = _series(sub["OBS_VALUE"], sub["TIME_PERIOD"], "M")
                adj = "seasonally adjusted" if f".{area}.Y." in sid else "not seasonally adjusted"
                note = (f"National contribution to euro-area {var.upper()} (deposits and other components held "
                        f"with resident banks; currency in circulation is not allocated by country), million "
                        f"euro, {adj}; period average of month-end stocks.")
            return _cand("ECB", sid, "ECB", convert(s, freq), "M", TIER_OFFICIAL, note)
        add(var, [(f"BSI {item}", build)])

    def mir(item, sector):
        return _ecb_monthly(ec(), "MIR", f"M.{area}.B.{item}.A.B.A.{sector}.EUR.N")

    def firms():
        if kind == "EA":
            s = mir("A2A", "2240")
            return _cand("ECB", f"MIR.M.{area}.B.A2A.A.B.A.2240.EUR.N", "ECB", convert(s, freq, "sum"), "M",
                         TIER_OFFICIAL, "New business volumes of bank loans to non-financial corporations "
                         "(excluding revolving loans and overdrafts), million euro: monthly flows, summed over "
                         "quarters and years (only complete periods). From January 2003.")
        s, how = mir_firms_total(ec(), area)
        return _cand("ECB", f"MIR.M.{area}.B.A2A.*.B.*.2240.EUR.N", "ECB", convert(s, freq, "sum"), "M",
                     TIER_OFFICIAL, "New business volumes of bank loans to non-financial corporations (excluding "
                     f"revolving loans and overdrafts), million euro: {how}. Monthly flows, summed over quarters "
                     "and years (only complete periods).")
    add("loans_firms", [("MIR A2A", firms)])

    def households():
        parts = {"house purchase": mir("A2C", "2250"), "consumer credit": mir("A2B", "2250")}
        missing = []
        try:
            parts["other lending"] = mir("A2D", "2250")
        except DataError:
            missing.append("other lending")
        s = pd.concat(parts.values(), axis=1).dropna().sum(axis=1)
        return _cand("ECB", f"MIR.M.{area}.B.(A2C+A2B{'+A2D' if not missing else ''}).A.B.A.2250.EUR.N", "ECB",
                     convert(s, freq, "sum"), "M", TIER_OFFICIAL,
                     "New business volumes of bank loans to households (" + ", ".join(parts) + ")" +
                     (f"; {', '.join(missing)} not published for this area" if missing else "") +
                     ", million euro: monthly flows, summed over quarters and years (only complete periods). "
                     "From January 2003.")
    add("loans_households", [("MIR households", households)])

    spf = {"ecb_spf_infl_1y": ("HICP", "P12M", "HICP inflation expected 12 months after the survey"),
           "ecb_spf_infl_2y": ("HICP", "P24M", "HICP inflation expected 24 months after the survey"),
           "ecb_spf_infl_lt": ("HICP", "LT", "Longer-term HICP inflation expectation (about five years ahead)"),
           "ecb_spf_gdp_1y": ("RGDP", "P12M", "Real GDP growth expected about one year after the survey"),
           "ecb_spf_gdp_lt": ("RGDP", "LT", "Longer-term real GDP growth expectation (about five years ahead)")}
    for var, (topic, hz, text) in spf.items():
        def build(topic=topic, hz=hz, text=text):
            s, sid = ecb_spf(ec(), topic, hz)
            return _cand("ECB SPF", sid, "ECB", convert(s, freq, "mean", complete=False), "Q", TIER_OFFICIAL,
                         f"{text}; average point forecast, percent, by survey quarter (annual = average of the "
                         "year's four rounds). Euro area only.")
        add(var, [("SPF", build)])

    proj = {"ecb_proj_gdp_cy": ("YER", 0, "real GDP growth for the current year"),
            "ecb_proj_gdp_ny": ("YER", 1, "real GDP growth for the next year"),
            "ecb_proj_hicp_cy": ("HIC", 0, "HICP inflation for the current year"),
            "ecb_proj_hicp_ny": ("HIC", 1, "HICP inflation for the next year")}
    for var, (item, h, text) in proj.items():
        def build(item=item, h=h, text=text):
            s, sid = ecb_projection(ec(), area, item, h)
            scope = ("ECB and Eurosystem staff exercises, four a year (March, June, September, December)"
                     if kind == "EA" else "Eurosystem June and December exercises (national breakdown)")
            return _cand("ECB/Eurosystem staff projections", sid, "ECB", convert(s, freq, "mean", complete=False),
                         "Q", TIER_OFFICIAL, f"Staff projection of {text}, annual percent change. Each quarter "
                         f"shows the latest exercise published by then ({scope}), for the target year counted "
                         "from that quarter, so a December exercise supplies the next first quarter's 'this "
                         "year'. Annual = average of the four quarters.")
        add(var, [("MPD", build)])
    return tasks


def start_tasks(tasks: Dict[str, Task], workers: int = 3):
    """Submit tasks to a small pool and return a handle for ``collect_tasks``."""
    from concurrent.futures import ThreadPoolExecutor
    if not tasks:
        return None
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(tasks))), thread_name_prefix="extra")
    return pool, {var: pool.submit(fn) for var, (_sid, fn) in tasks.items()}, time.monotonic()


def collect_tasks(handle, tasks: Dict[str, Task], geo_label: str, deadline_seconds: float = 120):
    """Wait (until the deadline, counted from submission) and return (data, sources, meta)."""
    from concurrent.futures import wait as fwait
    data: Dict[str, pd.Series] = {}
    sources: Dict[str, str] = {}
    meta: List[FetchMeta] = []
    if handle is None:
        return data, sources, meta
    pool, futures, t0 = handle
    fwait(list(futures.values()), timeout=max(0.0, deadline_seconds - (time.monotonic() - t0)))
    for var in tasks:                       # stable order
        fut = futures[var]
        if not fut.done():
            absorb_failure(var, tasks[var][0], geo_label,
                           DataError(f"no response within {int(deadline_seconds)}s"), data, sources, meta)
            continue
        try:
            data[var], sources[var], m = fut.result()
            meta.append(m)
        except Exception as exc:
            absorb_failure(var, tasks[var][0], geo_label, exc, data, sources, meta)
    pool.shutdown(wait=False, cancel_futures=True)
    return data, sources, meta


def run_tasks(tasks: Dict[str, Task], geo_label: str, deadline_seconds: float = 120, workers: int = 3):
    return collect_tasks(start_tasks(tasks, workers), tasks, geo_label, deadline_seconds)


def absorb_failure(var, sid, geo_label, exc, data, sources, meta):
    reason = redact(exc)
    data[var] = pd.Series(dtype=float)
    sources[var] = f"Unavailable: {reason[:200]}"
    meta.append(unavailable_meta(var, sid, geo_label, reason))
