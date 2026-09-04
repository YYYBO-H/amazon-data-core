from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from .db import connect
from .engine import health_summary


def json_safe(value: Any) -> Any:
    """Convert database values into values every Agent host can serialize."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def get_data_health() -> dict[str, Any]:
    with connect() as conn:
        return json_safe(health_summary(conn))


def list_dataset_status(
    store_id: str | None = None,
    dataset: str | None = None,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[str] = []
    if store_id:
        filters.append("c.store_id = %s")
        params.append(store_id)
    if dataset:
        filters.append("c.dataset = %s")
        params.append(dataset)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT
                    c.source, c.store_id, c.marketplace, c.dataset,
                    c.business_date, c.fetched_at, c.source_updated_at,
                    c.ingestion_status, c.source_count, c.normalized_count,
                    c.duplicate_count, c.error_count,
                    r.timezone, r.currency, r.raw_reference,
                    r.schema_version, r.formula_version, r.is_provisional
                FROM current_dataset_state c
                JOIN dataset_runs r ON r.id = c.run_id
                {where}
                ORDER BY c.store_id, c.marketplace, c.dataset""",
            params,
        ).fetchall()
    return json_safe([dict(row) for row in rows])


def list_open_data_issues(
    store_id: str | None = None,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[str] = []
    if store_id:
        filters.append("store_id = %s")
        params.append(store_id)
    if severity:
        filters.append("severity = %s")
        params.append(severity)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT rule_code, source, store_id, marketplace, dataset,
                       target_date, check_status, severity, details, evaluated_at
                FROM v_open_issues
                {where}
                ORDER BY evaluated_at DESC""",
            params,
        ).fetchall()
    return json_safe([dict(row) for row in rows])


def get_orders_summary(
    store_id: str,
    business_date: str,
    marketplace: str | None = None,
) -> dict[str, Any]:
    """Return local order facts plus the data state needed to qualify them."""
    try:
        target_date = date.fromisoformat(business_date)
    except ValueError as exc:
        raise ValueError("business_date must use YYYY-MM-DD") from exc
    filters = ["o.store_id = %s", "(o.created_time AT TIME ZONE s.timezone)::date = %s"]
    params: list[Any] = [store_id, target_date]
    if marketplace:
        filters.append("o.marketplace = %s")
        params.append(marketplace)
    where = " AND ".join(filters)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT o.marketplace, o.fulfillment_status, o.currency,
                       COUNT(*) AS order_count,
                       COALESCE(SUM(o.item_count), 0) AS item_count,
                       COUNT(o.proceeds_total_amount) AS orders_with_proceeds,
                       SUM(o.proceeds_total_amount) AS proceeds_total
                FROM amazon_orders o
                JOIN amazon_store_connections s
                  ON s.store_id=o.store_id AND s.marketplace=o.marketplace
                WHERE {where}
                GROUP BY o.marketplace, o.fulfillment_status, o.currency
                ORDER BY o.marketplace, o.fulfillment_status, o.currency""",
            params,
        ).fetchall()
        state_params: list[Any] = [store_id]
        state_marketplace = ""
        if marketplace:
            state_marketplace = "AND c.marketplace = %s"
            state_params.append(marketplace)
        states = conn.execute(
            f"""SELECT c.marketplace, c.ingestion_status, c.business_date,
                       c.source_count, c.normalized_count, c.duplicate_count,
                       c.error_count, c.source_updated_at,
                       r.fetched_at, r.raw_reference, r.schema_version
                FROM current_dataset_state c
                JOIN dataset_runs r ON r.id=c.run_id
                WHERE c.source='amazon_sp_api_orders_v2026'
                  AND c.store_id=%s AND c.dataset='orders'
                  {state_marketplace}
                ORDER BY c.marketplace""",
            state_params,
        ).fetchall()
        issue_count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM v_open_issues
                WHERE source='amazon_sp_api_orders_v2026'
                  AND store_id=%s AND dataset='orders'
                  {state_marketplace.replace('c.marketplace', 'marketplace')}""",
            state_params,
        ).fetchone()["n"]
        evaluated_check_count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM v_latest_checks
                WHERE source='amazon_sp_api_orders_v2026'
                  AND store_id=%s AND dataset='orders'
                  {state_marketplace.replace('c.marketplace', 'marketplace')}""",
            state_params,
        ).fetchone()["n"]
        coverage_rows = conn.execute(
            f"""SELECT s.marketplace, s.timezone,
                       MIN(a.window_start) FILTER (
                           WHERE a.status='completed'
                       ) AS coverage_start,
                       c.cursor_value AS coverage_end
                FROM amazon_store_connections s
                LEFT JOIN amazon_sync_attempts a
                  ON a.source='amazon_sp_api_orders_v2026'
                 AND a.store_id=s.store_id
                 AND a.marketplace=s.marketplace
                 AND a.dataset='orders'
                LEFT JOIN amazon_sync_cursors c
                  ON c.source='amazon_sp_api_orders_v2026'
                 AND c.store_id=s.store_id
                 AND c.marketplace=s.marketplace
                 AND c.dataset='orders'
                WHERE s.store_id=%s
                  {state_marketplace.replace('c.marketplace', 's.marketplace')}
                GROUP BY s.marketplace, s.timezone, c.cursor_value
                ORDER BY s.marketplace""",
            state_params,
        ).fetchall()

    total_orders = int(sum(row["order_count"] for row in rows))
    total_items = int(sum(row["item_count"] for row in rows))
    status_counts: dict[str, int] = {}
    totals: dict[str, Decimal] = {}
    orders_with_proceeds = 0
    for row in rows:
        status = row["fulfillment_status"] or "UNKNOWN"
        status_counts[status] = status_counts.get(status, 0) + row["order_count"]
        orders_with_proceeds += row["orders_with_proceeds"]
        if row["currency"] and row["proceeds_total"] is not None:
            code = row["currency"].strip()
            totals[code] = totals.get(code, Decimal("0")) + row["proceeds_total"]
    state_list = [dict(row) for row in states]
    coverage: list[dict[str, Any]] = []
    for row in coverage_rows:
        timezone = ZoneInfo(row["timezone"])
        requested_start = datetime.combine(target_date, time.min, timezone).astimezone(UTC)
        requested_end = datetime.combine(
            target_date + timedelta(days=1), time.min, timezone
        ).astimezone(UTC)
        covers_requested_date = bool(
            row["coverage_start"]
            and row["coverage_end"]
            and row["coverage_start"] <= requested_start
            and row["coverage_end"] >= requested_end
        )
        coverage.append(
            {
                **dict(row),
                "requested_start": requested_start,
                "requested_end": requested_end,
                "covers_requested_date": covers_requested_date,
            }
        )
    expected_check_count = len(state_list) * 4
    coverage_verified = bool(coverage) and all(
        row["covers_requested_date"] for row in coverage
    )
    safe_to_analyze = (
        bool(state_list)
        and coverage_verified
        and evaluated_check_count >= expected_check_count
        and issue_count == 0
        and all(row["ingestion_status"] == "complete" for row in state_list)
    )
    warnings: list[str] = []
    if not state_list:
        warnings.append("no_current_orders_dataset_state")
    if issue_count:
        warnings.append("open_data_quality_issues")
    if evaluated_check_count < expected_check_count:
        warnings.append("data_quality_checks_not_run")
    if not coverage_verified:
        warnings.append("requested_date_outside_verified_coverage")
    if orders_with_proceeds < total_orders:
        warnings.append("some_order_proceeds_not_available")
    return json_safe(
        {
            "store_id": store_id,
            "marketplace": marketplace,
            "business_date": target_date,
            "safe_to_analyze": safe_to_analyze,
            "open_issue_count": issue_count,
            "evaluated_check_count": evaluated_check_count,
            "order_count": total_orders,
            "item_count": total_items,
            "orders_with_proceeds": orders_with_proceeds,
            "proceeds_by_currency": totals,
            "proceeds_semantics": (
                "Amazon Orders proceeds.grandTotal; not profit or settlement payout"
            ),
            "orders_by_fulfillment_status": status_counts,
            "dataset_state": state_list,
            "verified_coverage": coverage,
            "count_semantics": "dataset_state counts describe the latest sync batch",
            "warnings": warnings,
        }
    )


