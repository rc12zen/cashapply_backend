"""
app.bank_statement.extractor
=============================
ExtractorFactory dispatches to the right format-specific extractor based on
the `engine` field in a config's `source` section.
"""
from __future__ import annotations

import pandas as pd

from .excel import ExcelExtractor
from .csv_extractor import CsvExtractor
from .pdf import PdfExtractor


class ExtractorFactory:
    @staticmethod
    def extract(filepath: str, source_cfg: dict,
                text_columns: list[str] | None = None) -> pd.DataFrame:
        """Build the DataFrame for a config's `source`.

        `text_columns` names columns that must be read as TEXT. Identifier columns
        (account number, bank reference) have to be: pandas infers a column of
        digit strings as int64, so an account stored as "000274178" arrives as
        274178 — leading zeros gone — and a column with one blank row becomes
        float64, which for a 16+ digit account silently loses precision as well.
        Amounts and dates are deliberately NOT included; they need pandas'
        numeric/datetime handling.
        """
        engine = source_cfg.get("engine", "excel")
        if engine == "excel":
            return ExcelExtractor.extract(filepath, source_cfg, text_columns)
        if engine == "csv":
            return CsvExtractor.extract(filepath, source_cfg, text_columns)
        if engine == "pdfplumber":
            return PdfExtractor.extract(filepath, source_cfg)
        raise ValueError(f"Unknown extraction engine: '{engine}'")


__all__ = ["ExtractorFactory"]
