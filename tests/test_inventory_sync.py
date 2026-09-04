from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest

from amazon_data_core.agent_tools import get_fba_inventory_status
from amazon_data_core.connectors.sp_api import SPAPIError
from amazon_data_core.db import connect
from amazon_data_core.engine import run_checks
from amazon_data_core.sync_inventory import (
    InventorySyncConfig,
    _fetch_full_snapshot,
    sync_inventory,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL is required for integration test"
)

MARKETPLACE = "ATVPDKIKX0DER"


def cleanup_store(store_id: str) -> None:
    """Remove only the isolated store created by this test."""
    with connect() as conn:
        conn.execute("DELETE FROM quality_check_events WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM current_dataset_state WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_fba_inventory WHERE store_id=%s", (store_id,))
        conn.execute(
            "DELETE FROM amazon_fba_inventory_snapshot_rows "
            "WHERE sync_attempt_id IN ("
            "SELECT id FROM amazon_sync_attempts WHERE store_id=%s)",
            (store_id,),
        )
        conn.execute("DELETE FROM amazon_fba_inventory_rejects WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_fba_inventory_raw WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_sync_cursors WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_sync_attempts WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM dataset_runs WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM quality_rules WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM data_scopes WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_store_connections WHERE store_id=%s", (store_id,))


@pytest.fixture
def isolated_store():
    store_id = f"inventory-e2e-{uuid4()}"
    try:
        yield store_id
    finally:
        cleanup_store(store_id)


def inventory_item(
    seller_sku: str,
    *,
    asin: str = "B000TEST01",
    fn_sku: str | None = None,
    fulfillable: int = 5,
    total: int | None = None,
) -> dict:
    resolved_total = fulfillable if total is None else total
    return {
        "asin": asin,
        "sellerSku": seller_sku,
        "fnSku": fn_sku or f"X-{seller_sku}",
        "condition": "NewItem",
        "productName": f"Product {seller_sku}",
        "totalQuantity": resolved_total,
        "lastUpdatedTime": "2026-09-04T10:00:00Z",
        "inventoryDetails": {
            "fulfillableQuantity": fulfillable,
            "inboundWorkingQuantity": 1,
            "inboundShippedQuantity": 2,
            "inboundReceivingQuantity": 3,
            "reservedQuantity": {"totalReservedQuantity": 4},
            "researchingQuantity": {"totalResearchingQuantity": 5},
            "unfulfillableQuantity": {"totalUnfulfillableQuantity": 6},
        },
    }


class FakeClient:
    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def get_inventory_summaries(self, params: dict) -> dict:
        self.calls.append(dict(params))
        page_number = 0
        token = params.get("nextToken")
        if token:
            page_number = int(token.removeprefix("page-")) - 1
        response = {"payload": {"inventorySummaries": self.pages[page_number]}}
        if page_number + 1 < len(self.pages):
            response["pagination"] = {"nextToken": f"page-{page_number + 2}"}
        return response


def config(store_id: str) -> InventorySyncConfig:
    return InventorySyncConfig(
        store_id=store_id,
        marketplace=MARKETPLACE,
        region="NA",
        timezone="America/Los_Angeles",
        currency="USD",
    )


