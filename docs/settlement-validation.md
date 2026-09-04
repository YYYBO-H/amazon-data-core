# Amazon settlement connector validation

This document records reusable connector contracts only. It intentionally omits
seller identifiers, order IDs, SKUs, credentials, payouts and production-system
details.

## Verified contracts

- The connector lists Amazon-generated closed settlement reports and does not
  claim that settlements can be created on demand.
- Report IDs are idempotent and pagination tokens are followed to completion.
- GZIP documents and UTF-8/CP1252 text are decoded before TSV validation.
- Amazon date variants using either year-first ISO-like timestamps or
  day-first `DD.MM.YYYY HH:MM:SS UTC` timestamps are normalized to UTC.
- Localized monetary values support decimal commas, decimal points, thousands
  separators and negative parentheses. Ambiguous values are rejected.
- A report is published only when it has exactly one summary row and every
  detail amount reconciles to net payout within `0.01` in the report currency.
- Report currencies remain separate, and a marketplace connection is not used
  as proof that every report row belongs to one marketplace.
- Raw Amazon transaction, amount and description dimensions remain available;
  subjective business-category mappings belong in a versioned analysis layer.

Automated coverage lives in `tests/test_settlement_sync.py` and includes the
day-first timestamp format observed in an Amazon-generated report.
