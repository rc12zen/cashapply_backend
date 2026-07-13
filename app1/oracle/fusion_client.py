"""
app.oracle.fusion_client  (PATCHED v2)
=======================================
POST /standardReceipts client. Supports Basic Auth and OAuth.

ORACLE PAYLOAD MODEL (this revision)
--------------------------------------
Three-currency model to match Oracle AR's own posting logic:

  credited_currency  : what arrived in the bank (e.g. GBP)
  invoice_currency   : what the invoice was raised in (e.g. USD)
  functional_currency: OU ledger currency (e.g. INR)

Oracle payload shapes:

  Case A — All three currencies are the same (fully same-currency)
  ─────────────────────────────────────────────────────────────────
  No ConversionRate/ConversionDate needed.
  Currency = invoice_currency (= credited = functional)
  Amount   = credit_amount (already in invoice currency)

  Case B — credited != invoice  (Leg 1 conversion needed by us)
  ─────────────────────────────────────────────────────────────────
  We converted credit_amount → invoice_currency at analysis time
  (fx_credit_to_invoice). Oracle receives amounts in invoice_currency.
  Currency     = invoice_currency
  Amount       = credit_amount * fx_credit_to_invoice  (in invoice ccy)

  If ALSO invoice != functional  (is_cross_ledger=True, Leg 2):
    ConversionRateType = "User"
    ConversionRate     = fx_invoice_to_functional
    ConversionDate     = statement_date
    Oracle uses ConversionRate internally to book in functional currency.
    We do NOT compute functional amounts — Oracle owns that.

  Case C — credited == invoice but invoice != functional
  ─────────────────────────────────────────────────────────────────
  No Leg 1 needed (same currency received as invoice denomination).
  Currency     = invoice_currency
  Amount       = credit_amount
  ConversionRateType = "User"
  ConversionRate     = fx_invoice_to_functional
  ConversionDate     = statement_date

Example (from spec):
  credit: 100 GBP, invoice: 100 USD, functional: INR
  → Currency="USD", Amount=100*fx(GBP→USD),
    ConversionRate=fx(USD→INR), ConversionDate=statement_date

  credit: 100 USD, invoice: 100 USD, functional: INR
  → Currency="USD", Amount=100,
    ConversionRate=fx(USD→INR), ConversionDate=statement_date

ReferenceAmount:
  Always in invoice_currency (same as Amount).
  stated_amount on each MatchedInvoice is already in invoice_currency
  (set by _resolve_matched_invoices in the evaluator using Leg 1).

PROBLEM 2 — is_cross_ou_currency in response
---------------------------------------------
build_standard_receipt_payload now includes is_cross_ou_currency from the
LineItem so the caller (state_machine / API response) can surface the flag
to the front-end without a separate DB query.
"""
from __future__ import annotations

import datetime as dt

import httpx

from ..db.models import LineItem
from ..db.settings import get_settings
from ..rule_engine.fx_service import get_ou_display_name
from .receipt_method_resolver import resolve_receipt_method
from ..common.http_debug_log import log_oracle_request, log_oracle_response, log_oracle_error


