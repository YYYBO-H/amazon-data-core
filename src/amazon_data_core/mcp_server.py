from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .agent_tools import (
    get_ads_campaign_summary,
    get_ads_purchased_product_summary,
    get_ads_search_term_summary,
    get_data_health,
    get_fba_inventory_status,
    get_orders_summary,
    get_settlement_summary,
    list_dataset_status,
    list_open_data_issues,
)

mcp = FastMCP(
    "amazon-data-core",
    instructions=(
        "Read-only access to the local Amazon operating-data health layer. "
        "Check data health before making business claims. A failed or unknown "
        "dataset must be disclosed; never present it as trustworthy data."
    ),
)


@mcp.tool()
def amazon_data_health() -> dict[str, Any]:
    """Check whether local Amazon data is complete, timely and safe to analyze."""
    return get_data_health()


@mcp.tool()
def amazon_dataset_status(
    store_id: str | None = None,
    dataset: str | None = None,
) -> list[dict[str, Any]]:
    """List current dataset lineage and sync status, optionally filtered."""
    return list_dataset_status(store_id=store_id, dataset=dataset)


@mcp.tool()
def amazon_data_issues(
    store_id: str | None = None,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    """List unresolved data-quality issues; results are facts, not ad advice."""
    return list_open_data_issues(store_id=store_id, severity=severity)


@mcp.tool()
def amazon_orders_summary(
    store_id: str,
    business_date: str,
    marketplace: str | None = None,
) -> dict[str, Any]:
    """Summarize local orders and available proceeds; disclose data readiness."""
    return get_orders_summary(
        store_id=store_id,
        business_date=business_date,
        marketplace=marketplace,
    )


@mcp.tool()
def amazon_fba_inventory_status(
    store_id: str,
    marketplace: str | None = None,
    max_fulfillable: int = 10,
    limit: int = 100,
) -> dict[str, Any]:
    """Read the latest verified FBA snapshot and low-stock records."""
    return get_fba_inventory_status(
        store_id=store_id,
        marketplace=marketplace,
        max_fulfillable=max_fulfillable,
        limit=limit,
    )


@mcp.tool()
def amazon_ads_campaign_summary(
    store_id: str,
    start_date: str,
    end_date: str,
    marketplace: str | None = None,
) -> dict[str, Any]:
    """Summarize verified Sponsored Products campaign facts and attribution state."""
    return get_ads_campaign_summary(
        store_id=store_id,
        start_date=start_date,
        end_date=end_date,
        marketplace=marketplace,
    )


@mcp.tool()
def amazon_ads_search_term_summary(
    store_id: str,
    start_date: str,
    end_date: str,
    marketplace: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Summarize verified SP customer search terms without implying ASIN coverage."""
    return get_ads_search_term_summary(
        store_id=store_id,
        start_date=start_date,
        end_date=end_date,
        marketplace=marketplace,
        limit=limit,
    )


@mcp.tool()
def amazon_ads_purchased_product_summary(
    store_id: str,
    start_date: str,
    end_date: str,
    marketplace: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Summarize verified SP purchased-ASIN attribution without traffic or spend."""
    return get_ads_purchased_product_summary(
        store_id=store_id,
        start_date=start_date,
        end_date=end_date,
        marketplace=marketplace,
        limit=limit,
    )


@mcp.tool()
def amazon_settlement_summary(
    store_id: str,
    start_date: str,
    end_date: str,
    marketplace: str | None = None,
    date_basis: str = "settlement_end",
    limit: int = 50,
) -> dict[str, Any]:
    """Summarize verified closed settlements and exact payout reconciliation."""
    return get_settlement_summary(
        store_id=store_id,
        start_date=start_date,
        end_date=end_date,
        marketplace=marketplace,
        date_basis=date_basis,
        limit=limit,
    )


def run() -> None:
    mcp.run(transport="stdio")
