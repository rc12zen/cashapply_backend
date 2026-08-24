"""
app.oracle.fusion_client  (PATCHED v5 -- simplified)
=======================================================
POST /standardReceipts client. Supports Basic Auth and OAuth.

RECEIPT-CREATION PAYLOAD MODEL -- exactly two cases, nothing else
--------------------------------------------------------------------
Every receipt-creation payload is built from ONE decision:

    is_cross_ledger = (invoice_currency != functional_currency)

CASE 1 -- NOT cross-ledger (invoice_currency == functional_currency)
    Send exactly these 9 fields, nothing more:
        ReceiptNumber, ReceiptMethod, BusinessUnit, CustomerAccountNumber,
        RemittanceBankAccountNumber, Currency, Amount, AccountingDate,
        ReceiptDate

CASE 2 -- cross-ledger (invoice_currency != functional_currency)
    Send the SAME 9 fields, PLUS exactly these 3:
        ConversionRateType ("User"), ConversionRate, ConversionDate
    -- 12 fields total.

That's it. `Currency` is ALWAYS present in both cases (confirmed via a
live Oracle test: a same-currency payload sent WITHOUT Currency was
rejected with "You must provide a value for the Currency attribute").
Only the 3 Conversion* fields are conditional on cross-ledger.

WHAT WAS REMOVED IN v5 (previously "audit" fields tacked onto the
payload dict itself -- _fx_sources, _is_cross_ou_currency,
_receipt_method_unresolved, _receipt_method_ambiguous,
_customer_account_number_unresolved, _customer_account_number_fallback_
lookup): these added no value to the actual payload, were a repeated
source of confusion about what was really being sent, and (confirmed via
a live captured request) were ACTUALLY GOING OUT to Oracle over the wire
despite comments claiming otherwise. Removed entirely -- nothing builds
them, nothing sends them. Anything worth knowing about how a field got
resolved (customer lookup fallback, unresolved receipt method, ambiguous
receipt method) is now a plain server-side log line instead of payload
clutter -- see the logger.info/warning calls below.

WHAT CHANGED FOR THE "cross-ledger but rate not resolved" EDGE CASE:
previously this silently returned a payload with a "_fx_leg2_missing"
"DO NOT POST" flag that nothing downstream actually checked before
posting anyway -- meaning an incomplete payload could genuinely reach
Oracle. Now raises ValueError instead, so a caller can never accidentally
post a cross-ledger receipt with no conversion rate; the row fails
cleanly and visibly instead of going out half-built.

Amount:
  Leg 1 (credited_currency -> invoice_currency) is resolved BEFORE this
  function runs (by the rule engine / orchestrator, stored on the
  LineItem) -- Amount here is always already in invoice_currency.
  Oracle itself handles converting Amount into functional_currency using
  ConversionRate when Case 2 applies -- this app never computes or sends
  a functional-currency amount.

ReceiptNumber: FAL-<ou_number>-<YYYYMMDD>-<line_item_id> -- "FAL" =
Fusion Auto LockBox (previously "CASHAPPLY", renamed to match the
product's new name). No extra suffix (a previous version had an
unexplained "-226" appended, confirmed via a live Oracle test to push
real values over Oracle's 30-character ReceiptNumber limit -- removed).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

import httpx

from ..db.models import LineItem
from ..db.settings import get_settings
from ..rule_engine.fx_service import get_ou_display_name
from .receipt_method_resolver import resolve_receipt_method
from ..common.http_debug_log import log_oracle_request, log_oracle_response, log_oracle_error
from ..aging import aging_store

logger = logging.getLogger("cashapply.oracle")


# ── Shared IDCS/OAuth token cache ───────────────────────────────────────
# Module-level, not per-instance: OracleFusionClient() is created fresh at
# 5 different call sites across the codebase (receipt_creation.py,
# hitl/service.py x2, hitl/distribution_actions.py x2) -- including inside
# the receipt-creation thread pool (ORACLE_RECEIPT_MAX_WORKERS), where
# several threads call this concurrently. A per-instance cache meant every
# single one of those calls fetched its own brand-new token from IDCS --
# for a batch of 50 concurrent receipts, that's 50 separate token
# requests, not 1. Sharing this at module level means every instance and
# every thread reuses the SAME cached token until it's actually close to
# expiring.
#
# _token_lock serializes the check-and-refresh section (not every request
# -- see _get_oauth_token below) specifically to prevent a "thundering
# herd": if the token has expired and 20 threads notice at once, this
# ensures exactly ONE of them actually calls IDCS while the other 19 wait
# and then reuse what it fetched, rather than firing 20 simultaneous
# token requests at IDCS.
_token_lock = threading.Lock()
_cached_token: str | None = None
_token_expires_at: float = 0.0  # time.monotonic() timestamp
_EXPIRY_SAFETY_MARGIN_SECONDS = 60  # refresh slightly before actual expiry,
# so a token doesn't expire mid-flight on a request that started right
# before the deadline.


class OracleFusionClient:
    def __init__(self):
        self.settings = get_settings()

    def _get_oauth_token(self) -> str:
        global _cached_token, _token_expires_at

        with _token_lock:
            now = time.monotonic()
            if _cached_token and now < _token_expires_at:
                return _cached_token

            s = self.settings
            token_body = {
                "grant_type": "client_credentials",
                "client_id": s.ORACLE_OAUTH_CLIENT_ID,
                "client_secret": s.ORACLE_OAUTH_CLIENT_SECRET,
            }
            # IDCS requires an explicit scope naming the target resource;
            # other OAuth providers may not use one at all, so this is only
            # included when actually configured.
            if s.ORACLE_OAUTH_SCOPE:
                token_body["scope"] = s.ORACLE_OAUTH_SCOPE

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

            body = resp.json()
            _cached_token = body["access_token"]
            # expires_in is in seconds, per the OAuth2 client-credentials
            # spec IDCS follows. Default of 3600 (1 hour) only used if
            # Oracle's response is ever missing the field -- shouldn't
            # happen in practice, just a defensive fallback rather than a
            # crash.
            expires_in = int(body.get("expires_in", 3600))
            _token_expires_at = now + expires_in - _EXPIRY_SAFETY_MARGIN_SECONDS
            return _cached_token

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

        As of v5, `payload` never contains anything but real Oracle
        fields (see build_receipt_creation_payload's module docstring) --
        no stripping needed here anymore. Sent exactly as built.

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
                    "oracle_ref_no":       data.get("ReceiptNumber"),
                    "standard_receipt_id": data.get("StandardReceiptId"),
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

    def post_remittance_reference(self, standard_receipt_id: str, reference_payload: dict) -> dict:
        """
        Invoice mapping — POSTs ONE remittance reference to the child
        collection of an ALREADY-CREATED receipt:
            /standardReceipts/{standard_receipt_id}/child/remittanceReferences

        This is a different Oracle resource than /standardReceipts itself
        — confirmed against a real Oracle GET response, whose `links`
        array exposes exactly this child collection (kind: "collection",
        name: "remittanceReferences"). It's how invoice mapping attaches
        to a receipt that was already created bare (no references) during
        Bank Reconciliation — NOT a PATCH on the receipt's own fields.

        Called once per matched invoice; callers loop for multi-invoice
        breakups.
        """
        s = self.settings
        url = f"{s.ORACLE_FUSION_BASE_URL}/standardReceipts/{standard_receipt_id}/child/remittanceReferences"
        auth = (
            (s.ORACLE_BASIC_USERNAME, s.ORACLE_BASIC_PASSWORD)
            if s.ORACLE_AUTH_MODE == "basic"
               and s.ORACLE_BASIC_USERNAME
               and s.ORACLE_BASIC_PASSWORD
            else None
        )
        headers = self._auth_headers()

        log_oracle_request(
            "POST", url, headers=headers, auth=auth, json_body=reference_payload,
            tag="oracle.remittanceReferences",
        )

        try:
            resp = httpx.post(
                url, json=reference_payload,
                headers=headers, auth=auth, timeout=60,
            )
            log_oracle_response(resp, tag="oracle.remittanceReferences")

            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "success":     True,
                    "status_code": str(resp.status_code),
                    "message":     "Reference added successfully",
                    "raw":         data,
                }
            return {
                "success":     False,
                "status_code": str(resp.status_code),
                "message":     resp.text[:2000],
                "raw":         None,
            }
        except httpx.HTTPError as e:
            log_oracle_error(e, tag="oracle.remittanceReferences")
            return {
                "success":     False,
                "status_code": "connection_error",
                "message":     str(e),
                "raw":         None,
            }


    def patch_standard_receipt(self, standard_receipt_id: str, payload: dict) -> dict:
        """
        Edits fields on an ALREADY-CREATED receipt via
            PATCH /standardReceipts/{standard_receipt_id}
        — used today for exactly one field: a SPOC-entered override of the
        Leg 2 (invoice -> functional) GL conversion rate (ConversionRate /
        ConversionRateType), for a cross-ledger-currency row where the
        Oracle-resolved rate needs a manual correction (e.g. finance wants
        83.98 instead of the 83.67 GL rate that was in effect at receipt-
        creation time). See hitl/service.py's edit_gl_rate() for the
        business guard on WHEN this is allowed (before invoice mapping only
        — see that function's docstring for why).

        Returns the same normalised shape as post_standard_receipt/
        post_remittance_reference: { success, status_code, message, raw }.
        Oracle's PATCH returns 200 with the updated resource body on
        success (no 201, since nothing is being created).
        """
        s = self.settings
        url = f"{s.ORACLE_FUSION_BASE_URL}/standardReceipts/{standard_receipt_id}"
        auth = (
            (s.ORACLE_BASIC_USERNAME, s.ORACLE_BASIC_PASSWORD)
            if s.ORACLE_AUTH_MODE == "basic"
               and s.ORACLE_BASIC_USERNAME
               and s.ORACLE_BASIC_PASSWORD
            else None
        )
        headers = self._auth_headers()

        log_oracle_request(
            "PATCH", url, headers=headers, auth=auth, json_body=payload,
            tag="oracle.standardReceipts.patch",
        )

        try:
            resp = httpx.patch(
                url, json=payload,
                headers=headers, auth=auth, timeout=60,
            )
            log_oracle_response(resp, tag="oracle.standardReceipts.patch")

            if resp.status_code in (200, 201, 204):
                data = resp.json() if resp.content else {}
                return {
                    "success":     True,
                    "status_code": str(resp.status_code),
                    "message":     "Receipt updated successfully",
                    "raw":         data,
                }
            return {
                "success":     False,
                "status_code": str(resp.status_code),
                "message":     resp.text[:2000],
                "raw":         None,
            }
        except httpx.HTTPError as e:
            log_oracle_error(e, tag="oracle.standardReceipts.patch")
            return {
                "success":     False,
                "status_code": "connection_error",
                "message":     str(e),
                "raw":         None,
            }


def _build_receipt_number(line_item: LineItem) -> str:
    """
    Generates the Oracle `ReceiptNumber` -- supplied by us, not Oracle.
    Format: FAL-<ou_number>-<YYYYMMDD>-<line_item_id>
      e.g.  FAL-111-20260604-1583

    "FAL" = Fusion Auto LockBox (previously "CASHAPPLY" -- renamed to
    match the product's new name; the format/uniqueness scheme itself is
    unchanged).

    Unique: <line_item_id> is LineItem.id, a DB auto-increment primary
    key -- globally unique for the table's lifetime, never reused.
    Idempotent across retries: same LineItem -> same ReceiptNumber, every
    time this is called.

    Defensive length guard: Oracle's ReceiptNumber field caps at 30
    characters. A larger OU number or line_item_id could in principle
    still exceed that -- truncates the recognizability prefix (never the
    trailing line_item_id, which is what guarantees uniqueness) and logs
    loudly if this ever fires.
    """
    ou = line_item.ou_number or "UNK"
    date_source = line_item.statement_date or line_item.created_at
    date_str = date_source.strftime("%Y%m%d") if date_source else "00000000"
    receipt_number = f"FAL-{ou}-{date_str}-{line_item.id}"
    if len(receipt_number) > 30:
        logger.warning(
            "ReceiptNumber '%s' (%d chars) exceeds Oracle's 30-char limit -- "
            "truncating the OU/date prefix. This format needs revisiting.",
            receipt_number, len(receipt_number),
        )
        suffix = f"-{line_item.id}"
        receipt_number = receipt_number[: 30 - len(suffix)] + suffix
    return receipt_number


def _resolve_customer_account_number(line_item: LineItem) -> str:
    """
    Returns the CustomerAccountNumber to send -- "" if genuinely
    unresolved (never null/omitted). Tries, in order:
      1. customer_number on any matched invoice.
      2. A fuzzy customer-name lookup against the currently-loaded aging
         report, if no invoice matched but a customer name was extracted.
    Logs which path was used / whether it's unresolved -- this used to
    be embedded in the payload as "_customer_account_number_unresolved"/
    "_fallback_lookup" audit keys; now it's just a log line, since
    nothing downstream ever parsed those keys programmatically.
    """
    matched_invoices_raw = line_item.matched_invoices or []
    customer_account_number = next(
        (m.get("customer_number") for m in matched_invoices_raw if m.get("customer_number")),
        None,
    )
    if not customer_account_number and line_item.extracted_customer_name:
        aging_map = aging_store.get_aging_map()
        if aging_map is not None:
            match, score = aging_map.fuzzy_customer(line_item.extracted_customer_name)
            if match:
                customer_account_number = match.customer_number
                logger.info(
                    "[receipt_payload] row=%s CustomerAccountNumber resolved via customer-name "
                    "fallback lookup -- matched '%s' at %.0f%% confidence for extracted name '%s'.",
                    line_item.id, match.customer_name, score, line_item.extracted_customer_name,
                )
    if not customer_account_number:
        logger.warning(
            "[receipt_payload] row=%s CustomerAccountNumber unresolved -- no matched-invoice "
            "customer_number and no confident aging-report name match. Posting with \"\" per spec.",
            line_item.id,
        )
    return customer_account_number or ""


def build_receipt_creation_payload(line_item: LineItem) -> dict:
    """
    Builds the Oracle AR standardReceipts CREATION payload — step 1 (Bank
    Reconciliation stage). Called for EVERY credit row, regardless of
    category, right after the analysis run finishes categorizing it — NOT
    gated on ready_for_oracle / SPOC approval. Never includes
    remittanceReferences — that's a separate, later POST (see
    build_remittance_reference_payloads), once a row reaches
    ready_for_oracle and a SPOC approves it.

    See this module's docstring for the exact two-case field model.
    Raises ValueError if this row is cross-ledger but its conversion rate
    hasn't been resolved -- see that docstring's note on why this is now
    a hard failure instead of a silently half-built payload.
    """
    # ── Resolve currency fields from LineItem ─────────────────────────────────
    invoice_currency    = (line_item.invoice_currency    or "").upper().strip()
    functional_currency = (line_item.functional_currency or "").upper().strip()
    credited_currency   = (line_item.statement_currency  or "").upper().strip()

    # Fall back gracefully when invoice_currency was not resolved (e.g. a
    # genuinely unidentified row with no matched invoice at all — this
    # payload is built for EVERY row, not just ones that matched).
    if not invoice_currency:
        invoice_currency = credited_currency or functional_currency

    credit_amount = float(line_item.credit_amount or 0)
    statement_date_iso = (
        line_item.statement_date.strftime("%Y-%m-%d")
        if line_item.statement_date else None
    )
    accounting_date_iso = dt.date.today().strftime("%Y-%m-%d")

    # ── Leg 1: convert credit amount → invoice currency (already resolved
    # upstream by the rule engine before this function ever runs) ───────────
    is_cross_currency    = bool(line_item.is_cross_currency)
    fx_credit_to_invoice = float(line_item.fx_credit_to_invoice) if line_item.fx_credit_to_invoice else None
    if is_cross_currency and fx_credit_to_invoice:
        amount_in_invoice_ccy = round(credit_amount * fx_credit_to_invoice, 2)
    else:
        amount_in_invoice_ccy = credit_amount

    # ── The ONE decision that drives everything else ─────────────────────────
    is_cross_ledger = invoice_currency != functional_currency

    fx_invoice_to_functional = float(line_item.fx_invoice_to_functional) if line_item.fx_invoice_to_functional else None
    if is_cross_ledger and not fx_invoice_to_functional:
        # Was previously a silent "_fx_leg2_missing" payload flag that
        # nothing downstream checked before posting anyway. Now a hard
        # failure -- a caller can never accidentally send a cross-ledger
        # receipt with no conversion rate.
        raise ValueError(
            f"Cannot build receipt payload for LineItem {line_item.id}: cross-ledger "
            f"conversion rate ({invoice_currency}->{functional_currency}) is not resolved. "
            f"Do not post until a rate is available."
        )

    # ── ReceiptMethod ──────────────────────────────────────────────────────────
    receipt_method_result = resolve_receipt_method(
        account_number=line_item.account_number,
        ou_number=line_item.ou_number,
    )
    receipt_method_name = receipt_method_result.receipt_method_name or "Standard"
    if not receipt_method_result.matched:
        logger.warning(
            "[receipt_payload] row=%s account='%s' not found in receipt_method_map.json -- "
            "posting with fallback ReceiptMethod='Standard'. Add this account to the extract "
            "before relying on it.",
            line_item.id, line_item.account_number,
        )
    elif receipt_method_result.ambiguous:
        logger.info(
            "[receipt_payload] row=%s account='%s' has multiple candidate receipt methods "
            "(class='%s' chosen by default priority order) -- confirm this is correct.",
            line_item.id, line_item.account_number, receipt_method_result.receipt_class,
        )

    # ── BusinessUnit: Oracle expects "NAME(ou)" (e.g. "PUNE(111)") ───────────
    business_unit = get_ou_display_name(line_item.ou_number) or line_item.business_unit

    # ── CustomerAccountNumber ──────────────────────────────────────────────────
    customer_account_number = _resolve_customer_account_number(line_item)

    # ── CASE 1: base 9 fields, always present ────────────────────────────────
    payload: dict = {
        "ReceiptNumber":               _build_receipt_number(line_item),
        "ReceiptMethod":               receipt_method_name,
        "BusinessUnit":                business_unit,
        "CustomerAccountNumber":       customer_account_number,
        "RemittanceBankAccountNumber": line_item.account_number,
        "Currency":                    invoice_currency,
        "Amount":                      amount_in_invoice_ccy,
        "AccountingDate":              accounting_date_iso,
        "ReceiptDate":                 statement_date_iso,
    }

    # ── CASE 2: + exactly 3 more, only when cross-ledger ─────────────────────
    if is_cross_ledger:
        payload["ConversionRateType"] = "User"
        payload["ConversionRate"]     = fx_invoice_to_functional
        payload["ConversionDate"]     = statement_date_iso

    return payload


def build_remittance_reference_payloads(
    line_item: LineItem,
    invoice_breakup: list[dict] | None,
) -> list[dict]:
    """
    Invoice mapping — step 2 (Finance Approval stage), ready_for_oracle
    rows only. Builds ONE payload per matched invoice for
    OracleFusionClient.post_remittance_reference(), which POSTs each to
    /standardReceipts/{standard_receipt_id}/child/remittanceReferences —
    the receipt itself must already exist (created in step 1).

    Parameters
    ----------
    invoice_breakup:
        Optional SPOC-confirmed split from the BreakupModal.
        Each entry: { "invoice_number": str, "reference_amount": float }
        reference_amount must be in invoice_currency.
        When provided, overrides matched_invoices stated_amounts.
    """
    if invoice_breakup:
        return [
            {
                "ReceiptMatchBy":  "Transaction Number",
                "ReferenceNumber": inv["invoice_number"],
                "ReferenceAmount": str(inv["reference_amount"]),
            }
            for inv in invoice_breakup
        ]

    matched = line_item.matched_invoices or []
    return [
        {
            "ReceiptMatchBy":  "Transaction Number",
            "ReferenceNumber": m["invoice_number"],
            "ReferenceAmount": str(m.get("stated_amount") or m["outstanding_amount"]),
        }
        for m in matched
    ]