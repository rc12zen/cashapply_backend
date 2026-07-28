"""
app.oracle.fusion_client  (PATCHED v3)
=======================================
POST /standardReceipts client. Supports Basic Auth and OAuth.

ORACLE PAYLOAD MODEL (this revision)
--------------------------------------
Three-currency model to match Oracle AR's own posting logic:

  credited_currency  : what arrived in the bank (e.g. GBP)
  invoice_currency   : what the invoice was raised in (e.g. USD)
  functional_currency: OU ledger currency (e.g. INR)

PATCH (v3): "Currency" is NOT a standalone field that's always sent. A
confirmed real Oracle sample payload shows Currency only ever appears
TOGETHER WITH ConversionRateType/ConversionRate/ConversionDate, as one
all-or-nothing group -- present only when invoice_currency differs from
functional_currency (Oracle needs telling what foreign currency this
receipt is in and how to convert it). When invoice_currency ==
functional_currency, NONE of these four fields are sent at all -- Oracle
defaults to the ledger's own functional currency on its own. Sending
Currency alone, with no conversion fields, on a same-functional-currency
receipt (the old v2 behavior) was simply wrong, not just redundant.

Oracle payload shapes:

  Case A — All three currencies are the same (fully same-currency)
  ─────────────────────────────────────────────────────────────────
  No Currency, no ConversionRate/ConversionDate/ConversionRateType.
  Amount   = credit_amount (already in invoice = functional currency)

  Case B — credited != invoice  (Leg 1 conversion needed by us)
  ─────────────────────────────────────────────────────────────────
  We converted credit_amount → invoice_currency at analysis time
  (fx_credit_to_invoice). Amount is in invoice_currency either way.
  Amount = credit_amount * fx_credit_to_invoice

    If invoice == functional (no Leg 2 needed):
      No Currency, no ConversionRate* fields — same as Case A from here.

    If ALSO invoice != functional  (is_cross_ledger=True, Leg 2):
      Currency            = invoice_currency
      ConversionRateType  = "User"
      ConversionRate      = fx_invoice_to_functional
      ConversionDate      = statement_date
      Oracle uses ConversionRate internally to book in functional currency.
      We do NOT compute functional amounts — Oracle owns that.

  Case C — credited == invoice but invoice != functional
  ─────────────────────────────────────────────────────────────────
  No Leg 1 needed (same currency received as invoice denomination).
  Amount              = credit_amount
  Currency            = invoice_currency
  ConversionRateType  = "User"
  ConversionRate      = fx_invoice_to_functional
  ConversionDate      = statement_date

Example (from spec):
  credit: 100 GBP, invoice: 100 USD, functional: INR
  → Amount=100*fx(GBP→USD), Currency="USD",
    ConversionRate=fx(USD→INR), ConversionDate=statement_date

  credit: 100 USD, invoice: 100 USD, functional: INR
  → Amount=100, Currency="USD",
    ConversionRate=fx(USD→INR), ConversionDate=statement_date

  credit: 100 USD, invoice: 100 USD, functional: USD (all match)
  → Amount=100. No Currency, no ConversionRate* fields at all.

ReferenceAmount:
  Always in invoice_currency (same as Amount).
  stated_amount on each MatchedInvoice is already in invoice_currency
  (set by _resolve_matched_invoices in the evaluator using Leg 1).

PROBLEM 2 — is_cross_ou_currency in response
---------------------------------------------
build_receipt_creation_payload now includes is_cross_ou_currency from the
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
from ..aging import aging_store


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

        # PATCH: these underscore-prefixed audit fields (_fx_sources,
        # _receipt_method_unresolved, _is_cross_ou_currency, etc.) used to
        # be sent to Oracle AS-IS, on the theory that "Oracle ignores
        # unrecognized fields" — that was never actually verified, and a
        # real captured outbound request confirmed they WERE going out
        # over the wire in the JSON body. Log the FULL payload (audit
        # fields included) so the log always shows everything that was
        # computed for this row — but build a SEPARATE, stripped dict for
        # the actual POST, so Oracle only ever receives real receipt
        # fields, never our own internal audit metadata.
        log_oracle_request(
            "POST", url, headers=headers, auth=auth, json_body=payload,
            tag="oracle.standardReceipts",
        )
        outbound_payload = {k: v for k, v in payload.items() if not k.startswith("_")}

        try:
            resp = httpx.post(
                url, json=outbound_payload,
                headers=headers, auth=auth, timeout=60,
            )
            log_oracle_response(resp, tag="oracle.standardReceipts")

            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "success":             True,
                    "oracle_ref_no":       data.get("ReceiptNumber"),
                    # BUGFIX: was `data.get("ReceiptId") or data.get("id")` —
                    # a real Oracle response (confirmed against an actual
                    # /standardReceipts POST result) returns the numeric
                    # receipt ID under "StandardReceiptId", never "ReceiptId"
                    # or "id". As written before, standard_receipt_id would
                    # silently come back None on every real Oracle call —
                    # which would have broken invoice-mapping entirely,
                    # since that step needs this ID to address
                    # /standardReceipts/{id}/child/remittanceReferences.
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


def build_receipt_creation_payload(line_item: LineItem) -> dict:
    """
    Builds the Oracle AR standardReceipts CREATION payload — step 1 (Bank
    Reconciliation stage). Called for EVERY credit row, regardless of
    category, right after the analysis run finishes categorizing it — NOT
    gated on ready_for_oracle / SPOC approval.

    Deliberately excludes `remittanceReferences` entirely — invoice
    mapping happens later, as a separate POST to
    /standardReceipts/{standard_receipt_id}/child/remittanceReferences
    (see build_remittance_reference_payloads + OracleFusionClient.
    post_remittance_reference), once the row reaches ready_for_oracle and
    a SPOC approves it. At this stage we may not even have SPOC
    confirmation yet, and rows that never reach ready_for_oracle should
    still get a bare receipt per the current spec.

    CustomerAccountNumber is "" (empty string, not null/omitted) when no
    customer was identified for this row — confirmed against the
    business's own sample payload. Rows that are genuinely unidentified
    never reach ready_for_oracle anyway (matching requires an identified
    customer), so this only ever applies to rows that will never get
    invoice-mapped, not to a temporary/fixable gap.

    Parameters
    ----------
    line_item:
        Persisted LineItem with all three currency fields and both FX legs
        already resolved by the orchestrator (this happens for every row
        in the same pass, before categorization — see orchestrator.py's
        Pass 2 — so this data is available even for rows that end up
        unidentified/needs_remittance/conflict_exception).

    Payload shapes for Amount/Currency/ConversionRate — see this module's
    docstring for the full three-currency-model examples. PATCHED (v3):
    Currency/ConversionRateType/ConversionRate/ConversionDate are now one
    all-or-nothing group, sent only when invoice_currency != functional_currency
    — confirmed against a real Oracle sample payload; previously Currency was
    sent unconditionally on every payload, which was wrong.
    """
    # ── Resolve currency fields from LineItem ─────────────────────────────────
    credited_currency   = (line_item.statement_currency  or "").upper().strip()
    invoice_currency    = (line_item.invoice_currency    or "").upper().strip()
    functional_currency = (line_item.functional_currency or "").upper().strip()

    # Fall back gracefully when invoice_currency was not resolved (e.g. a
    # genuinely unidentified row with no matched invoice at all — this
    # payload is built for EVERY row now, not just ones that matched).
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
        amount_in_invoice_ccy = round(credit_amount * fx_credit_to_invoice, 2)
    else:
        amount_in_invoice_ccy = credit_amount

    # ── Leg 2: invoice → functional (Oracle ConversionRate) ──────────────────
    is_cross_ledger          = bool(line_item.is_cross_ledger)   # invoice != functional
    fx_invoice_to_functional = float(line_item.fx_invoice_to_functional) if line_item.fx_invoice_to_functional else None

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
    business_unit = get_ou_display_name(line_item.ou_number) or line_item.business_unit

    # ── CustomerAccountNumber: "" when unresolved, never null/omitted ─────────
    # PATCH: this used to ONLY look at matched_invoices — which requires an
    # INVOICE to have been matched. A needs_remittance row (customer
    # identified in the narrative, but no invoice number found) has
    # matched_invoices = [] by definition, so CustomerAccountNumber always
    # came back blank even when the aging report genuinely has that
    # customer on file — "customer identified" (Card 2's extracted_customer)
    # and "customer resolved to an aging account number" were being
    # silently conflated. Now falls back to a customer-name lookup against
    # the currently-loaded aging report when no invoice was matched, so
    # this field can be populated correctly even before an invoice is known.
    matched_invoices_raw = line_item.matched_invoices or []
    customer_account_number = next(
        (m.get("customer_number") for m in matched_invoices_raw if m.get("customer_number")),
        None,
    )
    customer_lookup_note = None
    if not customer_account_number and line_item.extracted_customer_name:
        aging_map = aging_store.get_aging_map()
        if aging_map is not None:
            match, score = aging_map.fuzzy_customer(line_item.extracted_customer_name)
            if match:
                customer_account_number = match.customer_number
                customer_lookup_note = (
                    f"CustomerAccountNumber resolved via customer-name lookup against the aging "
                    f"report (no invoice was matched) — matched '{match.customer_name}' at {score:.0f}% "
                    f"confidence for extracted name '{line_item.extracted_customer_name}'."
                )
    customer_account_number = customer_account_number or ""

    # ── Base payload — NO remittanceReferences here, see docstring ───────────
    # PATCH: "Currency" used to be unconditionally set to invoice_currency
    # here, on every payload. A real confirmed Oracle sample payload shows
    # Currency is NOT a standalone field -- it only appears TOGETHER WITH
    # ConversionRateType/ConversionRate/ConversionDate, as one all-or-
    # nothing group, for a receipt being raised in a currency OTHER than
    # the OU's functional (ledger) currency. When invoice_currency ==
    # functional_currency, Oracle defaults Currency to the ledger currency
    # on its own -- sending it (with no conversion fields alongside it) was
    # simply wrong, not just redundant. See the block below the audit-flag
    # checks for where this whole group now gets added together.
    payload: dict = {
        "ReceiptNumber":               _build_receipt_number(line_item),
        "ReceiptMethod":               receipt_method_name,
        "BusinessUnit":                business_unit,
        "CustomerAccountNumber":       customer_account_number,
        "RemittanceBankAccountNumber": line_item.account_number,
        "Amount":                      amount_in_invoice_ccy,
        "AccountingDate":              accounting_date_iso,
        "ReceiptDate":                 statement_date_iso,
    }

    if customer_lookup_note:
        payload["_customer_account_number_fallback_lookup"] = customer_lookup_note

    if not customer_account_number:
        payload["_customer_account_number_unresolved"] = (
            "No customer_number found on any matched invoice, and no confident "
            "customer-name match was found in the currently-loaded aging report "
            "either — posted with CustomerAccountNumber=\"\" per spec. This row will "
            "never reach ready_for_oracle / invoice mapping unless a customer is "
            "later identified."
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

    # ── Currency + Conversion* fields — ONE ALL-OR-NOTHING GROUP ─────────────
    # Only added when invoice_currency != functional_currency (a foreign-
    # currency receipt from the ledger's point of view). When they match,
    # NONE of these four fields are sent — Oracle uses the ledger's own
    # functional currency automatically, and amount_in_invoice_ccy is
    # already the correct functional-currency amount at that point (Leg 1
    # already converted credited_currency -> invoice_currency upstream;
    # since invoice_currency == functional_currency here, no further
    # conversion is needed or wanted).
    if is_cross_ledger and fx_invoice_to_functional:
        payload["Currency"]           = invoice_currency
        payload["ConversionRateType"] = "User"
        payload["ConversionRate"]     = fx_invoice_to_functional
        payload["ConversionDate"]     = statement_date_iso
    elif is_cross_ledger and not fx_invoice_to_functional:
        # Cross-ledger but the rate isn't resolved -- do NOT send a partial
        # group (Currency with no rate is worse than sending neither).
        # Flagged for a human, not silently posted in the functional
        # currency it doesn't actually belong in.
        payload["_fx_leg2_missing"] = (
            f"fx_invoice_to_functional ({invoice_currency}→{functional_currency}) "
            f"not resolved. DO NOT POST — re-evaluate after providing rate."
        )


    # ── Audit trail (stripped before the actual POST -- see
    # OracleFusionClient.post_standard_receipt(), which builds a separate,
    # cleaned dict for the real outbound request and only uses this full
    # version for logging) ─────────────────────────────────────────────────
    payload["_fx_sources"] = {
        "credited_currency":              credited_currency,
        "invoice_currency":               invoice_currency,
        "functional_currency":            functional_currency,
        "fx_credit_to_invoice":           fx_credit_to_invoice,
        "fx_credit_to_invoice_source":    line_item.fx_credit_to_invoice_source,
        "fx_invoice_to_functional":       fx_invoice_to_functional,
        "fx_invoice_to_functional_source": line_item.fx_invoice_to_functional_source,
    }
    payload["_is_cross_ou_currency"] = bool(
        getattr(line_item, "is_cross_ou_currency", False)
    )

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