from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from psycopg.types.json import Jsonb

from .connectors.sp_api import SPAPIClient, SPAPIError
from .contracts import DatasetRunIn
from .engine import ingest_run
from .rules import ensure_default_rules

SOURCE = "amazon_sp_api_settlement_reports_v2"
DATASET = "settlement_periods"
REPORT_TYPE = "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2"
SCHEMA_VERSION = "amazon-settlement-flat-file-v2-raw-v1"
FORMULA_VERSION = "amazon-settlement-exact-reconciliation-v1"

REQUIRED_HEADERS = {
    "settlement-id",
    "settlement-start-date",
    "settlement-end-date",
    "deposit-date",
    "total-amount",
    "currency",
    "transaction-type",
    "amount-type",
    "amount-description",
    "amount",
}


class SettlementNormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class SettlementSyncConfig:
    store_id: str
    marketplace: str
    region: str = "NA"
    timezone: str = "UTC"
    currency: str | None = None
    created_since_days: int = 90
    page_size: int = 100
    max_pages: int = 100

    def validate(self) -> None:
        if not self.store_id.strip():
            raise ValueError("store_id is required")
        if not self.marketplace.strip():
            raise ValueError("marketplace is required")
        if self.region.upper() not in {"NA", "EU", "FE"}:
            raise ValueError("region must be NA, EU or FE")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        if self.currency and len(self.currency) != 3:
            raise ValueError("currency must be a three-letter code")
        if not 1 <= self.created_since_days <= 90:
            raise ValueError("created_since_days must be between 1 and 90")
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if not 1 <= self.max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")


