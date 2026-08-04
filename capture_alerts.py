"""Outbound alerts for the closing-capture worker.

Railway keeps the process alive even when The Odds API key is revoked, so
stdout alone is not enough. Configure one or both:

  CAPTURE_ALERT_WEBHOOK_URL  POST JSON {text, title, severity, code, ...}
                             Discord-compatible: also sends {content: text}
  CAPTURE_ALERT_NTFY_TOPIC   POST https://ntfy.sh/<topic> with the text body
                             (override host with CAPTURE_ALERT_NTFY_URL)

Cooldown defaults to 6 hours per alert code so a bad key does not spam every
poll cycle. A successful sample clears the invalid-key / zero-sample state.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

ALERT_WEBHOOK_URL = (os.getenv("CAPTURE_ALERT_WEBHOOK_URL") or "").strip()
ALERT_NTFY_TOPIC = (os.getenv("CAPTURE_ALERT_NTFY_TOPIC") or "").strip()
ALERT_NTFY_URL = (os.getenv("CAPTURE_ALERT_NTFY_URL") or "https://ntfy.sh").rstrip("/")
ALERT_COOLDOWN_SECONDS = max(
    60, int(os.getenv("CAPTURE_ALERT_COOLDOWN_SECONDS", str(6 * 3600)))
)

CODE_INVALID_KEY = "invalid_odds_api_key"
CODE_ZERO_SAMPLES = "zero_successful_samples"
CODE_LOOP_ERROR = "capture_loop_error"

_last_sent: dict[str, float] = {}
_invalid_key_streak = 0
_zero_sample_streak = 0


def alerts_configured() -> bool:
    return bool(ALERT_WEBHOOK_URL or ALERT_NTFY_TOPIC)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _cooldown_ok(code: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    last = _last_sent.get(code)
    return last is None or (now - last) >= ALERT_COOLDOWN_SECONDS


def _mark_sent(code: str, now: float | None = None) -> None:
    _last_sent[code] = time.monotonic() if now is None else now


def reset_alert_state_for_tests() -> None:
    """Clear in-memory cooldown/streak state (unit tests only)."""
    global _invalid_key_streak, _zero_sample_streak
    _last_sent.clear()
    _invalid_key_streak = 0
    _zero_sample_streak = 0


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as resp:
        resp.read()


def _post_text(url: str, text: str, headers: dict[str, str] | None = None) -> None:
    req = request.Request(
        url,
        data=text.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8", **(headers or {})},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as resp:
        resp.read()


def deliver_alert(
    *,
    code: str,
    title: str,
    text: str,
    severity: str = "critical",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send to configured channels. Returns a small delivery report."""
    report: dict[str, Any] = {
        "code": code, "sent": False, "webhook": None, "ntfy": None, "skipped": None,
    }
    if not alerts_configured():
        report["skipped"] = "no_alert_channel_configured"
        return report
    if not _cooldown_ok(code):
        report["skipped"] = "cooldown"
        return report

    payload = {
        "title": title,
        "text": text,
        "content": text,  # Discord incoming-webhook field
        "severity": severity,
        "code": code,
        "source": "closing-capture-worker",
        "at": _iso_now(),
        "details": details or {},
    }
    errors: list[str] = []
    if ALERT_WEBHOOK_URL:
        try:
            _post_json(ALERT_WEBHOOK_URL, payload)
            report["webhook"] = "ok"
        except (error.URLError, error.HTTPError, TimeoutError, OSError) as exc:
            report["webhook"] = f"error:{exc}"
            errors.append(str(exc))
    if ALERT_NTFY_TOPIC:
        try:
            _post_text(
                f"{ALERT_NTFY_URL}/{ALERT_NTFY_TOPIC}",
                text,
                headers={
                    "Title": title[:250],
                    "Priority": "high" if severity == "critical" else "default",
                    "Tags": "warning,odds-api",
                },
            )
            report["ntfy"] = "ok"
        except (error.URLError, error.HTTPError, TimeoutError, OSError) as exc:
            report["ntfy"] = f"error:{exc}"
            errors.append(str(exc))

    if report["webhook"] == "ok" or report["ntfy"] == "ok":
        _mark_sent(code)
        report["sent"] = True
    elif errors and report.get("skipped") is None:
        report["skipped"] = "delivery_failed"
    return report


def note_cycle_health(
    *,
    sampled: int,
    priced: int,
    invalid_key_errors: int,
    fallbacks_this_cycle: int = 0,
) -> list[dict[str, Any]]:
    """Evaluate one process_queue cycle and emit alerts when thresholds trip.

    - Any invalid-key sample error → critical alert (after first consecutive hit).
    - Active sampling with zero prices for 3 consecutive cycles → critical alert.
    - A priced sample resets both streaks.
    """
    global _invalid_key_streak, _zero_sample_streak
    reports: list[dict[str, Any]] = []

    if priced > 0:
        _invalid_key_streak = 0
        _zero_sample_streak = 0
        return reports

    if invalid_key_errors > 0:
        _invalid_key_streak += 1
        if _invalid_key_streak >= 1:
            reports.append(deliver_alert(
                code=CODE_INVALID_KEY,
                title="Closing capture: Odds API key rejected",
                text=(
                    f"Closing-capture worker got {invalid_key_errors} invalid-key "
                    f"error(s) this cycle (streak={_invalid_key_streak}). "
                    "Check Railway ODDS_API_KEY — samples will FALLBACK until fixed."
                ),
                severity="critical",
                details={
                    "sampled": sampled, "priced": priced,
                    "invalid_key_errors": invalid_key_errors,
                    "fallbacks_this_cycle": fallbacks_this_cycle,
                    "streak": _invalid_key_streak,
                },
            ))
    elif sampled > 0:
        _zero_sample_streak += 1
        if _zero_sample_streak >= 3:
            reports.append(deliver_alert(
                code=CODE_ZERO_SAMPLES,
                title="Closing capture: zero successful samples",
                text=(
                    f"Closing-capture sampled {sampled} bet(s) with 0 prices "
                    f"for {_zero_sample_streak} consecutive cycles. "
                    "Check Odds API key, quota, and Railway logs."
                ),
                severity="critical",
                details={
                    "sampled": sampled, "priced": priced,
                    "fallbacks_this_cycle": fallbacks_this_cycle,
                    "streak": _zero_sample_streak,
                },
            ))
    return reports


def alert_loop_error(exc: BaseException) -> dict[str, Any]:
    return deliver_alert(
        code=CODE_LOOP_ERROR,
        title="Closing capture: loop error",
        text=f"Closing-capture worker loop error: {exc}",
        severity="warning",
        details={"error": str(exc)},
    )


def is_invalid_key_error(message: str | None) -> bool:
    text = str(message or "").lower()
    if "invalid odds api key" in text or "invalid api key" in text:
        return True
    # fetch_pinnacle_featured / raw requests surface HTTP 401 rather than the
    # closing_odds helper's explicit RuntimeError message.
    return "401" in text and ("unauthorized" in text or "forbidden" in text)
