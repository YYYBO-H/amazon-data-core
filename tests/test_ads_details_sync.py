from datetime import date
import os
from uuid import uuid4

import pytest

from amazon_data_core.agent_tools import (
    get_ads_purchased_product_summary,
    get_ads_search_term_summary,
)
from amazon_data_core.db import connect
from amazon_data_core.engine import run_checks
from amazon_data_core.sync_ads import AdsCampaignSyncConfig
from amazon_data_core.sync_ads_details import (
    PURCHASED_PRODUCT_REPORT_SPEC,
    SEARCH_TERM_REPORT_SPEC,
    normalize_purchased_product_row,
    normalize_search_term_row,
    sync_ads_purchased_products,
    sync_ads_search_terms,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL is required for integration test"
)

MARKETPLACE = "ATVPDKIKX0DER"


class FakeAdsClient:
    def __init__(
        self,
        report_id: str,
        rows: list[dict],
        *,
        completed_at: str = "2026-09-04T12:01:00Z",
    ) -> None:
        self.report_id = report_id
        self.rows = rows
        self.completed_at = completed_at
        self.created: list[dict] = []
        self.polled: list[str] = []
        self.sleep = lambda _: None

    def get_profile(self) -> dict:
        return {
            "profileId": "profile-1",
            "countryCode": "US",
            "currencyCode": "USD",
            "timezone": "America/Los_Angeles",
            "accountInfo": {"marketplaceStringId": MARKETPLACE},
        }

    def create_report(self, body: dict) -> dict:
        self.created.append(body)
        return {
            "reportId": self.report_id,
            "status": "PENDING",
            "createdAt": "2026-09-04T12:00:00Z",
        }

    def get_report(self, report_id: str) -> dict:
        self.polled.append(report_id)
        return {
            "reportId": report_id,
            "status": "COMPLETED",
            "createdAt": "2026-09-04T12:00:00Z",
            "completedAt": self.completed_at,
            "url": "https://reports.example/report.gz",
        }

    def download_report(self, url: str) -> list[dict]:
        assert url.startswith("https://")
        return self.rows


def cleanup_store(store_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM quality_check_events WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM current_dataset_state WHERE store_id=%s", (store_id,))
        for table in (
            "amazon_ads_search_term_daily",
            "amazon_ads_purchased_product_daily",
        ):
            conn.execute(f"DELETE FROM {table} WHERE store_id=%s", (store_id,))
        for table in (
            "amazon_ads_search_term_rejects",
            "amazon_ads_purchased_product_rejects",
            "amazon_ads_search_term_raw",
            "amazon_ads_purchased_product_raw",
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE sync_attempt_id IN "
                "(SELECT id FROM amazon_sync_attempts WHERE store_id=%s)",
                (store_id,),
            )
        conn.execute("DELETE FROM amazon_ads_reports WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_sync_cursors WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_sync_attempts WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM dataset_runs WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM quality_rules WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM data_scopes WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_store_connections WHERE store_id=%s", (store_id,))


@pytest.fixture
def isolated_store():
    store_id = f"ads-detail-e2e-{uuid4()}"
    try:
        yield store_id
    finally:
        cleanup_store(store_id)


def config(store_id: str, *, attribution_days: int) -> AdsCampaignSyncConfig:
    return AdsCampaignSyncConfig(
        store_id=store_id,
        marketplace=MARKETPLACE,
        profile_id="profile-1",
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 2),
        timezone="America/Los_Angeles",
        currency="USD",
        attribution_window_days=attribution_days,
        poll_interval_seconds=0,
    )


def search_row(keyword_id: int, *, spend: str = "2.50") -> dict:
    return {
        "date": "2026-09-02",
        "campaignId": 101,
        "campaignName": "Campaign",
        "adGroupId": 201,
        "adGroupName": "Ad group",
        "keywordId": keyword_id,
        "keyword": "running shoes",
        "matchType": "BROAD",
        "searchTerm": "blue running shoes",
        "impressions": 100,
        "clicks": 5,
        "cost": spend,
        "spend": spend,
        "sales1d": "5.00",
        "sales7d": "8.00",
        "sales14d": "10.00",
        "purchases1d": 1,
        "purchases7d": 1,
        "purchases14d": 2,
        "unitsSoldClicks1d": 1,
        "unitsSoldClicks7d": 1,
        "unitsSoldClicks14d": 2,
    }


def purchased_row(
    advertised_asin: str,
    purchased_asin: str,
    *,
    sales_30d: str = "15.00",
) -> dict:
    return {
        "date": "2026-09-02",
        "campaignId": 101,
        "campaignName": "Campaign",
        "adGroupId": 201,
        "adGroupName": "Ad group",
        "keywordId": 301,
        "matchType": "EXACT",
        "portfolioId": 401,
        "advertisedAsin": advertised_asin,
        "advertisedSku": f"SKU-{advertised_asin}",
        "purchasedAsin": purchased_asin,
        "sales1d": "5.00",
        "sales7d": "8.00",
        "sales14d": "10.00",
        "sales30d": sales_30d,
        "purchases1d": 1,
        "purchases7d": 1,
        "purchases14d": 2,
        "purchases30d": 3,
        "unitsSoldClicks1d": 1,
        "unitsSoldClicks7d": 1,
        "unitsSoldClicks14d": 2,
        "unitsSoldClicks30d": 3,
    }


