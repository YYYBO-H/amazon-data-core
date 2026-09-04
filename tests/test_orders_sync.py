from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest

from amazon_data_core.db import connect
from amazon_data_core.agent_tools import get_orders_summary
from amazon_data_core.engine import run_checks
from amazon_data_core.sync_orders import OrdersSyncConfig, SOURCE, sync_orders


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL is required for integration test"
)


def cleanup_store(store_id: str) -> None:
    """Remove only the isolated store created by this test."""
    with connect() as conn:
        conn.execute("DELETE FROM quality_check_events WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM current_dataset_state WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_orders WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_order_rejects WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_sync_cursors WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_orders_raw WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_sync_attempts WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM dataset_runs WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM quality_rules WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM data_scopes WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_store_connections WHERE store_id=%s", (store_id,))


@pytest.fixture
def isolated_store():
    store_id = f"orders-e2e-{uuid4()}"
    try:
        yield store_id
    finally:
        cleanup_store(store_id)


def order(number: int, updated_hour: int) -> dict:
    return {
        "orderId": f"ORDER-{number}",
        "createdTime": f"2026-09-03T0{number}:00:00Z",
        "lastUpdatedTime": f"2026-09-03T{updated_hour:02d}:00:00Z",
        "salesChannel": {"marketplaceId": "ATVPDKIKX0DER"},
        "buyer": {"name": "must not persist"},
        "orderItems": [{"quantityOrdered": number}],
        "fulfillment": {
            "fulfillmentStatus": "SHIPPED",
            "fulfilledBy": "AMAZON",
        },
        "proceeds": {
            "grandTotal": {"amount": f"{number * 10}.00", "currencyCode": "USD"}
        },
    }


class FakeClient:
    def __init__(self, *, fail_second_page: bool = False) -> None:
        self.fail_second_page = fail_second_page
        self.calls: list[dict] = []

    def search_orders(self, params: dict) -> dict:
        self.calls.append(params)
        token = params.get("paginationToken")
        if token == "page-2":
            if self.fail_second_page:
                raise RuntimeError("simulated page failure")
            return {"orders": [order(2, 4)]}
        return {"orders": [order(1, 3)], "pagination": {"nextToken": "page-2"}}


def config(store_id: str) -> OrdersSyncConfig:
    return OrdersSyncConfig(
        store_id=store_id,
        marketplace="ATVPDKIKX0DER",
        region="NA",
        timezone="America/Los_Angeles",
        currency="USD",
        initial_days=3,
    )


def test_orders_sync_is_idempotent_and_raw_payload_is_redacted(isolated_store):
    store_id = isolated_store
    fixed_now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    with connect() as conn:
        first = sync_orders(conn, FakeClient(), config(store_id), now=fixed_now)
        second = sync_orders(
            conn,
            FakeClient(),
            config(store_id),
            now=datetime(2026, 9, 4, 13, tzinfo=UTC),
        )
        raw = conn.execute(
            "SELECT payload, pii_redacted FROM amazon_orders_raw WHERE store_id=%s",
            (store_id,),
        ).fetchall()
        canonical_count = conn.execute(
            "SELECT COUNT(*) AS n FROM amazon_orders WHERE store_id=%s", (store_id,)
        ).fetchone()["n"]
        run_checks(conn)
        conn.commit()
        summary = get_orders_summary(store_id, "2026-09-02", "ATVPDKIKX0DER")
        outside_coverage = get_orders_summary(
            store_id, "2026-08-31", "ATVPDKIKX0DER"
        )

    assert first["status"] == "completed"
    assert first["rows_inserted"] == 2
    assert first["pages_completed"] == 2
    assert second["status"] == "completed"
    assert second["rows_skipped"] == 2
    assert len(raw) == 2
    assert canonical_count == 2
    assert all(row["pii_redacted"] for row in raw)
    assert all("buyer" not in row["payload"] for row in raw)
    assert summary["safe_to_analyze"] is True
    assert summary["verified_coverage"][0]["covers_requested_date"] is True
    assert summary["order_count"] == 2
    assert summary["item_count"] == 3
    assert summary["proceeds_by_currency"] == {"USD": "30.0000"}
    assert outside_coverage["safe_to_analyze"] is False
    assert "requested_date_outside_verified_coverage" in outside_coverage["warnings"]


def test_orders_sync_resumes_from_the_failed_page(isolated_store):
    store_id = isolated_store
    fixed_now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    failing = FakeClient(fail_second_page=True)
    with connect() as conn:
        with pytest.raises(RuntimeError, match="simulated page failure"):
            sync_orders(conn, failing, config(store_id), now=fixed_now)
        failed = conn.execute(
            """SELECT * FROM amazon_sync_attempts
               WHERE source=%s AND store_id=%s ORDER BY created_at DESC LIMIT 1""",
            (SOURCE, store_id),
        ).fetchone()
        resumed_client = FakeClient()
        resumed = sync_orders(conn, resumed_client, config(store_id), now=fixed_now)
        attempts = conn.execute(
            "SELECT COUNT(*) AS n FROM amazon_sync_attempts WHERE store_id=%s",
            (store_id,),
        ).fetchone()["n"]

    assert failed["status"] == "failed"
    assert failed["pagination_token"] == "page-2"
    assert failed["pages_completed"] == 1
    assert resumed["id"] == failed["id"]
    assert resumed["status"] == "completed"
    assert resumed["rows_pulled"] == 2
    assert resumed_client.calls[0]["paginationToken"] == "page-2"
    assert attempts == 1
