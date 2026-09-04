from datetime import datetime, timedelta, timezone

from amazon_data_core.contracts import CheckStatus
from amazon_data_core.engine import _evaluate


def row(**overrides):
    values = {
        "check_type": "freshness",
        "config": {"max_age_minutes": 60},
        "fetched_at": datetime.now(timezone.utc) - timedelta(minutes=10),
        "ingestion_status": "complete",
        "source_count": 10,
        "normalized_count": 9,
        "duplicate_count": 1,
        "error_count": 0,
    }
    values.update(overrides)
    return values


def test_missing_data_is_a_real_freshness_failure():
    status, details = _evaluate(row(), None, None)
    assert status is CheckStatus.FAILED
    assert details["reason"] == "no_data_received"


def test_missing_data_skips_checks_that_cannot_decide():
    status, _ = _evaluate(row(check_type="reconciliation"), None, None)
    assert status is CheckStatus.SKIPPED


def test_reconciliation_passes_when_every_source_row_is_accounted_for():
    state = row(check_type="reconciliation")
    status, details = _evaluate(state, state, None)
    assert status is CheckStatus.PASSED
    assert details["unaccounted_count"] == 0


def test_partial_sync_is_not_reported_as_passed():
    state = row(check_type="completeness", ingestion_status="partial")
    status, _ = _evaluate(state, state, None)
    assert status is CheckStatus.FAILED


def test_completed_sync_with_row_errors_is_not_reported_as_complete():
    state = row(check_type="completeness", error_count=1)
    status, details = _evaluate(state, state, None)
    assert status is CheckStatus.FAILED
    assert details["error_count"] == 1


def test_stale_arrival_is_visible_as_ordering_failure():
    state = row(check_type="ordering")
    status, details = _evaluate(
        state,
        state,
        {"id": "old", "accepted": False, "rejection_reason": "older_source_version"},
    )
    assert status is CheckStatus.FAILED
    assert details["rejection_reason"] == "older_source_version"
