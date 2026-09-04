from datetime import date, datetime, timedelta, timezone
import os

import pytest

from amazon_data_core.contracts import DatasetRunIn, RuleIn
from amazon_data_core.db import connect
from amazon_data_core.engine import ingest_run, run_checks, upsert_rule


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL is required for integration test"
)


def test_stale_snapshot_opens_an_issue_and_new_data_recovers_it():
    now = datetime.now(timezone.utc)
    base = {
        "source": "test_connector",
        "store_id": "issue-lifecycle-test",
        "marketplace": "TEST",
        "dataset": "inventory_e2e",
        "business_date": date.today(),
        "fetched_at": now,
        "ingestion_status": "complete",
        "source_count": 10,
        "normalized_count": 10,
    }
    with connect() as conn:
        upsert_rule(
            conn,
            RuleIn(
                rule_code="ORDERING_E2E",
                check_type="ordering",
                dataset="inventory_e2e",
            ),
        )
        first_input = DatasetRunIn(
            **base, source_updated_at=now, external_run_id="first-upstream-run"
        )
        first = ingest_run(conn, first_input)
        assert first["accepted"] is True
        duplicate = ingest_run(conn, first_input)
        assert duplicate["duplicate"] is True
        assert duplicate["run_id"] == first["run_id"]
        run_checks(conn)

        stale = ingest_run(
            conn,
            DatasetRunIn(
                **{**base, "fetched_at": now + timedelta(minutes=1)},
                source_updated_at=now - timedelta(hours=1),
            ),
        )
        assert stale == {
            "run_id": stale["run_id"],
            "accepted": False,
            "rejection_reason": "older_source_version",
        }
        run_checks(conn)
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM v_open_issues WHERE rule_code='ORDERING_E2E'"
        ).fetchone()["n"] == 1

        recovered = ingest_run(
            conn,
            DatasetRunIn(
                **{**base, "fetched_at": now + timedelta(minutes=2)},
                source_updated_at=now + timedelta(minutes=2),
            ),
        )
        assert recovered["accepted"] is True
        run_checks(conn)
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM v_open_issues WHERE rule_code='ORDERING_E2E'"
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM v_recovered_issues WHERE rule_code='ORDERING_E2E'"
        ).fetchone()["n"] == 1
        conn.rollback()


def test_rule_selector_does_not_leak_between_sources():
    now = datetime.now(timezone.utc)
    shared = {
        "store_id": "scope-test",
        "marketplace": "TEST",
        "dataset": "orders_scope_e2e",
        "business_date": date.today(),
        "fetched_at": now,
        "source_updated_at": now,
        "ingestion_status": "complete",
        "source_count": 1,
        "normalized_count": 1,
    }
    with connect() as conn:
        upsert_rule(
            conn,
            RuleIn(
                rule_code="SCOPED_SOURCE_E2E",
                check_type="completeness",
                dataset="orders_scope_e2e",
                source="connector-a",
            ),
        )
        ingest_run(conn, DatasetRunIn(source="connector-a", **shared))
        ingest_run(conn, DatasetRunIn(source="connector-b", **shared))
        run_checks(conn)
        rows = conn.execute(
            """SELECT source FROM quality_check_events
               WHERE rule_code='SCOPED_SOURCE_E2E'"""
        ).fetchall()
        assert [row["source"] for row in rows] == ["connector-a"]
        conn.rollback()
