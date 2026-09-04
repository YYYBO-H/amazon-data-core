from datetime import UTC, date, datetime
from decimal import Decimal
import os
from uuid import uuid4

import pytest

import amazon_data_core.sync_ads as sync_ads_module
from amazon_data_core.agent_tools import get_ads_campaign_summary
from amazon_data_core.db import connect
from amazon_data_core.engine import run_checks
from amazon_data_core.sync_ads import (
    AdsCampaignSyncConfig,
    AdsReportPending,
    sync_ads_campaigns,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL is required for integration test"
)

MARKETPLACE = "ATVPDKIKX0DER"


def cleanup_store(store_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM quality_check_events WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM current_dataset_state WHERE store_id=%s", (store_id,))
        conn.execute("DELETE FROM amazon_ads_campaign_daily WHERE store_id=%s", (store_id,))
        conn.execute(
            "DELETE FROM amazon_ads_campaign_rejects WHERE sync_attempt_id IN "
            "(SELECT id FROM amazon_sync_attempts WHERE store_id=%s)",
            (store_id,),
        )
        conn.execute(
            "DELETE FROM amazon_ads_campaign_raw WHERE sync_attempt_id IN "
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
    store_id = f"ads-e2e-{uuid4()}"
    try:
        yield store_id
    finally:
        cleanup_store(store_id)


def campaign_row(
    campaign_id: str | int,
    report_date: str,
    *,
    spend: str = "10.00",
    sales_14d: str = "25.00",
) -> dict:
    return {
        "date": report_date,
        "campaignId": campaign_id,
        "campaignName": f"Campaign {campaign_id}",
        "campaignStatus": "ENABLED",
        "campaignBudgetAmount": "50.00",
        "campaignBudgetType": "DAILY_BUDGET",
        "campaignBiddingStrategy": "LEGACY_FOR_SALES",
        "impressions": 100,
        "clicks": 5,
        "cost": spend,
        "spend": spend,
        "sales1d": "20.00",
        "sales7d": sales_14d,
        "sales14d": sales_14d,
        "purchases1d": 1,
        "purchases7d": 2,
        "purchases14d": 2,
        "unitsSoldClicks1d": 1,
        "unitsSoldClicks7d": 2,
        "unitsSoldClicks14d": 2,
    }


def test_numeric_campaign_id_is_normalized():
    from amazon_data_core.sync_ads import normalize_campaign_row

    normalized = normalize_campaign_row(
        campaign_row(123456789, "2026-09-02"),
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 2),
        attribution_window_days=14,
    )

    assert normalized["campaign_id"] == "123456789"


class FakeAdsClient:
    def __init__(self, report_id: str, rows: list[dict], statuses=None) -> None:
        self.report_id = report_id
        self.rows = rows
        self.statuses = list(statuses or ["COMPLETED"])
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
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        response = {
            "reportId": report_id,
            "status": status,
            "createdAt": "2026-09-04T12:00:00Z",
        }
        if status == "COMPLETED":
            response.update(
                {
                    "completedAt": "2026-09-04T12:01:00Z",
                    "url": "https://reports.example/report.gz",
                }
            )
        return response

    def download_report(self, url: str) -> list[dict]:
        assert url.startswith("https://")
        return self.rows


def config(store_id: str, *, timeout: int = 10) -> AdsCampaignSyncConfig:
    return AdsCampaignSyncConfig(
        store_id=store_id,
        marketplace=MARKETPLACE,
        profile_id="profile-1",
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 3),
        region="NA",
        timezone="America/Los_Angeles",
        currency="USD",
        poll_timeout_seconds=timeout,
        poll_interval_seconds=0,
    )


