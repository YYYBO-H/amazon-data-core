from __future__ import annotations

import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "amazon_data_health",
    "amazon_dataset_status",
    "amazon_data_issues",
    "amazon_orders_summary",
    "amazon_fba_inventory_status",
    "amazon_ads_campaign_summary",
    "amazon_ads_search_term_summary",
    "amazon_ads_purchased_product_summary",
    "amazon_settlement_summary",
}


async def verify() -> None:
    server = StdioServerParameters(
        command="amazon-data-core",
        args=["mcp"],
        env=dict(os.environ),
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tool_names = {tool.name for tool in listed.tools}
            missing = EXPECTED_TOOLS - tool_names
            if missing:
                raise RuntimeError(f"missing MCP tools: {sorted(missing)}")
            calls = {
                "amazon_data_health": {},
                "amazon_dataset_status": {"dataset": "orders"},
                "amazon_data_issues": {},
                "amazon_orders_summary": {
                    "store_id": "demo-us",
                    "business_date": "2026-09-03",
                },
                "amazon_fba_inventory_status": {"store_id": "demo-us"},
                "amazon_ads_campaign_summary": {
                    "store_id": "demo-us",
                    "start_date": "2026-09-03",
                    "end_date": "2026-09-03",
                },
                "amazon_ads_search_term_summary": {
                    "store_id": "demo-us",
                    "start_date": "2026-09-03",
                    "end_date": "2026-09-03",
                },
                "amazon_ads_purchased_product_summary": {
                    "store_id": "demo-us",
                    "start_date": "2026-09-03",
                    "end_date": "2026-09-03",
                },
                "amazon_settlement_summary": {
                    "store_id": "demo-us",
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-03",
                },
            }
            for tool_name, arguments in calls.items():
                result = await session.call_tool(tool_name, arguments)
                if result.isError:
                    raise RuntimeError(
                        f"{tool_name} returned an MCP error: {result.content!r}"
                    )
            print(
                json.dumps(
                    {
                        "mcp": "passed",
                        "tools": sorted(tool_names),
                        "tools_called": sorted(calls),
                    },
                    ensure_ascii=False,
                )
            )


if __name__ == "__main__":
    asyncio.run(verify())
