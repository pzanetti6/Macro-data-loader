from __future__ import annotations

import io
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

EUROSTAT_STATS = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
ECB_DATA = "https://data-api.ecb.europa.eu/service/data"

EA19 = ["AT", "BE", "CY", "DE", "EE", "EL", "ES", "FI", "FR", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PT", "SI", "SK"]
COUNTRY_NAMES = {
    "AT": "Austria", "BE": "Belgium", "CY": "Cyprus", "DE": "Germany", "EE": "Estonia",
    "EL": "Greece", "ES": "Spain", "FI": "Finland", "FR": "France", "IE": "Ireland",
    "IT": "Italy", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "MT": "Malta",
    "NL": "Netherlands", "PT": "Portugal", "SI": "Slovenia", "SK": "Slovakia",
}

# Familiar headline/reference equity-index names used for display and workbook headers.
# Data retrieval remains the harmonised OECD national share-price series; these names make
# country outputs immediately recognisable to users.
REFERENCE_EQUITY_INDEX = {
    "AT": "ATX",
    "BE": "BEL 20",
    "CY": "Cyprus General Index",
    "DE": "DAX",
    "EE": "OMX Tallinn",
    "EL": "Athens General Composite",
    "ES": "IBEX 35",
    "FI": "OMX Helsinki 25",
    "FR": "CAC 40",
    "IE": "ISEQ Overall",
    "IT": "FTSE MIB",
    "LT": "OMX Vilnius",
    "LU": "LuxX",
    "LV": "OMX Riga",
    "MT": "MSE Equity Price Index",
    "NL": "AEX",
    "PT": "PSI",
    "SI": "SBITOP",
    "SK": "SAX",
}

# Euro area aggregate codes have changed as membership expanded. EA20 is used as the aggregate
# default for historical consistency with most recent Eurostat dissemination. Country mode avoids
# this composition issue entirely.
EA_GEO_CANDIDATES = ["EA21", "EA20", "EA19"]

CANONICAL_VARS = [
    "real_gdp", "real_gdp_growth", "log_real_gdp", "hicp_rate", "hicp_level",
    "unemployment_rate", "ip", "log_ip", "energy_index", "house_price_index",
    "consumption", "gcf", "trade_balance", "budget_balance", "fdi",
    "estr", "dfr", "mro", "aaa_1y", "aaa_10y",
    "sovereign_1y", "sovereign_10y", "m1", "m2", "m3",
    "eurostoxx50", "country_equity_index", "move", "credit_spread",
    "loans_households", "loans_firms",
    "ecb_spf_infl_1y", "ecb_spf_infl_2y", "ecb_spf_infl_lt", "ecb_spf_gdp_1y", "ecb_spf_gdp_lt",
    "ecb_proj_gdp_cy", "ecb_proj_gdp_ny", "ecb_proj_hicp_cy", "ecb_proj_hicp_ny",
]

US_VARS = [
    "real_gdp", "real_gdp_growth", "log_real_gdp",
    "cpi_level", "inflation_rate", "unemployment_rate",
    "ip", "log_ip", "energy_cpi", "house_price_index",
    "consumption", "gcf", "trade_balance", "budget_balance", "fdi",
    "fed_funds", "sofr", "treasury_1y", "treasury_10y", "m1", "m2",
    "sp500", "djia", "nasdaq", "vix", "credit_spread",
    "loans_households", "loans_firms",
    "spf_gdp", "spf_cpi", "spf_cpi10", "spf_unemp",
    "fed_dots_cy", "fed_dots_ny", "fed_dots_lr", "sep_gdp_cy", "sep_pce_cy", "sep_unemp_cy",
]
# Variables served by macro_extra (national accounts, money, lending, surveys).
EXTRA_EU_VARS = CANONICAL_VARS[CANONICAL_VARS.index("consumption"):CANONICAL_VARS.index("estr")] + [
    "m1", "m2", "m3", "loans_households", "loans_firms"] + CANONICAL_VARS[CANONICAL_VARS.index("ecb_spf_infl_1y"):]
EXTRA_US_VARS = ["consumption", "gcf", "trade_balance", "budget_balance", "fdi", "m1", "m2",
                 "loans_households", "loans_firms"] + US_VARS[US_VARS.index("spf_gdp"):]

SOURCE_URLS = {
    "hicp": "https://ec.europa.eu/eurostat/databrowser/view/prc_hicp_midx/default/table",
    "unemployment": "https://ec.europa.eu/eurostat/databrowser/view/une_rt_m/default/table",
    "ip": "https://ec.europa.eu/eurostat/databrowser/view/sts_inpr_m/default/table",
    "gdp": "https://ec.europa.eu/eurostat/databrowser/view/namq_10_gdp/default/table",
    "house_prices": "https://ec.europa.eu/eurostat/databrowser/view/prc_hpi_q/default/table",
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


# ---------------------------------------------------------------------------
# Shared HTTP layer: one pooled session per thread, retries on transient
# errors, a small in-process cache, and redaction of secrets in every message.
# ---------------------------------------------------------------------------

USER_AGENT = "Macro-Data-Loader/2.0 (+https://github.com; research use)"
_CACHE_TTL_SECONDS = 6 * 3600
_TRANSIENT = {429, 500, 502, 503, 504}
_SECRET_RE = re.compile(r"(api_key=)[^&\s'\"]+", re.I)


def redact(text) -> str:
    """Remove API keys from any text that might reach the UI or a workbook."""
    return _SECRET_RE.sub(r"\1***", str(text))


class _TTLCache:
    """Thread-safe process-level cache. Streamlit reruns and country workers share it."""

    def __init__(self, ttl: float, max_items: int = 1024):
        self.ttl = ttl
        self.max_items = max_items
        self._data: Dict[tuple, Tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            stamp, value = hit
            if time.time() - stamp > self.ttl:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key, value):
        with self._lock:
            if len(self._data) >= self.max_items:
                oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])[: self.max_items // 4]
                for k, _ in oldest:
                    self._data.pop(k, None)
            self._data[key] = (time.time(), value)

    def clear(self):
        with self._lock:
            self._data.clear()


_CACHE = _TTLCache(_CACHE_TTL_SECONDS)


def clear_download_cache() -> None:
    """Forget every cached download (used by the UI's 'refresh' action)."""
    _CACHE.clear()


class _RateLimiter:
    """Minimum spacing between calls to one host (FRED allows about 2 requests/second)."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.min_interval


_thread_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8)
        s.mount("https://", adapter)
        _thread_local.session = s
    return s


def _http_get(url: str, *, params=None, headers=None, timeout=(6, 30), retries: int = 2,
              limiter: Optional[_RateLimiter] = None, label: str = "") -> requests.Response:
    """GET with bounded retries on 429/5xx and network errors.

    Returns the final response whatever its status (callers decide what a 404
    means). Raises DataError only when no response could be obtained. Messages
    never include the request URL, because FRED URLs carry the API key.
    """
    last = "no response"
    for attempt in range(retries + 1):
        if limiter is not None:
            limiter.wait()
        try:
            r = _session().get(url, params=params, headers=headers, timeout=timeout)
        except requests.Timeout:
            last = "timed out"
        except requests.ConnectionError:
            last = "connection failed"
        except requests.RequestException as exc:
            last = type(exc).__name__
        else:
            if r.status_code in _TRANSIENT and attempt < retries:
                retry_after = r.headers.get("Retry-After")
                try:
                    pause = float(retry_after) if retry_after else 1.5 * (2 ** attempt)
                except (TypeError, ValueError):
                    pause = 1.5 * (2 ** attempt)
                time.sleep(min(pause, 15))
                continue
            return r
        if attempt < retries:
            time.sleep(1.0 * (2 ** attempt))
    raise DataError(f"{label or url.split('/')[2]}: {last}")


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
    def __init__(self, timeout: int = 14):
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
        cache_key = ("eurostat", dataset, tuple(params))
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached.copy()
        url = f"{EUROSTAT_STATS}/{dataset}"
        try:
            r = self.session.get(url, params=params, timeout=(5, self.timeout))
        except requests.RequestException as exc:
            raise DataError(f"Eurostat {dataset}: network request failed ({type(exc).__name__})") from exc
        if r.status_code != 200:
            raise DataError(f"Eurostat {dataset} returned HTTP {r.status_code}: {r.text[:240]}")
        js = r.json()
        if js.get("error"):
            raise DataError(f"Eurostat {dataset}: {js['error']}")
        out = _decode_jsonstat(js)
        if not out.empty:
            _CACHE.set(cache_key, out)
        return out.copy()

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
        cache_key = ("ecb", flow, key, start, end)
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached.copy()
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
                    raise DataError(f"ECB {flow}/{key}: network error after 5 attempts ({type(exc).__name__})")
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
        s = s[~s.index.duplicated(keep="last")].sort_index()
        _CACHE.set(cache_key, s)
        return s.copy()

    def frame(self, flow: str, key: str) -> pd.DataFrame:
        """All series matching a (possibly wildcarded) key: columns KEY, TIME_PERIOD, OBS_VALUE.

        ECB SDMX keys accept empty positions as wildcards, e.g. ``A.U2.YER.A..0000``
        returns every projection exercise in one request.
        """
        cache_key = ("ecb-frame", flow, key)
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached.copy()
        r = _http_get(f"{ECB_DATA}/{flow}/{key}", params={"format": "csvdata", "detail": "dataonly"},
                      headers={"Accept": "text/csv"}, timeout=(6, self.timeout), retries=3, label=f"ECB {flow}")
        if r.status_code == 404:
            raise DataError(f"ECB {flow}/{key}: no matching series")
        if r.status_code != 200:
            raise DataError(f"ECB {flow}/{key} returned HTTP {r.status_code}")
        try:
            df = pd.read_csv(io.StringIO(r.text))
        except Exception:
            raise DataError(f"ECB {flow}/{key}: response could not be parsed")
        need = {"TIME_PERIOD", "OBS_VALUE"}
        if df.empty or not need <= set(df.columns):
            raise DataError(f"ECB {flow}/{key}: no observations returned")
        if "KEY" not in df.columns:
            df["KEY"] = f"{flow}.{key}"
        df = df[["KEY", "TIME_PERIOD", "OBS_VALUE"]].copy()
        df["OBS_VALUE"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
        df = df.dropna(subset=["OBS_VALUE"])
        _CACHE.set(cache_key, df)
        return df.copy()


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
    """Current HICP: one geo+component request, then choose index unit locally.

    Avoid sequential attempts for every base-year code, a major timeout multiplier.
    """
    code = {"CP00": "TOTAL", "NRG": "NRG"}.get(coicop, coicop)
    errors = []
    for dataset, dim, component in (("prc_hicp_minr", "coicop18", code),
                                    ("prc_hicp_midx", "coicop", coicop)):
        try:
            df = client.query(dataset, {"geo": geo, dim: component}, start_m, end_m)
            if df.empty:
                raise DataError("No observations")
            if "unit" in df.columns:
                units = [str(x) for x in df["unit"].dropna().unique()]
                selected = next((x for x in ("I25", "I15", "I10") if x in units), None)
                if selected is None:
                    selected = next((x for x in units if x.startswith("I")), None)
                if selected is None:
                    raise DataError(f"No HICP index unit in {units}")
                df = df[df["unit"].astype(str) == selected]
            return _time_value(df)
        except Exception as exc:
            errors.append(f"{dataset}: {exc}")
    raise DataError("HICP index unavailable: " + " | ".join(errors))


def _fetch_house_price_index(client: EurostatClient, geo: str, start_q: str, end_q: str) -> pd.Series:
    """Eurostat nominal House Price Index for EA aggregates and countries.

    Eurostat occasionally changes/rebases UNIT positions in ``prc_hpi_q``.  Rather
    than making the country download depend on one hard-coded UNIT code, request
    the small country slice and select TOTAL purchases + an index unit locally.
    This is robust to the 2010/2015/2025 reference-base transition.
    """
    # First try a minimally filtered country slice.  One country over the requested
    # window is small (purchase x unit x quarter) and avoids brittle SDMX code guesses.
    attempts = [{"geo": geo, "freq": "Q"}]
    df = None
    errors = []
    for filt in attempts:
        try:
            x = client.query("prc_hpi_q", filt, start=start_q, end=end_q)
            if not x.empty:
                df = x
                break
        except Exception as exc:
            errors.append(str(exc))
    if df is None or df.empty:
        detail = errors[-1] if errors else "No observations returned."
        raise DataError(f"Could not retrieve prc_hpi_q for {geo}. Last error: {detail}")

    # Total residential-property purchases.  Keep a defensive fallback in case a
    # future Eurostat structure omits PURCHASE from an already-totalled view.
    if "purchase" in df.columns:
        total = df[df["purchase"].astype(str).str.upper() == "TOTAL"]
        if not total.empty:
            df = total

    # Prefer the newest index base, then legacy index bases.  Do not accidentally
    # pick quarterly/annual percentage-change units from the same dataset.
    if "unit" in df.columns:
        units = [str(u) for u in df["unit"].dropna().unique()]
        preferred = ["I25_Q", "I15_Q", "I10_Q", "I25", "I15", "I10"]
        chosen = next((u for u in preferred if u in units), None)
        if chosen is None:
            # Generic Eurostat index-unit fallback; rate units normally start RCH.
            index_units = [u for u in units if u.upper().startswith("I")]
            chosen = index_units[0] if index_units else None
        if chosen is None:
            raise DataError(f"House price index for {geo}: no index unit found in prc_hpi_q (units: {units}).")
        df = df[df["unit"].astype(str) == chosen]

    q = _series_to_quarterly(_time_value(df))
    if q.dropna().empty:
        raise DataError(f"House price index: Eurostat returned no numeric observations for {geo}.")
    return q


def _house_price_to_frequency(q: pd.Series, freq: str) -> pd.Series:
    """Harmonise a native quarterly house-price index to the requested frequency.

    Monthly output repeats the quarterly observation within its three months rather
    than inventing intra-quarter price movements. Annual output averages quarters.
    """
    q = _series_to_quarterly(q)
    if freq == "Q":
        return q
    if freq == "A":
        return q.groupby(q.index.asfreq("Y")).mean()
    if freq == "M":
        out = {}
        for qp, val in q.items():
            for mp in pd.period_range(qp.asfreq("M", "start"), qp.asfreq("M", "end"), freq="M"):
                out[mp] = val
        return pd.Series(out, dtype=float).sort_index()
    raise ValueError(freq)


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
    # CLV10_MEUR stays first so existing workbooks keep the same level basis;
    # the newer 2020/2015 reference years are fallbacks if Eurostat retires it.
    cands = [
        {"geo": geo, "na_item": "B1GQ", "unit": "CLV10_MEUR", "s_adj": "SCA"},
        {"geo": geo, "na_item": "B1GQ", "unit": "CLV20_MEUR", "s_adj": "SCA"},
        {"geo": geo, "na_item": "B1GQ", "unit": "CLV15_MEUR", "s_adj": "SCA"},
        {"geo": geo, "na_item": "B1GQ", "unit": "CLV_I20", "s_adj": "SCA"},
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



_CL_D = np.arange(-2, 3)                 # month offsets a-b within a quarter pair
_CL_W = (3 - np.abs(_CL_D)).astype(float)  # how often each offset occurs (1,2,3,2,1)


def _chow_lin_cov(rho: float, q_start: np.ndarray) -> np.ndarray:
    """Quarterly covariance C V C' of an AR(1) monthly error, in closed form.

    V[m, n] = rho^|m-n| on actual month ordinals and C averages the three months
    of each quarter, so (C V C')[i, j] = (1/9) * sum_{a,b} rho^|s_i - s_j + a - b|
    with s the first-month ordinal of each quarter. This is O(nq^2) instead of
    the O(nm^2 nq) dense product, which made long US monthly samples time out.
    """
    lag = q_start[:, None] - q_start[None, :]
    expo = np.abs(lag[:, :, None] + _CL_D[None, None, :])
    return (np.power(rho, expo) * _CL_W).sum(axis=2) / 9.0


def _chow_lin_cross(rho: float, m_ord: np.ndarray, q_start: np.ndarray) -> np.ndarray:
    """Cross covariance V C' between monthly errors and quarterly averages."""
    lag = m_ord[:, None] - q_start[None, :]
    expo = np.abs(lag[:, :, None] - np.arange(3)[None, None, :])
    return np.power(rho, expo).sum(axis=2) / 3.0


def _chow_lin_monthly(q: pd.Series, indicator_m: pd.Series, refine: bool = True) -> pd.Series:
    """Chow-Lin (ML) disaggregation of quarterly GDP using monthly IP.

    Regressors: constant, standardised IP and a linear time trend. Quarterly real
    GDP is a level/volume measure, so each quarter's three monthly estimates
    average exactly to the published quarterly level. The AR(1) parameter is
    chosen by maximising the concentrated likelihood on a grid over
    [-0.90, 0.98], then refined locally by golden-section search.
    No months are extrapolated beyond the last published quarter.
    """
    q = q.dropna().astype(float).sort_index()
    ind_all = indicator_m.dropna().astype(float).sort_index()
    ind_all = ind_all[~ind_all.index.duplicated(keep="last")]
    have = set(ind_all.index)
    usable_q = []
    for qp in q.index:
        first = qp.asfreq("M", "start")
        if first in have and (first + 1) in have and (first + 2) in have:
            usable_q.append(qp)

    if len(usable_q) < 4:
        raise DataError(
            "Chow-Lin monthly GDP needs at least four complete quarters with "
            "monthly industrial-production data. Use Linear interpolation for a shorter sample."
        )

    nq = len(usable_q)
    y = q.loc[usable_q].to_numpy(dtype=float)
    first_months = [qp.asfreq("M", "start") for qp in usable_q]
    mindex = pd.PeriodIndex([fm + k for fm in first_months for k in range(3)], freq="M")
    q_start = np.array([fm.ordinal for fm in first_months], dtype=np.int64)
    m_ord = np.array([m.ordinal for m in mindex], dtype=np.int64)

    ind = ind_all.reindex(mindex).to_numpy(dtype=float)
    sd = float(np.std(ind))
    if not np.isfinite(sd) or sd == 0:
        raise DataError("Industrial production has no usable variation for Chow-Lin.")
    z = (ind - ind.mean()) / sd
    span = max(int(m_ord[-1] - m_ord[0]), 1)
    trend = -1.0 + 2.0 * (m_ord - m_ord[0]) / span
    X = np.column_stack([np.ones(len(ind)), z, trend])
    Xq = X.reshape(nq, 3, X.shape[1]).mean(axis=1)
    rhs = np.column_stack([Xq, y])

    # The lag structure does not depend on rho: precompute it once, then each
    # likelihood evaluation only builds a small power table and gathers from it.
    lag_q = q_start[:, None] - q_start[None, :]
    uniq, inv = np.unique(lag_q, return_inverse=True)
    expo_u = np.abs(uniq[:, None] + _CL_D[None, :])
    max_e = int(expo_u.max())
    inv = inv.reshape(lag_q.shape)

    def cov_q(rho: float) -> np.ndarray:
        pw = np.power(rho, np.arange(max_e + 1, dtype=float))
        vals = (pw[expo_u] * _CL_W).sum(axis=1) / 9.0
        return vals[inv]

    def fit(rho: float):
        Vq = cov_q(rho)
        try:
            L = np.linalg.cholesky(Vq)
        except np.linalg.LinAlgError:
            return None
        W = np.linalg.solve(L, rhs)            # whitened [Xq | y]
        Wx, wy = W[:, :-1], W[:, -1]
        beta, *_ = np.linalg.lstsq(Wx, wy, rcond=None)
        resid = wy - Wx @ beta
        sigma2 = float(resid @ resid) / nq
        if not np.isfinite(sigma2) or sigma2 <= 0:
            return None
        logdet = 2.0 * float(np.log(np.diag(L)).sum())
        return -0.5 * (nq * np.log(sigma2) + logdet), beta, Vq

    lo, hi = -0.90, 0.98
    best = None
    for rho in np.linspace(lo, hi, 189):
        res = fit(float(rho))
        if res is not None and (best is None or res[0] > best[0]):
            best = (res[0], float(rho), res[1], res[2])
    if best is None:
        raise DataError("Chow-Lin estimation failed numerically; use Linear interpolation.")

    if refine:
        step = (hi - lo) / 188
        a, b = max(lo, best[1] - step), min(hi, best[1] + step)
        g = (np.sqrt(5.0) - 1.0) / 2.0
        def score(r):
            res = fit(r)
            return -np.inf if res is None else res[0]
        c, d = b - g * (b - a), a + g * (b - a)
        fc, fd = score(c), score(d)
        for _ in range(30):
            if fc > fd:
                b, d, fd = d, c, fc
                c = b - g * (b - a)
                fc = score(c)
            else:
                a, c, fc = c, d, fd
                d = a + g * (b - a)
                fd = score(d)
        r_star = (a + b) / 2.0
        res = fit(r_star)
        if res is not None and res[0] > best[0]:
            best = (res[0], float(r_star), res[1], res[2])

    _, rho, beta, Vq = best
    base = X @ beta
    u = y - Xq @ beta
    monthly = base + _chow_lin_cross(rho, m_ord, q_start) @ np.linalg.solve(Vq, u)
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
    q = q.dropna().sort_index()
    if q.empty:
        return pd.Series(dtype=float)
    full = pd.period_range(q.index.min().asfreq("M", how="start"),
                           q.index.max().asfreq("M", how="end"), freq="M")
    if monthly_method == "Quarterly step (repeat within quarter)":
        # Each quarter's GDP level is repeated for its three constituent months.
        out = pd.Series(np.nan, index=full, dtype=float)
        for qp, val in q.items():
            first = qp.asfreq("M", "start")
            out.loc[[first, first + 1, first + 2]] = val
        return out
    if monthly_method == "Chow-Lin (industrial production)":
        if monthly_indicator is None or monthly_indicator.dropna().empty:
            raise DataError("Chow-Lin requires monthly industrial production as an indicator.")
        return _chow_lin_monthly(q, monthly_indicator)
    if monthly_method == "Linear interpolation":
        # A quarterly flow level is centred on the middle month of its quarter
        # (placing it on the last month would shift GDP one month late), then
        # interpolated linearly; the outer half-quarters hold the edge value.
        mid = pd.Series(q.to_numpy(dtype=float),
                        index=pd.PeriodIndex([p.asfreq("M", "start") + 1 for p in q.index], freq="M"))
        return mid.reindex(full).interpolate(method="linear", limit_area="inside").ffill().bfill()
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
        # Stop at today: carrying a rate into future days would invent averages
        # for months that have not happened yet.
        last_day = min(pd.Timestamp(f"{end_year}-12-31"), pd.Timestamp(date.today()))
        daily_index = pd.date_range(f"{start_year}-01-01", last_day, freq="D")
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
    fred_api_key: Optional[str] = None,
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
            # Eurostat unreachable: ECB series still work (they use U2); Eurostat-based
            # series will report their own errors instead of blocking everything.
            actual_geo = "EA20"

    area = _ecb_area(actual_geo, evolving_ea=requested_ea)
    # National accounts, money, lending and surveys download in the background
    # while the core series below are fetched.
    extra_vars = vars_set & set(EXTRA_EU_VARS)
    extra_tasks, extra_handle = {}, None
    if extra_vars:
        import macro_extra
        extra_tasks = macro_extra.eu_tasks(actual_geo, requested_ea, extra_vars, freq, start_year, end_year)
        extra_handle = macro_extra.start_tasks(extra_tasks, workers=3 if requested_ea else 2)
    data: Dict[str, pd.Series] = {}
    sources: Dict[str, str] = {}
    meta: List[FetchMeta] = []
    ip_monthly_indicator: Optional[pd.Series] = None
    ip_indicator_source: Optional[str] = None

    def add_meta(variable, src, sid, native, s, note=""):
        meta.append(FetchMeta(variable, src, sid, actual_geo, native,
                              str(s.dropna().index.max()) if not s.dropna().empty else None,
                              note))

    def _fail(vars_, label, exc):
        """A series that cannot be fetched becomes an empty, explained column."""
        reason = redact(exc)
        for v in vars_:
            if v in vars_set:
                data[v] = pd.Series(dtype=float)
                sources[v] = f"Unavailable: {reason[:200]}"
        if any(v in vars_set for v in vars_):
            meta.append(FetchMeta(label, "Unavailable", "", actual_geo, "", None, reason))

    # HICP overall
    # Index and annual inflation are selected independently. This matters because
    # Eurostat's direct annual-rate series can have a longer history than a
    # particular index-reference-base series.
    try:
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
    except Exception as exc:
        _fail(['hicp_level'], 'HICP level', exc)

    try:
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
            # Annual HICP inflation is a published monthly rate, not a rate inferred
            # from a potentially stale index-base vintage.  Select the candidate
            # with the freshest observed month, then fill earlier gaps from other
            # equivalent candidates.  ECB is the tie-breaker when equally fresh.
            usable = [(source, series.dropna().sort_index(), series_id)
                      for source, series, series_id in candidates_rate
                      if series is not None and not series.dropna().empty]
            usable.sort(key=lambda x: (x[1].index.max().ordinal,
                                       1 if x[0] == "ECB" else 0,
                                       len(x[1])), reverse=True)
            src, hr, sid = usable[0]
            latest_primary = hr.index.max()
            # Only backfill history: never overwrite the freshest published value
            # with a different source or a derived index-based annual rate.
            for fallback_source, fallback, fallback_sid in usable[1:]:
                earlier = fallback[fallback.index < hr.index.min()]
                if not earlier.empty:
                    hr = pd.concat([earlier, hr]).sort_index()
            data["hicp_rate"] = _aggregate_monthly(hr, freq, "mean")
            sources["hicp_rate"] = f"{src} · {sid}"
            add_meta("HICP rate", src, sid, "M", hr,
                     f"Published annual % change; freshest source selected first. "
                     f"Latest direct observation: {latest_primary}. "
                     "Earlier history may be extended from an equivalent alternative source.")
    except Exception as exc:
        _fail(['hicp_rate'], 'HICP rate', exc)

    try:
        # Energy HICP: Eurostat is authoritative mapping in the app; ECB candidate is
        # deliberately not guessed because the ICP classification differs from Eurostat's NRG aggregate.
        if "energy_index" in vars_set:
            e = _series_to_monthly(_fetch_hicp_level(client, actual_geo, start_m, end_m, "NRG"))
            data["energy_index"] = _aggregate_monthly(e, freq, "mean")
            sources["energy_index"] = "Eurostat"
            add_meta("Energy HICP", "Eurostat", "prc_hicp_minr (NRG)", "M", e,
                     "Eurostat HICP energy aggregate; current ECOICOP v2 table with legacy fallback.")
    except Exception as exc:
        _fail(['energy_index'], 'Energy HICP', exc)

    try:
        # Residential property prices. Eurostat HPI is natively quarterly. For a monthly
        # workbook the quarterly observation is repeated within the quarter (no invented
        # interpolation); annual output is the arithmetic mean of the available quarters.
        if "house_price_index" in vars_set:
            hpi_q = _fetch_house_price_index(client, actual_geo, start_q, end_q)
            hpi = _house_price_to_frequency(hpi_q, freq)
            data["house_price_index"] = hpi
            sources["house_price_index"] = "Eurostat · prc_hpi_q"
            add_meta("House price index", "Eurostat", "prc_hpi_q (TOTAL; current base 2025=100, legacy-base fallback)", "Q", hpi_q,
                     "Residential properties purchased by households. Current Eurostat HPI base is 2025=100; legacy index bases are accepted as fallbacks. Monthly output repeats each quarterly value within its three months; annual output averages quarters.")
    except Exception as exc:
        _fail(['house_price_index'], 'House price index', exc)

    try:
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
    except Exception as exc:
        _fail(['unemployment_rate'], 'Unemployment rate', exc)

    try:
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
    except Exception as exc:
        _fail(['ip', 'log_ip'], 'Industrial production', exc)

    try:
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
    except Exception as exc:
        _fail(['real_gdp', 'real_gdp_growth', 'log_real_gdp'], 'Real GDP', exc)

    if "credit_spread" in vars_set:
        # Euro-area corporate credit spread: best of the available candidates
        # (same rule as the US). The euro-denominated ICE index is a market-wide
        # benchmark, so it is never relabelled as a national spread.
        if requested_ea or str(actual_geo).startswith("EA"):
            cands, errs = [], []
            for sid, label in (("BAMLHE00EHYIOAS", "ICE BofA euro high-yield OAS"),):
                try:
                    cands.append({"label": label, "sid": sid, "publisher": "ICE BofA via FRED", "tier": 3, "native": "D",
                                  "series": _hy_spread_fred(sid, start_year, end_year, freq, fred_api_key)})
                except Exception as exc:
                    errs.append(redact(exc))
            best, summary = pick_best_source(cands, freq)
            if best is None:
                _fail(["credit_spread"], "Credit spread", DataError("; ".join(errs) or "no candidate returned data"))
            else:
                data["credit_spread"] = best["series"]
                sources["credit_spread"] = f"{best['label']} ({best['publisher']} · {best['sid']})"
                add_meta("Credit spread", best["publisher"], best["sid"], "D", best["series"],
                         f"Corporate credit spread, percentage points, period average; selected {best['label']}. "
                         f"Compared: {summary}. FRED keeps only three years of ICE BofA data since April 2026; "
                         "no free longer euro-area corporate spread is published.")
        else:
            data["credit_spread"] = pd.Series(dtype=float)
            sources["credit_spread"] = "Not available as a comparable country-specific corporate spread"
            add_meta("Credit spread", "Unavailable", "", "D", data["credit_spread"],
                     "No validated country-level corporate spread; left blank rather than substituting the euro-wide one.")

    # Country-specific sovereign yields.
    # For EA aggregate output the AAA curve remains available separately via
    # aaa_1y / aaa_10y. For individual countries, sovereign_* means that country's
    # own government yield; never silently replace it with the EA AAA curve.
    if "sovereign_10y" in vars_set:
      try:
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
      except Exception as exc:
        _fail(["sovereign_10y"], "Sovereign 10Y yield", exc)

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
            _fail([v], v, DataError(f"ECB {flow}.{key}: {redact(exc)}"))

    # Equity indices. EURO STOXX 50 is common to the euro-area/country outputs.
    # Individual-country output additionally uses the OECD harmonised national
    # share-price index directly from the OECD SDMX API.
    if "eurostoxx50" in vars_set:
        try:
            sx5e, sx5e_sid = _fetch_eurostoxx50(ecb, freq, start_year, end_year)
            data["eurostoxx50"] = sx5e
            sources["eurostoxx50"] = "ECB"
            add_meta("EURO STOXX 50", "ECB", sx5e_sid, "M", sx5e,
                     "Historical close; average of observations through period. Quarterly/annual output is averaged from monthly observations.")
        except Exception as exc:
            _fail(["eurostoxx50"], "EURO STOXX 50", exc)

    if "move" in vars_set:
        try:
            mv = _to_frequency(_yahoo_daily_closes("^MOVE", max(start_year - 1, 1900)), freq, "D")
            data["move"] = mv
            sources["move"] = "Yahoo Finance · ^MOVE"
            add_meta("MOVE index", "Yahoo Finance", "^MOVE", "D", mv,
                     "ICE BofA MOVE index: implied volatility of US Treasury options, basis points; average of daily "
                     "closes. It is the standard global rates-volatility gauge and is the same series in every "
                     "euro-area sheet. Not published on FRED.")
        except Exception as exc:
            _fail(["move"], "MOVE index", exc)

    if "country_equity_index" in vars_set and not requested_ea:
        try:
            eq, eq_sid = _fetch_country_equity_index(actual_geo, freq, start_year, end_year)
        except Exception as exc:
            eq, eq_sid = pd.Series(dtype=float), f"Unavailable: {redact(exc)[:200]}"
        data["country_equity_index"] = eq
        sources["country_equity_index"] = ("OECD · " + eq_sid) if not eq.empty else (
            eq_sid if eq_sid.startswith("Unavailable") else "Unavailable: OECD returned no observations")
        _idx_name = REFERENCE_EQUITY_INDEX.get(actual_geo, "National equity index")
        add_meta(_idx_name, "OECD" if not eq.empty else "Unavailable",
                 eq_sid, "M", eq,
                 f"{_idx_name} reference-market label; values use the OECD Financial Market harmonised national share-price series retrieved directly through the OECD SDMX API. Monthly observations are averaged for quarterly/annual output; unavailable country series are left blank.")

    if extra_tasks:
        import macro_extra
        d, s, m = macro_extra.collect_tasks(extra_handle, extra_tasks, actual_geo)
        data.update(d)
        sources.update(s)
        meta.extend(m)

    target = _period_range(freq, start_year, end_year)
    out = pd.DataFrame(index=target)
    # Put provenance immediately after each variable, as requested.
    for var in CANONICAL_VARS:
        if var in data:
            out[var] = data[var].reindex(target)
            out[f"{var}_source"] = sources.get(var, "")
    out.insert(0, "date", [_period_label(p, freq) for p in target])
    return out.reset_index(drop=True), meta


# ---------------------------------------------------------------------------
# FRED (US series and ICE BofA spreads)
# ---------------------------------------------------------------------------
#
# Order of attempts for every series:
#   1. Official FRED API (api.stlouisfed.org) when a key is available.
#   2. Keyless CSV (fred.stlouisfed.org/graph/fredgraph.csv?id=...) WITHOUT
#      cosd/coed. Date windows on that endpoint are unreliable: windows can be
#      ignored, a window that lands on a gap returns HTTP 404 instead of data,
#      and windowed requests have been observed to time out from cloud hosts
#      while plain requests succeed. The full series is small, so it is fetched
#      once, cached, and sliced locally.
# Both responses are validated (an HTML maintenance page can arrive with 200).

FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_FRED_KEY_RE = re.compile(r"[a-z0-9]{32}")
_FRED_API_LIMITER = _RateLimiter(0.55)
_FRED_CSV_LIMITER = _RateLimiter(0.35)

# Backwards-compatible process default. The UI passes keys explicitly per
# session, because a module global is shared by every visitor of a hosted app.
_FRED_API_KEY: Optional[str] = None


def configure_fred_api_key(key: Optional[str]):
    """Set a process-wide default key (kept for compatibility with older UIs)."""
    global _FRED_API_KEY
    _FRED_API_KEY = clean_fred_api_key(key)


def clean_fred_api_key(key: Optional[str]) -> Optional[str]:
    k = (key or "").strip().strip('"').strip("'")
    return k or None


def fred_key_problem(key: Optional[str]) -> Optional[str]:
    """Return a human-readable problem with a key's format, or None if it looks valid."""
    k = clean_fred_api_key(key)
    if k is None:
        return None
    if not _FRED_KEY_RE.fullmatch(k):
        return "A FRED API key is 32 characters, lowercase letters and digits only."
    return None


def _resolve_fred_key(api_key: Optional[str]) -> Optional[str]:
    k = clean_fred_api_key(api_key) or _FRED_API_KEY or clean_fred_api_key(os.environ.get("FRED_API_KEY"))
    return k if (k and _FRED_KEY_RE.fullmatch(k)) else None


def _fred_error_text(r: requests.Response) -> str:
    try:
        js = r.json()
        msg = js.get("error_message") or js.get("message") or ""
    except ValueError:
        msg = re.sub(r"<[^>]+>", " ", r.text or "")
    msg = re.sub(r"\s+", " ", str(msg)).strip()
    return redact(msg[:160])


def _series_from_dates(dates, values) -> pd.Series:
    idx = pd.to_datetime(pd.Series(list(dates)), errors="coerce")
    vals = pd.to_numeric(pd.Series(list(values)), errors="coerce")
    s = pd.Series(vals.to_numpy(dtype=float), index=pd.DatetimeIndex(idx))
    s = s[~s.index.isna()].dropna().sort_index()
    return s[~s.index.duplicated(keep="last")]


def _fred_via_api(series_id: str, key: str) -> pd.Series:
    r = _http_get(FRED_API_URL,
                  params={"series_id": series_id, "api_key": key, "file_type": "json"},
                  timeout=(6, 30), retries=2, limiter=_FRED_API_LIMITER, label="FRED API")
    if r.status_code != 200:
        detail = _fred_error_text(r)
        raise DataError(f"API HTTP {r.status_code}" + (f" ({detail})" if detail else ""))
    try:
        rows = r.json().get("observations") or []
    except ValueError:
        raise DataError("API returned a non-JSON response")
    s = _series_from_dates((x.get("date") for x in rows), (x.get("value") for x in rows))
    if s.empty:
        raise DataError("API returned no observations")
    return s


def _fred_via_csv(series_id: str) -> pd.Series:
    r = _http_get(FRED_CSV_URL, params={"id": series_id},
                  headers={"Accept": "text/csv,*/*;q=0.5"},
                  timeout=(6, 30), retries=2, limiter=_FRED_CSV_LIMITER, label="FRED CSV")
    if r.status_code == 404:
        raise DataError("CSV HTTP 404 (FRED does not recognise this series id, "
                        "or refused the request from this network)")
    if r.status_code != 200:
        raise DataError(f"CSV HTTP {r.status_code}")
    text = (r.text or "").lstrip("﻿")
    head = text[:300].lower()
    if "<html" in head or "<!doctype" in head:
        raise DataError("CSV request returned a web page instead of data "
                        "(FRED maintenance page or a blocked request)")
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception:
        raise DataError("CSV response could not be parsed")
    if df.shape[1] < 2:
        raise DataError("CSV response has no data column")
    col = next((c for c in df.columns if str(c).strip().upper() == series_id.upper()), df.columns[1])
    s = _series_from_dates(df.iloc[:, 0], df[col])
    if s.empty:
        raise DataError("CSV contained no observations")
    return s


def fred_series(series_id: str, api_key: Optional[str] = None) -> pd.Series:
    """Full history of a FRED series as a float Series on a DatetimeIndex (cached)."""
    cached = _CACHE.get(("fred", series_id))
    if cached is not None:
        return cached.copy()
    key = _resolve_fred_key(api_key)
    errors = []
    attempts = ([("API", lambda: _fred_via_api(series_id, key))] if key else []) + \
               [("CSV", lambda: _fred_via_csv(series_id))]
    for _, attempt in attempts:
        try:
            s = attempt()
            _CACHE.set(("fred", series_id), s)
            return s.copy()
        except DataError as exc:
            errors.append(redact(exc))
        except Exception as exc:  # parsing surprises must not leak URLs either
            errors.append(redact(type(exc).__name__))
    hint = "" if key else " Adding a free FRED API key enables the official API route."
    raise DataError(f"{series_id}: " + "; ".join(errors) + "." + hint)


def _fred_series(series_id: str, start_year: int, end_year: int,
                 api_key: Optional[str] = None) -> pd.Series:
    """Compatibility wrapper: FRED series restricted to a calendar-year window."""
    s = fred_series(series_id, api_key)
    return s[(s.index.year >= start_year) & (s.index.year <= end_year)]


def check_fred_connection(api_key: Optional[str] = None) -> Dict[str, str]:
    """Probe both FRED routes with a small request; never cached. For the UI."""
    out: Dict[str, str] = {}
    key = _resolve_fred_key(api_key)
    problem = fred_key_problem(api_key)
    if problem:
        out["api"] = problem
    elif not key:
        out["api"] = "No key entered."
    else:
        try:
            r = _http_get(FRED_API_URL, params={"series_id": "UNRATE", "api_key": key, "file_type": "json",
                                                "sort_order": "desc", "limit": 1},
                          timeout=(5, 15), retries=0, label="FRED API")
            out["api"] = "OK" if r.status_code == 200 else f"HTTP {r.status_code} {_fred_error_text(r)}".strip()
        except DataError as exc:
            out["api"] = redact(exc)
    try:
        _fred_via_csv("UNRATE")
        out["csv"] = "OK"
    except DataError as exc:
        out["csv"] = redact(exc)
    return out


def _hy_spread_fred(series_id: str, start_year: int, end_year: int, freq: str,
                    api_key: Optional[str] = None) -> pd.Series:
    return _to_frequency(fred_series(series_id, api_key), freq, "D")


# ---------------------------------------------------------------------------
# Frequency conversion for dated series
# ---------------------------------------------------------------------------

def _to_frequency(s: pd.Series, freq: str, native: str) -> pd.Series:
    """Convert a dated series to M/Q/A periods by arithmetic averaging.

    Daily/business-day data are averaged directly over each target period, so a
    quarter is the mean of its trading days rather than of three monthly means.
    Monthly data are averaged over the months in each quarter/year.
    """
    s = s.dropna()
    if s.empty:
        return pd.Series(dtype=float)
    idx = pd.DatetimeIndex(s.index)
    if native == "Q":
        q = pd.Series(s.to_numpy(dtype=float), index=idx.to_period("Q")).groupby(level=0).last()
        if freq == "Q":
            return q
        if freq == "A":
            return q.groupby(q.index.asfreq("Y")).mean()
        raise ValueError("Quarterly source series need an explicit monthly treatment.")
    target = {"M": "M", "Q": "Q", "A": "Y"}[freq]
    if native == "M":
        m = pd.Series(s.to_numpy(dtype=float), index=idx.to_period("M")).groupby(level=0).mean()
        return m if freq == "M" else m.groupby(m.index.asfreq(target)).mean()
    return pd.Series(s.to_numpy(dtype=float), index=idx.to_period(target)).groupby(level=0).mean()


def _dated_to_period(s: pd.Series, freq: str, native: str) -> pd.Series:
    """Compatibility alias for older callers."""
    return _to_frequency(s, freq, native)


def _daily_market_to_target(s: pd.Series, freq: str) -> pd.Series:
    """Aggregate daily market closes to the requested frequency by arithmetic mean."""
    return _to_frequency(s, freq, "D")


# ---------------------------------------------------------------------------
# Yahoo Finance (long S&P 500 / Dow history; FRED only carries ten years)
# ---------------------------------------------------------------------------

_YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")


def _yahoo_daily_closes(symbol: str, start_year: int) -> pd.Series:
    """Daily closes from Yahoo's chart endpoint, trying both public hosts."""
    from datetime import datetime, timezone
    from urllib.parse import quote

    cache_key = ("yahoo", symbol, start_year)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached.copy()
    params = {
        "period1": int(datetime(start_year, 1, 1, tzinfo=timezone.utc).timestamp()),
        "period2": int(time.time()) + 86400,
        "interval": "1d",
        "events": "history",
    }
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Macro-Data-Loader/2.0)",
               "Accept": "application/json,text/plain,*/*"}
    errors = []
    for host in _YAHOO_HOSTS:
        url = f"https://{host}/v8/finance/chart/{quote(symbol, safe='')}"
        try:
            r = _http_get(url, params=params, headers=headers, timeout=(5, 25), retries=1, label="Yahoo")
        except DataError as exc:
            errors.append(str(exc))
            continue
        try:
            chart = r.json().get("chart", {})
        except ValueError:
            errors.append(f"HTTP {r.status_code}, non-JSON body")
            continue
        if r.status_code != 200 or chart.get("error"):
            err = chart.get("error") or {}
            errors.append(f"HTTP {r.status_code}" + (f" ({err.get('description')})" if err.get("description") else ""))
            continue
        result = (chart.get("result") or [{}])[0]
        stamps = result.get("timestamp") or []
        quote_block = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote_block.get("close") or []
        n = min(len(stamps), len(closes))
        if n == 0:
            errors.append("no observations")
            continue
        exch_tz = (result.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York"
        try:
            dt = pd.to_datetime(stamps[:n], unit="s", utc=True).tz_convert(exch_tz).tz_localize(None).normalize()
        except Exception:
            dt = pd.to_datetime(stamps[:n], unit="s", utc=True).tz_localize(None).normalize()
        s = _series_from_dates(dt, closes[:n])
        if s.empty:
            errors.append("no numeric observations")
            continue
        _CACHE.set(cache_key, s)
        return s.copy()
    raise DataError(f"Yahoo Finance {symbol}: " + "; ".join(errors))


def _yahoo_market_series(symbol: str, start_year: int, end_year: int, freq: str) -> pd.Series:
    """Period average of daily closes (kept for compatibility)."""
    s = _yahoo_daily_closes(symbol, start_year)
    return _to_frequency(s[s.index.year <= end_year], freq, "D")


def _fetch_eurostoxx50(ecb: ECBClient, freq: str, start_year: int, end_year: int) -> tuple[pd.Series, str]:
    """EURO STOXX 50 from the ECB Real Time Database, monthly period-average close."""
    key = "M.S0.N.C_DJE50.X"
    s = _ecb_monthly_to_target(ecb, "RTD", key, freq, start_year, end_year)
    return s, f"RTD.{key}"


# ISO-3 codes used by the OECD Financial Market dataset.
# Greece is EL internally in the app but GRC at the OECD boundary.
_OECD_COUNTRY3 = {
    "AT": "AUT", "BE": "BEL", "CY": "CYP", "DE": "DEU", "EE": "EST",
    "EL": "GRC", "ES": "ESP", "FI": "FIN", "FR": "FRA", "IE": "IRL",
    "IT": "ITA", "LT": "LTU", "LU": "LUX", "LV": "LVA", "MT": "MLT",
    "NL": "NLD", "PT": "PRT", "SI": "SVN", "SK": "SVK",
}


def _oecd_country_equity_monthly(country: str, start_year: int, end_year: int) -> pd.Series:
    """Download the OECD harmonised national share-price index directly via SDMX.

    Dataset: OECD.SDD.STES, DSD_STES@DF_FINMARK, version 4.0
    Selection: <country>.M.SHARE.IX.....

    Direct OECD access avoids the former Yahoo ticker-by-ticker route and the
    FRED mirror.  The query is deliberately narrow (one country, one measure,
    requested dates only) so country-panel downloads remain small.
    """
    iso3 = _OECD_COUNTRY3.get(country)
    if not iso3:
        return pd.Series(dtype=float)

    key = f"{iso3}.M.SHARE.IX....."
    url = (
        "https://sdmx.oecd.org/public/rest/data/"
        f"OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/{key}"
    )
    params = {
        "startPeriod": f"{start_year}-01",
        "endPeriod": f"{end_year}-12",
        "dimensionAtObservation": "AllDimensions",
        "format": "csvfile",
    }
    headers = {
        "User-Agent": "Macro-Data-Updater/1.0",
        "Accept": "text/csv,application/vnd.sdmx.data+csv;q=0.9,*/*;q=0.1",
    }

    cache_key = ("oecd", key, start_year, end_year)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached.copy()
    r = _http_get(url, params=params, headers=headers, timeout=(8, 30), retries=2, label=f"OECD {iso3}")
    if r.status_code == 404 or not r.content.strip():
        return pd.Series(dtype=float)          # OECD answers 404 when a country has no series
    if r.status_code != 200:
        raise DataError(f"OECD national share-price request for {iso3} returned HTTP {r.status_code}")
    try:
        df = pd.read_csv(io.BytesIO(r.content))
    except Exception:
        raise DataError(f"OECD national share-price response for {iso3} could not be parsed")
    if df.empty:
        return pd.Series(dtype=float)
    # SDMX CSV normally exposes TIME_PERIOD and OBS_VALUE; tolerate casing variants.
    cols = {str(c).upper(): c for c in df.columns}
    tcol, vcol = cols.get("TIME_PERIOD"), cols.get("OBS_VALUE")
    if tcol is None or vcol is None:
        raise DataError(f"OECD national equity response has unexpected columns: {list(df.columns)[:12]}")
    periods = pd.PeriodIndex(df[tcol].astype(str), freq="M")
    values = pd.to_numeric(df[vcol], errors="coerce").to_numpy()
    out = pd.Series(values, index=periods, dtype=float).dropna()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    _CACHE.set(cache_key, out)
    return out.copy()




def _fetch_country_equity_index(country: str, freq: str, start_year: int, end_year: int) -> tuple[pd.Series, str]:
    """Fetch a harmonised national share-price index directly from OECD.

    Monthly OECD observations are returned as-is. Quarterly and annual series
    are arithmetic averages of those monthly observations, matching the app's
    period-average convention for EURO STOXX 50 and other financial series.
    """
    monthly = _oecd_country_equity_monthly(country, start_year, end_year)
    if monthly.empty:
        return pd.Series(dtype=float), "OECD national share-price index unavailable"

    if freq == "M":
        out = monthly
    elif freq == "Q":
        out = monthly.groupby(monthly.index.asfreq("Q")).mean()
    elif freq == "A":
        out = monthly.groupby(monthly.index.asfreq("Y")).mean()
    else:
        raise ValueError(freq)

    iso3 = _OECD_COUNTRY3.get(country, country)
    return out, f"OECD DSD_STES@DF_FINMARK 4.0 {iso3}.M.SHARE.IX"


# ---------------------------------------------------------------------------
# United States
# ---------------------------------------------------------------------------

GDP_VARS = {"real_gdp", "real_gdp_growth", "log_real_gdp"}


TIER_OFFICIAL, TIER_INTERNATIONAL, TIER_MARKET = 1, 2, 3
TIER_NAMES = {1: "official statistics or central bank", 2: "international organisation or official mirror",
              3: "market data vendor"}


def pick_best_source(candidates: list, freq: str):
    """Rank candidate series for one concept and return (best or None, summary).

    ``candidates`` holds dicts with label, sid, publisher, series (already at the
    output frequency) and optionally ``tier`` (1 official statistics or central
    bank, 2 international organisation or official mirror, 3 market data vendor;
    default 2). The ranking is:

    1. up to date: last observation within three months (one quarter or one
       year at lower frequencies) of the freshest candidate;
    2. sample length: candidates within 5% of the longest span stay in the race,
       so a meaningfully longer history always wins;
    3. reliability tier, then the longer span, then the more recent end.
    """
    usable = [c for c in candidates if not c["series"].dropna().empty]
    if not usable:
        return None, ""
    def bounds(c):
        x = c["series"].dropna()
        return x.index.min(), x.index.max()
    def span(c):
        lo, hi = bounds(c)
        return hi.ordinal - lo.ordinal
    tol = {"M": 3, "Q": 1, "A": 1}[freq]
    freshest = max(bounds(c)[1].ordinal for c in usable)
    current = [c for c in usable if bounds(c)[1].ordinal >= freshest - tol]
    longest = max(span(c) for c in current)
    near = [c for c in current if span(c) >= longest - int(0.05 * longest)]
    best = min(near, key=lambda c: (c.get("tier", 2), -span(c), -bounds(c)[1].ordinal))
    summary = "; ".join(f"{c['label']} ({c['sid']}): {bounds(c)[0]} to {bounds(c)[1]}"
                        for c in sorted(usable, key=lambda c: bounds(c)[0].ordinal))
    return best, summary

# variable -> (FRED id, publisher, native frequency, note)
_US_DIRECT = {
    "unemployment_rate": ("UNRATE", "BLS", "M", "Civilian unemployment rate (CPS), seasonally adjusted, percent."),
    "energy_cpi": ("CPIENGSL", "BLS", "M", "CPI-U energy index, seasonally adjusted, 1982-84=100."),
    "fed_funds": ("FEDFUNDS", "Federal Reserve", "M", "Effective federal funds rate, monthly average of daily values, percent."),
    "sofr": ("SOFR", "Federal Reserve Bank of New York", "D", "Secured Overnight Financing Rate, daily; averaged over each period. Starts April 2018."),
    "treasury_1y": ("GS1", "Federal Reserve H.15", "M", "1-year Treasury constant-maturity yield, monthly average, percent."),
    "treasury_10y": ("GS10", "Federal Reserve H.15", "M", "10-year Treasury constant-maturity yield, monthly average, percent."),
}
_US_MARKETS = {   # variable -> (Yahoo symbol, FRED id, label, note)
    "sp500": ("^GSPC", "SP500", "S&P 500", "Price index (no dividends); average of daily closes. FRED carries only ten years."),
    "djia": ("^DJI", "DJIA", "Dow Jones Industrial Average", "Price index (no dividends); average of daily closes. FRED carries only ten years."),
    "nasdaq": ("^IXIC", "NASDAQCOM", "Nasdaq Composite", "Nasdaq Composite price index (from February 1971); average of daily closes."),
    "vix": ("^VIX", "VIXCLS", "CBOE VIX", "CBOE Volatility Index, 30-day implied volatility of the S&P 500 (from 1990), index points; average of daily closes."),
}
_US_LABELS = {
    "real_gdp": "Real GDP", "real_gdp_growth": "Real GDP growth", "log_real_gdp": "Log real GDP",
    "cpi_level": "CPI level", "inflation_rate": "CPI inflation", "unemployment_rate": "Unemployment rate",
    "ip": "Industrial production", "log_ip": "Log industrial production", "energy_cpi": "Energy CPI",
    "house_price_index": "House price index", "fed_funds": "Federal funds rate", "sofr": "SOFR",
    "treasury_1y": "Treasury 1Y yield", "treasury_10y": "Treasury 10Y yield",
    "sp500": "S&P 500", "djia": "Dow Jones Industrial Average", "nasdaq": "Nasdaq Composite",
    "vix": "CBOE VIX", "credit_spread": "Credit spread",
}

FED_EBP_URL = "https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv"


def fed_gz_ebp() -> pd.DataFrame:
    """Gilchrist-Zakrajsek credit spread and excess bond premium (Federal Reserve Board).

    Monthly from January 1973, percentage points. The Board revises the whole
    history each month, so the file is cached like any other download.
    """
    cached = _CACHE.get(("fed", "ebp"))
    if cached is not None:
        return cached.copy()
    r = _http_get(FED_EBP_URL, headers={"Accept": "text/csv,*/*;q=0.5"}, timeout=(6, 30),
                  retries=2, label="Federal Reserve Board")
    if r.status_code != 200:
        raise DataError(f"Federal Reserve Board EBP file returned HTTP {r.status_code}")
    text = (r.text or "").lstrip("\ufeff")
    if "<html" in text[:300].lower():
        raise DataError("Federal Reserve Board returned a web page instead of the EBP CSV")
    df = pd.read_csv(io.StringIO(text))
    df.columns = [str(c).strip().lower() for c in df.columns]
    if not {"date", "gz_spread", "ebp"} <= set(df.columns):
        raise DataError(f"Unexpected EBP file columns: {list(df.columns)[:8]}")
    idx = pd.to_datetime(df["date"], errors="coerce")
    out = pd.DataFrame({c: pd.to_numeric(df[c], errors="coerce").to_numpy() for c in ("gz_spread", "ebp")},
                       index=pd.DatetimeIndex(idx))
    out = out[~out.index.isna()].sort_index()
    if out.dropna(how="all").empty:
        raise DataError("Federal Reserve Board EBP file contained no observations")
    _CACHE.set(("fed", "ebp"), out)
    return out.copy()


def _latest(s: pd.Series) -> Optional[str]:
    s = s.dropna()
    return str(s.index.max()) if not s.empty else None


def _yoy_lag(freq: str) -> int:
    return 12 if freq == "M" else 4 if freq == "Q" else 1


def fetch_us_dataset(freq: str, start_year: int, end_year: int,
                     variables: Sequence[str] = US_VARS,
                     monthly_gdp_method: str = "Chow-Lin (industrial production)",
                     fred_api_key: Optional[str] = None,
                     deadline_seconds: float = 90,
                     progress: Optional[Callable[[float, str], None]] = None,
                     ) -> Tuple[pd.DataFrame, List[FetchMeta]]:
    """US macro dataset from official publishers, distributed through FRED.

    Every raw series is downloaded once (INDPRO serves both IP and the Chow-Lin
    indicator), concurrently and politely rate-limited, then transformed. A
    series that fails becomes an empty column whose *_source cell states why,
    so one unavailable provider never blocks the workbook.

    Definitions (identical for the euro-area side):
      * growth and inflation are simple year-on-year percentage changes;
      * inflation at quarterly/annual frequency is the period average of the
        monthly year-on-year rates;
      * daily series are averaged over the trading days of each period.
    """
    requested = [v for v in dict.fromkeys(variables) if v in US_VARS]
    vars_set = set(requested)
    key = _resolve_fred_key(fred_api_key)

    need_gdp = bool(GDP_VARS & vars_set)
    chow_lin = need_gdp and freq == "M" and monthly_gdp_method == "Chow-Lin (industrial production)"
    jobs: Dict[str, Callable[[], object]] = {}
    if need_gdp:
        jobs["GDPC1"] = lambda: fred_series("GDPC1", key)
    if {"ip", "log_ip"} & vars_set or chow_lin:
        jobs["INDPRO"] = lambda: fred_series("INDPRO", key)
    if {"cpi_level", "inflation_rate"} & vars_set:
        jobs["CPIAUCSL"] = lambda: fred_series("CPIAUCSL", key)
    if "house_price_index" in vars_set:
        jobs["USSTHPI"] = lambda: fred_series("USSTHPI", key)
    for var, (sid, *_rest) in _US_DIRECT.items():
        if var in vars_set:
            jobs[sid] = (lambda sid=sid: fred_series(sid, key))
    for var, (symbol, fred_id, *_rest) in _US_MARKETS.items():
        if var in vars_set:
            jobs[f"yahoo:{symbol}"] = (lambda symbol=symbol: _yahoo_daily_closes(symbol, max(start_year - 1, 1900)))
            jobs[fred_id] = (lambda fred_id=fred_id: fred_series(fred_id, key))
    if "credit_spread" in vars_set:
        jobs["BAA10YM"] = lambda: fred_series("BAA10YM", key)
        jobs["FED_EBP"] = fed_gz_ebp
        jobs["BAMLH0A0HYM2"] = lambda: fred_series("BAMLH0A0HYM2", key)

    extra_tasks = {}
    if vars_set & set(EXTRA_US_VARS):
        import macro_extra
        extra_tasks = macro_extra.us_tasks(vars_set & set(EXTRA_US_VARS), freq, key)
        for var, (_sid, fn) in extra_tasks.items():
            jobs[f"extra:{var}"] = fn

    raw: Dict[str, object] = {}
    failed: Dict[str, str] = {}
    if jobs:
        pool = ThreadPoolExecutor(max_workers=min(5, len(jobs)), thread_name_prefix="us-fetch")
        futures = {pool.submit(fn): name for name, fn in jobs.items()}
        pending = set(futures)
        t_end = time.monotonic() + deadline_seconds
        while pending:
            done, pending = _futures_wait(pending, timeout=max(0.0, t_end - time.monotonic()),
                                          return_when="FIRST_COMPLETED")
            if not done:
                break
            for fut in done:
                name = futures[fut]
                try:
                    raw[name] = fut.result()
                except Exception as exc:
                    failed[name] = redact(exc)
            if progress:
                n_done = len(raw) + len(failed)
                progress(n_done / len(jobs), f"Downloaded {n_done} of {len(jobs)} US series")
        for fut in pending:
            failed[futures[fut]] = f"no response within {int(deadline_seconds)}s"
        pool.shutdown(wait=False, cancel_futures=True)

    data: Dict[str, pd.Series] = {}
    sources: Dict[str, str] = {}
    meta: List[FetchMeta] = []

    def unavailable(var: str, sid: str, reason: str):
        data[var] = pd.Series(dtype=float)
        sources[var] = f"Unavailable: {reason[:200]}"
        meta.append(FetchMeta(_US_LABELS.get(var, var), "Unavailable", sid, "US", "", None, reason))

    ip_m = None
    if "INDPRO" in raw:
        ip_m = _to_frequency(raw["INDPRO"], "M", "M")

    # Industrial production
    if {"ip", "log_ip"} & vars_set:
        if ip_m is None:
            for v in ("ip", "log_ip"):
                if v in vars_set:
                    unavailable(v, "INDPRO", failed.get("INDPRO", "not downloaded"))
        else:
            ip_f = _aggregate_monthly(ip_m, freq, "mean")
            if "ip" in vars_set:
                data["ip"] = ip_f
                sources["ip"] = "Federal Reserve G.17 via FRED · INDPRO"
            if "log_ip" in vars_set:
                data["log_ip"] = np.log(ip_f.where(ip_f > 0))
                sources["log_ip"] = "Derived: ln(INDPRO)"
            meta.append(FetchMeta("Industrial production", "Federal Reserve G.17", "INDPRO", "US", "M",
                                  _latest(ip_m), "Industrial production index, 2017=100, seasonally adjusted."))

    # Real GDP
    if need_gdp:
        if "GDPC1" not in raw:
            for v in GDP_VARS & vars_set:
                unavailable(v, "GDPC1", failed.get("GDPC1", "not downloaded"))
        elif chow_lin and ip_m is None:
            for v in GDP_VARS & vars_set:
                unavailable(v, "GDPC1", "Chow-Lin needs INDPRO as indicator: " + failed.get("INDPRO", "not downloaded"))
        else:
            gq = _to_frequency(raw["GDPC1"], "Q", "Q")
            try:
                g = _gdp_to_frequency(gq, freq, monthly_gdp_method, ip_m)
                label = "BEA via FRED · GDPC1"
                if freq == "M":
                    label = (f"Chow-Lin (GDPC1 + INDPRO, rho={g.attrs.get('chow_lin_rho', float('nan')):.3f})"
                             if chow_lin else f"{monthly_gdp_method} of GDPC1")
                if "real_gdp" in vars_set:
                    data["real_gdp"] = g
                    sources["real_gdp"] = label
                if "log_real_gdp" in vars_set:
                    data["log_real_gdp"] = np.log(g.where(g > 0))
                    sources["log_real_gdp"] = f"Derived: ln({label})"
                if "real_gdp_growth" in vars_set:
                    data["real_gdp_growth"] = g.pct_change(_yoy_lag(freq), fill_method=None) * 100.0
                    sources["real_gdp_growth"] = f"Derived: y/y % change of {label}"
                meta.append(FetchMeta("Real GDP", "BEA", "GDPC1", "US", "Q", _latest(gq),
                                      "Real GDP, billions of chained 2017 dollars, SAAR. "
                                      + ("Monthly values from the selected temporal disaggregation; each quarter's "
                                         "monthly values average to the published quarter." if freq == "M" else
                                         "Annual values average the four quarterly levels." if freq == "A" else "")))
            except DataError as exc:
                for v in GDP_VARS & vars_set:
                    unavailable(v, "GDPC1", redact(exc))

    # CPI
    if {"cpi_level", "inflation_rate"} & vars_set:
        if "CPIAUCSL" not in raw:
            for v in ("cpi_level", "inflation_rate"):
                if v in vars_set:
                    unavailable(v, "CPIAUCSL", failed.get("CPIAUCSL", "not downloaded"))
        else:
            cm = _to_frequency(raw["CPIAUCSL"], "M", "M")
            if "cpi_level" in vars_set:
                data["cpi_level"] = _aggregate_monthly(cm, freq, "mean")
                sources["cpi_level"] = "BLS via FRED · CPIAUCSL"
            if "inflation_rate" in vars_set:
                infl_m = cm.pct_change(12, fill_method=None) * 100.0
                data["inflation_rate"] = _aggregate_monthly(infl_m.dropna(), freq, "mean")
                sources["inflation_rate"] = "Derived: 12-month % change of CPIAUCSL" + (
                    ", averaged over the period" if freq != "M" else "")
            meta.append(FetchMeta("CPI", "BLS", "CPIAUCSL", "US", "M", _latest(cm),
                                  "CPI-U all items, seasonally adjusted, 1982-84=100."))

    # Direct series
    for var, (sid, publisher, native, note) in _US_DIRECT.items():
        if var not in vars_set:
            continue
        if sid not in raw:
            unavailable(var, sid, failed.get(sid, "not downloaded"))
            continue
        s = _to_frequency(raw[sid], freq, native)
        data[var] = s
        sources[var] = f"{publisher} via FRED · {sid}" if "FRED" not in publisher else f"{publisher} · {sid}"
        meta.append(FetchMeta(_US_LABELS[var], publisher, sid, "US", native, _latest(s), note))

    # House prices (native quarterly)
    if "house_price_index" in vars_set:
        if "USSTHPI" not in raw:
            unavailable("house_price_index", "USSTHPI", failed.get("USSTHPI", "not downloaded"))
        else:
            hq = _to_frequency(raw["USSTHPI"], "Q", "Q")
            data["house_price_index"] = _house_price_to_frequency(hq, freq)
            sources["house_price_index"] = "FHFA via FRED · USSTHPI"
            meta.append(FetchMeta("House price index", "FHFA", "USSTHPI", "US", "Q", _latest(hq),
                                  "FHFA All-Transactions HPI, 1980Q1=100, not seasonally adjusted. Monthly output "
                                  "repeats each quarter's value; annual output averages quarters."))

    # Credit spread: best available of three corporate-spread measures
    if "credit_spread" in vars_set:
        cands = []
        if "BAA10YM" in raw:
            cands.append({"label": "Moody's Baa minus 10Y Treasury", "sid": "BAA10YM", "publisher": "Moody's via FRED", "tier": 3,
                          "native": "M", "series": _to_frequency(raw["BAA10YM"], freq, "M")})
        if "FED_EBP" in raw:
            cands.append({"label": "Gilchrist-Zakrajsek credit spread", "sid": "gz_spread",
                          "publisher": "Federal Reserve Board", "tier": 1, "native": "M",
                          "series": _to_frequency(raw["FED_EBP"]["gz_spread"], freq, "M")})
        if "BAMLH0A0HYM2" in raw:
            cands.append({"label": "ICE BofA US high-yield OAS", "sid": "BAMLH0A0HYM2", "publisher": "ICE BofA via FRED",
                          "native": "D", "series": _to_frequency(raw["BAMLH0A0HYM2"], freq, "D")})
        best, summary = pick_best_source(cands, freq)
        if best is None:
            reasons = "; ".join(failed[k] for k in ("BAA10YM", "FED_EBP", "BAMLH0A0HYM2") if k in failed)
            unavailable("credit_spread", "BAA10YM / gz_spread / BAMLH0A0HYM2", reasons or "no candidate returned data")
        else:
            data["credit_spread"] = best["series"]
            sources["credit_spread"] = f"{best['label']} ({best['publisher']} · {best['sid']})"
            meta.append(FetchMeta("Credit spread", best["publisher"], best["sid"], "US", best["native"],
                                  _latest(best["series"]),
                                  f"Corporate credit spread, percentage points; selected {best['label']} as the "
                                  f"longest up-to-date candidate. Compared: {summary}. The candidates cover "
                                  "different bonds (Baa-rated, all senior unsecured, high-yield), so one is used, "
                                  "never spliced. FRED keeps only three years of ICE BofA data since April 2026."))

    # Equity and volatility indices: Yahoo and FRED compared, best one kept
    for var, (symbol, fred_id, label, note) in _US_MARKETS.items():
        if var not in vars_set:
            continue
        cands = []
        if f"yahoo:{symbol}" in raw:
            cands.append({"label": "Yahoo Finance", "sid": symbol, "publisher": "Yahoo Finance", "tier": 3, "native": "D",
                          "series": _to_frequency(raw[f"yahoo:{symbol}"], freq, "D")})
        if fred_id in raw:
            cands.append({"label": "FRED", "sid": fred_id, "publisher": "FRED", "tier": 2, "native": "D",
                          "series": _to_frequency(raw[fred_id], freq, "D")})
        best, summary = pick_best_source(cands, freq)
        if best is None:
            reasons = "; ".join(failed[k] for k in (f"yahoo:{symbol}", fred_id) if k in failed)
            unavailable(var, f"{symbol} / {fred_id}", reasons or "no source returned data")
            continue
        data[var] = best["series"]
        sources[var] = f"{best['publisher']} · {best['sid']}"
        meta.append(FetchMeta(label, best["publisher"], best["sid"], "US", "D", _latest(best["series"]),
                              f"{note} Compared: {summary}."))

    for var, (sid, _fn) in extra_tasks.items():
        res = raw.get(f"extra:{var}")
        if res is None:
            macro_extra.absorb_failure(var, sid, "US", DataError(failed.get(f"extra:{var}", "not downloaded")),
                                       data, sources, meta)
        else:
            data[var], sources[var], m = res
            meta.append(m)

    target = _period_range(freq, start_year, end_year)
    out = pd.DataFrame(index=target)
    for var in US_VARS:
        if var in data:
            out[var] = data[var].reindex(target)
            out[f"{var}_source"] = sources.get(var, "")
    out.insert(0, "date", [_period_label(p, freq) for p in target])
    return out.reset_index(drop=True), meta


def fetch_us_dataset_fast(freq, start_year, end_year, variables,
                          monthly_gdp_method="Chow-Lin (industrial production)",
                          deadline_seconds=90, fred_api_key=None, progress=None):
    """Kept for compatibility: fetch_us_dataset is now concurrent itself."""
    return fetch_us_dataset(freq, start_year, end_year, variables, monthly_gdp_method,
                            fred_api_key=fred_api_key, deadline_seconds=deadline_seconds,
                            progress=progress)


def fetch_country_panel(countries: Sequence[str], freq: str, start_year: int, end_year: int,
                        variables: Sequence[str], monthly_gdp_method: str,
                        progress=None, fred_api_key: Optional[str] = None,
                        ) -> Tuple[Dict[str, pd.DataFrame], List[FetchMeta]]:
    """Download country panels concurrently, with independent HTTP sessions."""
    from concurrent.futures import as_completed
    results = {}
    metas = {}
    errors = {}
    countries = list(countries)

    def worker(geo):
        return fetch_macro_dataset(geo, freq, start_year, end_year, variables,
                                   monthly_gdp_method, client=EurostatClient(),
                                   fred_api_key=fred_api_key)

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(countries)))) as pool:
        futures = {pool.submit(worker, geo): geo for geo in countries}
        for i, future in enumerate(as_completed(futures), 1):
            geo = futures[future]
            try:
                results[geo], metas[geo] = future.result()
            except Exception as exc:
                errors[geo] = redact(exc)
            if progress:
                progress(i / max(1, len(countries)), f"Completed {i} of {len(countries)} countries")
    if errors:
        raise DataError("Country downloads failed: " + "; ".join(f"{c}: {e}" for c, e in errors.items()))
    return ({c: results[c] for c in countries},
            [item for c in countries for item in metas[c]])


