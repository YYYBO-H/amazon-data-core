# FBA inventory connector validation

This document records reusable connector contracts only. It intentionally omits
seller identifiers, credentials, SKUs, quantities and production-system details.

## Verified contracts

- The connector retrieves every page before publishing a current snapshot.
- Records remain distinct at `sellerSku + fnSku + condition` grain; multiple
  records for one ASIN do not overwrite each other.
- Invalid rows make the ingestion partial and cannot deactivate the previous
  complete snapshot.
- A complete replacement snapshot deactivates records absent from the new result.
- An expired pagination token restarts the snapshot instead of mixing pages from
  different observation times.
- Unchanged rows remain normalized snapshot members rather than being reported
  as duplicate source rows.
- MCP output always states that this is current FBA inventory only. Historical
  inventory and FBM/MFN quantities are outside this connector's scope.

Automated coverage lives in `tests/test_inventory_sync.py` and
`tests/test_sp_api.py`.
