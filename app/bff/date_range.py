"""
app.bff.date_range
===================
Shared date-range parsing for every `date_from`/`date_to` query param in this
app. Comparing a raw string against a `timestamp` column throws in Postgres
(`operator does not exist: timestamp >= character varying`) — this bug was
independently reintroduced in 5 different bff routes before being pulled out
here. Always parse before filtering.
"""
from __future__ import annotations

import datetime as dt


def parse_date_from(date_from: str) -> dt.datetime:
    return dt.datetime.strptime(date_from, "%Y-%m-%d")


def parse_date_to(date_to: str) -> dt.datetime:
    # created_at/started_at have a real time-of-day component — a bare
    # "<= date_to" would only match rows before midnight on that date.
    return dt.datetime.strptime(date_to, "%Y-%m-%d") + dt.timedelta(days=1) - dt.timedelta(microseconds=1)
