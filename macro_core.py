from __future__ import annotations

import io
import itertools
import math
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests
import time

EUROSTAT_STATS = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
ECB_DATA = "https://data-api.ecb.europa.eu/service/data"

EA19 = ["AT", "BE", "CY", "DE", "EE", "EL", "ES", "FI", "FR", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PT", "SI", "SK"]
COUNTRY_NAMES = {
    "AT": "Austria", "BE": "Belgium", "CY": "Cyprus", "DE": "Germany", "EE": "Estonia",
    "EL": "Greece", "ES": "Spain", "FI": "Finland", "FR": "France", "IE": "Ireland",
    "IT": "Italy", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "MT": "Malta",
    "NL": "Netherlands", "PT": "Portugal", "SI": "Slovenia", "SK": "Slovakia",
}

# Euro area aggregate codes have changed as membership expanded. EA20 is used as the aggregate
# default for historical consistency with most recent Eurostat dissemination. Country mode avoids
# this composition issue entirely.
EA_GEO_CANDIDATES = ["EA21", "EA20", "EA19"]

CANONICAL_VARS = [
    "real_gdp", "real_gdp_growth", "log_real_gdp", "hicp_rate", "hicp_level",
    "unemployment_rate", "ip", "log_ip", "energy_index",
    "estr", "dfr", "mro", "aaa_1y", "aaa_10y",
    "sovereign_1y", "sovereign_10y",
]

US_VARS = [
    "real_gdp", "real_gdp_growth", "log_real_gdp",
    "cpi_level", "inflation_rate", "unemployment_rate",
    "ip", "log_ip", "energy_cpi",
    "fed_funds", "sofr", "treasury_1y", "treasury_10y",
]

SOURCE_URLS = {
    "hicp": "https://ec.europa.eu/eurostat/databrowser/view/prc_hicp_midx/default/table",
    "unemployment": "https://ec.europa.eu/eurostat/databrowser/view/une_rt_m/default/table",
    "ip": "https://ec.europa.eu/eurostat/databrowser/view/sts_inpr_m/default/table",
    "gdp": "https://ec.europa.eu/eurostat/databrowser/view/namq_10_gdp/default/table",
}


class DataError(RuntimeError):
    pass


@dataclass
class FetchMeta:
    variable: str
    source: str
    dataset: str
    geography: str
    native_frequency: str
    latest_period: Optional[str]
    note: str = ""


def _decode_jsonstat(js: dict) -> pd.DataFrame:
    """Decode Eurostat JSON-stat 2.0 response to a tidy DataFrame."""
    ids = js.get("id", [])
    sizes = js.get("size", [])
    dims = js.get("dimension", {})
    values = js.get("value", {})
    if not ids or not sizes or not values:
        return pd.DataFrame(columns=list(ids) + ["value"])

    ordered_codes: List[List[str]] = []
    for dim in ids:
        idx = dims.get(dim, {}).get("category", {}).get("index", {})
        if isinstance(idx, dict):
            codes = [k for k, _ in sorted(idx.items(), key=lambda kv: kv[1])]
        elif isinstance(idx, list):
            codes = list(idx)
        else:
            codes = []
        ordered_codes.append(codes)

    rows = []
    for flat_key, val in values.items():
        try:
            flat = int(flat_key)
        except Exception:
            continue
        coords = []
        rem = flat
        # JSON-stat uses row-major flattening; unwind from last dimension.
        for size in reversed(sizes):
            coords.append(rem % size)
            rem //= size
        coords = list(reversed(coords))
        row = {}
        valid = True
        for dim, codes, pos in zip(ids, ordered_codes, coords):
            if pos >= len(codes):
                valid = False
                break
            row[dim] = codes[pos]
        if valid:
            row["value"] = val
            rows.append(row)
    return pd.DataFrame(rows)


class EurostatClient:
    def __init__(self, timeout: int = 45):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "EA-Macro-Updater/1.0"})

    def query(self, dataset: str, filters: Dict[str, object], start: Optional[str] = None,
              end: Optional[str] = None) -> pd.DataFrame:
        params: List[Tuple[str, str]] = [("lang", "en")]
        for k, v in filters.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                params.extend((k, str(x)) for x in v)
            else:
                params.append((k, str(v)))
        if start:
            params.append(("sinceTimePeriod", start))
        if end:
            params.append(("untilTimePeriod", end))
        url = f"{EUROSTAT_STATS}/{dataset}"
        r = self.session.get(url, params=params, timeout=self.timeout)
        if r.status_code != 200:
            raise DataError(f"Eurostat {dataset} returned HTTP {r.status_code}: {r.text[:240]}")
        js = r.json()
        if js.get("error"):
            raise DataError(f"Eurostat {dataset}: {js['error']}")
        return _decode_jsonstat(js)

    def first_working(self, dataset: str, filter_candidates: Sequence[Dict[str, object]],
                      start: Optional[str], end: Optional[str]) -> pd.DataFrame:
        errors = []
        for filt in filter_candidates:
            try:
                out = self.query(dataset, filt, start=start, end=end)
                if not out.empty:
                    return out
            except Exception as exc:
                errors.append(str(exc))
        msg = errors[-1] if errors else "No observations returned."
        raise DataError(f"Could not retrieve {dataset}. Last error: {msg}")



