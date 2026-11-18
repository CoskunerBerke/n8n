from __future__ import annotations

import re
from typing import Optional

_NEGATIVE_RE = re.compile(
    r"\b(gta\s*5|gta\s*v(?!i)|grand\s+theft\s+auto\s+v(?!i)|gta\s+online|fivem)\b",
    re.IGNORECASE,
)

_POSITIVE_RE = re.compile(
    r"\b(gta\s*6|gta\s*vi|grand\s+theft\s+auto\s*6|grand\s+theft\s+auto\s+vi)\b",
    re.IGNORECASE,
)


def _matches_positive(text: str, extra_keywords: Optional[list[str]] = None) -> bool:
    if not text:
        return False
    if _POSITIVE_RE.search(text):
        return True
    low = text.lower()
    for kw in extra_keywords or []:
        clean = kw.strip().lower()
        if not clean or clean == "grand theft auto":
            continue
        if clean in low:
            return True
    return False


def is_gta6_match(
    category: str,
    title: str,
    extra_keywords: Optional[list[str]] = None,
    allow_unknown_category: bool = True,
) -> bool:
    """Strictly verify whether a stream or clip belongs to Grand Theft Auto VI (GTA 6)."""
    cat = (category or "").strip()
    ttl = (title or "").strip()

    # Reject explicit GTA 5 / GTA Online / FiveM mentions everywhere
    if _NEGATIVE_RE.search(cat) or _NEGATIVE_RE.search(ttl):
        return False

    if _matches_positive(cat, extra_keywords):
        return True

    if _matches_positive(ttl, extra_keywords):
        return True

    # If the platform provided an explicit non-GTA6 category (e.g. Just Chatting, CS2), reject
    if cat:
        return False

    return allow_unknown_category
