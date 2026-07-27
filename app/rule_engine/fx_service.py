"""
app.rule_engine.fx_service
============================
FX rate resolution for cross-currency payment evaluation.

Priority order:
  1. gl_daily_rates DB table   (loaded from a finance-provided file --
                                see gl_rates/watcher.py -- NOT a live REST
                                call. There is no Oracle GL Daily Rates
                                REST API available in this environment;
                                finance drops an extract file into a
                                watched folder, the same way the aging
                                report arrives, and it gets parsed and
                                upserted into this table.)
  2. Static fallback rate map  (hardcoded approximates -- NEVER use for
                                posting, only for rule-band evaluation
                                when a rate isn't in the table for that
                                date/pair yet)

Usage:
    fx = FxService(db=db)
    rate = fx.get_rate(from_ccy="USD", to_ccy="INR", rate_date=statement_date)
    if rate is None:
        # R13 fires -- SPOC must provide rate manually

Currency direction convention (matches Oracle GL):
    get_rate("USD", "INR", date) -> how many INR per 1 USD
    i.e. from_amount * rate = to_amount

OU functional currency:
    Loaded from the organization_units DB table (OrganizationUnit) --
    see _load_organization_unit() below.
    get_functional_currency(ou_number) -> "INR" / "GBP" / "USD" etc.

DATE LOOKUP (for the given day):
    Looks for an exact (from_ccy, to_ccy, conversion_date, rate_type) match
    in gl_daily_rates first. If the file for that exact date hasn't landed
    yet (weekend, holiday, same-day lag before the morning drop), falls
    back to the MOST RECENT PRIOR date on record for that pair/type --
    see _fetch_from_gl_rates_table()'s docstring for exactly how far back
    it will look.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from pathlib import Path
from typing import Optional

from ..common.json_cache import load_json_cached

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent

# How many calendar days back to search for the most recent prior rate if
# the exact conversion_date isn't in gl_daily_rates yet (e.g. weekend/
# holiday gap, or the file for today simply hasn't been dropped/loaded
# yet). Keeps a single missed day from silently falling all the way
# through to the static approximate map.
_NEAREST_PRIOR_DATE_LOOKBACK_DAYS = 7

# ── Static fallback rate map ──────────────────────────────────────────────────
# These are APPROXIMATE rates for rule-band evaluation only.
# NEVER use these for Oracle posting -- always use a real gl_daily_rates row.
# Update periodically; keyed as "FROM_TO" e.g. "USD_INR".
#
# WARNING TO FINANCE TEAM: please review and correct these rates quarterly.

_STATIC_RATE_MAP: dict[str, float] = {
    # USD pairs
    "USD_INR": 83.50,
    "USD_GBP": 0.79,
    "USD_EUR": 0.92,
    "USD_CHF": 0.90,
    "USD_AUD": 1.53,
    "USD_SGD": 1.34,
    "USD_PLN": 4.02,
    "USD_ZAR": 18.60,
    "USD_COP": 3900.0,
    "USD_MXN": 17.20,
    "USD_CAD": 1.36,
    "USD_KES": 129.0,
    "USD_RSD": 108.0,

    # GBP pairs
    "GBP_INR": 105.50,
    "GBP_USD": 1.27,
    "GBP_EUR": 1.17,
    "GBP_CHF": 1.14,
    "GBP_AUD": 1.94,
    "GBP_SGD": 1.70,
    "GBP_PLN": 5.10,
    "GBP_ZAR": 23.60,

    # EUR pairs
    "EUR_INR": 90.20,
    "EUR_USD": 1.09,
    "EUR_GBP": 0.86,
    "EUR_CHF": 0.97,
    "EUR_PLN": 4.30,
    "EUR_ZAR": 20.30,

    # CHF pairs
    "CHF_INR": 93.50,
    "CHF_USD": 1.11,
    "CHF_GBP": 0.88,
    "CHF_EUR": 1.03,

    # AUD pairs
    "AUD_INR": 54.60,
    "AUD_USD": 0.65,

    # SGD pairs
    "SGD_INR": 62.30,
    "SGD_USD": 0.75,

    # INR pairs (for completeness)
    "INR_USD": 0.01198,
    "INR_GBP": 0.00948,
    "INR_EUR": 0.01109,
}


def _load_organization_unit(ou_number: str):
    """
    DB-BACKED (was ou_functional_currency.json). OrganizationUnit is now the
    single source of truth for both functional currency and the BU display
    name -- populated at Config Builder onboarding time (see
    bff/config_builder_routes.py), not a separately-maintained JSON file.
    No cache needed: a plain indexed lookup, and every process hits the
    same DB directly, so there is no cross-process staleness to solve for.
    """
    from ..db.session import session_scope
    from ..db.models import OrganizationUnit

    with session_scope() as db:
        ou = db.query(OrganizationUnit).filter(OrganizationUnit.ou_number == str(ou_number)).first()
        if ou is None:
            return None
        return {"functional_currency": ou.functional_currency, "ou_name": ou.ou_name}


def get_functional_currency(ou_number: str | None) -> Optional[str]:
    """
    Returns the functional (ledger) currency for a given OU number.
    e.g. get_functional_currency("111") -> "INR"
         get_functional_currency("312") -> "GBP"
    Returns None if the OU isn't onboarded yet (treat as unknown, flag for review).
    """
    if not ou_number:
        return None
    entry = _load_organization_unit(ou_number)
    return entry.get("functional_currency") if entry else None


def get_ou_display_name(ou_number: str | None) -> Optional[str]:
    """
    Returns Oracle's own BU display string for a given OU number, e.g.
    get_ou_display_name("111") -> "PUNE(111)"

    This is the value Oracle's standardReceipts payload expects for
    "BusinessUnit" -- it is NOT the same as LineItem.business_unit, which
    is populated during bank-statement parsing and can hold a plain
    bank-account description (e.g. "HSBC US Southern California") rather
    than Oracle's "NAME(ou)" format, depending on which OU-resolution path
    ran. Always derive the Oracle payload's BusinessUnit from here (keyed
    by ou_number), not from line_item.business_unit directly.
    """
    if not ou_number:
        return None
    entry = _load_organization_unit(ou_number)
    if not entry:
        return None
    ou_name = entry.get("ou_name")
    return f"{ou_name}({ou_number})" if ou_name else str(ou_number)


def _load_fx_conversion_type_map() -> dict:
    path = _HERE / "configs" / "fx_conversion_type_map.json"
    return load_json_cached(path)


def get_conversion_rate_type(from_ccy: str, to_ccy: str) -> tuple[str, str]:
    """
    Returns (human_type_name, oracle_type_code) to use for the
    gl_daily_rates lookup for this currency pair.

    Built from Zensar_GL_Daily_Rates_Extract__3_.txt (234,260 historical
    rate rows). "Corporate" -- the value this module used to hardcode --
    accounts for only 24 of those rows and was last seen 2023-06-05; it is
    a retired rate type. "MRC Daily" (Oracle type code 300000007815001) is
    the active daily rate type, present in 94.6% of the file through the
    most recent date on record. Falls back to the config's documented
    global default (MRC Daily) for any pair not explicitly in the map.
    """
    try:
        data = _load_fx_conversion_type_map()
    except Exception:
        return "MRC Daily", "300000007815001"

    pair_key = f"{(from_ccy or '').upper().strip()}_{(to_ccy or '').upper().strip()}"
    entry = data.get("pairs", {}).get(pair_key)
    if entry:
        return entry["recommended_type"], entry["recommended_type_code"]

    return (
        data.get("_default_conversion_rate_type", "MRC Daily"),
        data.get("_default_conversion_rate_type_code", "300000007815001"),
    )


class FxService:
    """
    Resolves FX rates with the gl_daily_rates DB table as primary source,
    static map as fallback.

    Instantiate once per analysis run (in orchestrator) and pass into
    _build_rule_input so the same instance is reused across all rows.
    Rate results are cached per (from_ccy, to_ccy, date) within the instance.
    """

    def __init__(
        self,
        oracle_base_url: str | None = None,   # kept for call-site backward compatibility; unused (no REST call)
        oracle_auth: tuple[str, str] | None = None,   # unused, see above
        oracle_token: str | None = None,              # unused, see above
    ):
        # PATCH: this class used to call Oracle's GL Daily Rates REST API
        # directly (self._oracle_base_url / self._oracle_auth /
        # self._oracle_token were used to build the HTTP request). There is
        # no such REST API available in this environment -- GL rates arrive
        # as a file (see gl_rates/watcher.py) and are looked up from the
        # gl_daily_rates DB table instead (see _fetch_from_gl_rates_table()
        # below). The constructor keeps accepting these three arguments,
        # unused, so existing call sites that still pass
        # oracle_base_url=settings.ORACLE_FUSION_BASE_URL etc. don't need to
        # change -- they're just no-ops now.
        self._oracle_base_url = oracle_base_url
        self._oracle_auth     = oracle_auth
        self._oracle_token    = oracle_token
        # "FROM_TO_YYYY-MM-DD" -> (rate, source). source is "gl_rates_table" | "static_map" | None.
        # PATCH: one FxService instance is now shared across MULTIPLE WORKER
        # THREADS (rule_engine/orchestrator.py's Step 4 fans out row
        # evaluation via ThreadPoolExecutor, all rows sharing this same
        # instance so the cache is actually useful across the whole run).
        # A plain dict here would risk two threads racing on the same
        # cache_key (best case: a harmless duplicate DB lookup; there's no
        # corruption risk since each entry is only ever written once with
        # the same value, but the lock makes that explicit rather than
        # relying on CPython dict-op atomicity as an implementation detail).
        self._cache: dict[str, tuple[Optional[float], Optional[str]]] = {}
        self._cache_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_rate(
        self,
        from_ccy: str,
        to_ccy: str,
        rate_date: dt.date | dt.datetime | None = None,
    ) -> Optional[float]:
        """
        Returns exchange rate: 1 unit of from_ccy = ? units of to_ccy.
        Returns None if rate cannot be resolved from any source.

        Priority:
          1. Same currency -> 1.0 (no conversion needed)
          2. gl_daily_rates DB table (exact date, then nearest prior date)
          3. Static fallback map (approximate)
        """
        rate, _source = self.get_rate_with_source(from_ccy, to_ccy, rate_date)
        return rate

    def get_rate_with_source(
        self,
        from_ccy: str,
        to_ccy: str,
        rate_date: dt.date | dt.datetime | None = None,
    ) -> tuple[Optional[float], Optional[str]]:
        """
        Same as get_rate(), but also returns where the rate came from:
        "gl_rates_table" | "static_map" | None.

        This is the ONLY place that queries gl_daily_rates for a given
        (from_ccy, to_ccy, date) -- both the rate and its source are
        cached together, so callers that need to know the source (e.g.
        for the Oracle payload's audit trail) don't need a second,
        uncached call to _fetch_from_gl_rates_table().
        """
        from_ccy = (from_ccy or "").upper().strip()
        to_ccy   = (to_ccy   or "").upper().strip()

        if not from_ccy or not to_ccy:
            return None, None

        # Same currency -- always 1.0, no lookup needed
        if from_ccy == to_ccy:
            return 1.0, None

        rate_date_obj = (
            rate_date.date() if isinstance(rate_date, dt.datetime)
            else rate_date if isinstance(rate_date, dt.date)
            else dt.date.today()
        )
        date_str = rate_date_obj.strftime("%Y-%m-%d")
        cache_key = f"{from_ccy}_{to_ccy}_{date_str}"

        with self._cache_lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # 1. Try the gl_daily_rates DB table (file-loaded)
        rate = self._fetch_from_gl_rates_table(from_ccy, to_ccy, rate_date_obj)
        source = "gl_rates_table" if rate is not None else None

        # 2. Fallback to static map
        if rate is None:
            rate = self._lookup_static(from_ccy, to_ccy)
            if rate is not None:
                source = "static_map"
                logger.warning(
                    "FX rate for %s->%s on %s not found in gl_daily_rates (checked exact date "
                    "and up to %d prior day(s)) -- using static fallback %.4f. DO NOT use for posting.",
                    from_ccy, to_ccy, date_str, _NEAREST_PRIOR_DATE_LOOKBACK_DAYS, rate,
                )

        with self._cache_lock:
            self._cache[cache_key] = (rate, source)
        return rate, source

    def convert(
        self,
        amount: float,
        from_ccy: str,
        to_ccy: str,
        rate_date: dt.date | dt.datetime | None = None,
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Converts amount from from_ccy to to_ccy.
        Returns (converted_amount, rate_used).
        Returns (None, None) if rate cannot be resolved.
        """
        rate = self.get_rate(from_ccy, to_ccy, rate_date)
        if rate is None:
            return None, None
        return round(amount * rate, 2), rate

    # ── gl_daily_rates DB table (file-loaded, NOT a REST call) ──────────────

    def _fetch_from_gl_rates_table(
        self,
        from_ccy: str,
        to_ccy: str,
        rate_date: dt.date,
    ) -> Optional[float]:
        """
        Looks up gl_daily_rates for this (from_ccy, to_ccy, rate_date) --
        NO REST call. Rows in that table come from finance dropping a GL
        Daily Rates extract file into a watched folder (see
        gl_rates/watcher.py + gl_rates/parser.py), exactly the way the
        aging report is ingested -- the difference is these rows are
        persisted to the DB (aging stays in-memory only), since rate
        history needs to accumulate across files rather than be replaced
        wholesale by the latest one.

        ConversionRateType is resolved per currency pair via
        get_conversion_rate_type() (see fx_conversion_type_map.json, built
        from 234,260 historical rate rows) -- "MRC Daily" is the active
        daily rate type for the large majority of pairs; "Corporate" was
        retired 2023-06-05 and should not be queried by default.

        LOOKUP ORDER for the given day:
          1. Exact match on (from_ccy, to_ccy, rate_date, resolved type).
          2. If no exact-date row exists (e.g. a weekend/holiday, or
             today's file simply hasn't been dropped/loaded yet), falls
             back to the MOST RECENT row on or before rate_date, within
             _NEAREST_PRIOR_DATE_LOOKBACK_DAYS calendar days. This mirrors
             how Oracle GL itself carries forward the last daily rate over
             non-business days -- it does NOT search forward to a future
             date, and it gives up (returns None -> falls through to the
             static map) if nothing is on record within the lookback
             window, rather than silently reaching arbitrarily far back.
        """
        from ..db.session import session_scope
        from ..db.models import GlDailyRate

        conversion_rate_type, _type_code = get_conversion_rate_type(from_ccy, to_ccy)

        try:
            with session_scope() as db:
                # 1. Exact date match.
                row = (
                    db.query(GlDailyRate)
                    .filter(
                        GlDailyRate.from_currency == from_ccy,
                        GlDailyRate.to_currency == to_ccy,
                        GlDailyRate.conversion_date == rate_date,
                        GlDailyRate.conversion_rate_type == conversion_rate_type,
                    )
                    .first()
                )
                if row is not None:
                    return float(row.conversion_rate)

                # 2. Nearest prior date within the lookback window.
                earliest = rate_date - dt.timedelta(days=_NEAREST_PRIOR_DATE_LOOKBACK_DAYS)
                row = (
                    db.query(GlDailyRate)
                    .filter(
                        GlDailyRate.from_currency == from_ccy,
                        GlDailyRate.to_currency == to_ccy,
                        GlDailyRate.conversion_rate_type == conversion_rate_type,
                        GlDailyRate.conversion_date < rate_date,
                        GlDailyRate.conversion_date >= earliest,
                    )
                    .order_by(GlDailyRate.conversion_date.desc())
                    .first()
                )
                if row is not None:
                    logger.info(
                        "gl_daily_rates: no exact-date row for %s->%s on %s (type=%s) -- "
                        "using nearest prior date %s instead.",
                        from_ccy, to_ccy, rate_date, conversion_rate_type, row.conversion_date,
                    )
                    return float(row.conversion_rate)

                logger.info(
                    "gl_daily_rates: no rate found for %s->%s on or before %s (type=%s, "
                    "lookback=%d days).",
                    from_ccy, to_ccy, rate_date, conversion_rate_type, _NEAREST_PRIOR_DATE_LOOKBACK_DAYS,
                )
                return None

        except Exception as exc:
            logger.warning("gl_daily_rates lookup error for %s->%s: %s", from_ccy, to_ccy, exc)
            return None

    # ── Static map ────────────────────────────────────────────────────────────

    def _lookup_static(self, from_ccy: str, to_ccy: str) -> Optional[float]:
        """
        Looks up static approximate rate.
        Tries direct pair first, then inverse.
        """
        key = f"{from_ccy}_{to_ccy}"
        if key in _STATIC_RATE_MAP:
            return _STATIC_RATE_MAP[key]

        # Try inverse and compute reciprocal
        inv_key = f"{to_ccy}_{from_ccy}"
        inv_rate = _STATIC_RATE_MAP.get(inv_key)
        if inv_rate and inv_rate != 0:
            return round(1.0 / inv_rate, 6)

        # Try via USD as bridge currency
        # from_ccy -> USD -> to_ccy
        from_to_usd = _STATIC_RATE_MAP.get(f"{from_ccy}_USD")
        usd_to_target = _STATIC_RATE_MAP.get(f"USD_{to_ccy}")
        if from_to_usd and usd_to_target:
            return round(from_to_usd * usd_to_target, 6)

        return None