def test_full_snapshot_preserves_multiple_skus_per_asin_and_deactivates_absent_rows(
    isolated_store,
):
    store_id = isolated_store
    fixed_now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    first_client = FakeClient(
        [
            [
                inventory_item("SKU-A", fulfillable=8, total=29),
                inventory_item("SKU-B", fulfillable=0, total=21),
            ],
            [inventory_item("SKU-C", asin="B000TEST02", fulfillable=2, total=23)],
        ]
    )
    second_client = FakeClient(
        [
            [
                inventory_item("SKU-A", fulfillable=3, total=24),
                inventory_item("SKU-B", fulfillable=0, total=21),
            ]
        ]
    )

    with connect() as conn:
        first = sync_inventory(conn, first_client, config(store_id), now=fixed_now)
        first_rows = conn.execute(
            """SELECT seller_sku, active FROM amazon_fba_inventory
               WHERE store_id=%s ORDER BY seller_sku""",
            (store_id,),
        ).fetchall()
        first_membership = conn.execute(
            """SELECT COUNT(*) AS n FROM amazon_fba_inventory_snapshot_rows
               WHERE sync_attempt_id=%s""",
            (first["id"],),
        ).fetchone()["n"]
        second = sync_inventory(
            conn,
            second_client,
            config(store_id),
            now=datetime(2026, 9, 4, 13, tzinfo=UTC),
        )
        rows = conn.execute(
            """SELECT seller_sku, fulfillable_quantity, active
               FROM amazon_fba_inventory WHERE store_id=%s ORDER BY seller_sku""",
            (store_id,),
        ).fetchall()
        raw_count = conn.execute(
            "SELECT COUNT(*) AS n FROM amazon_fba_inventory_raw WHERE store_id=%s",
            (store_id,),
        ).fetchone()["n"]
        run_checks(conn)
        conn.commit()

    status = get_fba_inventory_status(store_id, MARKETPLACE, max_fulfillable=10)

    assert first["status"] == "completed"
    assert first["rows_inserted"] == 3
    assert first["pages_completed"] == 2
    assert first_membership == 3
    assert [(row["seller_sku"], row["active"]) for row in first_rows] == [
        ("SKU-A", True),
        ("SKU-B", True),
        ("SKU-C", True),
    ]
    assert second["status"] == "completed"
    assert second["rows_updated"] == 1
    assert second["rows_skipped"] == 1
    assert [
        (row["seller_sku"], row["fulfillable_quantity"], row["active"])
        for row in rows
    ] == [
        ("SKU-A", 3, True),
        ("SKU-B", 0, True),
        ("SKU-C", 2, False),
    ]
    assert raw_count == 4
    assert status["safe_to_analyze"] is True
    assert status["inventory_record_count"] == 2
    assert status["asin_count"] == 1
    assert status["seller_sku_count"] == 2
    assert status["fulfillable_quantity"] == 3
    assert status["asins_with_multiple_records"] == 1
    assert status["dataset_state"][0]["normalized_count"] == 2
    assert status["dataset_state"][0]["duplicate_count"] == 0
    assert "fba_only_mfn_inventory_not_included" in status["warnings"]
    assert "some_asins_have_multiple_inventory_records" in status["warnings"]


def test_partial_snapshot_does_not_deactivate_absent_rows(isolated_store):
    store_id = isolated_store
    with connect() as conn:
        sync_inventory(
            conn,
            FakeClient([[inventory_item("SKU-A"), inventory_item("SKU-B")]]),
            config(store_id),
            now=datetime(2026, 9, 4, 12, tzinfo=UTC),
        )
        invalid = inventory_item("SKU-A")
        invalid["inventoryDetails"]["fulfillableQuantity"] = -1
        result = sync_inventory(
            conn,
            FakeClient([[invalid]]),
            config(store_id),
            now=datetime(2026, 9, 4, 13, tzinfo=UTC),
        )
        active = conn.execute(
            """SELECT seller_sku, active FROM amazon_fba_inventory
               WHERE store_id=%s ORDER BY seller_sku""",
            (store_id,),
        ).fetchall()

    assert result["status"] == "partial"
    assert result["rows_errored"] == 1
    assert [(row["seller_sku"], row["active"]) for row in active] == [
        ("SKU-A", True),
        ("SKU-B", True),
    ]


def test_expired_next_token_restarts_the_entire_snapshot():
    class ExpiringClient:
        def __init__(self) -> None:
            self.calls: list[str | None] = []
            self.expired = False

        def get_inventory_summaries(self, params: dict) -> dict:
            token = params.get("nextToken")
            self.calls.append(token)
            if token == "page-2" and not self.expired:
                self.expired = True
                raise SPAPIError("expired", status_code=400)
            if token == "page-2":
                return {
                    "payload": {
                        "inventorySummaries": [inventory_item("SKU-B")]
                    }
                }
            return {
                "payload": {"inventorySummaries": [inventory_item("SKU-A")]},
                "pagination": {"nextToken": "page-2"},
            }

    client = ExpiringClient()
    items, pages = _fetch_full_snapshot(client, config("inventory-token-test"))

    assert [item["sellerSku"] for item in items] == ["SKU-A", "SKU-B"]
    assert pages == 2
    assert client.calls == [None, "page-2", None, "page-2"]