def get_fba_inventory_status(
    store_id: str,
    marketplace: str | None = None,
    max_fulfillable: int = 10,
    limit: int = 100,
) -> dict[str, Any]:
    """Return the latest complete FBA snapshot and low-stock records."""
    if max_fulfillable < 0:
        raise ValueError("max_fulfillable cannot be negative")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    inventory_filters = ["store_id=%s", "active=TRUE"]
    params: list[Any] = [store_id]
    if marketplace:
        inventory_filters.append("marketplace=%s")
        params.append(marketplace)
    where = " AND ".join(inventory_filters)
    with connect() as conn:
        aggregate = conn.execute(
            f"""SELECT COUNT(*) AS inventory_record_count,
                       COUNT(DISTINCT asin) AS asin_count,
                       COUNT(DISTINCT seller_sku) AS seller_sku_count,
                       COUNT(*) FILTER (
                           WHERE fulfillable_quantity=0
                       ) AS zero_fulfillable_records,
                       COALESCE(SUM(total_quantity), 0) AS total_quantity,
                       COALESCE(SUM(fulfillable_quantity), 0) AS fulfillable_quantity,
                       COALESCE(SUM(inbound_working_quantity), 0)
                           AS inbound_working_quantity,
                       COALESCE(SUM(inbound_shipped_quantity), 0)
                           AS inbound_shipped_quantity,
                       COALESCE(SUM(inbound_receiving_quantity), 0)
                           AS inbound_receiving_quantity,
                       COALESCE(SUM(reserved_quantity), 0) AS reserved_quantity,
                       COALESCE(SUM(researching_quantity), 0) AS researching_quantity,
                       COALESCE(SUM(unfulfillable_quantity), 0)
                           AS unfulfillable_quantity
                FROM amazon_fba_inventory WHERE {where}""",
            params,
        ).fetchone()
        low_stock = conn.execute(
            f"""SELECT marketplace, asin, seller_sku, fn_sku, item_condition,
                       fulfillable_quantity, inbound_working_quantity,
                       inbound_shipped_quantity, inbound_receiving_quantity,
                       reserved_quantity, researching_quantity,
                       unfulfillable_quantity, total_quantity,
                       source_updated_at, last_seen_at
                FROM amazon_fba_inventory
                WHERE {where} AND fulfillable_quantity <= %s
                ORDER BY fulfillable_quantity, total_quantity, seller_sku
                LIMIT %s""",
            [*params, max_fulfillable, limit],
        ).fetchall()
        state_params: list[Any] = [store_id]
        marketplace_filter = ""
        if marketplace:
            marketplace_filter = "AND c.marketplace=%s"
            state_params.append(marketplace)
        states = conn.execute(
            f"""SELECT c.marketplace, c.ingestion_status, c.business_date,
                       c.source_count, c.normalized_count, c.duplicate_count,
                       c.error_count, c.source_updated_at,
                       r.fetched_at, r.raw_reference, r.schema_version,
                       r.metadata
                FROM current_dataset_state c
                JOIN dataset_runs r ON r.id=c.run_id
                WHERE c.source='amazon_sp_api_fba_inventory_v1'
                  AND c.store_id=%s AND c.dataset='fba_inventory'
                  {marketplace_filter}
                ORDER BY c.marketplace""",
            state_params,
        ).fetchall()
        issue_count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM v_open_issues
                WHERE source='amazon_sp_api_fba_inventory_v1'
                  AND store_id=%s AND dataset='fba_inventory'
                  {marketplace_filter.replace('c.marketplace', 'marketplace')}""",
            state_params,
        ).fetchone()["n"]
        evaluated_check_count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM v_latest_checks
                WHERE source='amazon_sp_api_fba_inventory_v1'
                  AND store_id=%s AND dataset='fba_inventory'
                  {marketplace_filter.replace('c.marketplace', 'marketplace')}""",
            state_params,
        ).fetchone()["n"]
        duplicate_asins = conn.execute(
            f"""SELECT COUNT(*) AS n FROM (
                    SELECT marketplace, asin
                    FROM amazon_fba_inventory
                    WHERE {where}
                    GROUP BY marketplace, asin HAVING COUNT(*) > 1
                ) duplicates""",
            params,
        ).fetchone()["n"]

    state_list = [dict(row) for row in states]
    expected_check_count = len(state_list) * 4
    safe_to_analyze = (
        bool(state_list)
        and evaluated_check_count >= expected_check_count
        and issue_count == 0
        and all(row["ingestion_status"] == "complete" for row in state_list)
    )
    warnings = ["fba_only_mfn_inventory_not_included"]
    if not state_list:
        warnings.append("no_current_fba_inventory_dataset_state")
    if evaluated_check_count < expected_check_count:
        warnings.append("data_quality_checks_not_run")
    if issue_count:
        warnings.append("open_data_quality_issues")
    if duplicate_asins:
        warnings.append("some_asins_have_multiple_inventory_records")
    aggregate_dict = {
        key: int(value) if value is not None else 0
        for key, value in dict(aggregate).items()
    }
    return json_safe(
        {
            "store_id": store_id,
            "marketplace": marketplace,
            "scope": "FBA inventory only",
            "safe_to_analyze": safe_to_analyze,
            "open_issue_count": issue_count,
            "evaluated_check_count": evaluated_check_count,
            **aggregate_dict,
            "asins_with_multiple_records": duplicate_asins,
            "low_stock_threshold": max_fulfillable,
            "low_stock_records": [dict(row) for row in low_stock],
            "low_stock_result_limited": len(low_stock) == limit,
            "dataset_state": state_list,
            "warnings": warnings,
        }
    )


