"""Unit tests for redact.py — Odds API key scrubbing for logs/error payloads."""

from redact import redact_request_error, redact_secret_text


def test_redact_request_error_scrubs_apikey_query_param():
    rendered = redact_request_error(RuntimeError(
        "404 for https://api.example.test/odds?apiKey=top-secret&markets=h2h",
    ))
    assert "top-secret" not in rendered
    assert "apiKey=[REDACTED]" in rendered


def test_redact_secret_text_scrubs_literal_odds_api_key(monkeypatch):
    monkeypatch.setattr("redact.ODDS_API_KEY", "literal-secret-key-xyz")
    rendered = redact_secret_text(
        "upstream failed: bearer literal-secret-key-xyz was rejected"
    )
    assert "literal-secret-key-xyz" not in rendered
    assert "[REDACTED]" in rendered


def test_redact_secret_text_handles_empty_key(monkeypatch):
    monkeypatch.setattr("redact.ODDS_API_KEY", None)
    text = "404 for https://api.example.test/odds?apiKey=still-secret&markets=h2h"
    rendered = redact_secret_text(text)
    assert "still-secret" not in rendered
    assert "apiKey=[REDACTED]" in rendered


def test_closing_odds_reexport_still_redacts():
    """closing_odds keeps _redact_request_error as an alias of the shared helper."""
    import closing_odds as closing_odds_module

    rendered = closing_odds_module._redact_request_error(RuntimeError(
        "404 for https://api.example.test/odds?apiKey=top-secret&markets=h2h",
    ))
    assert "top-secret" not in rendered
    assert "apiKey=[REDACTED]" in rendered
