"""
build_csv_subheader_samples.py
===============================
Generates sample CSV bank statements for testing two-row (header + sub-header)
parsing in the Config Builder wizard, and the delimiter/encoding sniffing that
the wizard preview depends on.

    python build_csv_subheader_samples.py

Writes into test_data/csv_subheader/. Upload each through
Config > Bank Data Ingestion (the Config Builder wizard), NOT via Home --
these exist to exercise recipe building, not to produce a full analysis run.

WHY THESE SIX
-------------
Each file targets a specific code path in
app/bank_statement/extractor/csv_extractor.py:

  1. canonical      the ordinary shape -- a parent spanning Cr/Dr with blank
                    parents elsewhere. The happy path.
  2. repeated_parent the parent label repeated on BOTH sub-columns instead of
                    left blank. Worth testing because the merge rule must fire
                    on the sub-value even when the parent is present; if a real
                    bank does this, it is the shape most likely to surprise us.
  3. semicolon      ';' delimited, as European banks commonly send. Exercises
                    sniff_dialect end to end -- before that was shared with the
                    wizard preview, a file like this rendered as one unusable
                    column in the grid while extracting perfectly.
  4. latin1         latin-1 encoded with accented narratives. Exercises the
                    utf-8 -> latin-1 fallback.
  5. ragged         a sub-header row WIDER than the main header, plus trailing
                    commas. Exercises the max-width loop and the trailing
                    header-less column trim that keeps CSV column names
                    identical to Excel's.
  6. control        a single header row, no sub-header. Confirms the ordinary
                    path still behaves exactly as before.

Account numbers are real ones from this environment's bank_accounts table, so
the wizard's account locator resolves instead of failing on an unknown account.

NOTE ON RUNNING A FULL ANALYSIS with these: if you do, bump the dates first.
Oracle receipt numbers are derived from ou + date + line item id, so re-running
the same dates after a transactional reset can collide with receipts Oracle
already holds (error AR-857749) -- see reopen_with_edits_test_guide.md.
"""
from __future__ import annotations

import csv
import os

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "test_data", "csv_subheader")

# Real accounts in this environment (bank_accounts table).
HSBC_USD = ("HSBC Bank USA NA", "914031244", "USD")
SCB_GBP = ("Standard Chartered Bank", "01250858301", "GBP")
HSBC_GBP = ("HSBC", "41678876", "GBP")


def _preamble(bank: str, account: str, currency: str, width: int) -> list[list[str]]:
    """
    The block banks put above the table. Two things it gives the wizard:
    an account number in a single clean cell (so the account locator can use a
    `cell` source rather than parsing prose), and a blank spacer row -- which is
    exactly the kind of thing that makes header-row indexes non-obvious and so
    worth having in the test data.
    """
    pad = [""] * (width - 2)
    return [
        [bank, *[""] * (width - 1)],
        ["Account Number:", account, *pad],
        ["Currency:", currency, *pad],
        ["Statement Period:", "12/08/2026 to 19/08/2026", *pad],
        [""] * width,
    ]


def write(name: str, rows: list[list[str]], delimiter: str = ",",
          encoding: str = "utf-8") -> str:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding=encoding) as f:
        csv.writer(f, delimiter=delimiter).writerows(rows)
    return path


def build_canonical() -> tuple[str, str]:
    bank, account, currency = HSBC_USD
    rows = _preamble(bank, account, currency, 6)
    rows += [
        # main header (row 5) -- "Amount" is the parent; the two columns under
        # it are distinguished only by the sub-header.
        ["Date", "Narrative", "Reference", "Amount", "", "Balance"],
        # sub-header (row 6)
        ["", "", "", "Cr", "Dr", ""],
        ["12/08/2026", "ACME LTD PAYMENT INV 11172600005171", "TRF00001", "20800.55", "", "20800.55"],
        ["13/08/2026", "BETA INDUSTRIES INV 11172600005182", "TRF00002", "15420.00", "", "36220.55"],
        ["14/08/2026", "BANK CHARGES AUG", "CHG00001", "", "25.00", "36195.55"],
        ["15/08/2026", "GAMMA CORP PART PAYMENT INV 11172600005190", "TRF00003", "9875.40", "", "46070.95"],
        ["18/08/2026", "DELTA HOLDINGS INV 11172600005204", "TRF00004", "4310.25", "", "50381.20"],
    ]
    return write("sub_canonical_HSBC_914031244.csv", rows), "header=5, sub-header=6"


def build_repeated_parent() -> tuple[str, str]:
    bank, account, currency = HSBC_USD
    rows = _preamble(bank, account, currency, 6)
    rows += [
        # The parent is repeated rather than left blank -- the merge rule has to
        # fire on the sub-value regardless.
        ["Date", "Narrative", "Reference", "Amount", "Amount", "Balance"],
        ["", "", "", "Cr", "Dr", ""],
        ["12/08/2026", "EPSILON TRADING INV 11172600005211", "TRF00011", "7250.00", "", "7250.00"],
        ["13/08/2026", "WIRE FEE", "CHG00011", "", "18.50", "7231.50"],
        ["14/08/2026", "ZETA LLC INV 11172600005225", "TRF00012", "12100.75", "", "19332.25"],
    ]
    return write("sub_repeated_parent_HSBC_914031244.csv", rows), "header=5, sub-header=6"


