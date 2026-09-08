from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping

from psycopg import Connection

from .engine import health_summary


DATASET_DEFINITIONS = (
    {
        "id": "orders",
        "label": "订单",
        "aliases": ("orders",),
        "optional": False,
        "credential_group": "sp_api",
        "command": "amazon-data-core sync-orders",
    },
    {
        "id": "fba_inventory",
        "label": "FBA 库存",
        "aliases": ("fba_inventory", "inventory"),
        "optional": False,
        "credential_group": "sp_api",
        "command": "amazon-data-core sync-inventory",
    },
    {
        "id": "settlement_periods",
        "label": "已闭账结算",
        "aliases": ("settlement_periods", "settlements"),
        "optional": False,
        "credential_group": "sp_api",
        "command": "amazon-data-core sync-settlements",
    },
    {
        "id": "ads_campaign_daily",
        "label": "广告 Campaign",
        "aliases": ("ads_campaign_daily", "ads"),
        "optional": True,
        "credential_group": "ads",
        "command": "amazon-data-core sync-ads-campaigns",
    },
    {
        "id": "ads_search_term_daily",
        "label": "广告搜索词",
        "aliases": ("ads_search_term_daily",),
        "optional": True,
        "credential_group": "ads",
        "command": "amazon-data-core sync-ads-search-terms",
    },
    {
        "id": "ads_purchased_product_daily",
        "label": "广告购买商品",
        "aliases": ("ads_purchased_product_daily",),
        "optional": True,
        "credential_group": "ads",
        "command": "amazon-data-core sync-ads-purchased-products",
    },
)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _latest_for_aliases(
    rows: Iterable[Mapping[str, Any]], aliases: tuple[str, ...]
) -> Mapping[str, Any] | None:
    matches = [row for row in rows if row.get("dataset") in aliases]
    if not matches:
        return None
    return max(
        matches,
        key=lambda row: str(
            _json_value(row.get("updated_at") or row.get("fetched_at") or "")
        ),
    )


