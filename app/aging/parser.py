"""
app.aging.parser  (UPDATED)
==============================
Reads the aging report Excel file and builds an in-memory AgingMap.

CHANGES vs original:
  - load_aging_into_db() is REMOVED. No more `db.query(AgingInvoice).delete()`
    / row-by-row `db.add(AgingInvoice(...))` / `db.commit()`. The
    `aging_invoices` table is no longer written to at all.
  - New entry point: refresh_aging_map(db, source_file) -> dict
    Parses the Excel file and calls aging_store.set_aging_map() instead of
    writing to the DB. Returns the same {row count, etc} shape the old
    function returned, so the API route barely changes.
  - AgingMap.build() already accepts "a list of objects with these 8
    attributes" — it doesn't care if they're SQLAlchemy ORM rows or plain
    objects. So we feed it lightweight `RawAgingRow` instances built
    straight from the DataFrame, skip the DB entirely.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from ..db.models import SourceFile
from ..storage.client import get_storage_client
from .aging_map import AgingMap
from . import aging_store

AGING_BUCKET = "aging-reports"
_COLUMNS_CONFIG = Path(__file__).parent / "aging_columns.json"


@dataclass
class RawAgingRow:
    """
    Plain row shape matching exactly what AgingMap.build() reads.
    No DB model, no SQLAlchemy — pure in-memory parsing output.

    invoice_description / invoice_date carry DEFAULTS on purpose. This
    dataclass is reconstructed straight from the persisted snapshot
    (aging_store._load_snapshot_from_db does `RawAgingRow(**r)`), so a
    snapshot written before these fields existed would raise TypeError on
    the first read after deploy. Defaults make that old payload load
    cleanly instead of taking the aging map down until someone re-uploads.
    Any new field added here MUST carry a default for the same reason.
    """
    invoice_number: str
    customer_number: str
    customer_name: str
    invoice_type: str
    invoice_amount: float
    outstanding_amount: float
    invoice_currency: str
    ou_number: str
    # Blank on every unapplied receipt, populated on every credit memo —
    # that is exactly how the credit-memo pool tells the two apart. Also
    # the human-readable text the mapping card shows ("C-Worker Program
    # Rebate ...") instead of a bare document number.
    invoice_description: str = ""
    # Kept as normalised text, not a date object: it only ever gets
    # displayed, and this dataclass is JSON-serialised into the snapshot.
    invoice_date: str = ""


def _load_columns_config() -> dict:
    with open(_COLUMNS_CONFIG) as f:
        return json.load(f)


def _to_text(val) -> str:
    """
    Excel cell -> plain display text. Handles the three shapes this export
    actually produces: NaN (empty cell), a pandas Timestamp (date columns),
    and ordinary strings/numbers. Dates are reduced to YYYY-MM-DD rather
    than "2026-03-31 00:00:00", which is what str() on a Timestamp gives.
    """
    if val is None:
        return ""
    if isinstance(val, float) and str(val) == "nan":
        return ""
    if pd.isna(val):
        return ""
    if hasattr(val, "date"):          # datetime / pandas Timestamp
        try:
            return val.date().isoformat()
        except Exception:             # noqa: BLE001 — odd date-likes fall back to str()
            pass
    s = str(val).strip()
    return "" if s in ("nan", "-") else s


def _to_float(val) -> float:
    if val in (None, "", "-") or (isinstance(val, float) and str(val) == "nan"):
        return 0.0
    if isinstance(val, str):
        val = val.replace(",", "").strip()
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def parse_aging_file(local_path: str) -> list[RawAgingRow]:
    """
    Pure parsing — Excel -> list[RawAgingRow]. No DB interaction at all.
    Used by refresh_aging_map() and reusable for ad-hoc inspection/tests.
    """
    cfg_all = _load_columns_config()
    cfg = cfg_all["DEFAULT"]
    df = pd.read_excel(local_path, sheet_name=cfg["sheet_name"], header=cfg["header_row"])
    df.columns = [str(c).strip() for c in df.columns]
    cols = cfg["columns"]

    rows: list[RawAgingRow] = []
    for _, row in df.iterrows():
        invoice_number = row.get(cols["invoice_number"])
        if pd.isna(invoice_number) or str(invoice_number).strip() in ("", "-"):
            continue
        rows.append(RawAgingRow(
            invoice_number=str(invoice_number).strip(),
            customer_number=str(row.get(cols["customer_number"], "") or ""),
            customer_name=str(row.get(cols["customer_name"], "") or "").strip(),
            invoice_type=str(row.get(cols["invoice_type"], "") or ""),
            invoice_amount=_to_float(row.get(cols["invoice_amount"])),
            outstanding_amount=_to_float(row.get(cols["outstanding_amount"])),
            invoice_currency=str(row.get(cols["invoice_currency"], "") or ""),
            ou_number=str(row.get(cols["ou_number"], "") or ""),
            # cols.get(): these two are newer than the config file, and a
            # site-specific config that predates them should degrade to a
            # blank field rather than KeyError the whole refresh.
            invoice_description=_to_text(row.get(cols.get("invoice_description", ""))),
            invoice_date=_to_text(row.get(cols.get("invoice_date", ""))),
        ))
    return rows


def refresh_aging_map(db: Session, source_file: SourceFile) -> dict:
    """
    Replaces the old load_aging_into_db(). Parses the given SourceFile's
    Excel into an AgingMap and stores it in-memory via aging_store —
    NO database writes happen here.

    Returns: {"row_count": int, "invoice_count": int, "customer_count": int}
    """
    storage = get_storage_client()
    local_path = storage.local_path_for_read(AGING_BUCKET, source_file.storage_key)

    raw_rows = parse_aging_file(local_path)
    aging_map = AgingMap.build(raw_rows)   # build() only needs attribute access — works unchanged

    aging_store.set_aging_map(aging_map, filename=source_file.filename, row_count=len(raw_rows),
                               raw_rows=raw_rows)

    return {
        "row_count": len(raw_rows),
        "invoice_count": aging_map.invoice_count,
        "customer_count": aging_map.customer_count,
        # What AgingMap.build() refused to index, and why -- see
        # aging/aging_map.py's is_payable() / is_usable_invoice_number().
        # Surfaced here so a refresh reports its exclusions instead of
        # silently shrinking the matchable pool.
        "build_report": aging_map.build_report,
    }