class ECBClient:
    """Small ECB Data Portal SDMX-CSV client."""
    def __init__(self, timeout: int = 45):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "EA-Macro-Updater/1.0"})

    def series(self, flow: str, key: str, start: Optional[str] = None,
               end: Optional[str] = None) -> pd.Series:
        params = {"format": "csvdata", "detail": "dataonly"}
        if start:
            params["startPeriod"] = start
        if end:
            params["endPeriod"] = end
        url = f"{ECB_DATA}/{flow}/{key}"
        # ECB's Data Portal occasionally returns transient gateway errors (especially
        # when several country series are requested one after another). Retry with
        # exponential backoff instead of aborting the whole multi-country download.
        transient = {429, 500, 502, 503, 504}
        r = None
        for attempt in range(5):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout,
                                     headers={"Accept": "text/csv"})
            except requests.RequestException as exc:
                if attempt == 4:
                    raise DataError(f"ECB {flow}/{key}: network error after 5 attempts: {exc}")
                time.sleep(1.5 * (2 ** attempt))
                continue
            if r.status_code == 200:
                break
            if r.status_code not in transient or attempt == 4:
                raise DataError(f"ECB {flow}/{key} returned HTTP {r.status_code}: {r.text[:120]}")
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 1.5 * (2 ** attempt)
            except (TypeError, ValueError):
                wait = 1.5 * (2 ** attempt)
            time.sleep(min(wait, 20))
        if r is None or r.status_code != 200:
            raise DataError(f"ECB {flow}/{key}: request failed after retries.")
        try:
            df = pd.read_csv(io.StringIO(r.text))
        except Exception as exc:
            raise DataError(f"ECB {flow}/{key}: could not parse CSV: {exc}")
        if df.empty or "OBS_VALUE" not in df.columns or "TIME_PERIOD" not in df.columns:
            raise DataError(f"ECB {flow}/{key}: no observations returned.")
        s = pd.Series(pd.to_numeric(df["OBS_VALUE"], errors="coerce").values,
                      index=df["TIME_PERIOD"].astype(str).values).dropna()
        if s.empty:
            raise DataError(f"ECB {flow}/{key}: no numeric observations returned.")
        return s[~s.index.duplicated(keep="last")].sort_index()


def _ecb_area(geo: str, evolving_ea: bool = False) -> str:
    """ECB reference-area code. U2 is the evolving/default euro-area aggregate."""
    if evolving_ea:
        return "U2"
    return {"EA21": "I10", "EA20": "I9", "EA19": "I8"}.get(geo, geo)


def _source_span(s: pd.Series) -> Tuple[Optional[pd.Period], Optional[pd.Period], int]:
    x = s.dropna()
    if x.empty:
        return None, None, 0
    idx = x.index
    return idx.min(), idx.max(), int(x.shape[0])


def _choose_longest(candidates: Sequence[Tuple[str, pd.Series, str]]) -> Tuple[str, pd.Series, str]:
    """Choose the longest and most up-to-date equivalent series.

    Rank by calendar span first, latest endpoint second, observation count third.
    This prevents a slightly older/staler series from winning merely because it
    starts a little earlier. ECB is only an exact-tie breaker.
    """
    good = [(src, s.dropna(), sid) for src, s, sid in candidates if s is not None and not s.dropna().empty]
    if not good:
        raise DataError("Neither ECB Data Portal nor Eurostat returned observations.")
    def rank(item):
        src, s, sid = item
        first, last, n = _source_span(s)
        span = last.ordinal - first.ordinal
        # Primary: longest calendar span. Secondary: most up-to-date endpoint.
        # Then prefer more actual observations; ECB only breaks a complete tie.
        return (-span, -last.ordinal, first.ordinal, -n, 0 if src == "ECB" else 1)
    return min(good, key=rank)


def _try_ecb_monthly(flow: str, keys: Sequence[str], start_m: str, end_m: str,
                     client: ECBClient) -> Tuple[pd.Series, str]:
    errs = []
    for key in keys:
        try:
            s = _series_to_monthly(client.series(flow, key, start_m, end_m))
            if not s.empty:
                return s, f"{flow}.{key}"
        except Exception as exc:
            errs.append(str(exc))
    raise DataError(errs[-1] if errs else f"No ECB {flow} candidate returned observations.")


def _try_ecb_quarterly(flow: str, keys: Sequence[str], start_q: str, end_q: str,
                       client: ECBClient) -> Tuple[pd.Series, str]:
    errs = []
    for key in keys:
        try:
            s = _series_to_quarterly(client.series(flow, key, start_q, end_q))
            if not s.empty:
                return s, f"{flow}.{key}"
        except Exception as exc:
            errs.append(str(exc))
    raise DataError(errs[-1] if errs else f"No ECB {flow} candidate returned observations.")


def _dual_monthly(name: str, eurostat_getter, ecb_flow: str, ecb_keys: Sequence[str],
                  start_m: str, end_m: str, ecb_client: ECBClient):
    candidates = []
    errors = []
    try:
        es = _series_to_monthly(eurostat_getter())
        candidates.append(("Eurostat", es, name))
    except Exception as exc:
        errors.append(f"Eurostat: {exc}")
    try:
        ec, sid = _try_ecb_monthly(ecb_flow, ecb_keys, start_m, end_m, ecb_client)
        candidates.append(("ECB", ec, sid))
    except Exception as exc:
        errors.append(f"ECB: {exc}")
    if not candidates:
        raise DataError(f"{name}: " + " | ".join(errors))
    return _choose_longest(candidates)


def _dual_quarterly(name: str, eurostat_getter, ecb_flow: str, ecb_keys: Sequence[str],
                    start_q: str, end_q: str, ecb_client: ECBClient):
    candidates = []
    errors = []
    try:
        es = _series_to_quarterly(eurostat_getter())
        candidates.append(("Eurostat", es, name))
    except Exception as exc:
        errors.append(f"Eurostat: {exc}")
    try:
        ec, sid = _try_ecb_quarterly(ecb_flow, ecb_keys, start_q, end_q, ecb_client)
        candidates.append(("ECB", ec, sid))
    except Exception as exc:
        errors.append(f"ECB: {exc}")
    if not candidates:
        raise DataError(f"{name}: " + " | ".join(errors))
    return _choose_longest(candidates)


def _pick_geo(client: EurostatClient, dataset: str, base_filters: Dict[str, object],
              start: str, end: str) -> Tuple[str, pd.DataFrame]:
    for geo in EA_GEO_CANDIDATES:
        try:
            df = client.query(dataset, {**base_filters, "geo": geo}, start, end)
            if not df.empty:
                return geo, df
        except Exception:
            pass
    raise DataError(f"No euro-area aggregate found for {dataset} using {EA_GEO_CANDIDATES}.")


