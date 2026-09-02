"""
app.oracle.soap_client
=======================
SOAP client for Oracle Fusion AR's standardReceiptService —
processUnapplyReceipt. This is the receipt-REVERSAL call: it undoes the
application of ONE invoice against an ALREADY-CREATED receipt, at the
individual-invoice level (see hitl/service.py::reverse_receipt_invoice()
for the caller — it loops one SOAP call per invoice being unapplied, never
a whole-receipt batch). The receipt itself (StandardReceiptId/
ReceiptNumber) is NEVER touched by this call — only the application is
undone; the receipt stays in Oracle to be reused for a later re-mapping.

This is the only SOAP call anywhere in the codebase — everything else
Oracle-side (app/oracle/fusion_client.py) is REST/JSON over httpx. Rather
than add a SOAP framework dependency (zeep, suds, etc.) for this one
fixed-shape call, the envelope is built via plain string templating (with
XML-escaping on every interpolated value) and the response is parsed with
the stdlib's xml.etree.ElementTree — enough for "did this succeed, and if
not, why".

Auth is shared with OracleFusionClient (same module-level OAuth token
cache / same ORACLE_AUTH_MODE basic-auth settings) via subclassing —
NOT a second independent token cache, which would double IDCS token
traffic under load (see fusion_client.py's _token_lock/_cached_token
comment for why that matters).

NAMESPACES / CHANGE OPERATION (2026-09-02): the Oracle team confirmed the
real (non-truncated) `typ`/`com` namespace URIs and the correct
`changeOperation` value — the original envelope this module was first
built from had both wrong: the namespaces were truncated to
`.../commonSe...` (literally, not a copy-paste artifact) and
`changeOperation` was sent as `Create`. Oracle's own sample uses `POST` for
`changeOperation`, and the SOAP fault we were seeing ("Unknown method")
lines up with that — a request whose element namespaces don't match the
service's real target namespaces looks to the ADF SOAP binding like it's
calling an operation it's never heard of, hence "Unknown method" rather
than a validation error naming a bad field.

SOAPACTION (2026-09-02): confirmed via a working reference call from the
Oracle team — `SOAPAction: ""` (an explicit empty string, not the header
omitted entirely). The earlier `_SOAP_ACTION` guess derived from the `com`
namespace + operation name is wrong; this ADF SOAP binding doesn't use
that convention. `_SOAP_ACTION` below is now just `""`.

OPEN RISK (still not resolvable from the sample alone): any WS-Security
requirements. Oracle's confirmed sample used plain HTTP basic auth (same
as our `ORACLE_AUTH_MODE == "basic"` path) with no `wsse:Security` header,
which matches what this module already sends (`<soapenv:Header/>` empty),
so this is likely a non-issue — flagging only because it wasn't explicitly
ruled out for the OAuth auth mode path.
"""
from __future__ import annotations

import logging
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

import httpx

from .fusion_client import OracleFusionClient
from ..common.http_debug_log import log_oracle_request, log_oracle_response, log_oracle_error

logger = logging.getLogger("cashapply.oracle")

_SOAP_NS = {"soapenv": "http://schemas.xmlsoap.org/soap/envelope/"}

# Confirmed by Oracle's own working reference call (2026-09-02) — this
# ADF SOAP binding expects an explicit empty SOAPAction, not the header
# omitted and not a namespace-derived operation URI (the earlier guess).
_SOAP_ACTION = ""

_ENVELOPE_TEMPLATE = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:typ="http://xmlns.oracle.com/apps/financials/receivables/receipts/shared/standardReceiptService/commonService/types/"
                  xmlns:com="http://xmlns.oracle.com/apps/financials/receivables/receipts/shared/standardReceiptService/commonService/"
                  xmlns:typ1="http://xmlns.oracle.com/adf/svc/types/">
   <soapenv:Header/>
   <soapenv:Body>
      <typ:processUnapplyReceipt>
         <typ:changeOperation>POST</typ:changeOperation>
         <typ:unapplyReceipt>
            <com:BusinessUnit>{business_unit}</com:BusinessUnit>
            <com:ReceiptNumber>{receipt_number}</com:ReceiptNumber>
            <com:TrxNumber>{trx_number}</com:TrxNumber>
         </typ:unapplyReceipt>
         <typ:processControl>
            <typ1:returnMode>Full</typ1:returnMode>
            <typ1:exceptionReturnMode>Full</typ1:exceptionReturnMode>
            <typ1:partialFailureAllowed>false</typ1:partialFailureAllowed>
         </typ:processControl>
      </typ:processUnapplyReceipt>
   </soapenv:Body>
