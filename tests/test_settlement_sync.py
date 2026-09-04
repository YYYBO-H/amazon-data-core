from __future__ import annotations

import csv
import gzip
import io
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from amazon_data_core.agent_tools import get_settlement_summary
from amazon_data_core.db import connect
from amazon_data_core.engine import run_checks
from amazon_data_core.sync_settlements import (
    SettlementNormalizationError,
    SettlementSyncConfig,
    list_all_settlement_reports,
    normalize_settlement_document,
    parse_localized_decimal,
    parse_settlement_tsv,
    sync_settlements,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL is required for integration test"
)

MARKETPLACE = "ATVPDKIKX0DER"
HEADERS = [
    "settlement-id",
    "settlement-start-date",
    "settlement-end-date",
    "deposit-date",
    "total-amount",
    "currency",
    "transaction-type",
    "order-id",
    "merchant-order-id",
    "adjustment-id",
    "shipment-id",
    "marketplace-name",
    "amount-type",
    "amount-description",
    "amount",
    "fulfillment-id",
    "posted-date",
    "posted-date-time",
    "order-item-code",
    "merchant-order-item-id",
    "merchant-adjustment-item-id",
    "sku",
    "quantity-purchased",
    "promotion-id",
]


def settlement_tsv(
    settlement_id: str,
    *,
    total: str = "100.00",
    principal: str = "120.00",
    fee: str = "-20.00",
) -> str:
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=HEADERS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerow(
        {
            "settlement-id": settlement_id,
            "settlement-start-date": "2026-08-15 12:00:00 UTC",
            "settlement-end-date": "2026-08-29 12:00:00 UTC",
            "deposit-date": "2026-09-01 12:00:00 UTC",
            "total-amount": total,
            "currency": "USD",
        }
    )
    writer.writerow(
        {
            "transaction-type": "Order",
            "order-id": "111-1111111-1111111",
            "marketplace-name": "Amazon.com",
            "amount-type": "ItemPrice",
            "amount-description": "Principal",
            "amount": principal,
            "posted-date-time": "2026-08-20 12:00:00 UTC",
            "sku": "SKU-A",
            "quantity-purchased": "1",
        }
    )
    writer.writerow(
        {
            "transaction-type": "Order",
            "order-id": "111-1111111-1111111",
            "marketplace-name": "Amazon.com",
            "amount-type": "ItemFees",
            "amount-description": "Commission",
            "amount": fee,
            "posted-date-time": "2026-08-20 12:00:00 UTC",
            "sku": "SKU-A",
            "quantity-purchased": "1",
        }
    )
    return stream.getvalue()


class FakeClient:
    def __init__(self, reports: list[dict], documents: dict[str, str]) -> None:
        self.reports = reports
        self.documents = documents
        self.list_calls: list[dict] = []

    def get_reports(self, params: dict) -> dict:
        self.list_calls.append(dict(params))
        return {"reports": self.reports}

    def get_report_document(self, document_id: str) -> dict:
        return {
            "url": f"https://reports.example/{document_id}",
            "compressionAlgorithm": "GZIP",
        }

    def download_report_document(self, url: str) -> bytes:
        document_id = url.rsplit("/", 1)[-1]
        return gzip.compress(self.documents[document_id].encode())


def report(report_id: str, document_id: str, created: str) -> dict:
    return {
        "reportId": report_id,
        "reportType": "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2",
        "processingStatus": "DONE",
        "reportDocumentId": document_id,
        "dataStartTime": "2026-08-15T00:00:00Z",
        "dataEndTime": "2026-08-29T00:00:00Z",
        "createdTime": created,
    }