def _time_value(df: pd.DataFrame) -> pd.Series:
    if "time" in df.columns:
        tcol = "time"
    elif "TIME_PERIOD" in df.columns:
        tcol = "TIME_PERIOD"
    else:
        candidates = [c for c in df.columns if c.lower() == "time_period"]
        if not candidates:
            raise DataError("Eurostat response does not contain a time dimension.")
        tcol = candidates[0]
    s = pd.Series(pd.to_numeric(df["value"], errors="coerce").values,
                  index=df[tcol].astype(str).values)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _fetch_hicp_level(client: EurostatClient, geo: str, start_m: str, end_m: str,
                      coicop: str = "CP00") -> pd.Series:
    # 2026 HICP dissemination moved to a 2025=100 reference; try current then legacy bases.
    candidates = [
        {"geo": geo, "coicop": coicop, "unit": "I25"},
        {"geo": geo, "coicop": coicop, "unit": "I15"},
        {"geo": geo, "coicop": coicop},
    ]
    df = client.first_working("prc_hicp_midx", candidates, start_m, end_m)
    # If no unit was specified and several units slipped through, prefer newest reference-base code.
    if "unit" in df.columns and df["unit"].nunique() > 1:
        for unit in ["I25", "I15", "I05"]:
            x = df[df["unit"] == unit]
            if not x.empty:
                df = x
                break
    return _time_value(df)


def _fetch_unemployment(client: EurostatClient, geo: str, start_m: str, end_m: str) -> pd.Series:
    """Monthly harmonised unemployment rate from Eurostat une_rt_m.

    Current Eurostat coding for the headline total rate is:
    s_adj=SA, unit=PC_ACT, sex=T, age=TOTAL.
    For euro-area aggregates we try EA21 first, then EA20/EA19, because
    aggregate availability can differ across datasets and vintages.
    """
    geos = [geo]
    if str(geo).startswith("EA"):
        geos = list(dict.fromkeys([geo, "EA21", "EA20", "EA19"]))

    candidates = []
    for g in geos:
        candidates.extend([
            {"geo": g, "s_adj": "SA", "unit": "PC_ACT", "sex": "T", "age": "TOTAL"},
            # Defensive fallback if Eurostat changes adjustment availability.
            {"geo": g, "s_adj": "TRE", "unit": "PC_ACT", "sex": "T", "age": "TOTAL"},
        ])

    df = client.first_working("une_rt_m", candidates, start_m, end_m)
    return _time_value(df)

def _fetch_ip(client: EurostatClient, geo: str, start_m: str, end_m: str) -> pd.Series:
    """Monthly industrial production, one observation per month.

    Every Eurostat dimension is explicitly filtered so the API never interprets
    this as a bulk extraction. We try the standard total-industry NACE aggregates
    and current/legacy base-index units.
    """
    nace_candidates = ["B-D", "B-D_X_K", "B-D_X_K_2025"]
    unit_candidates = ["I21", "I15", "I10"]
    candidates = []
    for nace in nace_candidates:
        for unit in unit_candidates:
            candidates.append({
                "geo": geo,
                "freq": "M",
                "s_adj": "SCA",
                "unit": unit,
                "nace_r2": nace,
            })
            candidates.append({
                "geo": geo,
                "freq": "M",
                "s_adj": "SA",
                "unit": unit,
                "nace_r2": nace,
            })
    return _time_value(client.first_working("sts_inpr_m", candidates, start_m, end_m))


def _fetch_gdp_q(client: EurostatClient, geo: str, start_q: str, end_q: str) -> pd.Series:
    # Seasonally/calendar adjusted GDP at market prices, chain-linked volumes.
    cands = [
        {"geo": geo, "na_item": "B1GQ", "unit": "CLV10_MEUR", "s_adj": "SCA"},
        {"geo": geo, "na_item": "B1GQ", "unit": "CLV_I15", "s_adj": "SCA"},
        {"geo": geo, "na_item": "B1GQ", "unit": "CLV10_MEUR"},
    ]
    df = client.first_working("namq_10_gdp", cands, start_q, end_q)
    return _time_value(df)


def _series_to_monthly(s: pd.Series) -> pd.Series:
    idx = pd.PeriodIndex(s.index, freq="M")
    return pd.Series(s.values.astype(float), index=idx).sort_index()


def _series_to_quarterly(s: pd.Series) -> pd.Series:
    idx_txt = [str(x).replace("-Q", "Q") for x in s.index]
    idx = pd.PeriodIndex(idx_txt, freq="Q")
    return pd.Series(s.values.astype(float), index=idx).sort_index()


def _aggregate_monthly(s: pd.Series, freq: str, how: str = "mean") -> pd.Series:
    if freq == "M":
        return s
    if freq == "Q":
        key = s.index.asfreq("Q")
    elif freq == "A":
        key = s.index.asfreq("Y")
    else:
        raise ValueError(freq)
    grouped = s.groupby(key)
    return getattr(grouped, how)()



def _ar1_cov(n: int, rho: float) -> np.ndarray:
    idx = np.arange(n)
    return rho ** np.abs(idx[:, None] - idx[None, :])


def _chow_lin_monthly(q: pd.Series, indicator_m: pd.Series) -> pd.Series:
    """Chow-Lin disaggregation of quarterly GDP using monthly IP.

    Quarterly real GDP is treated as a level index/volume measure: the three
    estimated monthly GDP levels are constrained to average exactly to the
    observed quarterly GDP level.
    """
    q = q.dropna().astype(float).sort_index()
    indicator_m = indicator_m.dropna().astype(float).sort_index()
    usable_q, months = [], []
    for qp in q.index:
        mm = pd.period_range(qp.asfreq("M", "start"), qp.asfreq("M", "end"), freq="M")
        if all(m in indicator_m.index and pd.notna(indicator_m.loc[m]) for m in mm):
            usable_q.append(qp)
            months.extend(mm)

    if len(usable_q) < 4:
        raise DataError(
            "Chow-Lin monthly GDP needs at least four complete quarters with "
            "monthly industrial-production data. Use Linear interpolation for a shorter sample."
        )

    y = q.loc[usable_q].to_numpy(dtype=float)
    mindex = pd.PeriodIndex(months, freq="M")
    ind = indicator_m.loc[mindex].to_numpy(dtype=float)
    sd = float(np.nanstd(ind))
    if not np.isfinite(sd) or sd == 0:
        raise DataError("Industrial production has no usable variation for Chow-Lin.")

    z = (ind - np.nanmean(ind)) / sd
    trend = np.linspace(-1.0, 1.0, len(ind))
    X = np.column_stack([np.ones(len(ind)), z, trend])

    nq = len(usable_q)
    nm = 3 * nq
    C = np.zeros((nq, nm))
    for i in range(nq):
        C[i, 3*i:3*i+3] = 1.0 / 3.0
    Xq = C @ X

    best = None
    for rho in np.linspace(-0.90, 0.98, 189):
        V = _ar1_cov(nm, float(rho))
        Vq = C @ V @ C.T
        try:
            Vinv = np.linalg.inv(Vq)
            beta = np.linalg.solve(Xq.T @ Vinv @ Xq, Xq.T @ Vinv @ y)
            resid = y - Xq @ beta
            sigma2 = float(resid.T @ Vinv @ resid) / nq
            sign, logdet = np.linalg.slogdet(Vq)
            if sign <= 0 or sigma2 <= 0 or not np.isfinite(sigma2):
                continue
            score = -0.5 * (nq * np.log(sigma2) + logdet)
            if best is None or score > best[0]:
                best = (score, float(rho), beta, V, Vq)
        except np.linalg.LinAlgError:
            continue

    if best is None:
        raise DataError("Chow-Lin estimation failed numerically; use Linear interpolation.")

    _, rho, beta, V, Vq = best
    base = X @ beta
    monthly = base + V @ C.T @ np.linalg.solve(Vq, y - C @ base)
    out = pd.Series(monthly, index=mindex, dtype=float)

    check = out.groupby(out.index.asfreq("Q")).mean().reindex(pd.PeriodIndex(usable_q, freq="Q"))
    if not np.allclose(check.to_numpy(), y, rtol=1e-8, atol=1e-5):
        max_err = float(np.nanmax(np.abs(check.to_numpy() - y)))
        raise DataError(f"Chow-Lin benchmarking check failed (max quarterly level error={max_err:.6g}).")
    out.attrs["chow_lin_rho"] = rho
    return out