def get_ads_campaign_summary(
    store_id: str,
    start_date: str,
    end_date: str,
    marketplace: str | None = None,
) -> dict[str, Any]:
    """Return verified Sponsored Products campaign facts for a date range."""
    try:
        requested_start = date.fromisoformat(start_date)
        requested_end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("start_date and end_date must use YYYY-MM-DD") from exc
    if requested_start > requested_end:
        raise ValueError("start_date cannot be after end_date")
    if (requested_end - requested_start).days > 366:
        raise ValueError("requested date range cannot exceed 367 days")
    filters = [
        "c.store_id=%s",
        "c.report_date BETWEEN %s AND %s",
        "c.active=TRUE",
    ]
    params: list[Any] = [store_id, requested_start, requested_end]
    if marketplace:
        filters.append("c.marketplace=%s")
        params.append(marketplace)
    where = " AND ".join(filters)
    with connect() as conn:
        aggregate = conn.execute(
            f"""SELECT COUNT(*) AS campaign_day_count,
                       COUNT(DISTINCT c.campaign_id) AS campaign_count,
                       COUNT(DISTINCT c.report_date) AS dates_with_activity,
                       COALESCE(SUM(c.impressions), 0) AS impressions,
                       COALESCE(SUM(c.clicks), 0) AS clicks,
                       COALESCE(SUM(c.spend), 0) AS spend,
                       COALESCE(SUM(c.sales_1d), 0) AS attributed_sales_1d,
                       COALESCE(SUM(c.sales_7d), 0) AS attributed_sales_7d,
                       COALESCE(SUM(c.sales_14d), 0) AS attributed_sales_14d,
                       COALESCE(SUM(c.purchases_1d), 0) AS attributed_purchases_1d,
                       COALESCE(SUM(c.purchases_7d), 0) AS attributed_purchases_7d,
                       COALESCE(SUM(c.purchases_14d), 0) AS attributed_purchases_14d,
                       COUNT(*) FILTER (
                           WHERE c.provisional_until >=
                               (NOW() AT TIME ZONE s.timezone)::date
                       ) AS provisional_campaign_days,
                       MAX(c.provisional_until) AS latest_provisional_until,
                       MIN(c.currency) AS first_currency,
                       COUNT(DISTINCT c.currency) AS currency_count
                FROM amazon_ads_campaign_daily c
                JOIN amazon_store_connections s
                  ON s.store_id=c.store_id AND s.marketplace=c.marketplace
                WHERE {where}""",
            params,
        ).fetchone()
        daily = conn.execute(
            f"""SELECT c.report_date, c.currency,
                       COUNT(*) AS campaign_count,
                       SUM(c.impressions) AS impressions,
                       SUM(c.clicks) AS clicks,
                       SUM(c.spend) AS spend,
                       SUM(c.sales_14d) AS attributed_sales_14d,
                       SUM(c.purchases_14d) AS attributed_purchases_14d,
                       BOOL_OR(
                           c.provisional_until >=
                               (NOW() AT TIME ZONE s.timezone)::date
                       ) AS provisional
                FROM amazon_ads_campaign_daily c
                JOIN amazon_store_connections s
                  ON s.store_id=c.store_id AND s.marketplace=c.marketplace
                WHERE {where}
                GROUP BY c.report_date, c.currency
                ORDER BY c.report_date, c.currency""",
            params,
        ).fetchall()
        currency_totals = conn.execute(
            f"""SELECT c.currency,
                       SUM(c.spend) AS spend,
                       SUM(c.sales_1d) AS attributed_sales_1d,
                       SUM(c.sales_7d) AS attributed_sales_7d,
                       SUM(c.sales_14d) AS attributed_sales_14d
                FROM amazon_ads_campaign_daily c
                WHERE {where}
                GROUP BY c.currency ORDER BY c.currency""",
            params,
        ).fetchall()
        state_params: list[Any] = [store_id]
        marketplace_filter = ""
        if marketplace:
            marketplace_filter = "AND c.marketplace=%s"
            state_params.append(marketplace)
        states = conn.execute(
            f"""SELECT c.marketplace, c.ingestion_status, c.business_date,
                       c.source_count, c.normalized_count, c.duplicate_count,
                       c.error_count, c.source_updated_at,
                       r.fetched_at, r.raw_reference, r.schema_version,
                       r.formula_version, r.is_provisional, r.metadata
                FROM current_dataset_state c
                JOIN dataset_runs r ON r.id=c.run_id
                WHERE c.source='amazon_ads_reporting_v3_sp_campaigns'
                  AND c.store_id=%s AND c.dataset='ads_campaign_daily'
                  {marketplace_filter}
                ORDER BY c.marketplace""",
            state_params,
        ).fetchall()
        issue_count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM v_open_issues
                WHERE source='amazon_ads_reporting_v3_sp_campaigns'
                  AND store_id=%s AND dataset='ads_campaign_daily'
                  {marketplace_filter.replace('c.marketplace', 'marketplace')}""",
            state_params,
        ).fetchone()["n"]
        evaluated_check_count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM v_latest_checks
                WHERE source='amazon_ads_reporting_v3_sp_campaigns'
                  AND store_id=%s AND dataset='ads_campaign_daily'
                  {marketplace_filter.replace('c.marketplace', 'marketplace')}""",
            state_params,
        ).fetchone()["n"]
        coverage_params: list[Any] = [
            store_id,
            requested_start,
            requested_end,
        ]
        coverage_marketplace = ""
        if marketplace:
            coverage_marketplace = "AND r.marketplace=%s"
            coverage_params.append(marketplace)
        coverage_rows = conn.execute(
            f"""SELECT r.start_date, r.end_date, r.marketplace,
                       r.report_id, r.report_completed_at
                FROM amazon_ads_reports r
                JOIN amazon_sync_attempts a ON a.id=r.sync_attempt_id
                WHERE r.source='amazon_ads_reporting_v3_sp_campaigns'
                  AND r.store_id=%s AND a.status='completed'
                  AND r.end_date >= %s AND r.start_date <= %s
                  {coverage_marketplace}
                ORDER BY r.start_date, r.end_date""",
            coverage_params,
        ).fetchall()

    covered_dates_by_marketplace: dict[str, set[date]] = {}
    for row in coverage_rows:
        covered_dates = covered_dates_by_marketplace.setdefault(
            row["marketplace"], set()
        )
        current = max(row["start_date"], requested_start)
        last = min(row["end_date"], requested_end)
        while current <= last:
            covered_dates.add(current)
            current += timedelta(days=1)
    requested_dates = {
        requested_start + timedelta(days=offset)
        for offset in range((requested_end - requested_start).days + 1)
    }
    state_list = [dict(row) for row in states]
    expected_marketplaces = (
        [marketplace]
        if marketplace
        else [row["marketplace"] for row in state_list]
    )
    missing_dates_by_marketplace = {
        market: sorted(
            requested_dates - covered_dates_by_marketplace.get(market, set())
        )
        for market in expected_marketplaces
    }
    missing_dates = sorted(
        {
            missing
            for dates in missing_dates_by_marketplace.values()
            for missing in dates
        }
    )
    expected_check_count = len(state_list) * 4
    safe_to_analyze = (
        bool(state_list)
        and not missing_dates
        and evaluated_check_count >= expected_check_count
        and issue_count == 0
        and all(row["ingestion_status"] == "complete" for row in state_list)
        and aggregate["currency_count"] <= 1
    )
    single_currency = aggregate["currency_count"] <= 1
    spend = (aggregate["spend"] or Decimal("0")) if single_currency else None
    sales_14d = (
        (aggregate["attributed_sales_14d"] or Decimal("0"))
        if single_currency
        else None
    )
    acos_14d = spend / sales_14d if spend is not None and sales_14d else None
    roas_14d = sales_14d / spend if sales_14d is not None and spend else None
    warnings = [
        "attributed_sales_are_not_total_store_sales",
        "reporting_v3_requires_migration_before_legacy_reporting_sunset",
    ]
    if not state_list:
        warnings.append("no_current_ads_campaign_dataset_state")
    if missing_dates:
        warnings.append("requested_dates_outside_verified_coverage")
    if evaluated_check_count < expected_check_count:
        warnings.append("data_quality_checks_not_run")
    if issue_count:
        warnings.append("open_data_quality_issues")
    if aggregate["provisional_campaign_days"]:
        warnings.append("recent_attribution_metrics_can_still_change")
    if aggregate["currency_count"] > 1:
        warnings.append("multiple_currencies_not_aggregated_into_one_value")
    return json_safe(
        {
            "store_id": store_id,
            "marketplace": marketplace,
            "start_date": requested_start,
            "end_date": requested_end,
            "scope": "Sponsored Products campaign daily reporting",
            "reporting_api_version": "v3",
            "safe_to_analyze": safe_to_analyze,
            "open_issue_count": issue_count,
            "evaluated_check_count": evaluated_check_count,
            "verified_coverage": {
                "complete": not missing_dates,
                "missing_dates": missing_dates,
                "missing_dates_by_marketplace": missing_dates_by_marketplace,
                "completed_reports": [dict(row) for row in coverage_rows],
            },
            "campaign_day_count": int(aggregate["campaign_day_count"]),
            "campaign_count": int(aggregate["campaign_count"]),
            "dates_with_activity": int(aggregate["dates_with_activity"]),
            "impressions": int(aggregate["impressions"]),
            "clicks": int(aggregate["clicks"]),
            "spend": spend,
            "currency": aggregate["first_currency"] if single_currency else None,
            "monetary_totals_by_currency": [dict(row) for row in currency_totals],
            "attributed_sales_1d": (
                aggregate["attributed_sales_1d"] if single_currency else None
            ),
            "attributed_sales_7d": (
                aggregate["attributed_sales_7d"] if single_currency else None
            ),
            "attributed_sales_14d": sales_14d,
            "attributed_purchases_1d": int(
                aggregate["attributed_purchases_1d"]
            ),
            "attributed_purchases_7d": int(
                aggregate["attributed_purchases_7d"]
            ),
            "attributed_purchases_14d": int(
                aggregate["attributed_purchases_14d"]
            ),
            "acos_14d": acos_14d,
            "roas_14d": roas_14d,
            "provisional_campaign_days": int(
                aggregate["provisional_campaign_days"]
            ),
            "latest_provisional_until": aggregate["latest_provisional_until"],
            "daily": [dict(row) for row in daily],
            "dataset_state": state_list,
            "metric_semantics": (
                "Amazon Ads v3 spCampaigns traffic-date metrics; spend/cost and "
                "click-attributed sales14d, not store revenue or profit"
            ),
            "warnings": warnings,
        }
    )