def _recovery(
    status: str,
    credential_group: str,
    command: str,
    attempt: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    if status == "needs_setup":
        return {
            "message": "先完成只在本机进行的 Amazon 授权配置。",
            "command": "./scripts/onboard.sh",
        }
    if status == "attention":
        retry_command = f"docker compose exec -T core {command}"
        error_type = str(attempt.get("error_type") or "") if attempt else ""
        if error_type == "AdsReportPending":
            start_date = _json_value(attempt.get("report_start_date"))
            end_date = _json_value(attempt.get("report_end_date"))
            if start_date and end_date:
                retry_command += (
                    f" --start-date {start_date} --end-date {end_date}"
                )
            return {
                "message": "Amazon 仍在生成该广告报表；稍后重试会续传同一任务。",
                "command": retry_command,
            }
        return {
            "message": "查看不含凭证的失败日志，修复授权、角色或网络问题后重试。",
            "command": retry_command,
        }
    if status == "optional" and credential_group == "ads":
        return {
            "message": "广告数据是可选项；需要时重新打开本机配置。",
            "command": "python3 scripts/onboard_background.py launch",
        }
    return None


def _dataset_card(
    definition: Mapping[str, Any],
    attempts: Iterable[Mapping[str, Any]],
    current_states: Iterable[Mapping[str, Any]],
    *,
    sp_api_configured: bool,
    ads_configured: bool,
) -> dict[str, Any]:
    aliases = tuple(definition["aliases"])
    attempt = _latest_for_aliases(attempts, aliases)
    current = _latest_for_aliases(current_states, aliases)
    credential_group = str(definition["credential_group"])
    configured = sp_api_configured if credential_group == "sp_api" else ads_configured
    attempt_status = str(attempt.get("status")) if attempt else None

    if attempt_status in {"running", "finalizing"}:
        status = "syncing"
    elif attempt_status in {"failed", "partial"}:
        status = "attention"
    elif attempt_status == "completed" or (
        current and current.get("ingestion_status") == "complete"
    ):
        status = "ready"
    elif current and current.get("ingestion_status") == "partial":
        status = "attention"
    elif not configured:
        status = "optional" if definition["optional"] else "needs_setup"
    else:
        status = "waiting"

    card = {
        "id": definition["id"],
        "label": definition["label"],
        "optional": definition["optional"],
        "configured": configured,
        "status": status,
        "data_available": current is not None,
        "attempt": None,
        "current": None,
        "recovery": _recovery(
            status,
            credential_group,
            str(definition["command"]),
            attempt,
        ),
    }
    if attempt:
        card["attempt"] = {
            key: _json_value(attempt.get(key))
            for key in (
                "status",
                "pages_completed",
                "rows_pulled",
                "rows_inserted",
                "rows_updated",
                "rows_skipped",
                "rows_errored",
                "error_type",
                "report_start_date",
                "report_end_date",
                "updated_at",
            )
        }
    if current:
        card["current"] = {
            key: _json_value(current.get(key))
            for key in (
                "business_date",
                "fetched_at",
                "source_updated_at",
                "ingestion_status",
                "source_count",
                "normalized_count",
                "duplicate_count",
                "error_count",
            )
        }
    return card


def build_sync_overview(
    attempts: Iterable[Mapping[str, Any]],
    current_states: Iterable[Mapping[str, Any]],
    health: Mapping[str, Any],
    *,
    sp_api_configured: bool,
    ads_configured: bool,
    demo_mode: bool = False,
) -> dict[str, Any]:
    attempts = list(attempts)
    current_states = list(current_states)
    datasets = [
        _dataset_card(
            definition,
            attempts,
            current_states,
            sp_api_configured=sp_api_configured or demo_mode,
            ads_configured=ads_configured,
        )
        for definition in DATASET_DEFINITIONS
    ]
    active_datasets = [card for card in datasets if not card["optional"] or card["configured"]]
    required_datasets = [card for card in datasets if not card["optional"]]
    tone = str(health.get("tone", "unknown"))

    required_ready = all(card["status"] == "ready" for card in required_datasets)

    if any(card["status"] == "syncing" for card in active_datasets):
        overall_status = "syncing"
        headline = "正在同步真实店铺数据"
    elif any(card["status"] == "attention" for card in active_datasets):
        overall_status = "attention"
        headline = "有同步步骤需要处理"
    elif required_ready and tone == "clear":
        overall_status = "complete"
        headline = "数据已同步并通过检查"
    elif required_ready and tone in {"critical", "warning"}:
        overall_status = "attention"
        headline = str(health.get("headline") or "数据检查需要处理")
    elif required_ready:
        overall_status = "checking"
        headline = "数据已到达，等待质量检查"
    elif not sp_api_configured and not demo_mode:
        overall_status = "needs_setup"
        headline = "等待完成 Amazon 授权配置"
    else:
        overall_status = "waiting"
        headline = "等待首次同步"

    completed = sum(card["status"] == "ready" for card in active_datasets)
    progress = round((completed / len(active_datasets)) * 100) if active_datasets else 0
    safe_to_analyze = bool(
        overall_status == "complete"
        and required_ready
        and tone == "clear"
    )
    return {
        "schema_version": 1,
        "overall": {
            "status": overall_status,
            "headline": headline,
            "progress_percent": progress,
            "safe_to_analyze": safe_to_analyze,
            "demo_mode": demo_mode,
        },
        "configuration": {
            "sp_api_configured": sp_api_configured,
            "ads_configured": ads_configured,
        },
        "datasets": datasets,
        "quality": dict(health),
    }


def sync_overview_from_database(
    conn: Connection,
    *,
    sp_api_configured: bool,
    ads_configured: bool,
    demo_mode: bool = False,
) -> dict[str, Any]:
    attempts = conn.execute(
        """SELECT DISTINCT ON (a.dataset)
                  a.dataset, a.status, a.pages_completed, a.rows_pulled,
                  a.rows_inserted, a.rows_updated, a.rows_skipped,
                  a.rows_errored, a.error_type, a.updated_at,
                  r.start_date AS report_start_date,
                  r.end_date AS report_end_date
             FROM amazon_sync_attempts a
             LEFT JOIN amazon_ads_reports r ON r.sync_attempt_id=a.id
            ORDER BY a.dataset, a.updated_at DESC, a.created_at DESC"""
    ).fetchall()
    current_states = conn.execute(
        """SELECT DISTINCT ON (dataset)
                  dataset, business_date, fetched_at, source_updated_at,
                  ingestion_status, source_count, normalized_count,
                  duplicate_count, error_count, updated_at
             FROM current_dataset_state
            ORDER BY dataset, updated_at DESC"""
    ).fetchall()
    return build_sync_overview(
        attempts,
        current_states,
        health_summary(conn),
        sp_api_configured=sp_api_configured,
        ads_configured=ads_configured,
        demo_mode=demo_mode,
    )
