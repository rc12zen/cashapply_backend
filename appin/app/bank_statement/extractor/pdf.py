"""
app.bank_statement.extractor.pdf
==================================
PdfExtractor using pdfplumber for text-based (digital) PDFs.
Scanned PDFs are rejected early with a descriptive error.
"""
from __future__ import annotations

import pandas as pd


class ScannedPdfError(Exception):
    pass


def _resolve_pages(all_pages: list, pages_cfg) -> list:
    if pages_cfg == "all":
        return all_pages
    if isinstance(pages_cfg, int):
        return [all_pages[pages_cfg]] if pages_cfg < len(all_pages) else []
    if isinstance(pages_cfg, list):
        return [all_pages[i] for i in pages_cfg if i < len(all_pages)]
    if isinstance(pages_cfg, str) and "-" in pages_cfg:
        start, end = pages_cfg.split("-", 1)
        return all_pages[int(start) - 1: int(end)]
    return all_pages


class PdfExtractor:
    @staticmethod
    def extract(filepath: str, source_cfg: dict) -> pd.DataFrame:
        try:
            import pdfplumber
        except ImportError:
            raise ImportError(
                "pdfplumber is required for PDF extraction. "
                "Install with: pip install pdfplumber"
            )

        pages_cfg = source_cfg.get("pages", "all")
        table_index = source_cfg.get("table_index", 0)
        header_on_each_page = source_cfg.get("header_on_each_page", False)
        skip_header_rows = source_cfg.get("skip_header_rows", 0)
        skip_footer_rows = source_cfg.get("skip_footer_rows", 0)

        all_rows: list = []
        header: list | None = None

        with pdfplumber.open(filepath) as pdf:
            if pdf.pages:
                first_text = (pdf.pages[0].extract_text() or "").strip()
                if len(first_text) < 50:
                    raise ScannedPdfError(
                        "This PDF appears to be a scanned image. "
                        "Please upload the original digital statement from your bank portal."
                    )

            pages = _resolve_pages(pdf.pages, pages_cfg)
            for i, page in enumerate(pages):
                tables = page.extract_tables()
                if table_index >= len(tables):
                    continue
                table = tables[table_index]

                if skip_header_rows:
                    table = table[skip_header_rows:]
                if skip_footer_rows:
                    table = table[:-skip_footer_rows]

                if i == 0:
                    if not table:
                        continue
                    header = table[0]
                    rows = table[1:]
                elif header_on_each_page:
                    rows = table[1:]
                else:
                    rows = table

                all_rows.extend(rows)

        if not header:
            raise ValueError("Could not extract table header from PDF")

        clean_header = [
            str(c).strip() if c else f"Col_{i}"
            for i, c in enumerate(header)
        ]
        df = pd.DataFrame(all_rows, columns=clean_header)
        return df
