"""
app.bank_statement.extractor.csv_extractor
==========================================
CsvExtractor with auto-detected encoding and delimiter.
"""
from __future__ import annotations

import csv

import pandas as pd


class CsvExtractor:
    @staticmethod
    def extract(filepath: str, source_cfg: dict) -> pd.DataFrame:
        encoding = source_cfg.get("encoding", "auto")
        delimiter = source_cfg.get("delimiter", "auto")
        header_row = source_cfg.get("header", {}).get("row", 0)

        encodings = ["utf-8", "latin-1"] if encoding == "auto" else [encoding]

        for enc in encodings:
            try:
                sniffed = delimiter
                if delimiter == "auto":
                    with open(filepath, encoding=enc, newline="") as f:
                        sample = f.read(4096)
                    try:
                        sniffed = csv.Sniffer().sniff(sample).delimiter
                    except csv.Error:
                        sniffed = ","

                df = pd.read_csv(
                    filepath, header=header_row, sep=sniffed,
                    encoding=enc, dtype=str
                )
                df.columns = [str(c).strip() for c in df.columns]
                return df
            except UnicodeDecodeError:
                continue

        raise ValueError(f"Could not decode {filepath} with encodings {encodings}")
