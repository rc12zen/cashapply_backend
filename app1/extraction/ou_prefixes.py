"""
app.extraction.ou_prefixes
============================
Single source of truth for OU → invoice-prefix knowledge, shared by
Layer 2A (regex) and Layer 2B (AI grounding).

Every Zensar invoice number starts with the 3-digit OU number that issued it.
e.g. OU 111 (PUNE India) -> all invoices start with '111'.

Pulled from Component 3 (Text Extraction & Matching Service) so both layers
of the new pipeline use IDENTICAL OU knowledge instead of two copies that
could drift apart.
"""
from __future__ import annotations

OU_INVOICE_PREFIXES: set[str] = {
    "111",  # India PUNE
    "162",  # India HYD-SEZ
    "163",  # India HYD SEZ II
    "166",  # India HYD SEZ III
    "167",  # India CESSNA BLR
    "168",  # India M3BI PUNE
    "169",  # India M3BI HYD
    "171",  # India PA BLR
    "175",  # India RMZ SEZ BLR / Cisco Bangalore
    "193",  # India KOL
    "194",  # India
    "151",  # Inter Co.
    "152",  # Inter Co.
    "153",  # Inter Co.
    "211",  # US GAAP
    "212",  # BVLS / BRIDGEVIEW
    "217",  # Keystone Mexico
    "224",  # ZENSAR INC PRODUCT SALE
    "273",  # Colombia
    "281",  # Canada
    "291",  # M3BI US
    "312",  # UK
    "323",  # Switzerland
    "324",  # Austria
    "326",  # Poland UK Branch
    "327",  # Germany
    "328",  # Netherlands
    "329",  # Ireland
    "411",  # Singapore
    "421",  # Australia
    "511",  # South Africa
    "512",  # Kenya
    "513",  # SA Holdings/Operating
}

# Human-readable OU descriptions — used to build the AI prompt grounding text
OU_DESCRIPTIONS: dict[str, str] = {
    "111": "India PUNE", "162": "India HYD-SEZ", "163": "India HYD SEZ II",
    "166": "India HYD SEZ III", "167": "India CESSNA BLR", "168": "India M3BI PUNE",
    "169": "India M3BI HYD", "171": "India PA BLR", "175": "India RMZ SEZ BLR / Cisco Bangalore",
    "193": "India KOL", "194": "India", "151": "Inter Co.", "152": "Inter Co.",
    "153": "Inter Co.", "211": "US GAAP", "212": "BVLS / BRIDGEVIEW",
    "217": "Keystone Mexico", "224": "ZENSAR INC PRODUCT SALE", "273": "Colombia",
    "281": "Canada", "291": "M3BI US", "312": "UK", "323": "Switzerland",
    "324": "Austria", "326": "Poland UK Branch", "327": "Germany",
    "328": "Netherlands", "329": "Ireland", "411": "Singapore", "421": "Australia",
    "511": "South Africa", "512": "Kenya", "513": "SA Holdings/Operating",
}


def ou_prefixes_for(ou_number: str | None) -> set[str]:
    """
    Scope the prefix set to the row's own OU when known, else fall back to
    the full table (used by Layer 2A's OU-prefix regex pass).
    """
    if ou_number and ou_number in OU_INVOICE_PREFIXES:
        return {ou_number}
    return OU_INVOICE_PREFIXES


def describe_ou(ou_number: str | None) -> str:
    if not ou_number:
        return "unknown OU"
    return f"{ou_number} ({OU_DESCRIPTIONS.get(ou_number, 'unknown region')})"