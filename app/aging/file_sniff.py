"""
app.aging.file_sniff
======================
Detects a mismatch between a file's claimed extension and its actual
bytes, for aging report files. Root cause this exists for: a file named
"...report.xlsx" whose content is actually the legacy Excel 97-2003
binary format (.xls / OLE2 Compound Document) — pandas can often still
parse it silently (IF the xlrd engine happens to be installed), so it
sails through upload, preview, and even a full analysis run without any
visible problem. The mismatch only surfaces much later, when someone
downloads the raw file and a real Excel/OneDrive client refuses to open
it with: "This workbook couldn't be opened because the file format may
not be matching with the file extension." — because Excel checks actual
content against the extension, not just whether some parser can read it.

Two ways this gets used:
  1. At ingestion (uploader.py / watcher.py) — REJECT / skip a mismatched
     file up front with a clear, actionable message, instead of silently
     accepting something that will only break later, elsewhere, for a
     confusing reason.
  2. At download (config_routes.py's aging_download) — for a file that's
     ALREADY sitting in storage with this mismatch (from before check #1
     existed, or from a watch-folder drop that predates it), serve it
     with the extension that actually matches its bytes so it opens
     correctly right now, without waiting on a re-upload.
"""
from __future__ import annotations

# Magic-byte signatures. Legacy .xls / .doc / .ppt (etc.) files all share
# the same OLE2/Compound-File-Binary signature — this can't distinguish
# "legacy .xls" from some other OLE2 document, but for an aging report
# upload the only realistic candidates are .xls or a genuine .xlsx, so
# that ambiguity doesn't matter here.
_OLE2_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # legacy .xls (Excel 97-2003)
_ZIP_SIG  = b"PK\x03\x04"                          # real .xlsx/.xlsm (OOXML is a zip)

_EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}


def sniff_extension(filename: str) -> str:
    """'.xlsx', '.xls', '.csv', etc. — whatever's after the last dot, lowercased."""
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx != -1 else ""


def sniff_actual_kind(data: bytes) -> str | None:
    """
    Best-effort guess at what a file's bytes actually are, independent of
    its filename. Returns 'xlsx' (real OOXML zip), 'xls' (legacy OLE2
    binary), or None (couldn't tell from the header alone — e.g. a
    genuine .csv, which has no reliable magic-byte signature to check).
    """
    head = data[:8]
    if head.startswith(_ZIP_SIG):
        return "xlsx"
    if head.startswith(_OLE2_SIG):
        return "xls"
    return None


def check_extension_mismatch(filename: str, data: bytes) -> str | None:
    """
    Returns a short, user-actionable warning string if `filename`'s
    extension doesn't match what `data` actually is, or None if there's
    no mismatch (or nothing checkable — e.g. .csv, or empty/unrecognized
    bytes, since those aren't false claims of being an Excel binary format).
    Only checks the Excel-vs-Excel case (.xlsx claiming to be .xls or vice
    versa) — that's the one that silently slips through pandas today and
    only breaks later in a real Excel client; a totally unrelated
    extension (e.g. ".pdf") would already fail loudly at parse time, so
    doesn't need this.
    """
    ext = sniff_extension(filename)
    if ext not in _EXCEL_EXTENSIONS:
        return None

    actual = sniff_actual_kind(data)
    if actual is None:
        return None

    claims_xlsx = ext in (".xlsx", ".xlsm")
    if claims_xlsx and actual == "xls":
        return (
            f"'{filename}' is named with a {ext} extension, but its contents are actually "
            "the older Excel 97-2003 (.xls) binary format — Excel will refuse to open it "
            "with a \"file format doesn't match the file extension\" error. Open it in Excel "
            "and use File \u2192 Save As \u2192 Excel Workbook (.xlsx) to convert it, or "
            "re-upload it with a .xls extension instead."
        )
    if ext == ".xls" and actual == "xlsx":
        return (
            f"'{filename}' has a .xls extension, but its contents are actually a modern "
            ".xlsx file — rename it to .xlsx (or re-upload it that way) so Excel opens it "
            "without a format-mismatch warning."
        )
    return None


def correct_extension_for(filename: str, data: bytes) -> str:
    """
    The filename to actually serve on download — corrected to match the
    real content if there's a detectable mismatch, otherwise unchanged.
    Used by config_routes.py's aging_download() so a file that slipped
    through before the ingestion-time check existed is still downloadable
    and openable today, without needing a fresh re-upload first.
    """
    ext = sniff_extension(filename)
    if ext not in _EXCEL_EXTENSIONS:
        return filename

    actual = sniff_actual_kind(data)
    if actual is None:
        return filename

    correct_ext = ".xlsx" if actual == "xlsx" else ".xls"
    if ext == correct_ext or (ext == ".xlsm" and actual == "xlsx"):
        return filename
    return filename[: filename.rfind(".")] + correct_ext