def test_report_contracts_and_numeric_identifiers():
    search = normalize_search_term_row(
        search_row(301),
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 2),
        attribution_window_days=14,
    )
    purchased = normalize_purchased_product_row(
        purchased_row("B000000001", "B000000002"),
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 2),
        attribution_window_days=30,
    )

    assert search["campaign_id"] == "101"
    assert search["keyword_id"] == "301"
    assert purchased["portfolio_id"] == "401"
    assert SEARCH_TERM_REPORT_SPEC.filters == ()
    assert PURCHASED_PRODUCT_REPORT_SPEC.filters == ()


def test_search_term_grain_keeps_same_term_from_different_keywords(isolated_store):
    store_id = isolated_store
    client = FakeAdsClient("search-report-1", [search_row(301), search_row(302)])
    with connect() as conn:
        result = sync_ads_search_terms(conn, client, config(store_id, attribution_days=14))
        run_checks(conn)
        rows = conn.execute(
            """SELECT keyword_id, search_term, active
               FROM amazon_ads_search_term_daily
               WHERE store_id=%s ORDER BY keyword_id""",
            (store_id,),
        ).fetchall()
        state = conn.execute(
            """SELECT ingestion_status, source_count, normalized_count, error_count
               FROM current_dataset_state
               WHERE source='amazon_ads_reporting_v3_sp_search_terms'
                 AND store_id=%s""",
            (store_id,),
        ).fetchone()
    summary = get_ads_search_term_summary(
        store_id, "2026-09-02", "2026-09-02", MARKETPLACE
    )

    assert result["status"] == "completed"
    assert result["rows_inserted"] == 2
    assert [(row["keyword_id"], row["active"]) for row in rows] == [
        ("301", True),
        ("302", True),
    ]
    assert rows[0]["search_term"] == rows[1]["search_term"]
    assert dict(state) == {
        "ingestion_status": "complete",
        "source_count": 2,
        "normalized_count": 2,
        "error_count": 0,
    }
    assert summary["safe_to_analyze"] is True
    assert summary["search_term_row_count"] == 2
    assert summary["search_term_count"] == 1
    assert summary["spend"] == "5.0000"
    assert "search_term_report_has_no_asin_or_sku" in summary["warnings"]


def test_search_term_newer_report_revises_and_deactivates(isolated_store):
    store_id = isolated_store
    with connect() as conn:
        sync_ads_search_terms(
            conn,
            FakeAdsClient("search-version-1", [search_row(301), search_row(302)]),
            config(store_id, attribution_days=14),
        )
        revised = search_row(301, spend="3.00")
        result = sync_ads_search_terms(
            conn,
            FakeAdsClient(
                "search-version-2",
                [revised],
                completed_at="2026-09-04T13:01:00Z",
            ),
            config(store_id, attribution_days=14),
        )
        rows = conn.execute(
            """SELECT keyword_id, spend, active
               FROM amazon_ads_search_term_daily
               WHERE store_id=%s ORDER BY keyword_id""",
            (store_id,),
        ).fetchall()
        raw_count = conn.execute(
            """SELECT COUNT(*) AS n FROM amazon_ads_search_term_raw r
               JOIN amazon_sync_attempts a ON a.id=r.sync_attempt_id
               WHERE a.store_id=%s""",
            (store_id,),
        ).fetchone()["n"]

    assert result["status"] == "completed"
    assert result["rows_updated"] == 1
    assert [(row["keyword_id"], str(row["spend"]), row["active"]) for row in rows] == [
        ("301", "3.0000", True),
        ("302", "2.5000", False),
    ]
    assert raw_count == 3


def test_purchased_product_grain_and_partial_safety(isolated_store):
    store_id = isolated_store
    first_rows = [
        purchased_row("B000000001", "B000000003"),
        purchased_row("B000000002", "B000000003"),
    ]
    with connect() as conn:
        first = sync_ads_purchased_products(
            conn,
            FakeAdsClient("purchase-report-1", first_rows),
            config(store_id, attribution_days=30),
        )
        run_checks(conn)
        conn.commit()
        first_summary = get_ads_purchased_product_summary(
            store_id, "2026-09-02", "2026-09-02", MARKETPLACE
        )
        invalid = purchased_row("B000000001", "invalid")
        second = sync_ads_purchased_products(
            conn,
            FakeAdsClient(
                "purchase-report-2",
                [invalid],
                completed_at="2026-09-04T13:01:00Z",
            ),
            config(store_id, attribution_days=30),
        )
        rows = conn.execute(
            """SELECT advertised_asin, purchased_asin, active
               FROM amazon_ads_purchased_product_daily
               WHERE store_id=%s ORDER BY advertised_asin""",
            (store_id,),
        ).fetchall()

    assert first["status"] == "completed"
    assert first["rows_inserted"] == 2
    assert second["status"] == "partial"
    assert second["rows_errored"] == 1
    assert [(row["advertised_asin"], row["active"]) for row in rows] == [
        ("B000000001", True),
        ("B000000002", True),
    ]
    assert {row["purchased_asin"] for row in rows} == {"B000000003"}
    assert first_summary["safe_to_analyze"] is True
    assert first_summary["attribution_row_count"] == 2
    assert first_summary["purchased_asin_count"] == 1
    assert first_summary["attributed_sales_30d"] == "30.0000"
    assert (
        "purchased_product_report_has_no_impressions_clicks_or_spend"
        in first_summary["warnings"]
    )