def cleanup_store(store_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM quality_check_events WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM current_dataset_state WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_settlement_periods WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_settlement_lines WHERE store_id=%s", (store_id,))
        conn.execute(
            "DELETE FROM amazon_settlement_rejects WHERE sync_attempt_id IN "
            "(SELECT id FROM amazon_sync_attempts WHERE store_id=%s)",
            (store_id,),
        )
        conn.execute("DELETE FROM amazon_settlement_raw WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_settlement_reports WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_sync_cursors WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_sync_attempts WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM dataset_runs WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM quality_rules WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM data_scopes WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_store_connections WHERE store_id=%s", (store_id,))


@pytest.fixture
def isolated_store():
    store_id = f"settlement-e2e-{uuid4()}"
    try:
        yield store_id
    finally:
        cleanup_store(store_id)


def config(store_id: str) -> SettlementSyncConfig:
    return SettlementSyncConfig(
        store_id=store_id,
        marketplace=MARKETPLACE,
        region="NA",
        timezone="America/Los_Angeles",
        currency="USD",
    )


def test_localized_decimal_parser_supports_us_and_eu_formats():
    assert parse_localized_decimal("1,234.56", "amount") == Decimal("1234.56")
    assert parse_localized_decimal("1.234,56", "amount") == Decimal("1234.56")
    assert parse_localized_decimal("95,00", "amount") == Decimal("95.00")
    assert parse_localized_decimal("(20,00)", "amount") == Decimal("-20.00")
    assert parse_localized_decimal("1,234", "amount") == Decimal("1234")
    with pytest.raises(SettlementNormalizationError, match="ambiguous_amount"):
        parse_localized_decimal("1,2,34", "amount")


def test_document_contract_and_exact_reconciliation():
    headers, rows = parse_settlement_tsv(settlement_tsv("SETTLEMENT-1"))
    normalized, context = normalize_settlement_document(rows)

    assert headers == HEADERS
    assert len(normalized) == 3
    assert context["settlement_id"] == "SETTLEMENT-1"
    assert context["net_payout"] == Decimal("100.00")
    assert context["detail_amount_total"] == Decimal("100.00")
    assert context["reconciliation_delta"] == Decimal("0.00")
    assert normalized[1]["order_item_code"] is None


def test_document_contract_accepts_amazon_day_first_utc_dates():
    text = settlement_tsv("SETTLEMENT-DAY-FIRST")
    text = text.replace(
        "2026-08-15 12:00:00 UTC", "09.07.2026 01:49:07 UTC"
    )
    text = text.replace(
        "2026-08-29 12:00:00 UTC", "23.07.2026 01:49:08 UTC"
    )
    text = text.replace(
        "2026-09-01 12:00:00 UTC", "25.07.2026 01:49:08 UTC"
    )
    _, rows = parse_settlement_tsv(text)

    normalized, context = normalize_settlement_document(rows)

    assert context["settlement_start_time"] == datetime(
        2026, 7, 9, 1, 49, 7, tzinfo=UTC
    )
    assert context["settlement_end_time"] == datetime(
        2026, 7, 23, 1, 49, 8, tzinfo=UTC
    )
    assert context["deposit_time"] == datetime(
        2026, 7, 25, 1, 49, 8, tzinfo=UTC
    )
    assert normalized[0]["settlement_start_time"] == context[
        "settlement_start_time"
    ]


def test_invalid_reconciliation_is_rejected():
    _, rows = parse_settlement_tsv(
        settlement_tsv("SETTLEMENT-BAD", total="99.98")
    )
    with pytest.raises(
        SettlementNormalizationError,
        match="net_payout_detail_reconciliation_failed",
    ):
        normalize_settlement_document(rows)


def test_sync_persists_closed_period_and_mcp_summary(isolated_store):
    store_id = isolated_store
    client = FakeClient(
        [report("REPORT-1", "DOC-1", "2026-09-01T12:00:00Z")],
        {"DOC-1": settlement_tsv("SETTLEMENT-1")},
    )
    with connect() as conn:
        result = sync_settlements(
            conn,
            client,
            config(store_id),
            now=datetime.now(UTC),
        )
        run_checks(conn)
        conn.commit()
        period = conn.execute(
            """SELECT net_payout, detail_amount_total, reconciliation_delta,
                      detail_row_count
               FROM amazon_settlement_periods WHERE store_id=%s""",
            (store_id,),
        ).fetchone()
    summary = get_settlement_summary(
        store_id,
        "2026-08-29",
        "2026-08-29",
        MARKETPLACE,
        date_basis="settlement_end",
    )

    assert result["status"] == "completed"
    assert result["reports_downloaded"] == 1
    assert result["rows_downloaded"] == 3
    assert dict(period) == {
        "net_payout": Decimal("100.0000"),
        "detail_amount_total": Decimal("100.0000"),
        "reconciliation_delta": Decimal("0.0000"),
        "detail_row_count": 2,
    }
    assert summary["safe_to_analyze"] is True
    assert summary["period_count"] == 1
    assert summary["net_payout"] == "100.0000"
    assert summary["max_abs_reconciliation_delta"] == "0.0000"
    assert "closed_settlement_cash_flow_not_order_date_revenue_or_profit" in summary[
        "warnings"
    ]


def test_new_invalid_report_does_not_replace_valid_period(isolated_store):
    store_id = isolated_store
    with connect() as conn:
        sync_settlements(
            conn,
            FakeClient(
                [report("REPORT-OLD", "DOC-OLD", "2026-09-01T12:00:00Z")],
                {"DOC-OLD": settlement_tsv("SETTLEMENT-1")},
            ),
            config(store_id),
            now=datetime(2026, 9, 2, 12, tzinfo=UTC),
        )
        result = sync_settlements(
            conn,
            FakeClient(
                [report("REPORT-BAD", "DOC-BAD", "2026-09-02T12:00:00Z")],
                {"DOC-BAD": settlement_tsv("SETTLEMENT-1", total="99.98")},
            ),
            config(store_id),
            now=datetime(2026, 9, 3, 12, tzinfo=UTC),
        )
        period = conn.execute(
            """SELECT report_id, net_payout FROM amazon_settlement_periods
               WHERE store_id=%s""",
            (store_id,),
        ).fetchone()
        bad_raw_count = conn.execute(
            """SELECT COUNT(*) AS n FROM amazon_settlement_raw
               WHERE store_id=%s AND report_id='REPORT-BAD'""",
            (store_id,),
        ).fetchone()["n"]

    assert result["status"] == "partial"
    assert result["reports_errored"] == 1
    assert bad_raw_count == 3
    assert period["report_id"] == "REPORT-OLD"
    assert period["net_payout"] == Decimal("100.0000")


def test_report_listing_uses_next_token_and_deduplicates():
    class PagingClient:
        def __init__(self):
            self.calls = []

        def get_reports(self, params):
            self.calls.append(dict(params))
            first = report("REPORT-1", "DOC-1", "2026-09-01T12:00:00Z")
            second = report("REPORT-2", "DOC-2", "2026-09-02T12:00:00Z")
            if params.get("nextToken"):
                return {"reports": [first, second]}
            return {"reports": [first], "nextToken": "NEXT"}

    client = PagingClient()
    reports, pages = list_all_settlement_reports(
        client,
        config("paging-store"),
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )

    assert pages == 2
    assert [item["reportId"] for item in reports] == ["REPORT-1", "REPORT-2"]
    assert set(client.calls[1]) == {"nextToken"}