class OracleFusionClient:
    def __init__(self):
        self.settings = get_settings()
        self._token_cache: str | None = None

    def _get_oauth_token(self) -> str:
        if self._token_cache:
            return self._token_cache
        s = self.settings
        token_body = {
            "grant_type": "client_credentials",
            "client_id": s.ORACLE_OAUTH_CLIENT_ID,
            "client_secret": s.ORACLE_OAUTH_CLIENT_SECRET,
        }
        # Redact the secret before logging — never print it, even in a debug curl.
        log_oracle_request(
            "POST", s.ORACLE_OAUTH_TOKEN_URL,
            form_body={**token_body, "client_secret": "***REDACTED***"},
            tag="oracle.oauth_token",
        )
        try:
            resp = httpx.post(s.ORACLE_OAUTH_TOKEN_URL, data=token_body, timeout=30)
        except httpx.HTTPError as e:
            log_oracle_error(e, tag="oracle.oauth_token")
            raise
        log_oracle_response(resp, tag="oracle.oauth_token")
        resp.raise_for_status()
        self._token_cache = resp.json()["access_token"]
        return self._token_cache

    def _auth_headers(self) -> dict:
        s = self.settings
        if s.ORACLE_AUTH_MODE == "oauth":
            return {"Authorization": f"Bearer {self._get_oauth_token()}"}
        return {}

    def post_standard_receipt(self, payload: dict) -> dict:
        """
        Posts a receipt to Oracle Fusion AR /standardReceipts.
        Returns normalised result dict:
          { success, oracle_ref_no, standard_receipt_id, status_code, message, raw }

        Every call logs the exact outbound request as a copy-pasteable curl
        command (credentials redacted — see http_debug_log.py) and the full
        response status/headers/body, so a failed post can be diagnosed or
        reproduced independently without touching the app.
        """
        s = self.settings
        url = f"{s.ORACLE_FUSION_BASE_URL}/standardReceipts"
        auth = (
            (s.ORACLE_BASIC_USERNAME, s.ORACLE_BASIC_PASSWORD)
            if s.ORACLE_AUTH_MODE == "basic"
               and s.ORACLE_BASIC_USERNAME
               and s.ORACLE_BASIC_PASSWORD
            else None
        )
        headers = self._auth_headers()

        # Oracle ignores the underscore-prefixed audit fields we tack onto the
        # payload for our own logging (_fx_sources, _receipt_method_unresolved,
        # etc.) — but log the payload EXACTLY as sent, audit fields included,
        # so the log always matches what actually went over the wire.
        log_oracle_request(
            "POST", url, headers=headers, auth=auth, json_body=payload,
            tag="oracle.standardReceipts",
        )

        try:
            resp = httpx.post(
                url, json=payload,
                headers=headers, auth=auth, timeout=60,
            )
            log_oracle_response(resp, tag="oracle.standardReceipts")

            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "success":             True,
                    "oracle_ref_no":       data.get("ReceiptNumber") or data.get("ref_no"),
                    "standard_receipt_id": data.get("ReceiptId")     or data.get("id"),
                    "status_code":         str(resp.status_code),
                    "message":             "Posted successfully",
                    "raw":                 data,
                }
            return {
                "success":             False,
                "oracle_ref_no":       None,
                "standard_receipt_id": None,
                "status_code":         str(resp.status_code),
                "message":             resp.text[:2000],
                "raw":                 None,
            }
        except httpx.HTTPError as e:
            log_oracle_error(e, tag="oracle.standardReceipts")
            return {
                "success":             False,
                "oracle_ref_no":       None,
                "standard_receipt_id": None,
                "status_code":         "connection_error",
                "message":             str(e),
                "raw":                 None,
            }


def _build_receipt_number(line_item: LineItem) -> str:
    """
    Generates the Oracle `ReceiptNumber` -- confirmed by the business to be
    supplied by us, not Oracle. Must be RECOGNIZABLE (a human scanning
    Oracle AR receipts can tell it came from CashApply, and roughly when/
    where from) and UNIQUE (never collides, including across retries of
    the same row).

    Format: CASHAPPLY-<ou_number>-<YYYYMMDD>-<line_item_id>
      e.g.  CASHAPPLY-111-20260604-1583

    Recognizable:
      - "CASHAPPLY" prefix identifies the source system at a glance.
      - <ou_number> shows which business unit/plant the receipt is for.
      - <YYYYMMDD> shows roughly when the underlying payment was received
        (statement_date; falls back to the row's created_at date if the
        statement date wasn't parsed, so this segment is never blank).

    Unique -- guaranteed, not just likely:
      - <line_item_id> is LineItem.id, a DB auto-increment primary key.
        It is globally unique for the lifetime of the table and is NEVER
        reused, even after a row is deleted. That alone is sufficient for
        uniqueness; the ou_number/date segments are for recognizability
        only and add no uniqueness risk.

    Idempotent across retries:
      - hitl.service.py's retry_oracle_post() rebuilds the payload from
        scratch on every attempt, but always against the SAME LineItem, so
        this function returns the exact same value every time for a given
        row -- a retry after a network timeout reuses the same
        ReceiptNumber rather than minting a new one.
    """
    ou = line_item.ou_number or "UNK"
    date_source = line_item.statement_date or line_item.created_at
    date_str = date_source.strftime("%Y%m%d") if date_source else "00000000"
    return f"CASHAPPLY-{ou}-{date_str}-{line_item.id}-226"


