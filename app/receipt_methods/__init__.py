"""
app.receipt_methods
======================
File-based ingestion of Oracle's AR Receipt Methods extract
(xxzen_ar_receipt_methods_extract.txt) — same "drop a file in a watched
folder" pattern as app.aging and app.gl_rates (see watcher.py), pulled
down daily by oracle_file_pull/puller.py alongside the aging report and
GL daily rates.

Deliberately modeled after app.gl_rates, with one key difference: GL
rates accumulate history in a DB table across files, whereas receipt
methods have no history to accumulate — each new extract is simply the
current, complete picture, so this REPLACES
oracle/configs/receipt_method_map.json wholesale on every run (see
parser.py) rather than upserting into anything.

oracle/receipt_method_resolver.py is the only consumer — it reads
receipt_method_map.json via app.common.json_cache's mtime-based cache, so
a freshly-regenerated file is picked up by every process (API + worker)
automatically, with no restart or manual reload call needed.
"""
from .parser import build_receipt_method_map, write_receipt_method_map, get_output_path, SEED_PATH
from .watcher import start_receipt_methods_watcher

__all__ = [
    "build_receipt_method_map",
    "write_receipt_method_map",
    "get_output_path",
    "SEED_PATH",
    "start_receipt_methods_watcher",
]