def _parse_ads_date_range(start_date: str, end_date: str) -> tuple[date, date]:
    try:
        requested_start = date.fromisoformat(start_date)
        requested_end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("start_date and end_date must use YYYY-MM-DD") from exc
    if requested_start > requested_end:
        raise ValueError("start_date cannot be after end_date")
    if (requested_end - requested_start).days > 366:
        raise ValueError("requested date range cannot exceed 367 days")
    return requested_start, requested_end


def _ads_detail_readiness(
    conn,
    *,
    source: str,
    dataset: str,
    store_id: str,
    marketplace: str | None,
    requested_start: date,
    requested_end: date,
) -> dict[str, Any]:
    state_params: list[Any] = [source, store_id, dataset]
    state_marketplace = ""
    if marketplace:
        state_marketplace = "AND c.marketplace=%s"
        state_params.append(marketplace)
    states = conn.execute(
        f"""SELECT c.marketplace, c.ingestion_status, c.business_date,
                   c.source_count, c.normalized_count, c.duplicate_count,
                   c.error_count, c.source_updated_at,
                   r.fetched_at, r.raw_reference, r.schema_version,
                   r.formula_version, r.is_provisional, r.metadata
            FROM current_dataset_state c
            JOIN dataset_runs r ON r.id=c.run_id
            WHERE c.source=%s AND c.store_id=%s AND c.dataset=%s
              {state_marketplace}
            ORDER BY c.marketplace""",
        state_params,
    ).fetchall()
    issue_count = conn.execute(
        f"""SELECT COUNT(*) AS n FROM v_open_issues
            WHERE source=%s AND store_id=%s AND dataset=%s
              {state_marketplace.replace('c.marketplace', 'marketplace')}""",
        state_params,
    ).fetchone()["n"]
    evaluated_check_count = conn.execute(
        f"""SELECT COUNT(*) AS n FROM v_latest_checks
            WHERE source=%s AND store_id=%s AND dataset=%s
              {state_marketplace.replace('c.marketplace', 'marketplace')}""",
        state_params,
    ).fetchone()["n"]
    coverage_params: list[Any] = [
        source,
        store_id,
        requested_start,
        requested_end,
    ]
    coverage_marketplace = ""
    if marketplace:
        coverage_marketplace = "AND r.marketplace=%s"
        coverage_params.append(marketplace)
    reports = conn.execute(
        f"""SELECT r.start_date, r.end_date, r.marketplace,
                   r.report_id, r.report_completed_at
            FROM amazon_ads_reports r
            JOIN amazon_sync_attempts a ON a.id=r.sync_attempt_id
            WHERE r.source=%s AND r.store_id=%s AND a.status='completed'
              AND r.end_date >= %s AND r.start_date <= %s
              {coverage_marketplace}
            ORDER BY r.start_date, r.end_date""",
        coverage_params,
    ).fetchall()
    covered_dates_by_marketplace: dict[str, set[date]] = {}
    for row in reports:
        covered_dates = covered_dates_by_marketplace.setdefault(
            row["marketplace"], set()
        )
        current = max(row["start_date"], requested_start)
        last = min(row["end_date"], requested_end)
        while current <= last:
            covered_dates.add(current)
            current += timedelta(days=1)
    requested_dates = {
        requested_start + timedelta(days=offset)
        for offset in range((requested_end - requested_start).days + 1)
    }
    state_list = [dict(row) for row in states]
    expected_marketplaces = (
        [marketplace]
        if marketplace
        else [row["marketplace"] for row in state_list]
    )
    missing_dates_by_marketplace = {
        market: sorted(
            requested_dates - covered_dates_by_marketplace.get(market, set())
        )
        for market in expected_marketplaces
    }
    missing_dates = sorted(
        {
            missing
            for dates in missing_dates_by_marketplace.values()
            for missing in dates
        }
    )
    expected_check_count = len(state_list) * 4
    safe_to_analyze = (
        bool(state_list)
        and not missing_dates
        and evaluated_check_count >= expected_check_count
        and issue_count == 0
        and all(row["ingestion_status"] == "complete" for row in state_list)
    )
    return {
        "safe_to_analyze": safe_to_analyze,
        "open_issue_count": issue_count,
        "evaluated_check_count": evaluated_check_count,
        "dataset_state": state_list,
        "missing_dates": missing_dates,
        "missing_dates_by_marketplace": missing_dates_by_marketplace,
        "completed_reports": [dict(row) for row in reports],
    }


