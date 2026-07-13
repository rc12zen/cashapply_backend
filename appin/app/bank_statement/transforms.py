"""
app.bank_statement.transforms
==============================
Registry of named value-cleaning transforms applied after field resolution.
"""
from __future__ import annotations

import re


def apply_transforms(record: dict, transforms_cfg: dict) -> dict:
    result = dict(record)
    for field_name, pipeline in transforms_cfg.items():
        value = result.get(field_name)
        if value is None:
            continue
        s = str(value)
        for step in pipeline:
            s = _apply_one(s, step)
        result[field_name] = s
    return result


def _apply_one(value: str, step: dict) -> str:
    t = step["type"]
    if t == "strip":
        return value.strip()
    if t == "replace":
        return re.sub(step["pattern"], step.get("with", ""), value)
    if t == "collapse_whitespace":
        return re.sub(r"\s+", " ", value)
    if t == "strip_prefix":
        return value.lstrip(step.get("chars", ""))
    if t == "take_first":
        delim = step.get("delimiter", ",")
        return value.split(delim)[0]
    if t == "uppercase":
        return value.upper()
    if t == "truncate":
        return value[: step.get("length", 255)]
    raise ValueError(f"Unknown transform type: '{t}'")