def test_ads_report_is_versioned_idempotent_and_queryable(isolated_store):
    store_id = isolated_store
    first_client = FakeAdsClient(
        "report-1",
        [
            campaign_row("C1", "2026-09-02"),
            campaign_row("C2", "2026-09-02", spend="5.00", sales_14d="10.00"),
            campaign_row("C1", "2026-09-03", spend="8.00", sales_14d="16.00"),
        ],
        statuses=["PROCESSING", "COMPLETED"],
    )
    second_client = FakeAdsClient(
        "report-2",
        [
            campaign_row("C1", "2026-09-02", spend="11.00", sales_14d="30.00"),
            campaign_row("C1", "2026-09-03", spend="8.00", sales_14d="16.00"),
        ],
    )

    with connect() as conn:
        first = sync_ads_campaigns(conn, first_client, config(store_id))
        second = sync_ads_campaigns(conn, second_client, config(store_id))
        rows = conn.execute(
            """SELECT report_date, campaign_id, spend, active
               FROM amazon_ads_campaign_daily
               WHERE store_id=%s ORDER BY report_date, campaign_id""",
            (store_id,),
        ).fetchall()
        raw_count = conn.execute(
            """SELECT COUNT(*) AS n FROM amazon_ads_campaign_raw r
               JOIN amazon_sync_attempts a ON a.id=r.sync_attempt_id
               WHERE a.store_id=%s""",
            (store_id,),
        ).fetchone()["n"]
        run_checks(conn)
        conn.commit()

    summary = get_ads_campaign_summary(
        store_id, "2026-09-02", "2026-09-03", MARKETPLACE
    )

    assert first["status"] == "completed"
    assert first["rows_inserted"] == 3
    assert second["status"] == "completed"
    assert second["rows_updated"] == 1
    assert second["rows_skipped"] == 1
    assert raw_count == 5
    assert [
        (row["report_date"].isoformat(), row["campaign_id"], row["spend"], row["active"])
        for row in rows
    ] == [
        ("2026-09-02", "C1", Decimal("11.0000"), True),
        ("2026-09-02", "C2", Decimal("5.0000"), False),
        ("2026-09-03", "C1", Decimal("8.0000"), True),
    ]
    assert summary["safe_to_analyze"] is True
    assert summary["verified_coverage"]["complete"] is True
    assert summary["campaign_day_count"] == 2
    assert summary["campaign_count"] == 1
    assert summary["spend"] == "19.0000"
    assert summary["attributed_sales_14d"] == "46.0000"
    assert summary["dataset_state"][0]["normalized_count"] == 2
    assert summary["dataset_state"][0]["duplicate_count"] == 0
    assert "recent_attribution_metrics_can_still_change" in summary["warnings"]


def test_partial_report_does_not_deactivate_previous_campaigns(isolated_store):
    store_id = isolated_store
    with connect() as conn:
        sync_ads_campaigns(
            conn,
            FakeAdsClient(
                "report-good",
                [campaign_row("C1", "2026-09-02"), campaign_row("C2", "2026-09-02")],
            ),
            config(store_id),
        )
        invalid = campaign_row("C1", "2026-09-02")
        invalid["clicks"] = -1
        result = sync_ads_campaigns(
            conn,
            FakeAdsClient("report-bad", [invalid]),
            config(store_id),
        )
        rows = conn.execute(
            """SELECT campaign_id, active FROM amazon_ads_campaign_daily
               WHERE store_id=%s ORDER BY campaign_id""",
            (store_id,),
        ).fetchall()

    assert result["status"] == "partial"
    assert result["rows_errored"] == 1
    assert [(row["campaign_id"], row["active"]) for row in rows] == [
        ("C1", True),
        ("C2", True),
    ]


def test_parser_upgrade_reprocesses_same_completed_report(
    isolated_store, monkeypatch
):
    store_id = isolated_store
    invalid = campaign_row(123456789, "2026-09-02")
    invalid["campaignId"] = 1.5
    first_client = FakeAdsClient("report-reprocess", [invalid])

    with connect() as conn:
        first = sync_ads_campaigns(conn, first_client, config(store_id))
        monkeypatch.setattr(
            sync_ads_module,
            "FORMULA_VERSION",
            "amazon-ads-sp-campaigns-v3-canonical-test-upgrade",
        )
        resumed_client = FakeAdsClient(
            "must-not-be-created",
            [campaign_row(123456789, "2026-09-02")],
        )
        second = sync_ads_campaigns(conn, resumed_client, config(store_id))
        attempt_count = conn.execute(
            "SELECT COUNT(*) AS n FROM amazon_sync_attempts WHERE store_id=%s",
            (store_id,),
        ).fetchone()["n"]
        report_count = conn.execute(
            "SELECT COUNT(*) AS n FROM amazon_ads_reports WHERE store_id=%s",
            (store_id,),
        ).fetchone()["n"]
        run_count = conn.execute(
            "SELECT COUNT(*) AS n FROM dataset_runs WHERE store_id=%s",
            (store_id,),
        ).fetchone()["n"]

    assert first["status"] == "partial"
    assert second["status"] == "completed"
    assert second["rows_inserted"] == 1
    assert resumed_client.created == []
    assert resumed_client.polled == ["report-reprocess"]
    assert attempt_count == 1
    assert report_count == 1
    assert run_count == 2


def test_pending_report_resumes_without_creating_a_second_report(isolated_store):
    store_id = isolated_store
    pending = FakeAdsClient("report-resume", [campaign_row("C1", "2026-09-02")])
    pending.statuses = ["PROCESSING"]
    with connect() as conn:
        with pytest.raises(AdsReportPending):
            sync_ads_campaigns(conn, pending, config(store_id, timeout=0))
        resumed = FakeAdsClient(
            "must-not-be-created",
            [campaign_row("C1", "2026-09-02")],
            statuses=["COMPLETED"],
        )
        result = sync_ads_campaigns(conn, resumed, config(store_id))
        attempts = conn.execute(
            "SELECT COUNT(*) AS n FROM amazon_sync_attempts WHERE store_id=%s",
            (store_id,),
        ).fetchone()["n"]

    assert result["status"] == "completed"
    assert len(pending.created) == 1
    assert resumed.created == []
    assert resumed.polled == ["report-resume"]
    assert attempts == 1
