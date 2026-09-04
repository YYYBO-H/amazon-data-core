from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from amazon_data_core.contracts import DatasetRunIn


def make_run(**overrides):
    values = {
        "source": "amazon_sp_api",
        "store_id": "store-1",
        "marketplace": "ATVPDKIKX0DER",
        "dataset": "orders",
        "business_date": date(2026, 9, 3),
        "fetched_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
        "ingestion_status": "complete",
        "source_count": 10,
        "normalized_count": 9,
        "duplicate_count": 1,
    }
    values.update(overrides)
    return DatasetRunIn(**values)


def test_valid_reconciled_counts():
    run = make_run()
    assert run.source_count == 10
    assert run.timezone == "UTC"
    assert run.schema_version == "1"


def test_accounted_counts_cannot_exceed_source():
    with pytest.raises(ValidationError):
        make_run(normalized_count=10, duplicate_count=1)


def test_error_rows_are_first_class_and_must_reconcile():
    run = make_run(normalized_count=8, duplicate_count=1, error_count=1)
    assert run.error_count == 1
