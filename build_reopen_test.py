"""
Build the reopen-with-edits test bank statement from the LIVE aging report.

Every amount is resolved from the currently-loaded AgingMap and the script fails
loudly if an invoice has gone, so a stale figure can never quietly produce a
confusing test result.

Narratives are prefixed T1..T10 so a row can be identified at a glance in the
UI. The prefix is deliberately short: the invoice-number locator regex requires
6+ alphanumeric characters, so "T1".."T10" cannot be mistaken for an invoice.

VALUE DATE MUST BE UNIQUE PER TEST CYCLE -- read before re-running
-----------------------------------------------------------------
Oracle rejects a second receipt carrying the same (ReceiptNumber, date, amount,
customer) with AR-857749. Our ReceiptNumber is
    CASHAPPLY-<ou>-<YYYYMMDD>-<line_item_id>
where YYYYMMDD comes from the row's VALUE DATE and <line_item_id> is a DB
auto-increment.

scripts/reset_transactional.py restarts that sequence at 1, but the receipts
already created in Oracle are NOT deleted -- the reset only clears our database.
So re-running the same statement after a reset regenerates receipt numbers that
Oracle already holds, and receipt creation fails on any row whose date, amount
and customer also line up. That is exactly what happened on 2026-08-18: T1 and
T10 failed with AR-857749 while the other eight went through, purely because
their line_item ids happened to land on the same payments as the previous run.

This is a dev/test artifact only -- in a real deployment ids never restart, so
numbers never repeat. The fix is therefore here, not in the app: bump VALUE_DATE
to a date not used in a previous cycle before each reset-and-rerun.

    VALUE_DATE must be in the past relative to today, or Oracle may reject the
    receipt on a closed/not-yet-open accounting period.

Run from cashapply_backend/:
    PYTHONPATH=. venv/Scripts/python.exe build_reopen_test.py
"""
import pandas as pd
from app.aging import aging_store

aging = aging_store.get_aging_map()
st = aging_store.get_status()
if aging is None:
    raise SystemExit("No aging report loaded -- load one first.")
print("aging:", st.get("filename"))


def inv(cust, num):
    for v in aging.invoices_for_customer(cust):
        if v.invoice_number == num:
            return v
    raise SystemExit(f"MISSING {cust} / {num} -- the aging report changed; pick a new invoice.")


voltaire = inv("Voltaire Inc.", "11172600006350")
sitime   = inv("SiTime Corporation", "11172600003366")
assurant = inv("Assurant, Inc.", "11172600006255")
uber     = inv("Uber Technologies Inc", "11172600002745")
muus     = inv("Muus Collective, Inc.", "11172600001566")
beck     = inv("Beckman Coulter Inc.", "11172600006119")
ceph_a   = inv("Cepheid", "11172600003285")
ceph_b   = inv("Cepheid", "11172600003286")
esko     = inv("Esko Graphics BV", "11172600005171")

# Bump this before every reset-and-rerun cycle (see the module docstring).
# Cycle log -- append, never reuse:
#   20/03/2026  first cycle (2026-08-18) -- T1 and T10 hit AR-857749 on re-run
#   05/08/2026  second cycle (2026-08-18) -- fresh receipt numbers
VALUE_DATE = "05/08/2026"
D = VALUE_DATE
print("value date:", VALUE_DATE, "(drives every ReceiptNumber -- must be unused)")

# (id, narrative-after-prefix, amount, expected start)
# NOTE on T5: with AI extraction DISABLED this lands in Unidentified, not Needs
# Remittance -- a bare customer name in narrative text needs the AI layer to be
# resolved. Both outcomes are a valid starting point for the reopen tests.
rows = [
    ("T1",  f"PAYMENT INV {voltaire.invoice_number} VOLTAIRE INC",
     voltaire.outstanding_amount, "Ready for Oracle (R9a exact)"),
    ("T2",  f"REMIT {sitime.invoice_number} SITIME CORPORATION",
     round(sitime.outstanding_amount * 0.95, 2), "Short Payment (R9b, 5% short, no credit memos)"),
    ("T3",  f"ACH {assurant.invoice_number} ASSURANT INC",
     round(assurant.outstanding_amount * 0.93, 2), "Conflict/Shortage (R9c - 7% short but customer holds credit memos)"),
    ("T4",  f"WIRE {uber.invoice_number} UBER TECHNOLOGIES",
     round(uber.outstanding_amount + 4000, 2), "Conflict (R11 unexplained overpayment)"),
    ("T5",  "PAYMENT RECEIVED HACH COMPANY THANK YOU",
     25000.00, "Unidentified with AI off (Needs Remittance with AI on)"),
    ("T6",  "MISC CREDIT 88213 ADJUSTMENT NO REF",
     1500.00, "Unidentified (R8 no signal)"),
    ("T7",  f"PMT {ceph_a.invoice_number} {ceph_b.invoice_number} CEPHEID",
     round(ceph_a.outstanding_amount + ceph_b.outstanding_amount, 2), "Ready for Oracle (R9a, two invoices)"),
    ("T8",  f"WIRE {muus.invoice_number} MUUS COLLECTIVE",
     round(muus.outstanding_amount + 1600, 2), "Conflict (R11) - park it, then reopen the PARKED row"),
    ("T9",  f"ACH {beck.invoice_number} BECKMAN COULTER INC",
     beck.outstanding_amount, "Ready for Oracle (R9a) - remap to a sibling on reopen"),
    ("T10", f"REMIT {esko.invoice_number} ESKO GRAPHICS BV",
     esko.outstanding_amount, "Ready for Oracle (R9a) - the no-edit case"),
]

df = pd.DataFrame([{
    "Value date":     D,
    "Narrative":      f"{rid} {narr}",
    "Credit amount":  amt,
    "Account number": "914031244",
    "Currency":       "USD",
    "Bank name":      "HSBC Bank USA NA",
    "Bank reference": f"REOPEN-{rid}",
} for rid, narr, amt, _ in rows])

key = pd.DataFrame([{
    "Row": rid, "Bank reference": f"REOPEN-{rid}", "Amount": amt,
    "Expected initial outcome": exp,
} for rid, narr, amt, exp in rows])

out = "storage/bank-statements/Reopen_Test_HSBC_914031244.xlsx"
with pd.ExcelWriter(out, engine="openpyxl") as w:
    df.to_excel(w, sheet_name="Sheet1", index=False)
    key.to_excel(w, sheet_name="EXPECTED (not read by app)", index=False)

print(f"wrote {out}\n")
for rid, narr, amt, exp in rows:
    print(f"  {rid:<4} {amt:>13,.2f}  {rid} {narr[:44]:<44} -> {exp}")