</soapenv:Envelope>"""


def _build_unapply_envelope(business_unit: str, receipt_number: str, trx_number: str) -> str:
    """
    Builds the processUnapplyReceipt SOAP envelope. Every interpolated
    value is XML-escaped (xml.sax.saxutils.escape) before insertion — none
    of BusinessUnit/ReceiptNumber/TrxNumber are validated-safe strings by
    construction (BusinessUnit in particular is a free-text "NAME(ou)"
    display string), so raw f-string interpolation into XML would risk a
    malformed envelope if any of them ever contained '&', '<', or '>'.
    """
    return _ENVELOPE_TEMPLATE.format(
        business_unit=xml_escape(business_unit or ""),
        receipt_number=xml_escape(receipt_number or ""),
        trx_number=xml_escape(trx_number or ""),
    )


def _extract_soap_fault(body_text: str) -> str | None:
    """
    Returns the fault message if `body_text` parses as XML and contains a
    <soapenv:Fault> (Oracle's SOAP binding may return a fault with HTTP 200
    OR 500 depending on the binding — callers must check both, not just
    status code). Returns None if the body isn't a fault (success, or not
    parseable as XML at all — e.g. an HTML error page from a proxy/gateway
    in front of the real endpoint).
    """
    try:
        root = ElementTree.fromstring(body_text)
    except ElementTree.ParseError:
        return None

    fault = root.find(".//soapenv:Fault", _SOAP_NS)
    if fault is None:
        # Some SOAP stacks emit an unprefixed/default-namespaced <Fault> —
        # fall back to a tag-name search that ignores namespace entirely.
        fault = next((el for el in root.iter() if el.tag.endswith("}Fault") or el.tag == "Fault"), None)
    if fault is None:
        return None

    faultstring = next((el for el in fault.iter() if el.tag.endswith("}faultstring") or el.tag == "faultstring"), None)
    return faultstring.text if faultstring is not None and faultstring.text else "SOAP Fault (no faultstring)"


class OracleSoapClient(OracleFusionClient):
    """
    Subclasses OracleFusionClient purely to reuse its shared
    _get_oauth_token()/_auth_headers()/settings — this class adds no REST
    behavior and does not call any of OracleFusionClient's REST methods.
    """

    def unapply_receipt(self, business_unit: str, receipt_number: str, trx_number: str) -> dict:
        """
        Calls processUnapplyReceipt for ONE invoice (trx_number) against an
        existing receipt (receipt_number). Returns the same normalised
        result shape the REST client methods use:
            { success, status_code, message, raw }
        `raw` is the full response text (SOAP has no clean JSON body).
        """
        s = self.settings
        url = s.ORACLE_SOAP_RECEIPT_SERVICE_URL
        if not url:
            return {
                "success": False,
                "status_code": "not_configured",
                "message": "ORACLE_SOAP_RECEIPT_SERVICE_URL is not set — cannot call processUnapplyReceipt.",
                "raw": None,
            }

        envelope = _build_unapply_envelope(business_unit, receipt_number, trx_number)

        auth = (
            (s.ORACLE_BASIC_USERNAME, s.ORACLE_BASIC_PASSWORD)
            if s.ORACLE_AUTH_MODE == "basic"
               and s.ORACLE_BASIC_USERNAME
               and s.ORACLE_BASIC_PASSWORD
            else None
        )
        headers = {
            **self._auth_headers(),
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": _SOAP_ACTION,
        }

        log_oracle_request(
            "POST", url, headers=headers, auth=auth, xml_body=envelope,
            tag="oracle.processUnapplyReceipt",
        )

        try:
            resp = httpx.post(
                url, content=envelope.encode("utf-8"),
                headers=headers, auth=auth, timeout=s.ORACLE_SOAP_TIMEOUT_SECONDS,
            )
            log_oracle_response(resp, tag="oracle.processUnapplyReceipt")

            fault_message = _extract_soap_fault(resp.text)
            if fault_message is not None:
                return {
                    "success": False,
                    "status_code": str(resp.status_code),
                    "message": fault_message[:2000],
                    "raw": resp.text[:8000],
                }
            if resp.status_code in (200, 201):
                return {
                    "success": True,
                    "status_code": str(resp.status_code),
                    "message": "Invoice unapplied successfully",
                    "raw": resp.text[:8000],
                }
            return {
                "success": False,
                "status_code": str(resp.status_code),
                "message": resp.text[:2000],
                "raw": resp.text[:8000],
            }
        except httpx.HTTPError as e:
            log_oracle_error(e, tag="oracle.processUnapplyReceipt")
            return {
                "success": False,
                "status_code": "connection_error",
                "message": str(e),
                "raw": None,
            }