# ---------------------------------------------------------------------------
# Output window
# ---------------------------------------------------------------------------

def label_to_period(v, freq: str) -> Optional[pd.Period]:
    """Parse the date labels this module writes (YYYY-MM, YYYYQn / YYYYqn, YYYY)."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, pd.Period):
        return v.asfreq({"M": "M", "Q": "Q", "A": "Y"}[freq])
    s = str(v).strip().upper().replace("-Q", "Q").replace(" ", "")
    try:
        if freq == "A":
            return pd.Period(s[:4], freq="Y")
        if freq == "Q":
            return pd.Period(s, freq="Q")
        return pd.Period(s[:7], freq="M")
    except Exception:
        return None


def trim_to_window(df: pd.DataFrame, freq: str, start, end, drop_empty_edges: bool = True) -> pd.DataFrame:
    """Keep rows inside [start, end] at the output frequency.

    ``start``/``end`` are calendar dates from the UI: 15 March 2001 means
    2001-03 (monthly), 2001Q1 (quarterly) or 2001 (annual). With
    ``drop_empty_edges`` rows before the first and after the last observation of
    any selected series are removed, so the sheet does not end in empty months
    that have not been published yet.
    """
    if df is None or df.empty or "date" not in df.columns:
        return df
    pfreq = {"M": "M", "Q": "Q", "A": "Y"}[freq]
    lo, hi = pd.Period(pd.Timestamp(start), freq=pfreq), pd.Period(pd.Timestamp(end), freq=pfreq)
    periods = df["date"].map(lambda v: label_to_period(v, freq))
    keep = periods.map(lambda p: p is not None and lo <= p <= hi).astype(bool)
    out = df.loc[keep]
    if drop_empty_edges and not out.empty:
        value_cols = [c for c in out.columns
                      if c not in ("date", "country") and not str(c).endswith("_source")]
        if value_cols:
            has = out[value_cols].apply(pd.to_numeric, errors="coerce").notna().any(axis=1).to_numpy()
            if has.any():
                first, last = int(np.argmax(has)), len(has) - 1 - int(np.argmax(has[::-1]))
                out = out.iloc[first:last + 1]
            else:
                out = out.iloc[0:0]
    return out.reset_index(drop=True)
