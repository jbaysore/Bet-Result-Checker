from datetime import datetime, timedelta, timezone

import pytest

from scripts.clv_start_audit import classify_start_audit, summarize_clv_buckets


SCHEDULED = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)


def test_audit_geometry_strict_boundaries():
    assert classify_start_audit(SCHEDULED, SCHEDULED) == "SAFE"
    assert classify_start_audit(SCHEDULED - timedelta(minutes=7), SCHEDULED) == "LIKELY_SUSPECT"
    assert classify_start_audit(SCHEDULED - timedelta(minutes=1), SCHEDULED) == "INDETERMINATE"
    assert classify_start_audit(SCHEDULED - timedelta(minutes=6), SCHEDULED) == "INDETERMINATE"
    assert classify_start_audit(None, SCHEDULED) == "UNRESOLVABLE"


def test_audit_report_includes_aggregate_with_and_without_each_bucket():
    report = summarize_clv_buckets({"SAFE": [0.01, 0.03], "LIKELY_SUSPECT": [-0.02]})
    assert report["all"]["count"] == 3
    assert report["all"]["average"] == pytest.approx(0.006666666666666665)
    assert report["without_bucket"]["LIKELY_SUSPECT"] == {"count": 2, "average": 0.02}
    assert report["without_bucket"]["SAFE"] == {"count": 1, "average": -0.02}
