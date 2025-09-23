from __future__ import annotations

import re
import unicodedata

_discogs_suffix_re = re.compile(r"\s*\(\d+\)\s*$")

def fold_to_ascii(s: str) -> str:
    try:
        return unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode('ascii')
    except Exception:
        return s or ''


def strip_discogs_suffix(s: str) -> str:
    return _discogs_suffix_re.sub('', s or '')


def sanitize_for_local(s: str) -> str:
    return fold_to_ascii(strip_discogs_suffix(s)).strip()

def normalize_title(s: str) -> str:
    """Robust, shared normalizer for track/album titles.

    - Lowercase
    - Replace separators (underscore, hyphen, colon, en/em dash, dot, comma, slash) with spaces
    - Replace '&' with 'and'
    - Drop bracketed parts: (), [], {}
    - Drop leading track numbers or side markers (e.g., '01 -', '1.', 'A1 ')
    - Remove remaining non-alphanumeric chars (except space)
    - Collapse spaces
    """
    try:
        s = fold_to_ascii(s or "").lower().strip()
        s = re.sub(r"[_\-:\u2013\u2014\.,/]+", " ", s)
        s = s.replace("&", " and ")
        s = re.sub(r"\s*[\(\[\{].*?[\)\]\}]\s*", " ", s)
        s = re.sub(r"^(?:[a-d]\d+|\d+)(?:\s*|[_.\-]+)+", "", s)
        s = re.sub(r"[^a-z0-9\s]", "", s)
        s = re.sub(r"\s+", " ", s)
        return s.strip()
    except Exception:
        return (s or "").lower().strip()

