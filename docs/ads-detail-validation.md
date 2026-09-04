# Amazon Ads detail connector validation

This document records reusable connector contracts only. It intentionally omits
seller identifiers, search terms, ASINs, credentials and operating metrics.

## Search-term report

- The canonical grain preserves report date, campaign, ad group, targeting and
  customer search term.
- The dataset contains traffic, spend and attributed results, but no promoted or
  purchased ASIN/SKU. Neither Core nor an Agent may infer one.

## Purchased-product report

- The canonical grain preserves report date, campaign, ad group, targeting,
  advertised ASIN and purchased ASIN.
- The dataset contains attributed results but no impressions, clicks or spend,
  so it cannot calculate ACOS by itself.

## Shared safeguards

- Asynchronous report requests are resumable and every downloaded version is
  retained.
- Recent attribution remains provisional and may be revised by a later report.
- Campaign, search-term and purchased-product totals are overlapping views and
  must never be added together.
- Missing API dimensions remain explicitly missing rather than being guessed.

Automated coverage lives in `tests/test_ads_details_sync.py`.
