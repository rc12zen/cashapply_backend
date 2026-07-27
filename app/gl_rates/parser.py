"""
app.gl_rates.parser
======================
Reads a GL Daily Rates extract file (.xlsx/.xls/.csv) and UPSERTS its rows
into the gl_daily_rates table (db/models.py's GlDailyRate).

Unlike aging/parser.py (which builds an in-memory AgingMap and never
touches the DB), GL rates need to actually persist and ACCUMULATE across
files -- a new day's rates arrive in a new file each morning and should
ADD to rate history, not replace it. So this module writes to the DB
directly, upserting on the (from_currency, to_currency, conversion_date,
conversion_rate_type) natural key so re-loading the same file twice is
safe (idempotent) instead of creating duplicate rows.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from ..db.models import GlDailyRate, SourceFile
from ..storage.client import get_storage_client

logger = logging.getLogger(__name__)

GL_RATES_BUCKET = "gl-rates"
_COLUMNS_CONFIG = Path(__file__).parent / "gl_rates_columns.json"

# Rows are upserted in batches rather than one INSERT per row -- a real GL
# Daily Rates extract can be hundreds of thousands of rows (the reference
# extract this app was built against was 234,260 rows).
_UPSERT_BATCH_SIZE = 2000


def _load_columns_config() -> dict:
    with open(_COLUMNS_CONFIG) as f:
        return json.load(f)


def _to_date(val) -> dt.date | None:
    """Accepts a pandas Timestamp, python datetime/date, or common string
    formats -- Excel's own date cells usually arrive as pandas Timestamps
    already, but a CSV export will come through as plain strings."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, dt.datetime):
        return val.date()
    if isinstance(val, dt.date):
        return val
    if isinstance(val, pd.Timestamp):
        return val.date()
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Last resort -- let pandas guess.
    try:
        return pd.to_datetime(s).date()
    except Exception:
        logger.warning("[gl_rates] Could not parse ConversionDate value %r -- row skipped.", val)
        return None


def _to_rate(val) -> float | None:
    if val in (None, "", "-") or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, str):
        val = val.replace(",", "").strip()
    try:
        rate = float(val)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def parse_gl_rates_file(local_path: str) -> list[dict]:
    """
    Pure parsing -- file -> list of row dicts. No DB interaction.
    Each dict has: from_currency, to_currency, conversion_date (date),
    conversion_rate_type, conversion_rate (float).
    Rows with an unparseable date or rate are skipped (logged, not raised --
    one bad row in a 200k-row file shouldn't fail the whole load).
    """
    cfg_all = _load_columns_config()
    cfg = cfg_all["DEFAULT"]
    cols = cfg["columns"]

    suffix = Path(local_path).suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(local_path, header=cfg["header_row"])
    else:
        df = pd.read_excel(local_path, sheet_name=cfg["sheet_name"], header=cfg["header_row"])
    df.columns = [str(c).strip() for c in df.columns]

    rows: list[dict] = []
    skipped = 0
    for _, row in df.iterrows():
        from_ccy = str(row.get(cols["from_currency"], "") or "").strip().upper()
        to_ccy = str(row.get(cols["to_currency"], "") or "").strip().upper()
        rate_type = str(row.get(cols["conversion_rate_type"], "") or "").strip()
        conv_date = _to_date(row.get(cols["conversion_date"]))
        rate = _to_rate(row.get(cols["conversion_rate"]))

        if not from_ccy or not to_ccy or not rate_type or conv_date is None or rate is None:
            skipped += 1
            continue

        rows.append({
            "from_currency": from_ccy,
            "to_currency": to_ccy,
            "conversion_date": conv_date,
            "conversion_rate_type": rate_type,
            "conversion_rate": rate,
        })

    if skipped:
        logger.warning("[gl_rates] Skipped %d row(s) with missing/unparseable fields.", skipped)

    return rows


def _upsert_postgres(db: Session, rows: list[dict], source_filename: str) -> int:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    written = 0
    for i in range(0, len(rows), _UPSERT_BATCH_SIZE):
        batch = rows[i:i + _UPSERT_BATCH_SIZE]
        for r in batch:
            r["source_filename"] = source_filename
            r["loaded_at"] = dt.datetime.utcnow()
        stmt = pg_insert(GlDailyRate).values(batch)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_gl_daily_rate_pair_date_type",
            set_={
                "conversion_rate": stmt.excluded.conversion_rate,
                "source_filename": stmt.excluded.source_filename,
                "loaded_at": stmt.excluded.loaded_at,
            },
        )
        db.execute(stmt)
        written += len(batch)
    return written


def _upsert_generic(db: Session, rows: list[dict], source_filename: str) -> int:
    """
    Fallback path for non-Postgres backends (e.g. sqlite in local unit
    tests) that don't support ON CONFLICT the same way -- slower
    (query-then-insert-or-update per row) but correct anywhere SQLAlchemy
    runs. Not expected to be the hot path in real deployments.
    """
    written = 0
    for r in rows:
        existing = (
            db.query(GlDailyRate)
            .filter(
                GlDailyRate.from_currency == r["from_currency"],
                GlDailyRate.to_currency == r["to_currency"],
                GlDailyRate.conversion_date == r["conversion_date"],
                GlDailyRate.conversion_rate_type == r["conversion_rate_type"],
            )
            .first()
        )
        if existing:
            existing.conversion_rate = r["conversion_rate"]
            existing.source_filename = source_filename
            existing.loaded_at = dt.datetime.utcnow()
        else:
            db.add(GlDailyRate(
                from_currency=r["from_currency"],
                to_currency=r["to_currency"],
                conversion_date=r["conversion_date"],
                conversion_rate_type=r["conversion_rate_type"],
                conversion_rate=r["conversion_rate"],
                source_filename=source_filename,
                loaded_at=dt.datetime.utcnow(),
            ))
        written += 1
        if written % _UPSERT_BATCH_SIZE == 0:
            db.flush()
    return written


def load_gl_rates_into_db(db: Session, source_file: SourceFile) -> dict:
    """
    Parses the given SourceFile's GL rates extract and UPSERTS every row
    into gl_daily_rates. This is the DB-writing counterpart to
    aging/parser.py's refresh_aging_map() (which deliberately does NOT
    write to the DB) -- GL rates need to persist and accumulate, so this
    one does.

    Returns: {"row_count": int, "written_count": int, "skipped_count": int}
    """
    storage = get_storage_client()
    local_path = storage.local_path_for_read(GL_RATES_BUCKET, source_file.storage_key)

    raw_rows = parse_gl_rates_file(local_path)

    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        written = _upsert_postgres(db, raw_rows, source_file.filename)
    else:
        written = _upsert_generic(db, raw_rows, source_file.filename)

    return {
        "row_count": len(raw_rows),
        "written_count": written,
    }