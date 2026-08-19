"""
app.bank_statement.date_inference
==================================
Column-level date-format DETECTION for the config builder.

The wizard used to send a fixed guess-list of formats and let the parser try
them first-match-wins — which silently mis-reads ambiguous numeric dates (the
classic US MM/DD vs intl DD/MM problem: "08/04/26" is 8-Apr or 4-Aug?). This
module instead looks at a whole sample of a column's real values and:

  * resolves the format automatically whenever the DATA proves it — e.g. any
    value with a first component > 12 ("13/08/26") can only be day-first, so
    month-first is eliminated; month-name ("4-Aug-26") and ISO ("2026-08-04")
    are never ambiguous;
  * only reports "ambiguous" when the ENTIRE sample is genuinely undecidable
    (every value ≤ 12 in both slots), returning the competing interpretations
    (with the first sample shown resolved both ways) so the UI can ask the SPOC
    to pick — see ConfigBuilderWizard.tsx.

infer_date_format(samples) -> dict:
    {"status": "resolved"|"ambiguous"|"unparseable"|"empty",
     "formats": [<format key>],          # resolved: the one key to store
     "label": <human label>,             # resolved
     "example": {"value","date"},        # resolved
     "choices": [                         # ambiguous
        {"id","label","formats":[key],"example":{"value","date"}}, ...],
     "detail": <message>}

Every returned format key exists in parser._FORMAT_MAP, so the stored recipe
value round-trips through the exact same parser at runtime.
"""
from __future__ import annotations

import datetime as dt

from .parser import _FORMAT_MAP, strip_time_component

# Candidate formats to test, unambiguous families first so that when several
# survive AND agree on every date, the canonical pick is the least surprising
# one. Every key must be present in parser._FORMAT_MAP.
_CANDIDATES = [
    # ISO / year-first — unambiguous
    "YYYY-MM-DD", "YYYY/MM/DD",
    # month-name — unambiguous (the alpha token fixes which slot is the month)
    "DD-Mon-YYYY", "DD-Mon-YY", "DD Mon YYYY", "DD Mon YY",
    "Mon DD, YYYY", "Mon DD YYYY", "Mon-DD-YYYY",
    # numeric, 4-digit year — day-first listed before month-first (see note)
    "DD/MM/YYYY", "MM/DD/YYYY", "DD-MM-YYYY", "MM-DD-YYYY", "DD.MM.YYYY", "MM.DD.YYYY",
    # numeric, 2-digit year
    "DD/MM/YY", "MM/DD/YY", "DD-MM-YY", "MM-DD-YY", "DD.MM.YY", "MM.DD.YY",
]

_MAX_SAMPLES = 40


def _friendly(key: str) -> str:
    """Human label for a choice, calling out day-first vs month-first when the
    key makes the order explicit (that's the ambiguity the SPOC is resolving)."""
    if key[:2] == "DD":
        return f"Day first ({key})"
    if key[:2] == "MM":
        return f"Month first ({key})"
    return key


def infer_date_format(samples: list[str]) -> dict:
    vals: list[str] = []
    for s in samples or []:
        s = str(s).strip()
        if s and s.lower() not in ("nan", "none", "nat"):
            vals.append(s)
    if not vals:
        return {"status": "empty", "detail": "No date values to inspect yet."}

    vals = vals[:_MAX_SAMPLES]

    # A format "survives" only if it parses EVERY sample. This is what makes
    # detection evidence-based: a single "13/08/26" kills every month-first
    # candidate, a single "08/13/26" kills every day-first one.
    surviving: list[tuple[str, tuple]] = []
    for key in _CANDIDATES:
        fmt = _FORMAT_MAP[key]
        seq: list[dt.date] = []
        ok = True
        for v in vals:
            # Raw value first, then with a trailing time component removed --
            # the same two candidates, in the same order, that parser._parse_date
            # tries at runtime.
            #
            # Without the second candidate this detector was STRICTER than the
            # parser it exists to predict: a column of pandas-rendered dates
            # ("2026-05-01 00:00:00", which is simply how a real Excel/CSV date
            # cell stringifies) killed every candidate and reported "none of the
            # known date formats parse every sample" -- while ingestion read
            # those same values perfectly as YYYY-MM-DD. Reporting a format as
            # unrecognised when the engine handles it is worse than a missing
            # format: it sends someone off to re-check a correctly-mapped column.
            parsed = None
            for candidate in (v, strip_time_component(v)):
                try:
                    parsed = dt.datetime.strptime(candidate, fmt).date()
                    break
                except ValueError:
                    continue
            if parsed is None:
                ok = False
                break
            seq.append(parsed)
        if ok:
            surviving.append((key, tuple(seq)))

    if not surviving:
        return {
            "status": "unparseable",
            "detail": (f"None of the known date formats parse every sample "
                       f"(e.g. {vals[0]!r}). The Date field may point at the wrong "
                       f"column, or the format isn't one we recognise."),
        }

    # Group survivors by the actual sequence of dates they produce. One group →
    # every survivor agrees → resolved. More than one → genuinely ambiguous.
    groups: dict[tuple, list[str]] = {}
    for key, seq in surviving:
        groups.setdefault(seq, []).append(key)

    if len(groups) == 1:
        key, seq = surviving[0][0], surviving[0][1]
        return {
            "status": "resolved",
            "formats": [key],
            "label": key,
            "example": {"value": vals[0], "date": seq[0].isoformat()},
            "detail": f"Detected date format {key} (e.g. {vals[0]} → {seq[0].isoformat()}).",
        }

    # Ambiguous — present each distinct interpretation, first sample shown as it
    # would be read under that interpretation, so the SPOC can pick the right one.
    choices = []
    for seq, keys in groups.items():
        key = keys[0]
        choices.append({
            "id": key,
            "label": _friendly(key),
            "formats": [key],
            "example": {"value": vals[0], "date": seq[0].isoformat()},
        })
    return {
        "status": "ambiguous",
        "choices": choices,
        "detail": (f"Dates like {vals[0]!r} are ambiguous — the sample doesn't prove "
                   f"whether the day or the month comes first. Please choose the correct "
                   f"interpretation."),
    }
