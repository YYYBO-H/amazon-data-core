from datetime import datetime, timezone

from amazon_data_core.sync_status import build_sync_overview


EMPTY_HEALTH = {
    "tone": "unknown",
    "headline": "还没有数据健康结果",
    "expected_checks": 0,
    "checked": 0,
    "passed": 0,
    "failed": 0,
    "skipped": 0,
    "error": 0,
    "open_issues": 0,
    "recovered_historical": 0,
    "last_checked_at": None,
}


def test_unconfigured_core_returns_actionable_setup_state():
    result = build_sync_overview(
        [], [], EMPTY_HEALTH, sp_api_configured=False, ads_configured=False
    )

    assert result["overall"]["status"] == "needs_setup"
    assert result["overall"]["safe_to_analyze"] is False
    assert result["datasets"][0]["recovery"]["command"] == "./scripts/onboard.sh"
    assert result["datasets"][3]["status"] == "optional"


def test_running_attempt_exposes_real_counts_without_credentials():
    now = datetime.now(timezone.utc)
    result = build_sync_overview(
        [
            {
                "dataset": "orders",
                "status": "running",
                "pages_completed": 3,
                "rows_pulled": 240,
                "rows_inserted": 220,
                "rows_updated": 20,
                "rows_skipped": 0,
                "rows_errored": 0,
                "error_type": None,
                "updated_at": now,
            }
        ],
        [],
        EMPTY_HEALTH,
        sp_api_configured=True,
        ads_configured=False,
    )

    orders = result["datasets"][0]
    assert result["overall"]["status"] == "syncing"
    assert orders["status"] == "syncing"
    assert orders["attempt"]["pages_completed"] == 3
    assert orders["attempt"]["rows_pulled"] == 240
    assert orders["attempt"]["updated_at"] == now.isoformat()


def test_required_datasets_and_clear_health_are_safe_to_analyze():
    now = datetime.now(timezone.utc)
    current = [
        {
            "dataset": dataset,
            "ingestion_status": "complete",
            "source_count": 10,
            "normalized_count": 10,
            "duplicate_count": 0,
            "error_count": 0,
            "fetched_at": now,
            "updated_at": now,
        }
        for dataset in ("orders", "fba_inventory", "settlement_periods")
    ]
    health = {**EMPTY_HEALTH, "tone": "clear", "headline": "经营数据可以正常使用"}
    result = build_sync_overview(
        [], current, health, sp_api_configured=True, ads_configured=False
    )

    assert result["overall"]["status"] == "complete"
    assert result["overall"]["safe_to_analyze"] is True


def test_latest_failure_is_visible_even_when_previous_data_remains_available():
    now = datetime.now(timezone.utc)
    result = build_sync_overview(
        [{"dataset": "orders", "status": "failed", "error_type": "SPAPIError", "updated_at": now}],
        [{"dataset": "orders", "ingestion_status": "complete", "fetched_at": now, "updated_at": now}],
        EMPTY_HEALTH,
        sp_api_configured=True,
        ads_configured=False,
    )

    orders = result["datasets"][0]
    assert orders["status"] == "attention"
    assert orders["data_available"] is True
    assert orders["recovery"]["command"] == (
        "docker compose exec -T core amazon-data-core sync-orders"
    )


def test_pending_ads_report_has_resume_guidance_for_the_same_window():
    now = datetime.now(timezone.utc)
    result = build_sync_overview(
        [
            {
                "dataset": "ads_campaign_daily",
                "status": "failed",
                "error_type": "AdsReportPending",
                "report_start_date": "2026-09-07",
                "report_end_date": "2026-09-07",
                "updated_at": now,
            }
        ],
        [],
        EMPTY_HEALTH,
        sp_api_configured=True,
        ads_configured=True,
    )

    campaign = result["datasets"][3]
    assert campaign["status"] == "attention"
    assert campaign["recovery"] == {
        "message": "Amazon 仍在生成该广告报表；稍后重试会续传同一任务。",
        "command": (
            "docker compose exec -T core amazon-data-core sync-ads-campaigns "
            "--start-date 2026-09-07 --end-date 2026-09-07"
        ),
    }


def test_removing_credentials_does_not_invalidate_healthy_local_data():
    now = datetime.now(timezone.utc)
    current = [
        {
            "dataset": dataset,
            "ingestion_status": "complete",
            "source_count": 1,
            "normalized_count": 1,
            "fetched_at": now,
            "updated_at": now,
        }
        for dataset in ("orders", "fba_inventory", "settlement_periods")
    ]
    health = {**EMPTY_HEALTH, "tone": "clear", "headline": "经营数据可以正常使用"}
    result = build_sync_overview(
        [], current, health, sp_api_configured=False, ads_configured=False
    )

    assert result["overall"]["status"] == "complete"
    assert result["overall"]["safe_to_analyze"] is True
    assert result["configuration"]["sp_api_configured"] is False
