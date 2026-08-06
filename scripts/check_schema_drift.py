"""
scripts/check_schema_drift.py
================================
Compares db/models.py (Base.metadata -- the SQLAlchemy model definitions)
against the ACTUAL live database schema, and reports every difference:
  - tables that exist in models.py but not in the DB
  - columns that exist in models.py but not in the DB
  - Postgres ENUM types where the DB is missing a label the Python Enum
    class defines (this is exactly the DISTRIBUTED bug from earlier --
    SQLAlchemy's Enum(SomePyEnum) stores by .name, so a DB enum type is
    missing labels whenever a member gets added to models.py without a
    matching `ALTER TYPE ... ADD VALUE` ever being run against this DB)

This project has no Alembic/migration tooling (checked -- none exists),
so schema changes are applied by hand. This script is a manual
stand-in for `alembic upgrade` finding nothing to do vs. something to do
-- it doesn't replace real migration tooling, just tells you what's out
of sync RIGHT NOW so you're not finding out one crash at a time.

USAGE
-----
  Report only (always safe, makes no changes):
      python scripts/check_schema_drift.py

  Report AND apply every statement it finds (adds missing columns/enum
  labels only -- never drops or alters an existing column, never touches
  data):
      python scripts/check_schema_drift.py --apply

Run this from the project root (same level as the `app/` package), with
the same virtualenv/DATABASE_URL your app itself uses -- it reuses
app.db.session's engine, so it always points at whatever DB your app is
actually configured for.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make sure the project root (parent of this scripts/ folder -- where the
# `app` package actually lives) is importable, regardless of how this
# script is invoked. `python scripts/check_schema_drift.py` only puts
# scripts/ itself on sys.path, not the project root -- without this,
# `from app.db.models import ...` fails with ModuleNotFoundError even
# though the app package is right there one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

# Import every model module so Base.metadata is fully populated before we
# read it -- mirrors how app/main.py brings models into scope at startup.
from app.db.models import Base, RowState  # noqa: F401,E402 -- RowState imported for the enum walk below
from app.db import models as models_module
from app.db.session import get_engine


def _python_enum_classes():
    """Every `class Foo(str, enum.Enum)` defined in db/models.py, paired
    with the Postgres enum type name SQLAlchemy will have created for it
    (lowercased class name, matching SQLAlchemy's default convention --
    e.g. RowState -> 'rowstate', the exact type from the DISTRIBUTED
    error earlier)."""
    import enum
    found = []
    for name in dir(models_module):
        obj = getattr(models_module, name)
        if isinstance(obj, type) and issubclass(obj, enum.Enum) and obj is not enum.Enum:
            found.append((obj.__name__.lower(), obj))
    return found


def check(apply: bool) -> int:
    engine = get_engine()
    insp = inspect(engine)
    problems: list[str] = []
    statements: list[str] = []

    existing_tables = set(insp.get_table_names())

    print("=" * 70)
    print("TABLES & COLUMNS")
    print("=" * 70)
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            msg = f"MISSING TABLE: '{table_name}' exists in models.py but not in the DB."
            problems.append(msg)
            print(f"  [MISSING TABLE] {table_name}")
            continue

        db_columns = {c["name"]: c for c in insp.get_columns(table_name)}
        for col in table.columns:
            if col.name not in db_columns:
                col_type = col.type.compile(dialect=engine.dialect)
                nullable = "" if col.nullable else " NOT NULL"
                stmt = f'ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}{nullable};'
                problems.append(f"MISSING COLUMN: {table_name}.{col.name}")
                statements.append(stmt)
                print(f"  [MISSING COLUMN] {table_name}.{col.name}  ({col_type})")

    print()
    print("=" * 70)
    print("ENUM TYPES  (Postgres label vs. Python Enum member .name)")
    print("=" * 70)
    with engine.connect() as conn:
        for pg_type_name, py_enum in _python_enum_classes():
            row = conn.execute(
                text("SELECT 1 FROM pg_type WHERE typname = :t"), {"t": pg_type_name},
            ).first()
            if row is None:
                print(f"  [SKIP] no Postgres enum type named '{pg_type_name}' found "
                      f"(only relevant if some column actually uses {py_enum.__name__})")
                continue

            existing_labels = {
                r[0] for r in conn.execute(
                    text(
                        "SELECT enumlabel FROM pg_enum "
                        "WHERE enumtypid = (SELECT oid FROM pg_type WHERE typname = :t) "
                        "ORDER BY enumsortorder"
                    ),
                    {"t": pg_type_name},
                )
            }
            for member in py_enum:
                if member.name not in existing_labels:
                    stmt = f"ALTER TYPE {pg_type_name} ADD VALUE IF NOT EXISTS '{member.name}';"
                    problems.append(f"MISSING ENUM LABEL: {pg_type_name}.{member.name}")
                    statements.append(stmt)
                    print(f"  [MISSING LABEL] {pg_type_name}.{member.name}")
            if all(m.name in existing_labels for m in py_enum):
                print(f"  [OK] {pg_type_name} — all {len(list(py_enum))} labels present")

    print()
    print("=" * 70)
    if not problems:
        print("No drift found — models.py and the live DB agree.")
        return 0

    print(f"{len(problems)} difference(s) found.")
    print()
    print("-- Statements to bring the DB in line with models.py:")
    for stmt in statements:
        print(f"  {stmt}")

    if apply:
        print()
        print("--apply passed — running the statements above now...")
        # NOTE: ALTER TYPE ... ADD VALUE cannot run inside an explicit
        # transaction on Postgres < 12 -- autocommit avoids that entirely
        # and is also simply correct here (each statement is independent
        # and additive; there's nothing to roll back as a unit).
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for stmt in statements:
                print(f"  applying: {stmt}")
                conn.execute(text(stmt))
        print("Done. Re-run without --apply to confirm a clean report.")
    else:
        print()
        print("Re-run with --apply to run these automatically, or run them yourself.")

    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually run the missing ALTER statements (additive only).")
    args = parser.parse_args()
    sys.exit(check(apply=args.apply))