def _gdp_to_frequency(q: pd.Series, freq: str, monthly_method: str, monthly_indicator: Optional[pd.Series] = None) -> pd.Series:
    if freq == "Q":
        return q
    if freq == "A":
        # Match the convention in the user's annual workbook: average quarterly GDP level.
        return q.groupby(q.index.asfreq("Y")).mean()
    if freq != "M":
        raise ValueError(freq)
    if monthly_method == "Leave blank":
        return pd.Series(dtype=float)
    q_ts = q.copy()
    # place quarterly observation on the final month of the quarter
    q_ts.index = q_ts.index.asfreq("M", how="end")
    full = pd.period_range(q_ts.index.min().asfreq("M", how="start") - 2,
                           q_ts.index.max(), freq="M")
    m = q_ts.reindex(full)
    if monthly_method == "Quarterly step (repeat within quarter)":
        # Each quarter's GDP level is repeated for its three constituent months.
        out = pd.Series(index=full, dtype=float)
        for qp, val in q.items():
            months = pd.period_range(qp.asfreq("M", "start"), qp.asfreq("M", "end"), freq="M")
            out.loc[months] = val
        return out
    if monthly_method == "Chow-Lin (industrial production)":
        if monthly_indicator is None or monthly_indicator.dropna().empty:
            raise DataError("Chow-Lin requires monthly industrial production as an indicator.")
        return _chow_lin_monthly(q, monthly_indicator)
    if monthly_method == "Linear interpolation":
        return m.interpolate(method="linear").bfill()
    raise ValueError(monthly_method)


def _period_range(freq: str, start_year: int, end_year: int) -> pd.PeriodIndex:
    if freq == "M":
        return pd.period_range(f"{start_year}-01", f"{end_year}-12", freq="M")
    if freq == "Q":
        return pd.period_range(f"{start_year}Q1", f"{end_year}Q4", freq="Q")
    return pd.period_range(str(start_year), str(end_year), freq="Y")


def _period_label(p: pd.Period, freq: str):
    if freq == "M":
        return p.strftime("%Y-%m")
    if freq == "Q":
        return f"{p.year}Q{p.quarter}"
    return int(p.year)



def _ecb_monthly_to_target(client: ECBClient, flow: str, key: str, freq: str,
                            start_year: int, end_year: int) -> pd.Series:
    """Fetch an ECB monthly series and convert to M/Q/A by arithmetic averaging.

    A full-range request is tried first. If the ECB gateway still fails after the
    client's retries, the request is split into 10-year chunks. This is useful for
    long multi-country IRS downloads, for which the ECB API can intermittently 504.
    """
    try:
        raw = client.series(flow, key, f"{start_year}-01", f"{end_year}-12")
    except DataError as first_exc:
        pieces = []
        chunk_errors = []
        for y0 in range(start_year, end_year + 1, 10):
            y1 = min(y0 + 9, end_year)
            try:
                pieces.append(client.series(flow, key, f"{y0}-01", f"{y1}-12"))
            except DataError as exc:
                chunk_errors.append(f"{y0}-{y1}: {exc}")
        if not pieces or chunk_errors:
            detail = "; ".join(chunk_errors[:3])
            raise DataError(f"{first_exc} Chunked fallback also failed: {detail}")
        raw = pd.concat(pieces)
        raw = raw[~raw.index.duplicated(keep="last")].sort_index()
    idx = pd.PeriodIndex([str(x)[:7] for x in raw.index], freq="M")
    s = pd.Series(pd.to_numeric(raw.values, errors="coerce"), index=idx).dropna().sort_index()
    if freq == "M":
        return s
    if freq == "Q":
        return s.groupby(s.index.asfreq("Q")).mean()
    return s.groupby(s.index.asfreq("Y")).mean()


def _ecb_country_code(country: str) -> str:
    """Map app/Eurostat geography codes to ECB country codes.

    Greece is EL in Eurostat and in the app, but GR in ECB datasets such as IRS/FM.
    Keep this mapping at the ECB boundary so the rest of the app can consistently use EL.
    """
    return {"EL": "GR"}.get(country, country)


def _country_sovereign_10y(client: ECBClient, country: str, freq: str,
                            start_year: int, end_year: int) -> tuple[pd.Series, str]:
    """Country 10Y government yield from ECB long-term interest-rate statistics."""
    ecb_country = _ecb_country_code(country)
    key = f"M.{ecb_country}.L.L40.CI.0000.EUR.N.Z"
    return _ecb_monthly_to_target(client, "IRS", key, freq, start_year, end_year), f"IRS.{key}"


