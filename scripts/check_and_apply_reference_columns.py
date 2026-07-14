"""
scripts/check_and_apply_reference_columns.py
===============================================
Verifies whether line_items has the reference_* columns the two-step
Oracle receipt flow needs, and adds them if missing — using the SAME
DATABASE_URL the app itself reads from .env (via app.db.settings), so
there's no risk of accidentally running this against a different
database than the one uvicorn/the worker actually connect to.

Run from the project root (same folder as app/):
    python scripts/check_and_apply_reference_columns.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.db.settings import get_settings  # noqa: E402

REQUIRED_COLUMNS = {
    "reference_status":   "VARCHAR",
    "reference_added_at": "TIMESTAMP",
    "reference_message":  "TEXT",
    "reference_payload":  "JSON",
}


def main():
    settings = get_settings()
    engine = get_engine()

    # Print exactly which DB this script (and therefore the app) is using —
    # compare this against whatever you ran psql against before.
    print(f"DATABASE_URL from .env: {settings.DATABASE_URL}")

    with engine.connect() as conn:
        existing = {
            row[0]
            for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'line_items'"
            ))
        }

        print("\nChecking line_items columns...")
        missing = []
        for col, coltype in REQUIRED_COLUMNS.items():
            status = "OK — already exists" if col in existing else "MISSING"
            print(f"  {col:22s} {status}")
            if col not in existing:
                missing.append((col, coltype))

        if not missing:
            print("\nNothing to do — all reference_* columns already exist.")
            print("If you're still seeing 'column ... does not exist', the")
            print("running API process is connected to a DIFFERENT database")
            print("than this script just checked. Compare the DATABASE_URL")
            print("printed above against your actual .env file, and make")
            print("sure the API/worker were restarted AFTER this script ran.")
            return

        print(f"\nAdding {len(missing)} missing column(s)...")
        # SQLAlchemy 2.0 auto-begins a transaction on the first execute()
        # above (the SELECT) — commit that one out before starting a fresh
        # explicit transaction for the ALTERs, or conn.begin() below raises
        # "transaction already initialized".
        conn.commit()
        with conn.begin():
            for col, coltype in missing:
                conn.execute(text(
                    f"ALTER TABLE line_items ADD COLUMN IF NOT EXISTS {col} {coltype}"
                ))
                print(f"  + added {col} ({coltype})")

        print("\nDone. Restart the API and worker now.")


if __name__ == "__main__":
    main()