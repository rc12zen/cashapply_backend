"""
app.bank_statement.extractor.csv_extractor
==========================================
CsvExtractor with auto-detected encoding and delimiter, and support for a
two-row (header + sub-header) layout.

SUB-HEADER SUPPORT -- AND A DELIBERATE DUPLICATION
--------------------------------------------------
`header.merge_rows` in a recipe means "the column names span two rows": a main
header row, plus a sub-header row whose values (e.g. "Cr" / "Dr" under a shared
"Balance" parent) decide the final flat column name. The Config Builder wizard
writes this block; see ConfigBuilderWizard.tsx's buildSource/buildConfigDraft.

The naming logic below is a DELIBERATE DUPLICATE of
excel.py's _read_merged_header (Step 2 / Step 4 there). It was copied rather
than shared so that adding CSV support could not change Excel behaviour.

  *** If you change how flat column names are derived here, change excel.py
  *** too. A recipe must resolve to the SAME column names regardless of whether
  *** the bank sent .csv or .xlsx -- every field mapping downstream is keyed by
  *** those names, so a divergence silently breaks mappings for one format only.

Kept in step with excel.py as of this change:
  - a sub-header value matching a rule (case-insensitive) renames the parent
  - otherwise the name is `top or sub or _col_<i>`
  - data rows are read positionally, then trimmed to the header width

ONE INTENTIONAL DIFFERENCE FROM excel.py
----------------------------------------
excel.py iterates over the MAIN header row's cells only, which is safe there
because both worksheet engines pad every row to the sheet's column count. CSV
rows are genuinely ragged -- `Date,Narrative,Balance` over `,,Cr,Dr` is a real
shape -- so a sub-header row can be WIDER than the main one. Iterating to the
longer of the two (see _flat_columns) keeps that last column instead of
dropping it. For equal-length rows, which is the Excel case, the result is
identical.
"""
from __future__ import annotations

import csv

import pandas as pd

# Bytes sampled for delimiter sniffing. Enough to cover a header block plus a
# few data rows without reading a large statement into memory.
_SNIFF_BYTES = 4096

# Tried in order when the recipe says encoding="auto". latin-1 never fails to
# decode, so it doubles as the last-resort fallback.
#
# utf-8-sig, NOT utf-8: it is identical to utf-8 on a file with no BOM, and
# strips the byte-order mark (EF BB BF) when there is one. Plain utf-8 keeps the
# BOM as a ﻿ character glued to the FIRST cell of the file -- which is
# invisible in the wizard grid but makes the first column's name a different
# string from what it appears to be.
#
# That split the two read paths. pandas (read_csv, below) strips a BOM on its
# own, so extraction saw "Account Number"; the Config Builder's raw-preview
# endpoint reads with a plain csv.reader through resolve_encodings() and saw
# "﻿Account Number". The wizard offered the BOM'd name, the user picked it,
# and every locator or field mapping pointed at the first column of a BOM'd CSV
# resolved to column_found=False -- surfacing as "No account number found with
# this rule", with a column plainly visible in the preview holding the value.
# A South African FNB export (SA 513) is one such file.
#
# Fixed here rather than by stripping ﻿ at the preview, so the two paths
# agree at the point they decode instead of one of them cleaning up after the
# other. See sniff_dialect's note on why the preview shares this module at all.
_AUTO_ENCODINGS = ("utf-8-sig", "latin-1")

# Characters that cannot serve as a delimiter no matter what the sniffer says:
# the line terminators (csv.reader raises "bad delimiter value") and the quote
# character (it would collide with quoting). See sniff_dialect.
_ILLEGAL_DELIMITERS = frozenset('\r\n"')


def sniff_dialect(filepath: str, encoding: str) -> str:
    """
    The delimiter this file appears to use, or "," if it can't be determined.

    Public because the Config Builder's raw-preview endpoint calls it too (see
    bff/config_builder_routes.py). That preview MUST split the file the same way
    extraction will, otherwise a semicolon-delimited statement renders as one
    unusable column in the wizard grid while extracting perfectly -- and the
    user cannot pick a header or sub-header row out of it. Sharing this one
    function is the fix for that class of bug, so resist duplicating it.
    """
    with open(filepath, encoding=encoding, newline="") as f:
        sample = f.read(_SNIFF_BYTES)
    try:
        sniffed = csv.Sniffer().sniff(sample).delimiter
    except csv.Error:
        # Sniffer raises on a single-column file, or one whose punctuation it
        # can't read. Comma is the safe default: for a genuinely single-column
        # file any delimiter yields the same one column.
        return ","
    # Sniffer does not always return a USABLE delimiter. On a file it misreads
    # -- a ragged header block over CRLF line endings is enough -- it happily
    # returns "\r", which is a single character and passes a naive length check,
    # but csv.reader rejects it with "bad delimiter value" and pandas fails
    # differently again. Empty and multi-character results are possible too.
    #
    # So the result is validated against what a delimiter can actually BE: one
    # character, not a line terminator, not the quote character. Anything else
    # degrades to comma-splitting, which at worst shows the user one wide column
    # in the wizard, rather than failing the ingest with an error that names
    # neither the file nor the cause.
    if not isinstance(sniffed, str) or len(sniffed) != 1 or sniffed in _ILLEGAL_DELIMITERS:
        return ","
    return sniffed


def resolve_encodings(encoding: str) -> tuple[str, ...]:
    """Encodings to try, honouring an explicit recipe setting over auto."""
    return _AUTO_ENCODINGS if encoding == "auto" else (encoding,)