def _country_sovereign_1y(client: ECBClient, country: str, freq: str,
                           start_year: int, end_year: int) -> tuple[pd.Series, str]:
    """Try exact country 1Y sovereign-yield series published in ECB FM.

    ECB's harmonised IRS country dataset is a 10-year convergence-yield dataset,
    not a 1-year dataset. We therefore only accept an exact country 1Y benchmark
    if the ECB endpoint exposes one; there is deliberately no EA-AAA substitution.
    """
    ecb_country = _ecb_country_code(country)
    candidates = [
        f"M.{ecb_country}.EUR.4F.BB.{ecb_country}_1Y.YLD",
        f"M.{ecb_country}.EUR.4F.BB.{ecb_country}_1Y.YLDA",
    ]
    errs=[]
    for key in candidates:
        try:
            return _ecb_monthly_to_target(client, "FM", key, freq, start_year, end_year), f"FM.{key}"
        except Exception as exc:
            errs.append(str(exc))
    raise DataError("No exact ECB country 1Y sovereign benchmark series found; "
                    "EA AAA 1Y is not substituted.")


def _ecb_daily_to_target(client: ECBClient, flow: str, key: str, freq: str,
                         start_year: int, end_year: int, method: str = "mean",
                         carry_forward: bool = False) -> pd.Series:
    """Fetch ECB daily/change-date data and aggregate to M/Q/A.

    For policy-rate level series, carry_forward=True expands sparse change dates
    into the rate actually prevailing on every calendar day before averaging.
    """
    # Pull a short pre-sample window so the first requested period can inherit
    # a rate set just before the sample begins.
    fetch_start = f"{start_year-1}-01-01" if carry_forward else f"{start_year}-01-01"
    raw = client.series(flow, key, fetch_start, f"{end_year}-12-31")
    dt = pd.to_datetime(raw.index, errors="coerce")
    s = pd.Series(pd.to_numeric(raw.values, errors="coerce"), index=dt).dropna()
    s = s[~s.index.isna()].sort_index()
    if s.empty:
        raise DataError(f"ECB {flow}.{key}: no dated observations.")

    if carry_forward:
        # ECB official-rate endpoints can be change-date series. Convert them
        # to a continuous level path: unchanged days retain the previous rate.
        daily_index = pd.date_range(f"{start_year}-01-01", f"{end_year}-12-31", freq="D")
        full_index = s.index.union(daily_index).sort_values()
        s = s.reindex(full_index).ffill().reindex(daily_index)
        s = s.dropna()

    if freq == "M":
        x = s.resample("ME").mean()
        return x.set_axis(x.index.to_period("M"))
    if freq == "Q":
        x = s.resample("QE").mean()
        return x.set_axis(x.index.to_period("Q"))
    x = s.resample("YE").mean()
    return x.set_axis(x.index.to_period("Y"))


def _ecb_policy_rate_to_target(client: ECBClient, key: str, freq: str,
                               start_year: int, end_year: int) -> pd.Series:
    """Policy-rate period averages from the ECB daily level series."""
    return _ecb_daily_to_target(client, "FM", key, freq, start_year, end_year, "mean")