def build_semicolon() -> tuple[str, str]:
    bank, account, currency = SCB_GBP
    rows = _preamble(bank, account, currency, 6)
    rows += [
        ["Date", "Narrative", "Reference", "Amount", "", "Balance"],
        ["", "", "", "Cr", "Dr", ""],
        ["12/08/2026", "OMEGA RETAIL LTD INV 11172600005240", "SCB0001", "8400.00", "", "8400.00"],
        ["13/08/2026", "SIGMA PLC INV 11172600005251", "SCB0002", "3275.60", "", "11675.60"],
        ["15/08/2026", "ACCOUNT MAINTENANCE", "SCBCHG1", "", "12.00", "11663.60"],
    ]
    return write("sub_semicolon_SCB_01250858301.csv", rows, delimiter=";"), \
        "header=5, sub-header=6 (semicolon delimited)"


def build_latin1() -> tuple[str, str]:
    bank, account, currency = HSBC_GBP
    rows = _preamble(bank, account, currency, 6)
    rows += [
        ["Date", "Narrative", "Reference", "Amount", "", "Balance"],
        ["", "", "", "Cr", "Dr", ""],
        # Characters that exist in latin-1 but are not valid utf-8 once encoded,
        # so the utf-8 attempt fails and the fallback must take over.
        ["12/08/2026", "CAFÉ MÜNCHEN GMBH INV 11172600005260", "EUR0001", "5600.00", "", "5600.00"],
        ["13/08/2026", "ZÜRICH LOGISTIK AG INV 11172600005271", "EUR0002", "2340.90", "", "7940.90"],
        ["14/08/2026", "FRAIS BANCAIRES AOÛT", "CHG0002", "", "9.75", "7931.15"],
    ]
    return write("sub_latin1_HSBC_41678876.csv", rows, encoding="latin-1"), \
        "header=5, sub-header=6 (latin-1 encoded)"


def build_ragged() -> tuple[str, str]:
    bank, account, currency = HSBC_USD
    rows = _preamble(bank, account, currency, 6)
    rows += [
        # Main header STOPS at Amount; the sub-header is one column wider, and
        # data rows carry a trailing empty field. Both are real-world artefacts
        # of hand-built exports.
        ["Date", "Narrative", "Reference", "Amount"],
        ["", "", "", "Cr", "Dr", ""],
        ["12/08/2026", "THETA SYSTEMS INV 11172600005280", "TRF00021", "6150.00", "", ""],
        ["13/08/2026", "RETURNED ITEM FEE", "CHG00021", "", "30.00", ""],
    ]
    return write("sub_ragged_HSBC_914031244.csv", rows), "header=5, sub-header=6 (ragged)"


def build_control() -> tuple[str, str]:
    bank, account, currency = HSBC_USD
    rows = _preamble(bank, account, currency, 6)
    rows += [
        # One header row, no sub-header. Do NOT add a sub-header in the wizard.
        ["Date", "Narrative", "Reference", "Credit Amount", "Debit Amount", "Balance"],
        ["12/08/2026", "IOTA VENTURES INV 11172600005291", "TRF00031", "11000.00", "", "11000.00"],
        ["13/08/2026", "KAPPA GROUP INV 11172600005302", "TRF00032", "2500.00", "", "13500.00"],
        ["14/08/2026", "SERVICE CHARGE", "CHG00031", "", "15.00", "13485.00"],
    ]
    return write("no_subheader_control_HSBC_914031244.csv", rows), "header=5, NO sub-header"


BUILDERS = [
    ("canonical",        build_canonical),
    ("repeated parent",  build_repeated_parent),
    ("semicolon",        build_semicolon),
    ("latin-1",          build_latin1),
    ("ragged",           build_ragged),
    ("control",          build_control),
]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    print()
    print(f"Writing samples to {OUT_DIR}")
    print()
    for label, fn in BUILDERS:
        path, note = fn()
        print(f"  {label:<16} {os.path.basename(path):<44} {note}")
    print()
    print("WIZARD SETTINGS (same for files 1-5):")
    print("  Step 2 Header Row : click row 5  (Date / Narrative / Reference / Amount ...)")
    print("  Add sub-header row: click row 6  (blank / blank / blank / Cr / Dr)")
    print("  Merge rules       : Cr -> Credit Amount     Dr -> Debit Amount")
    print()
    print("  Expected columns after merging:")
    print("    Date | Narrative | Reference | Credit Amount | Debit Amount | Balance")
    print()
    print("FIELD MAPPING (step 3):")
    print("  date           -> column  Date")
    print("  narrative      -> column  Narrative")
    print("  credit_amount  -> column  Credit Amount")
    print("  bank_reference -> column  Reference")
    print("  account_number -> cell    row 1, col 1")
    print("  currency       -> cell    row 2, col 1   (or fixed)")
    print("  bank_name      -> cell    row 0, col 0   (or fixed)")
    print()
    print("File 6 (control) has no sub-header: set header row 5 and map")
    print("credit_amount straight to the 'Credit Amount' column.")
    print()


if __name__ == "__main__":
    main()
