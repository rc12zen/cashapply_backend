"""
app.rule_engine.fx_service
============================
FX rate resolution for cross-currency payment evaluation.

Priority order:
  1. Oracle Fusion GL Daily Rates API  (live, date-specific)
  2. Static fallback rate map          (hardcoded approximates — NEVER use for posting,
                                        only for rule-band evaluation when Oracle is down)

Usage:
    fx = FxService(db=db, oracle_base_url=settings.ORACLE_FUSION_BASE_URL)
    rate = fx.get_rate(from_ccy="USD", to_ccy="INR", rate_date=statement_date)
    if rate is None:
        # R13 fires — SPOC must provide rate manually

Currency direction convention (matches Oracle GL):
    get_rate("USD", "INR", date) → how many INR per 1 USD
    i.e. from_amount * rate = to_amount

OU functional currency:
    Loaded from ou_functional_currency.json (one entry per OU).
    get_functional_currency(ou_number) → "INR" / "GBP" / "USD" etc.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Optional

import httpx

from ..common.json_cache import load_json_cached

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent

# ── Static fallback rate map ──────────────────────────────────────────────────
# These are APPROXIMATE rates for rule-band evaluation only.
# NEVER use these for Oracle posting — always use Oracle GL rate for posting.
# Update periodically; keyed as "FROM_TO" e.g. "USD_INR".
#
# ⚠️  TO FINANCE TEAM: please review and correct these rates quarterly.

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


def _load_ou_functional_currency() -> dict:
    # PATCH: was @lru_cache(maxsize=1) — same cross-process staleness bug
    # as bank_statement/configs/account_loader.py (see that file's PATCH
    # note for the full explanation). A newly-onboarded OU's functional
    # currency would never be seen by the worker process until it was
    # manually restarted. Now mtime-based via app.common.json_cache.
    path = _HERE / "configs" / "ou_functional_currency.json"
    return load_json_cached(path)


def get_functional_currency(ou_number: str | None) -> Optional[str]:
    """
    Returns the functional (ledger) currency for a given OU number.
    e.g. get_functional_currency("111") → "INR"
         get_functional_currency("312") → "GBP"
    Returns None if OU not in map (treat as unknown, flag for review).
    """
    if not ou_number:
        return None
    mapping = _load_ou_functional_currency()
    entry = mapping.get(str(ou_number))
    return entry.get("functional_currency") if entry else None


def get_ou_display_name(ou_number: str | None) -> Optional[str]:
    """
    Returns Oracle's own BU display string for a given OU number, e.g.
    get_ou_display_name("111") → "PUNE(111)"

    This is the value Oracle's standardReceipts payload expects for
    "BusinessUnit" -- it is NOT the same as LineItem.business_unit, which
    is populated during bank-statement parsing from account_ou_map.json /
    bank_ou_mapping.json and can hold a plain bank-account description
    (e.g. "HSBC US Southern California") rather than Oracle's "NAME(ou)"
    format, depending on which OU-resolution path ran. Always derive the
    Oracle payload's BusinessUnit from here (keyed by ou_number), not from
    line_item.business_unit directly.
    """
    if not ou_number:
        return None
    mapping = _load_ou_functional_currency()
    entry = mapping.get(str(ou_number))
    return entry.get("ou") if entry else None


def _load_fx_conversion_type_map() -> dict:
    path = _HERE / "configs" / "fx_conversion_type_map.json"
    return load_json_cached(path)


def get_conversion_rate_type(from_ccy: str, to_ccy: str) -> tuple[str, str]:
    """
    Returns (human_type_name, oracle_type_code) to use for the Oracle GL
    Daily Rates query for this currency pair.

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
    Resolves FX rates with Oracle GL as primary source, static map as fallback.

    Instantiate once per analysis run (in orchestrator) and pass into
    _build_rule_input so the same instance is reused across all rows.
    Rate results are cached per (from_ccy, to_ccy, date) within the instance.
    """

    def __init__(
        self,
        oracle_base_url: str | None = None,
        oracle_auth: tuple[str, str] | None = None,   # (username, password) for basic auth
        oracle_token: str | None = None,               # Bearer token for OAuth
    ):
        self._oracle_base_url = oracle_base_url
        self._oracle_auth     = oracle_auth
        self._oracle_token    = oracle_token
        # "FROM_TO_YYYY-MM-DD" -> (rate, source). source is "oracle_gl" | "static_map" | None.
        self._cache: dict[str, tuple[Optional[float], Optional[str]]] = {}

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
          1. Same currency → 1.0 (no conversion needed)
          2. Oracle Fusion GL Daily Rates API
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
        "oracle_gl" | "static_map" | None.

        This is the ONLY place that talks to Oracle GL for a given
        (from_ccy, to_ccy, date) — both the rate and its source are cached
        together, so callers that need to know the source (e.g. for the
        Oracle payload's audit trail) don't need a second, uncached call
        to _fetch_from_oracle().
        """
        from_ccy = (from_ccy or "").upper().strip()
        to_ccy   = (to_ccy   or "").upper().strip()

        if not from_ccy or not to_ccy:
            return None, None

        # Same currency — always 1.0, no API call needed
        if from_ccy == to_ccy:
            return 1.0, None

        date_str = (
            rate_date.strftime("%Y-%m-%d")
            if rate_date else dt.date.today().strftime("%Y-%m-%d")
        )
        cache_key = f"{from_ccy}_{to_ccy}_{date_str}"

        if cache_key in self._cache:
            return self._cache[cache_key]

        # 1. Try Oracle GL
        rate = self._fetch_from_oracle(from_ccy, to_ccy, date_str)
        source = "oracle_gl" if rate is not None else None

        # 2. Fallback to static map
        if rate is None:
            rate = self._lookup_static(from_ccy, to_ccy)
            if rate is not None:
                source = "static_map"
                logger.warning(
                    "FX rate for %s→%s on %s not found in Oracle GL — "
                    "using static fallback %.4f. DO NOT use for posting.",
                    from_ccy, to_ccy, date_str, rate,
                )

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

    # ── Oracle GL API ─────────────────────────────────────────────────────────

    def _fetch_from_oracle(
        self,
        from_ccy: str,
        to_ccy: str,
        date_str: str,
    ) -> Optional[float]:
        """
        Calls Oracle Fusion GL Daily Rates REST API.

        Endpoint (placeholder — confirm exact path with Oracle admin):
          GET /fscmRestApi/resources/11.13.18.05/glDailyRates
              ?q=FromCurrency=<from>;ToCurrency=<to>;ConversionDate=<date>;ConversionRateType=<resolved>
              &fields=ConversionRate

        ConversionRateType is resolved per currency pair via
        get_conversion_rate_type() (see fx_conversion_type_map.json, built
        from 234,260 historical rate rows). This USED to hardcode
        "Corporate" — that type was retired 2023-06-05 (only 24 rows in
        the whole extract). "MRC Daily" is the active daily rate type
        (94.6% of the extract, still current as of the most recent date on
        record) and is now the default.

        TODO: Confirm with Oracle admin:
          - Exact endpoint path for your Oracle Fusion instance
          - Auth method (basic vs OAuth — matches ORACLE_AUTH_MODE in settings)
          - Whether to use ConversionDate or EffectiveDate in the filter
        """
        if not self._oracle_base_url:
            return None

        conversion_rate_type, _type_code = get_conversion_rate_type(from_ccy, to_ccy)

        try:
            url = f"{self._oracle_base_url}/glDailyRates"
            params = {
                "q": (
                    f"FromCurrency={from_ccy};"
                    f"ToCurrency={to_ccy};"
                    f"ConversionDate={date_str};"
                    f"ConversionRateType={conversion_rate_type}"
                ),
                "fields": "ConversionRate,FromCurrency,ToCurrency,ConversionDate",
                "limit": 1,
            }
            headers = {}
            auth = None

            if self._oracle_token:
                headers["Authorization"] = f"Bearer {self._oracle_token}"
            elif self._oracle_auth:
                auth = self._oracle_auth

            resp = httpx.get(url, params=params, headers=headers, auth=auth, timeout=15)

            if resp.status_code != 200:
                logger.warning(
                    "Oracle GL rates API returned %s for %s→%s (type=%s) on %s",
                    resp.status_code, from_ccy, to_ccy, conversion_rate_type, date_str,
                )
                return None

            data = resp.json()
            items = data.get("items") or []
            if not items:
                logger.info(
                    "Oracle GL rates API: no rate found for %s→%s on %s",
                    from_ccy, to_ccy, date_str,
                )
                return None

            rate = items[0].get("ConversionRate")
            return float(rate) if rate is not None else None

        except Exception as exc:
            logger.warning("Oracle GL rates API error for %s→%s: %s", from_ccy, to_ccy, exc)
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
        # from_ccy → USD → to_ccy
        from_to_usd = _STATIC_RATE_MAP.get(f"{from_ccy}_USD")
        usd_to_target = _STATIC_RATE_MAP.get(f"USD_{to_ccy}")
        if from_to_usd and usd_to_target:
            return round(from_to_usd * usd_to_target, 6)

        return None