def fetch_macro_dataset(
    geo: str,
    freq: str,
    start_year: int,
    end_year: int,
    variables: Sequence[str] = CANONICAL_VARS,
    monthly_gdp_method: str = "Chow-Lin (industrial production)",
    client: Optional[EurostatClient] = None,
) -> Tuple[pd.DataFrame, List[FetchMeta]]:
    """Fetch macro data, comparing ECB Data Portal and Eurostat series.

    For each concept, both official portals are attempted. The source with the
    earliest available observation is selected; ties use observation count and
    latest observation. Source provenance is written beside each data column.
    """
    client = client or EurostatClient()
    ecb = ECBClient()
    vars_set = set(variables)
    start_m, end_m = f"{start_year}-01", f"{end_year}-12"
    start_q, end_q = f"{start_year}-Q1", f"{end_year}-Q4"

    # For Eurostat EA_AUTO, use the current aggregate that actually exists.
    requested_ea = (geo == "EA_AUTO")
    actual_geo = geo
    if requested_ea:
        for cand in EA_GEO_CANDIDATES:
            try:
                if not _fetch_hicp_level(client, cand, start_m, end_m).empty:
                    actual_geo = cand
                    break
            except Exception:
                continue
        if actual_geo == "EA_AUTO":
            raise DataError("Could not resolve the default euro-area aggregate or fixed-composition fallbacks in Eurostat.")

    area = _ecb_area(actual_geo, evolving_ea=requested_ea)
    data: Dict[str, pd.Series] = {}
    sources: Dict[str, str] = {}
    meta: List[FetchMeta] = []
    ip_monthly_indicator: Optional[pd.Series] = None
    ip_indicator_source: Optional[str] = None

    def add_meta(variable, src, sid, native, s, note=""):
        meta.append(FetchMeta(variable, src, sid, actual_geo, native,
                              str(s.dropna().index.max()) if not s.dropna().empty else None,
                              note))

    # HICP overall
    # Index and annual inflation are selected independently. This matters because
    # Eurostat's direct annual-rate series can have a longer history than a
    # particular index-reference-base series.
    if "hicp_level" in vars_set:
        ecb_hicp_idx_keys = [
            f"M.{area}.N.000000.4.INX",
            f"M.{area}.N.000000.4D0.INX",
        ]
        src, h, sid = _dual_monthly(
            "prc_hicp_midx",
            lambda: _fetch_hicp_level(client, actual_geo, start_m, end_m, "CP00"),
            "ICP", ecb_hicp_idx_keys, start_m, end_m, ecb
        )
        data["hicp_level"] = _aggregate_monthly(h, freq, "mean")
        sources["hicp_level"] = src
        add_meta("HICP level", src, sid, "M", h,
                 f"Longest/freshest equivalent series. Range: {h.dropna().index.min()} to {h.dropna().index.max()}.")

    if "hicp_rate" in vars_set:
        # Eurostat direct annual rate: all-items, percentage change vs same month previous year.
        def eurostat_hicp_rate():
            candidates = []
            # Newer HICP tables can expose current and legacy unit codes; try tightly filtered forms.
            for unit in ["RCH_A", "RCH_A_AVG", "RCH_A_PY"]:
                candidates.append({
                    "geo": actual_geo, "freq": "M", "unit": unit, "coicop": "CP00"
                })
            # Dedicated annual-rate dataset is preferred.
            return _time_value(client.first_working("prc_hicp_manr", candidates, start_m, end_m))

        # ECB annual-rate variant; if unavailable, ECB index-derived rate is a fallback candidate.
        candidates_rate = []
        errors_rate = []
        try:
            er = _series_to_monthly(eurostat_hicp_rate())
            candidates_rate.append(("Eurostat", er, "prc_hicp_manr"))
        except Exception as exc:
            errors_rate.append(f"Eurostat annual rate: {exc}")
            # Fallback: derive from Eurostat all-items index.
            try:
                eh = _series_to_monthly(_fetch_hicp_level(client, actual_geo, start_m, end_m, "CP00"))
                candidates_rate.append(("Eurostat", eh.pct_change(12, fill_method=None)*100.0,
                                        "prc_hicp_midx -> YoY"))
            except Exception as exc2:
                errors_rate.append(f"Eurostat index fallback: {exc2}")
        try:
            ec, esid = _try_ecb_monthly(
                "ICP",
                [f"M.{area}.N.000000.4.ANR", f"M.{area}.N.000000.4D0.ANR"],
                start_m, end_m, ecb
            )
            candidates_rate.append(("ECB", ec, esid))
        except Exception as exc:
            errors_rate.append(f"ECB annual rate: {exc}")
            try:
                ecidx, esid2 = _try_ecb_monthly(
                    "ICP",
                    [f"M.{area}.N.000000.4.INX", f"M.{area}.N.000000.4D0.INX"],
                    start_m, end_m, ecb
                )
                candidates_rate.append(("ECB", ecidx.pct_change(12, fill_method=None)*100.0,
                                        esid2 + " -> YoY"))
            except Exception as exc2:
                errors_rate.append(f"ECB index fallback: {exc2}")
        if not candidates_rate:
            raise DataError("HICP rate: " + " | ".join(errors_rate))
        src, hr, sid = _choose_longest(candidates_rate)
        data["hicp_rate"] = _aggregate_monthly(hr, freq, "mean")
        sources["hicp_rate"] = src
        add_meta("HICP rate", src, sid, "M", hr,
                 f"Longest/freshest annual-rate series. Range: {hr.dropna().index.min()} to {hr.dropna().index.max()}.")

    # Energy HICP: Eurostat is authoritative mapping in the app; ECB candidate is
    # deliberately not guessed because the ICP classification differs from Eurostat's NRG aggregate.
    if "energy_index" in vars_set:
        e = _series_to_monthly(_fetch_hicp_level(client, actual_geo, start_m, end_m, "NRG"))
        data["energy_index"] = _aggregate_monthly(e, freq, "mean")
        sources["energy_index"] = "Eurostat"
        add_meta("Energy HICP", "Eurostat", "prc_hicp_midx (NRG)", "M", e,
                 "ECB equivalent is not substituted unless the classification is exactly matched.")

    # Unemployment
    if "unemployment_rate" in vars_set:
        ecb_unemp_keys = [
            f"M.{area}.S.UNEH.RTT000.4.000",
            f"M.{area}.Y.UNEH.RTT000.4.000",
        ]
        src, u, sid = _dual_monthly(
            "une_rt_m", lambda: _fetch_unemployment(client, actual_geo, start_m, end_m),
            "STS", ecb_unemp_keys, start_m, end_m, ecb
        )
        data["unemployment_rate"] = _aggregate_monthly(u, freq, "mean")
        sources["unemployment_rate"] = src
        add_meta("Unemployment rate", src, sid, "M", u,
                 f"Selected for longest history. First observation: {u.dropna().index.min()}.")

    # IP (also used as Chow-Lin indicator)
    need_ip_output = bool({"ip", "log_ip"} & vars_set)
    need_ip_for_chowlin = (
        freq == "M" and monthly_gdp_method == "Chow-Lin (industrial production)"
        and bool({"real_gdp", "real_gdp_growth", "log_real_gdp"} & vars_set)
    )
    if need_ip_output or need_ip_for_chowlin:
        ecb_ip_keys = [
            f"M.{area}.Y.PROD.NS0020.4.000",
            f"M.{area}.Y.PROD.B-D.4.000",
        ]
        src, ip, sid = _dual_monthly(
            "sts_inpr_m", lambda: _fetch_ip(client, actual_geo, start_m, end_m),
            "STS", ecb_ip_keys, start_m, end_m, ecb
        )
        ip_monthly_indicator = ip
        ip_indicator_source = src
        ip_f = _aggregate_monthly(ip, freq, "mean")
        if "ip" in vars_set:
            data["ip"] = ip_f
            sources["ip"] = ip_indicator_source
        if "log_ip" in vars_set:
            data["log_ip"] = np.log(ip_f.where(ip_f > 0))
            sources["log_ip"] = f"Derived ({ip_indicator_source})"
        add_meta("Industrial production", src, sid, "M", ip,
                 f"Selected for longest history. First observation: {ip.dropna().index.min()}.")

    # Real GDP
    if {"real_gdp", "real_gdp_growth", "log_real_gdp"} & vars_set:
        ecb_gdp_keys = [
            f"Q.Y.{area}.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.N",
            f"Q.Y.{area}.W2.S1.S1.B.B1GQ._Z._Z._Z.XDC.LR.N",
        ]
        src, gq, sid = _dual_quarterly(
            "namq_10_gdp", lambda: _fetch_gdp_q(client, actual_geo, start_q, end_q),
            "MNA", ecb_gdp_keys, start_q, end_q, ecb
        )
        gf = _gdp_to_frequency(gq, freq, monthly_gdp_method, monthly_indicator=ip_monthly_indicator)
        base_source = src
        if freq == "M" and monthly_gdp_method == "Chow-Lin (industrial production)":
            base_source = f"Chow-Lin ({base_source}+{ip_indicator_source})"
        elif freq == "M":
            base_source = f"{monthly_gdp_method} ({base_source})"
        if "real_gdp" in vars_set:
            data["real_gdp"] = gf
            sources["real_gdp"] = base_source
        if "log_real_gdp" in vars_set:
            data["log_real_gdp"] = np.log(gf.where(gf > 0))
            sources["log_real_gdp"] = f"Derived ({base_source})"
        if "real_gdp_growth" in vars_set:
            lag = 12 if freq == "M" else 4 if freq == "Q" else 1
            data["real_gdp_growth"] = gf.pct_change(lag, fill_method=None) * 100.0
            sources["real_gdp_growth"] = f"Derived ({base_source})"
        add_meta("Real GDP", src, sid, "Q", gq,
                 f"Selected for longest history. First observation: {gq.dropna().index.min()}.")

    # Country-specific sovereign yields.
    # For EA aggregate output the AAA curve remains available separately via
    # aaa_1y / aaa_10y. For individual countries, sovereign_* means that country's
    # own government yield; never silently replace it with the EA AAA curve.
    if "sovereign_10y" in vars_set:
        if requested_ea:
            # Harmonised euro-area 10Y convergence yield.
            key10 = "M.U2.L.L40.CI.0000.EUR.N.Z"
            s10 = _ecb_monthly_to_target(ecb, "IRS", key10, freq, start_year, end_year)
            sid10 = f"IRS.{key10}"
        else:
            s10, sid10 = _country_sovereign_10y(ecb, actual_geo, freq, start_year, end_year)
        data["sovereign_10y"] = s10
        sources["sovereign_10y"] = "ECB"
        add_meta(
            "EA sovereign/convergence 10Y yield" if requested_ea else "Country sovereign 10Y yield",
            "ECB", sid10, "M", s10,
            "Broad EA convergence-purpose 10Y government yield; distinct from the AAA curve."
            if requested_ea else
            "Country-specific convergence-purpose 10Y government yield; distinct from the common EA AAA curve."
        )

    if "sovereign_1y" in vars_set:
        if requested_ea:
            # Do NOT alias this to AAA 1Y. ECB IRS provides the harmonised
            # convergence sovereign yield at 10Y, not an equivalent broad-EA 1Y.
            target_tmp = _period_range(freq, start_year, end_year)
            data["sovereign_1y"] = pd.Series(index=target_tmp, dtype=float)
            sources["sovereign_1y"] = "ECB unavailable"
            add_meta(
                "EA sovereign 1Y yield", "ECB unavailable",
                "No harmonised broad-EA 1Y sovereign/convergence series",
                "M", data["sovereign_1y"],
                "Left blank deliberately. AAA 1Y is a separate ECB AAA yield-curve concept and is not substituted."
            )
        else:
            try:
                s1, sid1 = _country_sovereign_1y(ecb, actual_geo, freq, start_year, end_year)
                data["sovereign_1y"] = s1
                sources["sovereign_1y"] = "ECB"
                add_meta("Sovereign 1Y yield", "ECB", sid1, "M", s1,
                         "Country-specific exact 1Y government benchmark yield.")
            except Exception:
                # Keep country generation usable and transparent: blank, never substituted.
                target_tmp = _period_range(freq, start_year, end_year)
                data["sovereign_1y"] = pd.Series(index=target_tmp, dtype=float)
                sources["sovereign_1y"] = "ECB unavailable"
                add_meta("Sovereign 1Y yield", "ECB unavailable", "No exact country 1Y series",
                         "M", data["sovereign_1y"],
                         "No exact country 1Y ECB series was available; left blank rather than using EA AAA.")

    # Optional ECB financial / policy series.
    # Daily observations are averaged within the requested M/Q/A period.
    financial_specs = {
        "estr": ("EST", "B.EU000A2X2A25.WT"),
        "dfr": ("FM", "D.U2.EUR.4F.KR.DFR.LEV"),
        "mro": ("FM", "D.U2.EUR.4F.KR.MRR_RT.LEV"),
        "aaa_1y": ("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_1Y"),
        "aaa_10y": ("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"),
    }
    for v, (flow, key) in financial_specs.items():
        if v not in vars_set:
            continue
        try:
            s = _ecb_daily_to_target(
                ecb, flow, key, freq, start_year, end_year,
                carry_forward=(v in ("dfr", "mro"))
            )
            data[v] = s
            sources[v] = "ECB"
            add_meta(
                v, "ECB", f"{flow}.{key}", "D" if v in ("dfr", "mro") else "B", s,
                "Policy-rate levels are carried forward between ECB change dates and then period-averaged."
                if v in ("dfr", "mro")
                else "Daily/business-day observations averaged within period."
            )
        except Exception as exc:
            raise DataError(f"{v}: could not retrieve ECB series {flow}.{key}: {exc}")

    target = _period_range(freq, start_year, end_year)
    out = pd.DataFrame(index=target)
    # Put provenance immediately after each variable, as requested.
    for var in CANONICAL_VARS:
        if var in data:
            out[var] = data[var].reindex(target)
            out[f"{var}_source"] = sources.get(var, "")
    out.insert(0, "date", [_period_label(p, freq) for p in target])
    return out.reset_index(drop=True), meta


