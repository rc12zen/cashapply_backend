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
    def extract(filepath: str, source_cfg: dict) -> pd.DataFrame:
        engine = source_cfg.get("engine", "excel")
        if engine == "excel":
            return ExcelExtractor.extract(filepath, source_cfg)
        if engine == "csv":
            return CsvExtractor.extract(filepath, source_cfg)
        if engine == "pdfplumber":
            return PdfExtractor.extract(filepath, source_cfg)
        raise ValueError(f"Unknown extraction engine: '{engine}'")


__all__ = ["ExtractorFactory"]
