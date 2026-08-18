"""
app.bank_statement.transforms
==============================
Registry of named value-cleaning transforms applied after field resolution.
"""
from __future__ import annotations

import re

from ..common.regex_safety import MAX_SUBJECT_LENGTH, safe_compile


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
        # Validated + length-capped before hitting the backtracking engine --
        # see common/regex_safety.py (ReDoS / CWE-1333). No UI emits a
        # transform today (the wizard always sends transforms: {}), but the
        # API accepts a recipe body directly.
        compiled = safe_compile(step["pattern"])
        if compiled is None:
            return value
        return compiled.sub(step.get("with", ""), value[:MAX_SUBJECT_LENGTH])
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
