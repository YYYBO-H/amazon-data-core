---
name: amazon-data-core
description: Verify local Amazon order, inventory, advertising and settlement data before analyzing store performance.
---

# Amazon Data Core

Use this skill whenever the user asks whether Amazon business data is current,
complete, trustworthy, or ready for analysis.

## Required workflow

1. Call `amazon_data_health` first.
2. If health is `unknown`, `critical`, or reports open issues, call
   `amazon_data_issues` and explain the affected store and dataset.
3. Call `amazon_dataset_status` before quoting a date range, currency, source,
   record count, or provisional advertising result.
4. For an order question, call `amazon_orders_summary` with the requested store,
   business date and marketplace. Respect its `safe_to_analyze`, verified
   coverage and warnings. Latest-sync counts are batch counts, not daily sales.
5. For an inventory question, call `amazon_fba_inventory_status` with the
   requested store and marketplace. Respect its `safe_to_analyze` and warnings.
   It is a current FBA snapshot, not historical inventory, and it never includes
   merchant-fulfilled (MFN/FBM) quantity.
6. For an advertising question, call `amazon_ads_campaign_summary` with the
   requested store, marketplace and date range. Respect verified coverage and
   `safe_to_analyze`. Disclose provisional attribution and never present
   click-attributed sales as total store revenue or profit.
7. For a customer-search-term question, call `amazon_ads_search_term_summary`.
   This report has traffic and spend but no ASIN/SKU. Do not infer a promoted or
   purchased ASIN from a search-term row.
8. For a purchased-ASIN attribution question, call
   `amazon_ads_purchased_product_summary`. This report has attributed results but
   no impressions, clicks or spend, so it cannot calculate ACOS alone.
9. Never add campaign, search-term and purchased-product report totals together;
   they are overlapping views at different grains.
10. For a payout or settlement question, call `amazon_settlement_summary` with
    the requested store, date range, marketplace scope and date basis. Treat it
    as closed-statement cash flow, not order-date revenue or profit. Require the
    report details to reconcile to net payout and keep currencies separate.
11. Settlement reports are generated automatically by Amazon. No report in a
    requested range does not mean zero payout, and a connection marketplace is
    not proof that every row belongs to that marketplace.
12. Clearly separate data-quality facts from business interpretation.
13. Never silently treat `failed`, `skipped`, or `error` as passed.
14. Never claim that missing data means zero sales, zero spend, zero payout, or
    zero stock.

## Tools

- `amazon_data_health`: overall readiness and check counts.
- `amazon_dataset_status`: source lineage, dates, row counts, versions and
  provisional status.
- `amazon_data_issues`: unresolved freshness, completeness, reconciliation or
  ordering problems.
- `amazon_orders_summary`: local order counts, item counts, available seller
  proceeds, fulfillment states and the accompanying data-readiness result.
- `amazon_fba_inventory_status`: latest active FBA inventory by seller SKU/FNSKU,
  quantity buckets, low-stock rows and the accompanying data-readiness result.
- `amazon_ads_campaign_summary`: Sponsored Products campaign-day traffic, spend,
  attributed conversions, verified date coverage and attribution revision state.
- `amazon_ads_search_term_summary`: customer search terms, traffic, spend and
  attributed results; explicitly excludes ASIN/SKU.
- `amazon_ads_purchased_product_summary`: advertised-to-purchased ASIN attribution;
  explicitly excludes impressions, clicks and spend.
- `amazon_settlement_summary`: closed statement periods, deposit dates, net
  payouts, exact detail reconciliation and raw Amazon report dimensions.

If the MCP tools are unavailable, ask the host to run
`amazon-data-core doctor`, then configure the stdio command
`amazon-data-core mcp`.
