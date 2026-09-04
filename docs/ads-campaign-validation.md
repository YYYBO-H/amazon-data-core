# Amazon Ads campaign connector validation

This document records reusable connector contracts only. It intentionally omits
Profile IDs, Report IDs, campaign names, credentials and operating metrics.

## Verified contracts

- Ads Profile marketplace, currency and IANA timezone must match the configured
  store before report data is written.
- Sponsored Products campaign facts use `business date + campaignId` as their
  canonical grain.
- The connector stores report requests, polling state, downloaded raw rows and
  normalized versions so an interrupted asynchronous report can resume safely.
- Numeric and string campaign identifiers normalize to strings; booleans,
  floating-point identifiers and empty values are rejected.
- A newer attribution-window revision may replace an older canonical value, but
  older raw versions remain traceable.
- MCP output marks recent attribution as provisional and never describes
  click-attributed sales as total store revenue or profit.

Automated coverage lives in `tests/test_ads_sync.py` and
`tests/test_ads_api.py`.