def payload_checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_api_time(value: Any, field: str, *, required: bool = True) -> datetime | None:
    if value in (None, ""):
        if required:
            raise SettlementNormalizationError(f"missing_{field}")
        return None
    if not isinstance(value, str):
        raise SettlementNormalizationError(f"invalid_{field}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SettlementNormalizationError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise SettlementNormalizationError(f"timezone_missing_{field}")
    return parsed.astimezone(UTC)


def _parse_report_time(value: str | None, field: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    normalized = raw.replace(" UTC", "+00:00").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for pattern in (
            "%Y-%m-%d %H:%M:%S %z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d.%m.%Y %H:%M:%S %Z",
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y",
        ):
            try:
                parsed = datetime.strptime(raw, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise SettlementNormalizationError(f"invalid_{field}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_localized_decimal(
    value: str | None,
    field: str,
    *,
    required: bool = False,
) -> Decimal | None:
    raw = (value or "").strip().replace("\u00a0", "").replace(" ", "")
    if not raw:
        if required:
            raise SettlementNormalizationError(f"missing_{field}")
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1]
    if raw[:1] in {"+", "-"}:
        sign, raw = raw[0], raw[1:]
    else:
        sign = ""
    if not raw or any(char not in "0123456789,." for char in raw):
        raise SettlementNormalizationError(f"invalid_{field}")
    if "," in raw and "." in raw:
        decimal_separator = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        normalized = raw.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in raw or "." in raw:
        separator = "," if "," in raw else "."
        parts = raw.split(separator)
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            normalized = f"{parts[0]}.{parts[1]}"
        elif all(part.isdigit() for part in parts) and all(
            len(part) == 3 for part in parts[1:]
        ):
            normalized = "".join(parts)
        else:
            raise SettlementNormalizationError(f"ambiguous_{field}")
    else:
        normalized = raw
    if negative:
        sign = "-"
    try:
        parsed = Decimal(f"{sign}{normalized}")
    except InvalidOperation as exc:
        raise SettlementNormalizationError(f"invalid_{field}") from exc
    if not parsed.is_finite():
        raise SettlementNormalizationError(f"invalid_{field}")
    return parsed


def _optional_text(value: str | None) -> str | None:
    parsed = (value or "").strip()
    return parsed or None


def _quantity(value: str | None) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise SettlementNormalizationError("invalid_quantity_purchased") from exc
    if parsed != parsed.to_integral_value() or parsed < 0:
        raise SettlementNormalizationError("invalid_quantity_purchased")
    return int(parsed)


def decode_report_document(content: bytes, compression: str | None) -> str:
    if compression == "GZIP":
        try:
            content = gzip.decompress(content)
        except (OSError, EOFError) as exc:
            raise SettlementNormalizationError("invalid_gzip_document") from exc
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return content.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise SettlementNormalizationError("unsupported_document_encoding") from exc


def parse_settlement_tsv(text: str) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    try:
        headers = [value.strip().lstrip("\ufeff") for value in next(reader)]
    except StopIteration as exc:
        raise SettlementNormalizationError("empty_document") from exc
    if not headers or any(not value for value in headers):
        raise SettlementNormalizationError("invalid_header")
    if len(set(headers)) != len(headers):
        raise SettlementNormalizationError("duplicate_header")
    missing = sorted(REQUIRED_HEADERS - set(headers))
    if missing:
        raise SettlementNormalizationError("missing_headers:" + ",".join(missing))
    rows: list[tuple[int, dict[str, str]]] = []
    for line_no, values in enumerate(reader, start=1):
        if not values or not any(value.strip() for value in values):
            continue
        if len(values) != len(headers):
            raise SettlementNormalizationError(f"column_count_mismatch_line_{line_no}")
        rows.append((line_no, dict(zip(headers, values, strict=True))))
    if not rows:
        raise SettlementNormalizationError("empty_report_rows")
    return headers, rows


def _unique_context(
    rows: list[tuple[int, dict[str, str]]],
    key: str,
    *,
    required: bool,
) -> str | None:
    values = {_optional_text(row.get(key)) for _, row in rows}
    values.discard(None)
    if not values:
        if required:
            raise SettlementNormalizationError(f"missing_{key.replace('-', '_')}")
        return None
    if len(values) != 1:
        raise SettlementNormalizationError(f"conflicting_{key.replace('-', '_')}")
    return next(iter(values))


def normalize_settlement_document(
    rows: list[tuple[int, dict[str, str]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settlement_id = _unique_context(rows, "settlement-id", required=True)
    currency = (_unique_context(rows, "currency", required=True) or "").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise SettlementNormalizationError("invalid_currency")
    start_time = _parse_report_time(
        _unique_context(rows, "settlement-start-date", required=False),
        "settlement_start_date",
    )
    end_time = _parse_report_time(
        _unique_context(rows, "settlement-end-date", required=False),
        "settlement_end_date",
    )
    deposit_time = _parse_report_time(
        _unique_context(rows, "deposit-date", required=False),
        "deposit_date",
    )
    if start_time and end_time and start_time > end_time:
        raise SettlementNormalizationError("settlement_start_after_end")

    summary_rows = [row for _, row in rows if not _optional_text(row.get("transaction-type"))]
    if len(summary_rows) != 1:
        raise SettlementNormalizationError("summary_row_count_not_one")
    total_amount = parse_localized_decimal(
        summary_rows[0].get("total-amount"), "total_amount", required=True
    )
    assert total_amount is not None

    normalized: list[dict[str, Any]] = []
    detail_total = Decimal("0")
    marketplace_names: set[str] = set()
    for line_no, row in rows:
        transaction_type = _optional_text(row.get("transaction-type"))
        amount = parse_localized_decimal(
            row.get("amount"),
            "amount",
            required=transaction_type is not None,
        )
        if transaction_type is not None:
            assert amount is not None
            detail_total += amount
        marketplace_name = _optional_text(row.get("marketplace-name"))
        if marketplace_name:
            marketplace_names.add(marketplace_name)
        normalized.append(
            {
                "line_no": line_no,
                "settlement_id": settlement_id,
                "settlement_start_time": start_time,
                "settlement_end_time": end_time,
                "deposit_time": deposit_time,
                "total_amount": total_amount,
                "currency": currency,
                "transaction_type": transaction_type,
                "order_id": _optional_text(row.get("order-id")),
                "merchant_order_id": _optional_text(row.get("merchant-order-id")),
                "adjustment_id": _optional_text(row.get("adjustment-id")),
                "shipment_id": _optional_text(row.get("shipment-id")),
                "marketplace_name": marketplace_name,
                "amount_type": _optional_text(row.get("amount-type")),
                "amount_description": _optional_text(row.get("amount-description")),
                "amount": amount,
                "fulfillment_id": _optional_text(row.get("fulfillment-id")),
                "posted_time": _parse_report_time(
                    row.get("posted-date-time") or row.get("posted-date"),
                    "posted_time",
                ),
                "order_item_code": _optional_text(row.get("order-item-code")),
                "merchant_order_item_id": _optional_text(
                    row.get("merchant-order-item-id")
                ),
                "merchant_adjustment_item_id": _optional_text(
                    row.get("merchant-adjustment-item-id")
                ),
                "sku": _optional_text(row.get("sku")),
                "quantity_purchased": _quantity(row.get("quantity-purchased")),
                "promotion_id": _optional_text(row.get("promotion-id")),
            }
        )
    delta = total_amount - detail_total
    if abs(delta) > Decimal("0.01"):
        raise SettlementNormalizationError("net_payout_detail_reconciliation_failed")
    context = {
        "settlement_id": settlement_id,
        "settlement_start_time": start_time,
        "settlement_end_time": end_time,
        "deposit_time": deposit_time,
        "currency": currency,
        "net_payout": total_amount,
        "detail_amount_total": detail_total,
        "reconciliation_delta": delta,
        "summary_row_count": 1,
        "detail_row_count": len(normalized) - 1,
        "marketplace_name_count": len(marketplace_names),
    }
    return normalized, context


def list_all_settlement_reports(
    client: SPAPIClient,
    config: SettlementSyncConfig,
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], int]:
    reports: dict[str, dict[str, Any]] = {}
    token: str | None = None
    for page in range(1, config.max_pages + 1):
        params: dict[str, Any]
        if token:
            params = {"nextToken": token}
        else:
            params = {
                "reportTypes": [REPORT_TYPE],
                "processingStatuses": ["DONE"],
                "pageSize": config.page_size,
                "createdSince": (now - timedelta(days=config.created_since_days))
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "createdUntil": now.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        body = client.get_reports(params)
        for report in body["reports"]:
            report_id = report.get("reportId")
            if not isinstance(report_id, str) or not report_id:
                raise SettlementNormalizationError("report_list_missing_report_id")
            if report.get("reportType") != REPORT_TYPE:
                raise SettlementNormalizationError("unexpected_report_type")
            if report.get("processingStatus") != "DONE":
                raise SettlementNormalizationError("unexpected_report_status")
            reports[report_id] = report
        token = body.get("nextToken")
        if not token:
            return sorted(
                reports.values(),
                key=lambda item: str(item.get("createdTime") or ""),
            ), page
    raise SPAPIError("Amazon Reports pagination exceeded max_pages", retryable=True)


def _register_store(conn: Connection, config: SettlementSyncConfig) -> None:
    conn.execute(
        """INSERT INTO amazon_store_connections(
               store_id, marketplace, region, timezone, currency
           ) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (store_id, marketplace) DO UPDATE SET
               region=EXCLUDED.region,
               timezone=EXCLUDED.timezone,
               currency=COALESCE(EXCLUDED.currency, amazon_store_connections.currency),
               enabled=TRUE,
               updated_at=NOW()""",
        (
            config.store_id,
            config.marketplace,
            config.region.upper(),
            config.timezone,
            config.currency.upper() if config.currency else None,
        ),
    )


def _start_attempt(
    conn: Connection, config: SettlementSyncConfig, now: datetime
) -> dict[str, Any]:
    conn.execute(
        """UPDATE amazon_sync_attempts
           SET status='failed', error_type='AbandonedSettlementSync', updated_at=NOW()
           WHERE source=%s AND store_id=%s AND marketplace=%s AND dataset=%s
             AND status IN ('running', 'finalizing')""",
        (SOURCE, config.store_id, config.marketplace, DATASET),
    )
    row = conn.execute(
        """INSERT INTO amazon_sync_attempts(
               id, source, store_id, marketplace, dataset, status,
               window_start, window_end
           ) VALUES (%s, %s, %s, %s, %s, 'running', %s, %s)
           RETURNING *""",
        (
            uuid4(),
            SOURCE,
            config.store_id,
            config.marketplace,
            DATASET,
            now - timedelta(days=config.created_since_days),
            now,
        ),
    ).fetchone()
    conn.commit()
    return dict(row)


def _upsert_report_listing(
    conn: Connection,
    *,
    attempt_id: UUID,
    config: SettlementSyncConfig,
    report: dict[str, Any],
) -> dict[str, Any]:
    report_id = str(report["reportId"])
    created_at = _parse_api_time(report.get("createdTime"), "report_created_at")
    existing = conn.execute(
        """SELECT source, store_id, marketplace_scope
           FROM amazon_settlement_reports WHERE report_id=%s""",
        (report_id,),
    ).fetchone()
    if existing and (
        existing["source"] != SOURCE
        or existing["store_id"] != config.store_id
        or existing["marketplace_scope"] != config.marketplace
    ):
        raise SettlementNormalizationError("report_id_scope_mismatch")
    row = conn.execute(
        """INSERT INTO amazon_settlement_reports(
               report_id, sync_attempt_id, source, store_id, marketplace_scope,
               report_type, report_document_id, processing_status,
               data_start_time, data_end_time, report_created_at, metadata
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'LISTED', %s, %s, %s, %s)
           ON CONFLICT (report_id) DO UPDATE SET
               sync_attempt_id=EXCLUDED.sync_attempt_id,
               report_document_id=COALESCE(
                   EXCLUDED.report_document_id,
                   amazon_settlement_reports.report_document_id
               ),
               data_start_time=COALESCE(
                   EXCLUDED.data_start_time,
                   amazon_settlement_reports.data_start_time
               ),
               data_end_time=COALESCE(
                   EXCLUDED.data_end_time,
                   amazon_settlement_reports.data_end_time
               ),
               report_created_at=EXCLUDED.report_created_at,
               processing_status=CASE
                   WHEN EXCLUDED.report_document_id IS NOT NULL
                    AND amazon_settlement_reports.report_document_id
                        IS DISTINCT FROM EXCLUDED.report_document_id
                   THEN 'LISTED'
                   ELSE amazon_settlement_reports.processing_status
               END,
               metadata=amazon_settlement_reports.metadata || EXCLUDED.metadata,
               updated_at=NOW()
           RETURNING *""",
        (
            report_id,
            attempt_id,
            SOURCE,
            config.store_id,
            config.marketplace,
            REPORT_TYPE,
            report.get("reportDocumentId"),
            _parse_api_time(report.get("dataStartTime"), "data_start_time", required=False),
            _parse_api_time(report.get("dataEndTime"), "data_end_time", required=False),
            created_at,
            Jsonb(
                {
                    "amazon_processing_status": report.get("processingStatus"),
                    "marketplace_scope_is_connection_scope": True,
                }
            ),
        ),
    ).fetchone()
    return dict(row)


def _record_document_failure(
    conn: Connection,
    *,
    attempt_id: UUID,
    report_id: str,
    error_code: str,
) -> None:
    payload = {"document_error": error_code}
    checksum = payload_checksum(payload)
    conn.execute(
        """INSERT INTO amazon_settlement_rejects(
               sync_attempt_id, report_id, line_no, payload,
               payload_checksum, error_code
           ) VALUES (%s, %s, 0, %s, %s, %s)
           ON CONFLICT (report_id, line_no, payload_checksum, error_code)
           DO NOTHING""",
        (attempt_id, report_id, Jsonb(payload), checksum, error_code),
    )
    conn.execute(
        """UPDATE amazon_settlement_reports
           SET processing_status='PARTIAL', rejected_row_count=1, updated_at=NOW()
           WHERE report_id=%s""",
        (report_id,),
    )


def _store_raw_rows(
    conn: Connection,
    *,
    attempt_id: UUID,
    config: SettlementSyncConfig,
    report_id: str,
    rows: list[tuple[int, dict[str, str]]],
    fetched_at: datetime,
) -> dict[int, int]:
    settlement_values = {
        value
        for _, row in rows
        if (value := _optional_text(row.get("settlement-id"))) is not None
    }
    settlement_hint = next(iter(settlement_values)) if len(settlement_values) == 1 else None
    raw_ids: dict[int, int] = {}
    for line_no, payload in rows:
        checksum = payload_checksum(payload)
        raw = conn.execute(
            """INSERT INTO amazon_settlement_raw(
                   sync_attempt_id, report_id, source, store_id,
                   marketplace_scope, settlement_id, line_no, payload,
                   payload_checksum, fetched_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (report_id, line_no, payload_checksum) DO NOTHING
               RETURNING id""",
            (
                attempt_id,
                report_id,
                SOURCE,
                config.store_id,
                config.marketplace,
                settlement_hint,
                line_no,
                Jsonb(payload),
                checksum,
                fetched_at,
            ),
        ).fetchone()
        if raw:
            raw_ids[line_no] = raw["id"]
        else:
            raw_ids[line_no] = conn.execute(
                """SELECT id FROM amazon_settlement_raw
                   WHERE report_id=%s AND line_no=%s AND payload_checksum=%s""",
                (report_id, line_no, checksum),
            ).fetchone()["id"]
    return raw_ids


def _store_valid_document(
    conn: Connection,
    *,
    attempt_id: UUID,
    config: SettlementSyncConfig,
    report: dict[str, Any],
    headers: list[str],
    rows: list[tuple[int, dict[str, str]]],
    raw_ids: dict[int, int],
    normalized: list[dict[str, Any]],
    context: dict[str, Any],
    fetched_at: datetime,
    document_checksum: str,
) -> bool:
    report_id = str(report["report_id"])
    raw_payloads = {line_no: payload for line_no, payload in rows}

    current = conn.execute(
        """SELECT report_created_at FROM amazon_settlement_periods
           WHERE source=%s AND store_id=%s AND marketplace_scope=%s
             AND settlement_id=%s FOR UPDATE""",
        (SOURCE, config.store_id, config.marketplace, context["settlement_id"]),
    ).fetchone()
    applied = current is None or report["report_created_at"] >= current["report_created_at"]
    if applied:
        conn.execute(
            """UPDATE amazon_settlement_lines SET active=FALSE, updated_at=NOW()
               WHERE source=%s AND store_id=%s AND marketplace_scope=%s
                 AND settlement_id=%s""",
            (SOURCE, config.store_id, config.marketplace, context["settlement_id"]),
        )
        for row in normalized:
            raw_payload = dict(raw_payloads[row["line_no"]])
            checksum = payload_checksum(raw_payload)
            conn.execute(
                """INSERT INTO amazon_settlement_lines(
                       source, store_id, marketplace_scope, settlement_id, line_no,
                       settlement_start_time, settlement_end_time, deposit_time,
                       total_amount, currency, transaction_type, order_id,
                       merchant_order_id, adjustment_id, shipment_id,
                       marketplace_name, amount_type, amount_description, amount,
                       fulfillment_id, posted_time, order_item_code,
                       merchant_order_item_id, merchant_adjustment_item_id, sku,
                       quantity_purchased, promotion_id, report_id,
                       report_created_at, source_raw_id, payload_checksum, active
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE
                   )
                   ON CONFLICT (
                       source, store_id, marketplace_scope, settlement_id, line_no
                   ) DO UPDATE SET
                       settlement_start_time=EXCLUDED.settlement_start_time,
                       settlement_end_time=EXCLUDED.settlement_end_time,
                       deposit_time=EXCLUDED.deposit_time,
                       total_amount=EXCLUDED.total_amount,
                       currency=EXCLUDED.currency,
                       transaction_type=EXCLUDED.transaction_type,
                       order_id=EXCLUDED.order_id,
                       merchant_order_id=EXCLUDED.merchant_order_id,
                       adjustment_id=EXCLUDED.adjustment_id,
                       shipment_id=EXCLUDED.shipment_id,
                       marketplace_name=EXCLUDED.marketplace_name,
                       amount_type=EXCLUDED.amount_type,
                       amount_description=EXCLUDED.amount_description,
                       amount=EXCLUDED.amount,
                       fulfillment_id=EXCLUDED.fulfillment_id,
                       posted_time=EXCLUDED.posted_time,
                       order_item_code=EXCLUDED.order_item_code,
                       merchant_order_item_id=EXCLUDED.merchant_order_item_id,
                       merchant_adjustment_item_id=EXCLUDED.merchant_adjustment_item_id,
                       sku=EXCLUDED.sku,
                       quantity_purchased=EXCLUDED.quantity_purchased,
                       promotion_id=EXCLUDED.promotion_id,
                       report_id=EXCLUDED.report_id,
                       report_created_at=EXCLUDED.report_created_at,
                       source_raw_id=EXCLUDED.source_raw_id,
                       payload_checksum=EXCLUDED.payload_checksum,
                       active=TRUE,
                       updated_at=NOW()""",
                (
                    SOURCE,
                    config.store_id,
                    config.marketplace,
                    row["settlement_id"],
                    row["line_no"],
                    row["settlement_start_time"],
                    row["settlement_end_time"],
                    row["deposit_time"],
                    row["total_amount"],
                    row["currency"],
                    row["transaction_type"],
                    row["order_id"],
                    row["merchant_order_id"],
                    row["adjustment_id"],
                    row["shipment_id"],
                    row["marketplace_name"],
                    row["amount_type"],
                    row["amount_description"],
                    row["amount"],
                    row["fulfillment_id"],
                    row["posted_time"],
                    row["order_item_code"],
                    row["merchant_order_item_id"],
                    row["merchant_adjustment_item_id"],
                    row["sku"],
                    row["quantity_purchased"],
                    row["promotion_id"],
                    report_id,
                    report["report_created_at"],
                    raw_ids[row["line_no"]],
                    checksum,
                ),
            )
        conn.execute(
            """INSERT INTO amazon_settlement_periods(
                   source, store_id, marketplace_scope, settlement_id,
                   settlement_start_time, settlement_end_time, deposit_time,
                   currency, net_payout, detail_amount_total,
                   reconciliation_delta, summary_row_count, detail_row_count,
                   marketplace_name_count, report_id, report_created_at, active
               ) VALUES (
                   %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s, %s, %s, TRUE
               )
               ON CONFLICT (source, store_id, marketplace_scope, settlement_id)
               DO UPDATE SET
                   settlement_start_time=EXCLUDED.settlement_start_time,
                   settlement_end_time=EXCLUDED.settlement_end_time,
                   deposit_time=EXCLUDED.deposit_time,
                   currency=EXCLUDED.currency,
                   net_payout=EXCLUDED.net_payout,
                   detail_amount_total=EXCLUDED.detail_amount_total,
                   reconciliation_delta=EXCLUDED.reconciliation_delta,
                   summary_row_count=EXCLUDED.summary_row_count,
                   detail_row_count=EXCLUDED.detail_row_count,
                   marketplace_name_count=EXCLUDED.marketplace_name_count,
                   report_id=EXCLUDED.report_id,
                   report_created_at=EXCLUDED.report_created_at,
                   active=TRUE,
                   updated_at=NOW()""",
            (
                SOURCE,
                config.store_id,
                config.marketplace,
                context["settlement_id"],
                context["settlement_start_time"],
                context["settlement_end_time"],
                context["deposit_time"],
                context["currency"],
                context["net_payout"],
                context["detail_amount_total"],
                context["reconciliation_delta"],
                context["summary_row_count"],
                context["detail_row_count"],
                context["marketplace_name_count"],
                report_id,
                report["report_created_at"],
            ),
        )
    conn.execute(
        """UPDATE amazon_settlement_reports SET
               processing_status='COMPLETE', fetched_at=%s,
               report_checksum=%s, settlement_id=%s,
               source_row_count=%s, normalized_row_count=%s,
               rejected_row_count=0, reconciliation_delta=%s,
               metadata=metadata || %s, updated_at=NOW()
           WHERE report_id=%s""",
        (
            fetched_at,
            document_checksum,
            context["settlement_id"],
            len(rows),
            len(normalized),
            context["reconciliation_delta"],
            Jsonb(
                {
                    "headers": headers,
                    "localized_number_parser": "separator-inference-v1",
                    "applied_as_current": applied,
                    "marketplace_scope_warning": (
                        "connection scope; rows can contain multiple marketplace names"
                    ),
                }
            ),
            report_id,
        ),
    )
    return applied


def _process_report(
    conn: Connection,
    client: SPAPIClient,
    *,
    attempt_id: UUID,
    config: SettlementSyncConfig,
    listed_report: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    report = _upsert_report_listing(
        conn,
        attempt_id=attempt_id,
        config=config,
        report=listed_report,
    )
    if report["processing_status"] == "COMPLETE":
        return {
            "status": "duplicate",
            "rows": int(report["normalized_row_count"] or 0),
            "applied": False,
        }
    try:
        document_id = listed_report.get("reportDocumentId")
        if not isinstance(document_id, str) or not document_id:
            raise SettlementNormalizationError("missing_report_document_id")
        document = client.get_report_document(document_id)
        content = client.download_report_document(document["url"])
        text = decode_report_document(content, document.get("compressionAlgorithm"))
        headers, rows = parse_settlement_tsv(text)
        checksum = hashlib.sha256(content).hexdigest()
        raw_ids = _store_raw_rows(
            conn,
            attempt_id=attempt_id,
            config=config,
            report_id=report["report_id"],
            rows=rows,
            fetched_at=now,
        )
        conn.execute(
            """UPDATE amazon_settlement_reports
               SET fetched_at=%s, report_checksum=%s, source_row_count=%s,
                   updated_at=NOW()
               WHERE report_id=%s""",
            (now, checksum, len(rows), report["report_id"]),
        )
        normalized, context = normalize_settlement_document(rows)
        report["report_created_at"] = _parse_api_time(
            listed_report.get("createdTime"), "report_created_at"
        )
        applied = _store_valid_document(
            conn,
            attempt_id=attempt_id,
            config=config,
            report=report,
            headers=headers,
            rows=rows,
            raw_ids=raw_ids,
            normalized=normalized,
            context=context,
            fetched_at=now,
            document_checksum=checksum,
        )
        return {"status": "complete", "rows": len(rows), "applied": applied}
    except (SettlementNormalizationError, SPAPIError) as exc:
        _record_document_failure(
            conn,
            attempt_id=attempt_id,
            report_id=report["report_id"],
            error_code=str(exc)[:200],
        )
        return {"status": "error", "rows": 0, "applied": False}


def sync_settlements(
    conn: Connection,
    client: SPAPIClient,
    config: SettlementSyncConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    config.validate()
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    _register_store(conn, config)
    attempt = _start_attempt(conn, config, current_time)
    attempt_id = attempt["id"]
    try:
        reports, pages = list_all_settlement_reports(
            client, config, now=current_time
        )
    except Exception as exc:
        conn.execute(
            """UPDATE amazon_sync_attempts
               SET status='failed', error_type=%s, updated_at=NOW()
               WHERE id=%s""",
            (type(exc).__name__, attempt_id),
        )
        conn.commit()
        raise

    completed = duplicates = errors = applied = total_rows = 0
    for report in reports:
        with conn.transaction():
            outcome = _process_report(
                conn,
                client,
                attempt_id=attempt_id,
                config=config,
                listed_report=report,
                now=current_time,
            )
        if outcome["status"] == "complete":
            completed += 1
        elif outcome["status"] == "duplicate":
            duplicates += 1
        else:
            errors += 1
        applied += int(outcome["applied"])
        total_rows += outcome["rows"]

    latest = conn.execute(
        """SELECT MAX(COALESCE(settlement_end_time, deposit_time)) AS latest,
                  COUNT(DISTINCT currency) AS currency_count,
                  MIN(currency) AS currency
           FROM amazon_settlement_periods
           WHERE source=%s AND store_id=%s AND marketplace_scope=%s AND active""",
        (SOURCE, config.store_id, config.marketplace),
    ).fetchone()
    source_updated_at = max(
        (
            _parse_api_time(report.get("createdTime"), "report_created_at")
            for report in reports
        ),
        default=current_time,
    )
    catalog_checksum = hashlib.sha256(
        json.dumps(sorted(str(report["reportId"]) for report in reports)).encode()
    ).hexdigest()
    status = "partial" if errors else "complete"
    business_time = latest["latest"] or source_updated_at
    currency = latest["currency"] if latest["currency_count"] == 1 else None
    core_run = ingest_run(
        conn,
        DatasetRunIn(
            external_run_id=str(attempt_id),
            source=SOURCE,
            store_id=config.store_id,
            marketplace=config.marketplace,
            dataset=DATASET,
            business_date=business_time.astimezone(ZoneInfo(config.timezone)).date(),
            fetched_at=current_time,
            source_updated_at=source_updated_at,
            ingestion_status=status,
            source_count=len(reports),
            normalized_count=completed,
            duplicate_count=duplicates,
            error_count=errors,
            checksum=catalog_checksum,
            timezone=config.timezone,
            currency=currency,
            raw_reference=f"sp-api://reports/{REPORT_TYPE}",
            schema_version=SCHEMA_VERSION,
            formula_version=FORMULA_VERSION,
            is_provisional=False,
            metadata={
                "reports_pages": pages,
                "reports_seen": len(reports),
                "reports_downloaded": completed,
                "reports_existing": duplicates,
                "reports_applied": applied,
                "downloaded_row_count": total_rows,
                "created_since_days": config.created_since_days,
                "automatically_generated_reports": True,
                "marketplace_scope_is_not_row_marketplace": True,
            },
        ),
    )
    ensure_default_rules(
        conn,
        DATASET,
        max_age_minutes=4320,
        source=SOURCE,
        store_id=config.store_id,
        marketplace=config.marketplace,
    )
    conn.execute(
        """UPDATE amazon_sync_attempts SET
               status=%s, pages_completed=%s, rows_pulled=%s,
               rows_inserted=%s, rows_skipped=%s, rows_errored=%s,
               max_source_updated_at=%s, core_run_id=%s,
               error_type=%s, updated_at=NOW()
           WHERE id=%s""",
        (
            "partial" if errors else "completed",
            pages,
            total_rows,
            completed,
            duplicates,
            errors,
            source_updated_at,
            UUID(core_run["run_id"]),
            "SettlementReportError" if errors else None,
            attempt_id,
        ),
    )
    conn.commit()
    return {
        "id": str(attempt_id),
        "status": "partial" if errors else "completed",
        "reports_seen": len(reports),
        "reports_downloaded": completed,
        "reports_existing": duplicates,
        "reports_applied": applied,
        "reports_errored": errors,
        "rows_downloaded": total_rows,
        "pages_completed": pages,
        "core_run_id": core_run["run_id"],
    }