def _raw_rows(filepath: str, encoding: str, delimiter: str, upto: int) -> list[list[str]]:
    """
    Rows 0..upto inclusive, as stripped strings, read WITHOUT pandas.

    Deliberately not pandas: read_csv with a multi-row header forward-fills
    blank cells from the column to their left, which is exactly wrong here. An
    empty parent cell above "Dr" means "this column has no parent", not "it
    belongs to whatever was on the left" -- and forward-filling would invent a
    name that no field mapping refers to. Same reasoning as excel.py's
    _raw_row.
    """
    rows: list[list[str]] = []
    with open(filepath, encoding=encoding, newline="") as f:
        for i, row in enumerate(csv.reader(f, delimiter=delimiter)):
            if i > upto:
                break
            rows.append([str(v).strip() for v in row])
    return rows


def _flat_columns(top_vals: list[str], sub_vals: list[str],
                  merge_rules: dict[str, str]) -> list[str]:
    """
    Collapse a header row and its sub-header row into one flat name per column.

    Mirrors excel.py Step 2 -- see this module's docstring, including why the
    loop bound differs.
    """
    width = max(len(top_vals), len(sub_vals))

    # Drop TRAILING columns that carry no header text in either row. A trailing
    # comma ("a,b,c,,") makes csv.reader report extra empty fields, which would
    # otherwise become junk `_col_N` columns -- and Excel does not produce them,
    # because pandas discards a wholly empty trailing column when reading the
    # sheet. Trimming here keeps the two engines' column lists identical, which
    # is the property every field mapping depends on.
    #
    # Only trailing ones. An INTERIOR column with no header still gets its
    # `_col_N` placeholder, because removing it would shift every column to its
    # right and silently re-point the mappings.
    while width > 0:
        last = width - 1
        top = top_vals[last] if last < len(top_vals) else ""
        sub = sub_vals[last] if last < len(sub_vals) else ""
        if top or sub:
            break
        width -= 1

    flat: list[str] = []
    for i in range(width):
        top = top_vals[i] if i < len(top_vals) else ""
        sub = sub_vals[i] if i < len(sub_vals) else ""
        renamed = merge_rules.get(sub.lower())
        flat.append(renamed if renamed else (top or sub or f"_col_{i}"))
    return flat


def _read_merged_header(filepath: str, header_cfg: dict,
                        encoding: str, delimiter: str) -> pd.DataFrame:
    """Read a CSV whose column names span a header row plus a sub-header row."""
    main_row = header_cfg.get("row", 0)
    merge_rows_cfg = header_cfg.get("merge_rows") or []
    sub_row_nums = [mr["row"] for mr in merge_rows_cfg if "row" in mr]

    # Flatten every rule from every merge row into one lookup. The wizard only
    # ever writes a single merge row today, but the recipe schema stores a list,
    # so this reads all of them rather than assuming index 0 exists.
    merge_rules: dict[str, str] = {}
    for mr_cfg in merge_rows_cfg:
        for rule in mr_cfg.get("rules") or []:
            sub_value = rule.get("sub_value")
            rename_to = rule.get("rename_parent_to")
            if sub_value and rename_to:
                merge_rules[str(sub_value).strip().lower()] = rename_to

    last_header_row = max([main_row] + sub_row_nums)
    raw = _raw_rows(filepath, encoding, delimiter, last_header_row)

    def _row(n: int) -> list[str]:
        return raw[n] if n < len(raw) else []

    # Only the FIRST sub-header row participates in naming, matching excel.py.
    # A second one would need a defined precedence between them, which no
    # recipe expresses today.
    flat_cols = _flat_columns(
        _row(main_row),
        _row(sub_row_nums[0]) if sub_row_nums else [],
        merge_rules,
    )

    # Data only: skip every header row. header=None keeps the columns positional
    # so the names above are applied by position, not matched by content.
    df = pd.read_csv(
        filepath,
        header=None,
        skiprows=last_header_row + 1,
        sep=delimiter,
        encoding=encoding,
        dtype=str,
    )

    # A header block can be narrower or wider than the data rows (trailing
    # commas, or a ragged sub-header). Trim to the overlap so naming can't
    # raise a length mismatch, then drop any unnamed surplus data columns.
    n = min(len(flat_cols), len(df.columns))
    df = df.iloc[:, :n]
    df.columns = flat_cols[:n]
    return df


class CsvExtractor:
    @staticmethod
    def extract(filepath: str, source_cfg: dict,
                text_columns: list[str] | None = None) -> pd.DataFrame:
        # text_columns is accepted for a uniform extractor signature but is a
        # no-op: this reader already uses dtype=str for EVERY column, so CSV
        # sources never lost leading zeros the way Excel ones did.
        del text_columns
        encoding = source_cfg.get("encoding", "auto")
        delimiter = source_cfg.get("delimiter", "auto")
        header_cfg = source_cfg.get("header") or {"row": 0}

        for enc in resolve_encodings(encoding):
            try:
                delim = sniff_dialect(filepath, enc) if delimiter == "auto" else delimiter

                if header_cfg.get("merge_rows"):
                    df = _read_merged_header(filepath, header_cfg, enc, delim)
                else:
                    df = pd.read_csv(
                        filepath, header=header_cfg.get("row", 0), sep=delim,
                        encoding=enc, dtype=str,
                    )

                df.columns = [str(c).strip() for c in df.columns]
                return df
            except UnicodeDecodeError:
                # Wrong encoding for this file -- try the next candidate. Any
                # other exception is a real parse/config problem and propagates.
                continue

        raise ValueError(
            f"Could not decode {filepath} with encodings {list(resolve_encodings(encoding))}"
        )