def get_ads_search_term_summary(
    store_id: str,
    start_date: str,
    end_date: str,
    marketplace: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return verified SP search-term facts without implying ASIN coverage."""
    requested_start, requested_end = _parse_ads_date_range(start_date, end_date)
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    filters = [
        "t.store_id=%s",
        "t.report_date BETWEEN %s AND %s",
        "t.active=TRUE",
    ]
    params: list[Any] = [store_id, requested_start, requested_end]
    if marketplace:
        filters.append("t.marketplace=%s")
        params.append(marketplace)
    where = " AND ".join(filters)
    with connect() as conn:
        aggregate = conn.execute(
            f"""SELECT COUNT(*) AS search_term_row_count,
                       COUNT(DISTINCT t.search_term) AS search_term_count,
                       COUNT(DISTINCT t.campaign_id) AS campaign_count,
                       COALESCE(SUM(t.impressions), 0) AS impressions,
                       COALESCE(SUM(t.clicks), 0) AS clicks,
                       COALESCE(SUM(t.spend), 0) AS spend,
                       COALESCE(SUM(t.sales_14d), 0) AS attributed_sales_14d,
                       COALESCE(SUM(t.purchases_14d), 0) AS attributed_purchases_14d,
                       COUNT(*) FILTER (
                           WHERE t.provisional_until >=
                               (NOW() AT TIME ZONE s.timezone)::date
                       ) AS provisional_rows,
                       MAX(t.provisional_until) AS latest_provisional_until,
                       MIN(t.currency) AS first_currency,
                       COUNT(DISTINCT t.currency) AS currency_count
                FROM amazon_ads_search_term_daily t
                JOIN amazon_store_connections s
                  ON s.store_id=t.store_id AND s.marketplace=t.marketplace
                WHERE {where}""",
            params,
        ).fetchone()
        top_terms = conn.execute(
            f"""SELECT t.search_term, t.currency,
                       COUNT(DISTINCT t.campaign_id) AS campaign_count,
                       SUM(t.impressions) AS impressions,
                       SUM(t.clicks) AS clicks,
                       SUM(t.spend) AS spend,
                       SUM(t.sales_14d) AS attributed_sales_14d,
                       SUM(t.purchases_14d) AS attributed_purchases_14d
                FROM amazon_ads_search_term_daily t
                WHERE {where}
                GROUP BY t.search_term, t.currency
                ORDER BY SUM(t.spend) DESC, t.search_term
                LIMIT %s""",
            [*params, limit],
        ).fetchall()
        currency_totals = conn.execute(
            f"""SELECT t.currency, SUM(t.spend) AS spend,
                       SUM(t.sales_14d) AS attributed_sales_14d
                FROM amazon_ads_search_term_daily t
                WHERE {where}
                GROUP BY t.currency ORDER BY t.currency""",
            params,
        ).fetchall()
        readiness = _ads_detail_readiness(
            conn,
            source="amazon_ads_reporting_v3_sp_search_terms",
            dataset="ads_search_term_daily",
            store_id=store_id,
            marketplace=marketplace,
            requested_start=requested_start,
            requested_end=requested_end,
        )
    warnings = [
        "search_term_report_has_no_asin_or_sku",
        "do_not_sum_metrics_across_ads_report_grains",
        "attributed_sales_are_not_total_store_sales",
        "non_campaign_grain_uses_amazon_default_campaign_eligibility",
    ]
    if readiness["missing_dates"]:
        warnings.append("requested_dates_outside_verified_coverage")
    if not readiness["dataset_state"]:
        warnings.append("no_current_ads_search_term_dataset_state")
    if readiness["evaluated_check_count"] < len(readiness["dataset_state"]) * 4:
        warnings.append("data_quality_checks_not_run")
    if readiness["open_issue_count"]:
        warnings.append("open_data_quality_issues")
    if aggregate["provisional_rows"]:
        warnings.append("recent_attribution_metrics_can_still_change")
    if aggregate["currency_count"] > 1:
        warnings.append("multiple_currencies_not_aggregated_into_one_value")
        readiness["safe_to_analyze"] = False
    single_currency = aggregate["currency_count"] <= 1
    return json_safe(
        {
            "store_id": store_id,
            "marketplace": marketplace,
            "start_date": requested_start,
            "end_date": requested_end,
            "scope": "Sponsored Products search-term daily reporting",
            "safe_to_analyze": readiness["safe_to_analyze"],
            "open_issue_count": readiness["open_issue_count"],
            "evaluated_check_count": readiness["evaluated_check_count"],
            "verified_coverage": {
                "complete": not readiness["missing_dates"],
                "missing_dates": readiness["missing_dates"],
                "missing_dates_by_marketplace": readiness[
                    "missing_dates_by_marketplace"
                ],
                "completed_reports": readiness["completed_reports"],
            },
            "search_term_row_count": int(aggregate["search_term_row_count"]),
            "search_term_count": int(aggregate["search_term_count"]),
            "campaign_count": int(aggregate["campaign_count"]),
            "impressions": int(aggregate["impressions"]),
            "clicks": int(aggregate["clicks"]),
            "spend": aggregate["spend"] if single_currency else None,
            "currency": aggregate["first_currency"] if single_currency else None,
            "attributed_sales_14d": (
                aggregate["attributed_sales_14d"] if single_currency else None
            ),
            "monetary_totals_by_currency": [dict(row) for row in currency_totals],
            "attributed_purchases_14d": int(
                aggregate["attributed_purchases_14d"]
            ),
            "provisional_rows": int(aggregate["provisional_rows"]),
            "latest_provisional_until": aggregate["latest_provisional_until"],
            "top_search_terms_by_spend": [dict(row) for row in top_terms],
            "dataset_state": readiness["dataset_state"],
            "metric_semantics": (
                "Amazon Ads v3 spSearchTerm traffic-date rows; no ASIN/SKU; "
                "not additive with campaign or purchased-product reports"
            ),
            "warnings": warnings,
        }
    )


def get_ads_purchased_product_summary(
    store_id: str,
    start_date: str,
    end_date: str,
    marketplace: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return verified purchased-ASIN attribution without traffic or spend."""
    requested_start, requested_end = _parse_ads_date_range(start_date, end_date)
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    filters = [
        "p.store_id=%s",
        "p.report_date BETWEEN %s AND %s",
        "p.active=TRUE",
    ]
    params: list[Any] = [store_id, requested_start, requested_end]
    if marketplace:
        filters.append("p.marketplace=%s")
        params.append(marketplace)
    where = " AND ".join(filters)
    with connect() as conn:
        aggregate = conn.execute(
            f"""SELECT COUNT(*) AS attribution_row_count,
                       COUNT(DISTINCT p.advertised_asin) AS advertised_asin_count,
                       COUNT(DISTINCT p.purchased_asin) AS purchased_asin_count,
                       COALESCE(SUM(p.sales_14d), 0) AS attributed_sales_14d,
                       COALESCE(SUM(p.sales_30d), 0) AS attributed_sales_30d,
                       COALESCE(SUM(p.purchases_14d), 0) AS attributed_purchases_14d,
                       COALESCE(SUM(p.purchases_30d), 0) AS attributed_purchases_30d,
                       COALESCE(SUM(p.units_sold_clicks_30d), 0) AS attributed_units_30d,
                       COUNT(*) FILTER (
                           WHERE p.advertised_asin=p.purchased_asin
                       ) AS same_asin_rows,
                       COUNT(*) FILTER (
                           WHERE p.advertised_asin<>p.purchased_asin
                       ) AS other_asin_rows,
                       COUNT(*) FILTER (
                           WHERE p.provisional_until >=
                               (NOW() AT TIME ZONE s.timezone)::date
                       ) AS provisional_rows,
                       MAX(p.provisional_until) AS latest_provisional_until,
                       MIN(p.currency) AS first_currency,
                       COUNT(DISTINCT p.currency) AS currency_count
                FROM amazon_ads_purchased_product_daily p
                JOIN amazon_store_connections s
                  ON s.store_id=p.store_id AND s.marketplace=p.marketplace
                WHERE {where}""",
            params,
        ).fetchone()
        top_asins = conn.execute(
            f"""SELECT p.purchased_asin, p.currency,
                       COUNT(DISTINCT p.advertised_asin) AS advertised_asin_count,
                       SUM(p.sales_14d) AS attributed_sales_14d,
                       SUM(p.sales_30d) AS attributed_sales_30d,
                       SUM(p.purchases_30d) AS attributed_purchases_30d,
                       SUM(p.units_sold_clicks_30d) AS attributed_units_30d
                FROM amazon_ads_purchased_product_daily p
                WHERE {where}
                GROUP BY p.purchased_asin, p.currency
                ORDER BY SUM(p.sales_30d) DESC, p.purchased_asin
                LIMIT %s""",
            [*params, limit],
        ).fetchall()
        currency_totals = conn.execute(
            f"""SELECT p.currency,
                       SUM(p.sales_14d) AS attributed_sales_14d,
                       SUM(p.sales_30d) AS attributed_sales_30d
                FROM amazon_ads_purchased_product_daily p
                WHERE {where}
                GROUP BY p.currency ORDER BY p.currency""",
            params,
        ).fetchall()
        readiness = _ads_detail_readiness(
            conn,
            source="amazon_ads_reporting_v3_sp_purchased_products",
            dataset="ads_purchased_product_daily",
            store_id=store_id,
            marketplace=marketplace,
            requested_start=requested_start,
            requested_end=requested_end,
        )
    warnings = [
        "purchased_product_report_has_no_impressions_clicks_or_spend",
        "acos_cannot_be_calculated_from_this_report_alone",
        "do_not_sum_metrics_across_ads_report_grains",
        "attributed_sales_are_not_total_store_sales",
        "non_campaign_grain_uses_amazon_default_campaign_eligibility",
    ]
    if readiness["missing_dates"]:
        warnings.append("requested_dates_outside_verified_coverage")
    if not readiness["dataset_state"]:
        warnings.append("no_current_ads_purchased_product_dataset_state")
    if readiness["evaluated_check_count"] < len(readiness["dataset_state"]) * 4:
        warnings.append("data_quality_checks_not_run")
    if readiness["open_issue_count"]:
        warnings.append("open_data_quality_issues")
    if aggregate["provisional_rows"]:
        warnings.append("recent_attribution_metrics_can_still_change")
    if aggregate["currency_count"] > 1:
        warnings.append("multiple_currencies_not_aggregated_into_one_value")
        readiness["safe_to_analyze"] = False
    single_currency = aggregate["currency_count"] <= 1
    return json_safe(
        {
            "store_id": store_id,
            "marketplace": marketplace,
            "start_date": requested_start,
            "end_date": requested_end,
            "scope": "Sponsored Products purchased-product daily attribution",
            "safe_to_analyze": readiness["safe_to_analyze"],
            "open_issue_count": readiness["open_issue_count"],
            "evaluated_check_count": readiness["evaluated_check_count"],
            "verified_coverage": {
                "complete": not readiness["missing_dates"],
                "missing_dates": readiness["missing_dates"],
                "missing_dates_by_marketplace": readiness[
                    "missing_dates_by_marketplace"
                ],
                "completed_reports": readiness["completed_reports"],
            },
            "attribution_row_count": int(aggregate["attribution_row_count"]),
            "advertised_asin_count": int(aggregate["advertised_asin_count"]),
            "purchased_asin_count": int(aggregate["purchased_asin_count"]),
            "attributed_sales_14d": (
                aggregate["attributed_sales_14d"] if single_currency else None
            ),
            "attributed_sales_30d": (
                aggregate["attributed_sales_30d"] if single_currency else None
            ),
            "attributed_purchases_14d": int(
                aggregate["attributed_purchases_14d"]
            ),
            "attributed_purchases_30d": int(
                aggregate["attributed_purchases_30d"]
            ),
            "attributed_units_30d": int(aggregate["attributed_units_30d"]),
            "same_asin_rows": int(aggregate["same_asin_rows"]),
            "other_asin_rows": int(aggregate["other_asin_rows"]),
            "currency": aggregate["first_currency"] if single_currency else None,
            "monetary_totals_by_currency": [dict(row) for row in currency_totals],
            "provisional_rows": int(aggregate["provisional_rows"]),
            "latest_provisional_until": aggregate["latest_provisional_until"],
            "top_purchased_asins_by_attributed_sales_30d": [
                dict(row) for row in top_asins
            ],
            "dataset_state": readiness["dataset_state"],
            "metric_semantics": (
                "Amazon Ads v3 spPurchasedProduct click-attribution rows; no "
                "traffic/spend; not additive with campaign or search-term reports"
            ),
            "warnings": warnings,
        }
    )


def get_settlement_summary(
    store_id: str,
    start_date: str,
    end_date: str,
    marketplace: str | None = None,
    date_basis: str = "settlement_end",
    limit: int = 50,
) -> dict[str, Any]:
    """Return closed Amazon settlement payouts and raw report dimensions."""
    requested_start, requested_end = _parse_ads_date_range(start_date, end_date)
    if date_basis not in {"settlement_end", "deposit"}:
        raise ValueError("date_basis must be settlement_end or deposit")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    date_expression = (
        "COALESCE(p.deposit_time, p.settlement_end_time, p.settlement_start_time)"
        if date_basis == "deposit"
        else "COALESCE(p.settlement_end_time, p.deposit_time, p.settlement_start_time)"
    )
    filters = [
        "p.source='amazon_sp_api_settlement_reports_v2'",
        "p.store_id=%s",
        "p.active=TRUE",
        f"({date_expression} AT TIME ZONE s.timezone)::date BETWEEN %s AND %s",
    ]
    params: list[Any] = [store_id, requested_start, requested_end]
    if marketplace:
        filters.append("p.marketplace_scope=%s")
        params.append(marketplace)
    where = " AND ".join(filters)
    state_scope_filter = ""
    issue_scope_filter = ""
    state_params: list[Any] = [store_id]
    if marketplace:
        state_scope_filter = "AND c.marketplace=%s"
        issue_scope_filter = "AND marketplace=%s"
        state_params.append(marketplace)
    with connect() as conn:
        aggregate = conn.execute(
            f"""SELECT COUNT(*) AS period_count,
                       COUNT(DISTINCT p.currency) AS currency_count,
                       MIN(p.currency) AS first_currency,
                       COALESCE(SUM(p.net_payout), 0) AS net_payout,
                       COALESCE(SUM(p.detail_amount_total), 0) AS detail_amount_total,
                       COALESCE(MAX(ABS(p.reconciliation_delta)), 0) AS max_abs_delta,
                       COALESCE(SUM(p.detail_row_count), 0) AS detail_row_count,
                       COALESCE(MAX(p.marketplace_name_count), 0) AS max_marketplaces,
                       MAX(p.settlement_end_time) AS latest_selected_period_end,
                       MAX(p.deposit_time) AS latest_selected_deposit_time
                FROM amazon_settlement_periods p
                JOIN amazon_store_connections s
                  ON s.store_id=p.store_id AND s.marketplace=p.marketplace_scope
                WHERE {where}""",
            params,
        ).fetchone()
        currency_totals = conn.execute(
            f"""SELECT p.currency, SUM(p.net_payout) AS net_payout,
                       SUM(p.detail_amount_total) AS detail_amount_total,
                       MAX(ABS(p.reconciliation_delta)) AS max_abs_delta,
                       COUNT(*) AS period_count
                FROM amazon_settlement_periods p
                JOIN amazon_store_connections s
                  ON s.store_id=p.store_id AND s.marketplace=p.marketplace_scope
                WHERE {where}
                GROUP BY p.currency ORDER BY p.currency""",
            params,
        ).fetchall()
        periods = conn.execute(
            f"""SELECT p.settlement_id, p.settlement_start_time,
                       p.settlement_end_time, p.deposit_time, p.currency,
                       p.net_payout, p.detail_amount_total,
                       p.reconciliation_delta, p.detail_row_count,
                       p.marketplace_name_count, p.report_id
                FROM amazon_settlement_periods p
                JOIN amazon_store_connections s
                  ON s.store_id=p.store_id AND s.marketplace=p.marketplace_scope
                WHERE {where}
                ORDER BY {date_expression} DESC NULLS LAST, p.settlement_id
                LIMIT %s""",
            [*params, limit],
        ).fetchall()
        line_filters = [part.replace("p.", "l.") for part in filters]
        line_where = " AND ".join(line_filters)
        breakdown = conn.execute(
            f"""SELECT COALESCE(l.transaction_type, '(summary)') AS transaction_type,
                       COALESCE(l.amount_type, '(none)') AS amount_type,
                       l.currency, COUNT(*) AS row_count, SUM(l.amount) AS amount
                FROM amazon_settlement_lines l
                JOIN amazon_store_connections s
                  ON s.store_id=l.store_id AND s.marketplace=l.marketplace_scope
                WHERE {line_where} AND l.transaction_type IS NOT NULL
                GROUP BY l.transaction_type, l.amount_type, l.currency
                ORDER BY ABS(SUM(l.amount)) DESC, l.transaction_type, l.amount_type
                LIMIT %s""",
            [*params, limit],
        ).fetchall()
        dataset_state = conn.execute(
            f"""SELECT c.source, c.store_id, c.marketplace, c.dataset,
                       c.business_date, c.fetched_at, c.source_updated_at,
                       c.ingestion_status, c.source_count, c.normalized_count,
                       c.duplicate_count, c.error_count,
                       r.schema_version, r.formula_version, r.raw_reference,
                       r.is_provisional, r.metadata
                FROM current_dataset_state c
                JOIN dataset_runs r ON r.id=c.run_id
                WHERE c.source='amazon_sp_api_settlement_reports_v2'
                  AND c.dataset='settlement_periods' AND c.store_id=%s
                  {state_scope_filter}
                ORDER BY c.marketplace""",
            state_params,
        ).fetchall()
        issue_count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM v_open_issues
                WHERE source='amazon_sp_api_settlement_reports_v2'
                  AND dataset='settlement_periods' AND store_id=%s
                  {issue_scope_filter}""",
            state_params,
        ).fetchone()["n"]
        check_count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM v_latest_checks
                WHERE source='amazon_sp_api_settlement_reports_v2'
                  AND dataset='settlement_periods' AND store_id=%s
                  {issue_scope_filter}""",
            state_params,
        ).fetchone()["n"]
        latest_filter = ""
        latest_params: list[Any] = [store_id]
        if marketplace:
            latest_filter = "AND marketplace_scope=%s"
            latest_params.append(marketplace)
        latest = conn.execute(
            f"""SELECT MAX(settlement_end_time) AS period_end,
                       MAX(deposit_time) AS deposit_time
                FROM amazon_settlement_periods
                WHERE source='amazon_sp_api_settlement_reports_v2'
                  AND store_id=%s AND active=TRUE {latest_filter}""",
            latest_params,
        ).fetchone()

    states = [dict(row) for row in dataset_state]
    complete_states = bool(states) and all(
        row["ingestion_status"] == "complete" and row["error_count"] == 0
        for row in states
    )
    period_count = int(aggregate["period_count"])
    currency_count = int(aggregate["currency_count"])
    reconciled = Decimal(str(aggregate["max_abs_delta"])) <= Decimal("0.01")
    safe = (
        complete_states
        and issue_count == 0
        and check_count >= len(states) * 4
        and period_count > 0
        and reconciled
        and currency_count <= 1
    )
    warnings = [
        "closed_settlement_cash_flow_not_order_date_revenue_or_profit",
        "settlement_reports_are_automatically_generated_not_on_demand",
        "marketplace_is_connection_scope_rows_can_span_marketplace_names",
        "identifier_column_meaning_can_vary_by_transaction_type",
        "breakdown_uses_raw_amazon_dimensions_not_subjective_business_categories",
    ]
    if not states:
        warnings.append("no_current_settlement_dataset_state")
    if issue_count:
        warnings.append("open_data_quality_issues")
    if check_count < len(states) * 4:
        warnings.append("data_quality_checks_not_run")
    if period_count == 0:
        warnings.append("no_closed_settlement_in_requested_range_not_zero_payout")
    if not reconciled:
        warnings.append("settlement_detail_does_not_reconcile_to_net_payout")
    if currency_count > 1:
        warnings.append("multiple_currencies_not_aggregated_into_one_value")
    if int(aggregate["max_marketplaces"]) > 1:
        warnings.append("selected_settlement_contains_multiple_marketplace_names")
    single_currency = currency_count <= 1
    return json_safe(
        {
            "store_id": store_id,
            "marketplace_scope": marketplace,
            "start_date": requested_start,
            "end_date": requested_end,
            "date_basis": date_basis,
            "scope": "closed Amazon settlement cash-flow reports",
            "safe_to_analyze": safe,
            "open_issue_count": int(issue_count),
            "evaluated_check_count": int(check_count),
            "period_count": period_count,
            "detail_row_count": int(aggregate["detail_row_count"]),
            "currency": aggregate["first_currency"] if single_currency else None,
            "net_payout": aggregate["net_payout"] if single_currency else None,
            "detail_amount_total": (
                aggregate["detail_amount_total"] if single_currency else None
            ),
            "max_abs_reconciliation_delta": aggregate["max_abs_delta"],
            "monetary_totals_by_currency": [dict(row) for row in currency_totals],
            "latest_available": {
                "settlement_end_time": latest["period_end"],
                "deposit_time": latest["deposit_time"],
            },
            "selected_periods": [dict(row) for row in periods],
            "raw_dimension_breakdown": [dict(row) for row in breakdown],
            "dataset_state": states,
            "metric_semantics": (
                "net_payout is the closed statement total; detail rows must sum "
                "to it within 0.01 in report currency"
            ),
            "warnings": warnings,
        }
    )