def build_standard_receipt_payload(
    line_item: LineItem,
    invoice_breakup: list[dict] | None,
) -> dict:
    """
    Builds the Oracle AR standardReceipts payload.

    Parameters
    ----------
    line_item:
        Persisted LineItem with all three currency fields and both FX legs
        resolved by the orchestrator after Pass 2.

    invoice_breakup:
        Optional SPOC-confirmed split from the BreakupModal.
        Each entry: { "invoice_number": str, "reference_amount": float }
        reference_amount must be in invoice_currency.
        When provided, overrides matched_invoices stated_amounts.

    Payload shapes — see module docstring for full examples.

    The private audit field `_fx_sources` is included in the payload dict
    (prefixed with underscore) so the caller can log it; Oracle ignores
    unknown fields. Remove it before posting if Oracle rejects extras.
    """
    # ── Resolve currency fields from LineItem ─────────────────────────────────
    credited_currency   = (line_item.statement_currency  or "").upper().strip()
    invoice_currency    = (line_item.invoice_currency    or "").upper().strip()
    functional_currency = (line_item.functional_currency or "").upper().strip()

    # Fall back gracefully when invoice_currency was not resolved at analysis time
    # (should not happen for postable rows, but be defensive).
    if not invoice_currency:
        invoice_currency = credited_currency or functional_currency

    credit_amount    = float(line_item.credit_amount or 0)
    statement_date_iso = (
        line_item.statement_date.strftime("%Y-%m-%d")
        if line_item.statement_date else None
    )
    accounting_date_iso = dt.date.today().strftime("%Y-%m-%d")

    # ── Leg 1: convert credit amount → invoice currency ───────────────────────
    is_cross_currency    = bool(line_item.is_cross_currency)   # credited != invoice
    fx_credit_to_invoice = float(line_item.fx_credit_to_invoice) if line_item.fx_credit_to_invoice else None

    if is_cross_currency and fx_credit_to_invoice:
        # Amount Oracle receives is in invoice currency.
        amount_in_invoice_ccy = round(credit_amount * fx_credit_to_invoice, 2)
    else:
        # Credited currency IS invoice currency — no conversion needed.
        amount_in_invoice_ccy = credit_amount

    # ── Leg 2: invoice → functional (Oracle ConversionRate) ──────────────────
    is_cross_ledger              = bool(line_item.is_cross_ledger)   # invoice != functional
    fx_invoice_to_functional     = float(line_item.fx_invoice_to_functional) if line_item.fx_invoice_to_functional else None

    # ── Build remittance references (always in invoice currency) ─────────────
    if invoice_breakup:
        # SPOC manually confirmed split — use directly.
        references = [
            {
                "ReceiptMatchBy":  "Transaction Number",
                "ReferenceNumber": inv["invoice_number"],
                "ReferenceAmount": str(inv["reference_amount"]),
            }
            for inv in invoice_breakup
        ]
    else:
        # Use stated_amount from rule engine (_resolve_matched_invoices).
        # stated_amount is already in invoice_currency (Leg 1 applied during split).
        matched = line_item.matched_invoices or []
        references = [
            {
                "ReceiptMatchBy":  "Transaction Number",
                "ReferenceNumber": m["invoice_number"],
                "ReferenceAmount": str(m.get("stated_amount") or m["outstanding_amount"]),
            }
            for m in matched
        ]

    # ── Resolve the real ReceiptMethod (was hardcoded "Standard" — a value
    #    that appears nowhere in the actual Oracle AR Receipt Methods
    #    extract). Falls back to "Standard" ONLY if the account genuinely
    #    isn't in receipt_method_map.json, and flags that clearly so it
    #    doesn't silently post under a guessed method — see
    #    `_receipt_method_unresolved` below.
    receipt_method_result = resolve_receipt_method(
        account_number=line_item.account_number,
        ou_number=line_item.ou_number,
    )
    receipt_method_name = receipt_method_result.receipt_method_name or "Standard"

    # ── BusinessUnit: Oracle expects "NAME(ou)" (e.g. "PUNE(111)").
    #    line_item.business_unit is populated during bank-statement parsing
    #    and can hold a plain bank-description string instead (depends on
    #    which OU-resolution path ran) -- always derive this from ou_number
    #    via ou_functional_currency.json instead.
    business_unit = get_ou_display_name(line_item.ou_number) or line_item.business_unit

    # ── CustomerAccountNumber: pull from the first matched invoice's aging
    #    row (customer_number was already being looked up in AgingInvoice /
    #    AgingMap, just never threaded through matched_invoices until now).
    #    A receipt is posted against one customer, so the first match is
    #    representative; falls back to None (Oracle payload TODO) if this
    #    row somehow has no matched invoices with a customer_number.
    matched_invoices_raw = line_item.matched_invoices or []
    customer_account_number = next(
        (m.get("customer_number") for m in matched_invoices_raw if m.get("customer_number")),
        None,
    )

    # ── Base payload ──────────────────────────────────────────────────────────
    payload: dict = {
        "ReceiptNumber":               _build_receipt_number(line_item),
        "ReceiptMethod":               receipt_method_name,
        "BusinessUnit":                business_unit,
        "CustomerAccountNumber":       customer_account_number,
        "RemittanceBankAccountNumber": line_item.account_number,
        "Currency":                    invoice_currency,     # always post in invoice ccy
        "Amount":                      amount_in_invoice_ccy,
        "AccountingDate":              accounting_date_iso,
        "ReceiptDate":                 statement_date_iso,
        "remittanceReferences":        references,
    }

    if not customer_account_number:
        payload["_customer_account_number_unresolved"] = (
            "No customer_number found on any matched invoice for this row — "
            "check the aging report has customer_number populated for this customer. "
            "DO NOT post with CustomerAccountNumber=None."
        )

    if not receipt_method_result.matched:
        payload["_receipt_method_unresolved"] = (
            f"Account '{line_item.account_number}' not found in receipt_method_map.json — "
            f"posted with fallback ReceiptMethod='Standard'. DO NOT trust this for posting; "
            f"add this account to the extract / config before relying on it."
        )
    elif receipt_method_result.ambiguous:
        payload["_receipt_method_ambiguous"] = (
            f"Account '{line_item.account_number}' has multiple candidate receipt methods "
            f"(class='{receipt_method_result.receipt_class}' chosen by default priority order); "
            f"confirm this is the correct one — see _accounts_with_unresolved_ambiguity in "
            f"receipt_method_map.json."
        )

    # ── Add Leg 2 conversion fields when invoice != functional ────────────────
    if is_cross_ledger and fx_invoice_to_functional:
        # Oracle uses ConversionRate to book Amount (invoice ccy) into
        # the functional-currency ledger. We never compute that ourselves.
        payload["ConversionRateType"] = "User"
        payload["ConversionRate"]     = fx_invoice_to_functional
        payload["ConversionDate"]     = statement_date_iso
    elif is_cross_ledger and not fx_invoice_to_functional:
        # Should not reach ready_to_post without this rate — R13 guards it.
        # If somehow we get here, flag it clearly rather than posting silently.
        payload["_fx_leg2_missing"] = (
            f"fx_invoice_to_functional ({invoice_currency}→{functional_currency}) "
            f"not resolved. DO NOT POST — re-evaluate after providing rate."
        )

    # ── Audit trail (not sent to Oracle — strip before posting if needed) ─────
    payload["_fx_sources"] = {
        "credited_currency":              credited_currency,
        "invoice_currency":               invoice_currency,
        "functional_currency":            functional_currency,
        "fx_credit_to_invoice":           fx_credit_to_invoice,
        "fx_credit_to_invoice_source":    line_item.fx_credit_to_invoice_source,
        "fx_invoice_to_functional":       fx_invoice_to_functional,
        "fx_invoice_to_functional_source": line_item.fx_invoice_to_functional_source,
    }

    # ── Problem 2: surface cross-OU flag for front-end / state_machine ────────
    # The state_machine / API layer reads this to set the front-end badge.
    # It is NOT sent to Oracle.
    payload["_is_cross_ou_currency"] = bool(
        getattr(line_item, "is_cross_ou_currency", False)
    )

    return payload