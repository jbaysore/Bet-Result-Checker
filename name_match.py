"""
Shared exact-match name normalization (trap #7: one normalizer). Used by
espn_fights (fighter names) AND mlb_statsapi/prop_resolver (player names). The
RATIFIED standard: strip accents (Acuña→acuna), lowercase, fold punctuation to
spaces, drop generational suffixes (Jr/Sr/II…), collapse whitespace. Matching is
EXACT on the normalized string — no fuzzy/substring scoring, because a wrong
player/fighter is a wrong settlement.
"""

import re
import unicodedata

_SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.lower()
    spaced = re.sub(r"[^a-z0-9]+", " ", lowered)
    tokens = [t for t in spaced.split() if t and t not in _SUFFIX_TOKENS]
    return " ".join(tokens)


def names_match(a: str, b: str) -> bool:
    """Exact match on normalized names (both non-empty)."""
    na, nb = normalize_name(a), normalize_name(b)
    return bool(na) and na == nb
