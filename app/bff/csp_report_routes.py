"""
app.bff.csp_report_routes
==========================
Receives Content-Security-Policy violation reports the BROWSER sends
automatically via the `report-uri` directive (see the CSP header set at
the nginx layer -- this endpoint is exactly what report-uri points at).

Deliberately public / unauthenticated: the browser sending this report has
no session, no auth header, nothing to attach -- it's a fire-and-forget POST
the browser itself issues whenever a page violates the CSP. Requiring auth
here would make every single report silently fail to deliver, defeating the
entire point of having reporting at all.

Also deliberately tolerant of Content-Type: browsers have historically sent
`Content-Type: application/csp-report` (not `application/json`) for this,
which FastAPI's request.json() can reject depending on how strictly
Content-Type is enforced -- read the raw body and parse manually instead
of depending on a Pydantic model + exact content-type match.
"""
import json
import logging

from fastapi import APIRouter, Request, Response

log = logging.getLogger("app.security.csp")

router = APIRouter()


@router.post("/csp-report", status_code=204)
async def receive_csp_report(request: Request) -> Response:
    """
    Logs the violation and returns 204 either way -- this endpoint must
    never itself throw a 4xx/5xx back at the browser for a malformed or
    unexpected report body; that would just generate log noise on top of
    log noise. Worst case, an unparseable body is logged as-is.
    """
    try:
        raw = await request.body()
        parsed = json.loads(raw) if raw else {}
    except Exception:
        parsed = {"_unparsed_body": True}

    log.warning("[csp_violation] %s", parsed)
    return Response(status_code=204)