def _fred_series(series_id: str, start_year: int, end_year: int) -> pd.Series:
    """Download a public FRED series without requiring a user API key."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    r = requests.get(
        url,
        params={"id": series_id, "cosd": f"{start_year}-01-01", "coed": f"{end_year}-12-31"},
        timeout=45,
        headers={"User-Agent": "Macro-Data-Updater/1.0"},
    )
    if r.status_code != 200:
        raise DataError(f"FRED {series_id} returned HTTP {r.status_code}.")
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty or series_id not in df.columns:
        raise DataError(f"FRED {series_id}: no observations returned.")
    dt = pd.to_datetime(df.iloc[:,0], errors="coerce")
    vals = pd.to_numeric(df[series_id], errors="coerce")
    s = pd.Series(vals.values, index=dt).dropna()
    s = s[~s.index.isna()].sort_index()
    if s.empty:
        raise DataError(f"FRED {series_id}: no numeric observations returned.")
    return s


def _dated_to_period(s: pd.Series, freq: str, native: str) -> pd.Series:
    """Convert dated US series to requested M/Q/A frequency using period averages."""
    if native == "Q":
        q = pd.Series(s.values, index=s.index.to_period("Q")).groupby(level=0).last()
        if freq == "Q":
            return q
        if freq == "A":
            return q.groupby(q.index.asfreq("Y")).mean()
        raise ValueError("Quarterly source requires GDP-specific monthly treatment.")
    m = pd.Series(s.values, index=s.index.to_period("M")).groupby(level=0).mean()
    if freq == "M":
        return m
    if freq == "Q":
        return m.groupby(m.index.asfreq("Q")).mean()
    return m.groupby(m.index.asfreq("Y")).mean()


def fetch_us_dataset(freq: str, start_year: int, end_year: int,
                     variables: Sequence[str] = US_VARS,
                     monthly_gdp_method: str = "Chow-Lin (industrial production)") -> Tuple[pd.DataFrame, List[FetchMeta]]:
    """US macro dataset from official-source series distributed through FRED.

    Underlying publishers: BEA (real GDP), BLS (CPI/unemployment),
    Federal Reserve (industrial production, federal funds, SOFR), and
    U.S. Treasury/Federal Reserve H.15 (Treasury constant-maturity yields).
    """
    vars_set=set(variables)
    data={}
    sources={}
    meta=[]

    def add(var, series_id, publisher, native, note=""):
        raw=_fred_series(series_id, start_year-1, end_year)
        s=_dated_to_period(raw, freq, native)
        data[var]=s
        sources[var]=f"{publisher} via FRED · {series_id}"
        meta.append(FetchMeta(var, publisher, series_id, "US", native,
                              str(s.dropna().index.max()) if not s.dropna().empty else None, note))
        return s

    # Monthly indicator is useful both as output and for Chow-Lin GDP.
    ip_raw=_fred_series("INDPRO", start_year-1, end_year)
    ip_m=pd.Series(ip_raw.values,index=ip_raw.index.to_period("M")).groupby(level=0).mean()

    if any(v in vars_set for v in ("real_gdp","real_gdp_growth","log_real_gdp")):
        graw=_fred_series("GDPC1", start_year-1, end_year)
        gq=pd.Series(graw.values,index=graw.index.to_period("Q")).groupby(level=0).last()
        g=_gdp_to_frequency(gq, freq, monthly_gdp_method, ip_m)
        if "real_gdp" in vars_set:
            data["real_gdp"]=g; sources["real_gdp"]="BEA via FRED · GDPC1"
        if "log_real_gdp" in vars_set:
            data["log_real_gdp"]=np.log(g); sources["log_real_gdp"]="Derived from BEA GDPC1"
        if "real_gdp_growth" in vars_set:
            periods=12 if freq=="M" else 4 if freq=="Q" else 1
            data["real_gdp_growth"]=100*(np.log(g)-np.log(g.shift(periods)))
            sources["real_gdp_growth"]="Derived from BEA GDPC1"
        meta.append(FetchMeta("Real GDP","BEA","GDPC1","US","Q",
                              str(g.dropna().index.max()) if not g.dropna().empty else None,
                              "Quarterly real GDP; monthly values use selected temporal-disaggregation method."))

    if "cpi_level" in vars_set or "inflation_rate" in vars_set:
        craw=_fred_series("CPIAUCSL", start_year-1, end_year)
        cm=pd.Series(craw.values,index=craw.index.to_period("M")).groupby(level=0).mean()
        c=_dated_to_period(craw,freq,"M")
        if "cpi_level" in vars_set:
            data["cpi_level"]=c; sources["cpi_level"]="BLS via FRED · CPIAUCSL"
        if "inflation_rate" in vars_set:
            lag=12 if freq=="M" else 4 if freq=="Q" else 1
            data["inflation_rate"]=100*(np.log(c)-np.log(c.shift(lag)))
            sources["inflation_rate"]="Derived from BLS CPIAUCSL"
        meta.append(FetchMeta("CPI","BLS","CPIAUCSL","US","M",str(c.dropna().index.max()),"Seasonally adjusted CPI-U."))

    if "unemployment_rate" in vars_set: add("unemployment_rate","UNRATE","BLS","M","Seasonally adjusted unemployment rate.")
    if "ip" in vars_set:
        data["ip"]=_dated_to_period(ip_raw,freq,"M"); sources["ip"]="Federal Reserve via FRED · INDPRO"
        meta.append(FetchMeta("Industrial production","Federal Reserve","INDPRO","US","M",str(data["ip"].dropna().index.max()),"Industrial Production Index."))
    if "log_ip" in vars_set:
        data["log_ip"]=np.log(_dated_to_period(ip_raw,freq,"M")); sources["log_ip"]="Derived from Federal Reserve INDPRO"
    if "energy_cpi" in vars_set: add("energy_cpi","CPIENGSL","BLS","M","CPI energy index, seasonally adjusted.")
    if "fed_funds" in vars_set: add("fed_funds","FEDFUNDS","Federal Reserve","M","Effective federal funds rate.")
    if "sofr" in vars_set: add("sofr","SOFR","Federal Reserve Bank of New York","M","Secured Overnight Financing Rate.")
    if "treasury_1y" in vars_set: add("treasury_1y","GS1","U.S. Treasury / Federal Reserve","M","1-year Treasury constant maturity rate.")
    if "treasury_10y" in vars_set: add("treasury_10y","GS10","U.S. Treasury / Federal Reserve","M","10-year Treasury constant maturity rate.")

    target=_period_range(freq,start_year,end_year)
    out=pd.DataFrame(index=target)
    for var in US_VARS:
        if var in data:
            out[var]=data[var].reindex(target)
            out[f"{var}_source"]=sources.get(var,"")
    out.insert(0,"date",[_period_label(p,freq) for p in target])
    return out.reset_index(drop=True),meta


def fetch_country_panel(countries: Sequence[str], freq: str, start_year: int, end_year: int,
                        variables: Sequence[str], monthly_gdp_method: str,
                        progress=None) -> Tuple[Dict[str, pd.DataFrame], List[FetchMeta]]:
    client = EurostatClient()
    results: Dict[str, pd.DataFrame] = {}
    all_meta: List[FetchMeta] = []
    n = max(len(countries), 1)
    for i, geo in enumerate(countries):
        if progress:
            progress(i / n, f"Downloading {COUNTRY_NAMES.get(geo, geo)} ({geo})…")
        df, meta = fetch_macro_dataset(geo, freq, start_year, end_year, variables,
                                       monthly_gdp_method, client=client)
        results[geo] = df
        all_meta.extend(meta)
    if progress:
        progress(1.0, "Finished")
    return results, all_meta
