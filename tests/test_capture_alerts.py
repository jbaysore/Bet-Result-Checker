"""Unit tests for capture_alerts — cooldown, classification, delivery hooks."""

from __future__ import annotations

import capture_alerts as alerts


def setup_function():
    alerts.reset_alert_state_for_tests()


def test_is_invalid_key_error_matches_helper_and_http_401():
    assert alerts.is_invalid_key_error("invalid Odds API key")
    assert alerts.is_invalid_key_error("401 Client Error: Unauthorized for url: https://api.the-odds-api.com/...")
    assert not alerts.is_invalid_key_error("EXACT SELECTION NOT FOUND")
    assert not alerts.is_invalid_key_error("")


def test_note_cycle_health_alerts_on_invalid_key(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "ALERT_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setattr(alerts, "ALERT_NTFY_TOPIC", "")
    monkeypatch.setattr(
        alerts, "_post_json",
        lambda url, payload, headers=None: sent.append(payload),
    )
    reports = alerts.note_cycle_health(
        sampled=3, priced=0, invalid_key_errors=3, fallbacks_this_cycle=2,
    )
    assert len(reports) == 1
    assert reports[0]["sent"] is True
    assert reports[0]["code"] == alerts.CODE_INVALID_KEY
    assert sent and sent[0]["code"] == alerts.CODE_INVALID_KEY


def test_note_cycle_health_respects_cooldown(monkeypatch):
    monkeypatch.setattr(alerts, "ALERT_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setattr(alerts, "ALERT_NTFY_TOPIC", "")
    monkeypatch.setattr(alerts, "_post_json", lambda *args, **kwargs: None)
    first = alerts.note_cycle_health(sampled=1, priced=0, invalid_key_errors=1)
    second = alerts.note_cycle_health(sampled=1, priced=0, invalid_key_errors=1)
    assert first[0]["sent"] is True
    assert second[0]["sent"] is False
    assert second[0]["skipped"] == "cooldown"


def test_priced_sample_resets_streaks(monkeypatch):
    monkeypatch.setattr(alerts, "ALERT_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setattr(alerts, "ALERT_NTFY_TOPIC", "")
    calls = []
    monkeypatch.setattr(
        alerts, "_post_json",
        lambda url, payload, headers=None: calls.append(payload),
    )
    alerts.note_cycle_health(sampled=2, priced=0, invalid_key_errors=0)
    alerts.note_cycle_health(sampled=2, priced=0, invalid_key_errors=0)
    # Third consecutive zero-price cycle trips the alert.
    third = alerts.note_cycle_health(sampled=2, priced=0, invalid_key_errors=0)
    assert third and third[0]["code"] == alerts.CODE_ZERO_SAMPLES
    # A priced sample clears the streak; two more zeros should not yet alert.
    alerts.note_cycle_health(sampled=1, priced=1, invalid_key_errors=0)
    again = alerts.note_cycle_health(sampled=2, priced=0, invalid_key_errors=0)
    assert again == []
    again2 = alerts.note_cycle_health(sampled=2, priced=0, invalid_key_errors=0)
    assert again2 == []


def test_skips_when_no_channel_configured(monkeypatch):
    monkeypatch.setattr(alerts, "ALERT_WEBHOOK_URL", "")
    monkeypatch.setattr(alerts, "ALERT_NTFY_TOPIC", "")
    reports = alerts.note_cycle_health(sampled=1, priced=0, invalid_key_errors=1)
    assert reports[0]["sent"] is False
    assert reports[0]["skipped"] == "no_alert_channel_configured"
