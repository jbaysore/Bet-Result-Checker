"""Redact secrets from strings that may reach logs or error payloads."""

from __future__ import annotations

import re

from config import ODDS_API_KEY

_API_KEY_QUERY_RE = re.compile(r"(?i)(apiKey=)[^&\s]+")


def redact_secret_text(text: str) -> str:
    """Scrub Odds API credentials from an arbitrary string.

    Redacts ``apiKey=...`` query params (as rendered by requests HTTPError URLs)
    and, when set, the literal ``ODDS_API_KEY`` value anywhere it appears.
    """
    rendered = _API_KEY_QUERY_RE.sub(r"\1[REDACTED]", str(text))
    key = ODDS_API_KEY
    if key:
        rendered = rendered.replace(key, "[REDACTED]")
    return rendered


def redact_request_error(error: BaseException) -> str:
    """Prevent credentials embedded in requests' rendered URLs from reaching logs."""
    return redact_secret_text(str(error))
