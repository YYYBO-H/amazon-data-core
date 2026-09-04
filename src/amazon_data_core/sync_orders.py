from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from psycopg.types.json import Jsonb

from .connectors.sp_api import SPAPIClient
from .contracts import DatasetRunIn
from .engine import ingest_run
from .rules import ensure_default_rules

SOURCE = "amazon_sp_api_orders_v2026"
DATASET = "orders"
SCHEMA_VERSION = "amazon-orders-2026-01-01-raw-v1"
FORMULA_VERSION = "amazon-orders-canonical-v1"
DEFAULT_INCLUDED_DATA = ("FULFILLMENT", "PROCEEDS")
PII_INCLUDED_DATA = {"BUYER", "RECIPIENT"}


class OrderNormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class OrdersSyncConfig:
    store_id: str
    marketplace: str
    region: str = "NA"
    timezone: str = "UTC"
    currency: str | None = None
    initial_days: int = 60
    lookback_minutes: int = 5
    safety_lag_minutes: int = 2
    max_results_per_page: int = 100
    included_data: tuple[str, ...] = DEFAULT_INCLUDED_DATA

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
        if self.initial_days < 1:
            raise ValueError("initial_days must be positive")
        if self.lookback_minutes < 0:
            raise ValueError("lookback_minutes cannot be negative")
        if self.safety_lag_minutes < 2:
            raise ValueError("safety_lag_minutes must be at least 2")
        if not 1 <= self.max_results_per_page <= 100:
            raise ValueError("max_results_per_page must be between 1 and 100")
        requested_pii = PII_INCLUDED_DATA.intersection(self.included_data)
        if requested_pii:
            raise ValueError(
                "PII datasets are intentionally unsupported: "
                + ", ".join(sorted(requested_pii))
            )


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise OrderNormalizationError(f"missing_{field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrderNormalizationError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise OrderNormalizationError(f"timezone_missing_{field}")
    return parsed.astimezone(UTC)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_order_payload(order: dict[str, Any]) -> dict[str, Any]:
    """PII is out of scope for the first local connector."""
    blocked = {"buyer", "recipient", "deliveryAddress", "customerAddress"}

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: sanitize(item)
                for key, item in value.items()
                if key not in blocked
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return sanitize(order)


def payload_checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_order(order: dict[str, Any], expected_marketplace: str) -> dict[str, Any]:
    order_id = order.get("orderId")
    if not isinstance(order_id, str) or not order_id:
        raise OrderNormalizationError("missing_order_id")
    created_time = _parse_datetime(order.get("createdTime"), "created_time")
    last_updated_time = _parse_datetime(
        order.get("lastUpdatedTime"), "last_updated_time"
    )
    sales_channel = order.get("salesChannel") or {}
    marketplace = sales_channel.get("marketplaceId") or expected_marketplace
    if marketplace != expected_marketplace:
        raise OrderNormalizationError("marketplace_mismatch")
    fulfillment = order.get("fulfillment") or {}
    proceeds = order.get("proceeds") or {}
    grand_total = proceeds.get("grandTotal") or {}
    amount: Decimal | None = None
    amount_value = grand_total.get("amount")
    if amount_value not in (None, ""):
        try:
            amount = Decimal(str(amount_value))
        except InvalidOperation as exc:
            raise OrderNormalizationError("invalid_order_total") from exc
    item_count = 0
    for item in order.get("orderItems") or []:
        quantity = item.get("quantityOrdered", 0)
        if isinstance(quantity, int) and quantity >= 0:
            item_count += quantity
    return {
        "order_id": order_id,
        "created_time": created_time,
        "last_updated_time": last_updated_time,
        "marketplace": marketplace,
        "fulfillment_status": fulfillment.get("fulfillmentStatus"),
        "fulfilled_by": fulfillment.get("fulfilledBy"),
        "proceeds_total_amount": amount,
        "currency": grand_total.get("currencyCode"),
        "item_count": item_count,
        "programs": order.get("programs") or [],
    }


def _register_store(conn: Connection, config: OrdersSyncConfig) -> None:
    conn.execute(
        """INSERT INTO amazon_store_connections(
               store_id, marketplace, region, timezone, currency
           ) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (store_id, marketplace) DO UPDATE SET
               region=EXCLUDED.region,
               timezone=EXCLUDED.timezone,
               currency=EXCLUDED.currency,
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


def _start_or_resume_attempt(
    conn: Connection,
    config: OrdersSyncConfig,
    *,
    now: datetime,
    resume: bool,
) -> dict[str, Any]:
    if resume:
        existing = conn.execute(
            """SELECT * FROM amazon_sync_attempts
               WHERE source=%s AND store_id=%s AND marketplace=%s AND dataset=%s
                 AND status IN ('running', 'failed', 'finalizing')
               ORDER BY created_at DESC LIMIT 1""",
            (SOURCE, config.store_id, config.marketplace, DATASET),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE amazon_sync_attempts
                   SET status=CASE WHEN status='finalizing' THEN status ELSE 'running' END,
                       error_type=NULL, updated_at=NOW()
                   WHERE id=%s""",
                (existing["id"],),
            )
            conn.commit()
            return dict(existing)

    cursor = conn.execute(
        """SELECT cursor_value FROM amazon_sync_cursors
           WHERE source=%s AND store_id=%s AND marketplace=%s AND dataset=%s""",
        (SOURCE, config.store_id, config.marketplace, DATASET),
    ).fetchone()
    window_end = now.astimezone(UTC) - timedelta(minutes=config.safety_lag_minutes)
    if cursor:
        window_start = cursor["cursor_value"] - timedelta(
            minutes=config.lookback_minutes
        )
    else:
        window_start = window_end - timedelta(days=config.initial_days)
    attempt_id = uuid4()
    row = conn.execute(
        """INSERT INTO amazon_sync_attempts(
               id, source, store_id, marketplace, dataset, status,
               window_start, window_end
           ) VALUES (%s, %s, %s, %s, %s, 'running', %s, %s)
           RETURNING *""",
        (
            attempt_id,
            SOURCE,
            config.store_id,
            config.marketplace,
            DATASET,
            window_start,
            window_end,
        ),
    ).fetchone()
    conn.commit()
    return dict(row)


def _write_order(
    conn: Connection,
    *,
    attempt_id: UUID,
    config: OrdersSyncConfig,
    order: dict[str, Any],
    fetched_at: datetime,
) -> tuple[str, datetime]:
    safe_payload = sanitize_order_payload(order)
    checksum = payload_checksum(safe_payload)
    normalized = normalize_order(safe_payload, config.marketplace)
    raw = conn.execute(
        """INSERT INTO amazon_orders_raw(
               sync_attempt_id, source, store_id, marketplace, order_id,
               source_updated_at, fetched_at, payload, payload_checksum
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (
               source, store_id, marketplace, order_id,
               source_updated_at, payload_checksum
           ) DO NOTHING
           RETURNING id""",
        (
            attempt_id,
            SOURCE,
            config.store_id,
            config.marketplace,
            normalized["order_id"],
            normalized["last_updated_time"],
            fetched_at,
            Jsonb(safe_payload),
            checksum,
        ),
    ).fetchone()
    if raw:
        raw_id = raw["id"]
    else:
        raw_id = conn.execute(
            """SELECT id FROM amazon_orders_raw
               WHERE source=%s AND store_id=%s AND marketplace=%s
                 AND order_id=%s AND source_updated_at=%s AND payload_checksum=%s""",
            (
                SOURCE,
                config.store_id,
                config.marketplace,
                normalized["order_id"],
                normalized["last_updated_time"],
                checksum,
            ),
        ).fetchone()["id"]
    current = conn.execute(
        """SELECT last_updated_time, payload_checksum FROM amazon_orders
           WHERE source=%s AND store_id=%s AND marketplace=%s AND order_id=%s
           FOR UPDATE""",
        (SOURCE, config.store_id, config.marketplace, normalized["order_id"]),
    ).fetchone()
    if current is None:
        action = "inserted"
    elif (
        normalized["last_updated_time"] < current["last_updated_time"]
        or (
            normalized["last_updated_time"] == current["last_updated_time"]
            and checksum == current["payload_checksum"]
        )
    ):
        return "skipped", normalized["last_updated_time"]
    else:
        action = "updated"
    conn.execute(
        """INSERT INTO amazon_orders(
               source, store_id, marketplace, order_id, created_time,
               last_updated_time, fulfillment_status, fulfilled_by,
               proceeds_total_amount, currency, item_count, programs,
               source_raw_id, payload_checksum
           ) VALUES (
               %(source)s, %(store_id)s, %(marketplace)s, %(order_id)s,
               %(created_time)s, %(last_updated_time)s,
               %(fulfillment_status)s, %(fulfilled_by)s,
               %(proceeds_total_amount)s, %(currency)s, %(item_count)s,
               %(programs)s, %(source_raw_id)s, %(payload_checksum)s
           )
           ON CONFLICT (source, store_id, marketplace, order_id) DO UPDATE SET
               created_time=EXCLUDED.created_time,
               last_updated_time=EXCLUDED.last_updated_time,
               fulfillment_status=EXCLUDED.fulfillment_status,
               fulfilled_by=EXCLUDED.fulfilled_by,
               proceeds_total_amount=EXCLUDED.proceeds_total_amount,
               currency=EXCLUDED.currency,
               item_count=EXCLUDED.item_count,
               programs=EXCLUDED.programs,
               source_raw_id=EXCLUDED.source_raw_id,
               payload_checksum=EXCLUDED.payload_checksum,
               updated_at=NOW()""",
        {
            **normalized,
            "source": SOURCE,
            "store_id": config.store_id,
            "source_raw_id": raw_id,
            "payload_checksum": checksum,
            "programs": Jsonb(normalized["programs"]),
        },
    )
    return action, normalized["last_updated_time"]


def _write_reject(
    conn: Connection,
    *,
    attempt_id: UUID,
    config: OrdersSyncConfig,
    order: dict[str, Any],
    error_code: str,
) -> None:
    safe_payload = sanitize_order_payload(order)
    checksum = payload_checksum(safe_payload)
    conn.execute(
        """INSERT INTO amazon_order_rejects(
               sync_attempt_id, store_id, marketplace,
               payload, payload_checksum, error_code
           ) VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (sync_attempt_id, payload_checksum, error_code) DO NOTHING""",
        (
            attempt_id,
            config.store_id,
            config.marketplace,
            Jsonb(safe_payload),
            checksum,
            error_code,
        ),
    )


def _finalize_attempt(
    conn: Connection,
    attempt: dict[str, Any],
    config: OrdersSyncConfig,
) -> dict[str, Any]:
    current = conn.execute(
        "SELECT * FROM amazon_sync_attempts WHERE id=%s FOR UPDATE",
        (attempt["id"],),
    ).fetchone()
    if current["status"] == "completed":
        return dict(current)
    errored = int(current["rows_errored"])
    complete = errored == 0
    finished_at = datetime.now(UTC)
    business_date = current["window_end"].astimezone(
        ZoneInfo(config.timezone)
    ).date()
    ensure_default_rules(
        conn,
        DATASET,
        max_age_minutes=180,
        source=SOURCE,
        store_id=config.store_id,
        marketplace=config.marketplace,
    )
    core_result = ingest_run(
        conn,
        DatasetRunIn(
            external_run_id=str(current["id"]),
            source=SOURCE,
            store_id=config.store_id,
            marketplace=config.marketplace,
            dataset=DATASET,
            business_date=business_date,
            fetched_at=finished_at,
            source_updated_at=current["window_end"],
            ingestion_status="complete" if complete else "partial",
            source_count=current["rows_pulled"],
            normalized_count=current["rows_inserted"] + current["rows_updated"],
            duplicate_count=current["rows_skipped"],
            error_count=errored,
            checksum=f"amazon-orders-attempt:{current['id']}",
            timezone=config.timezone,
            currency=config.currency,
            raw_reference=f"postgres://amazon_orders_raw?sync_attempt_id={current['id']}",
            schema_version=SCHEMA_VERSION,
            formula_version=FORMULA_VERSION,
            metadata={
                "api_path": "/orders/2026-01-01/orders",
                "window_start": current["window_start"].isoformat(),
                "window_end": current["window_end"].isoformat(),
                "pages_completed": current["pages_completed"],
                "included_data": list(config.included_data),
                "pii_requested": False,
            },
        ),
    )
    final_status = "completed" if complete else "partial"
    conn.execute(
        """UPDATE amazon_sync_attempts
           SET status=%s, core_run_id=%s, error_type=NULL, updated_at=NOW()
           WHERE id=%s""",
        (final_status, UUID(core_result["run_id"]), current["id"]),
    )
    if complete:
        conn.execute(
            """INSERT INTO amazon_sync_cursors(
                   source, store_id, marketplace, dataset,
                   cursor_value, last_attempt_id
               ) VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (source, store_id, marketplace, dataset) DO UPDATE SET
                   cursor_value=EXCLUDED.cursor_value,
                   last_attempt_id=EXCLUDED.last_attempt_id,
                   updated_at=NOW()""",
            (
                SOURCE,
                config.store_id,
                config.marketplace,
                DATASET,
                current["window_end"],
                current["id"],
            ),
        )
    conn.commit()
    return dict(
        conn.execute(
            "SELECT * FROM amazon_sync_attempts WHERE id=%s", (current["id"],)
        ).fetchone()
    )


def sync_orders(
    conn: Connection,
    client: SPAPIClient,
    config: OrdersSyncConfig,
    *,
    now: datetime | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    config.validate()
    lock_key = f"{SOURCE}|{config.store_id}|{config.marketplace}|{DATASET}"
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,))
    try:
        _register_store(conn, config)
        attempt = _start_or_resume_attempt(
            conn,
            config,
            now=now or datetime.now(UTC),
            resume=resume,
        )
        if attempt["status"] == "finalizing":
            return _finalize_attempt(conn, attempt, config)
        token = attempt["pagination_token"]
        try:
            while True:
                params: dict[str, Any] = {
                    "lastUpdatedAfter": _iso_z(attempt["window_start"]),
                    "lastUpdatedBefore": _iso_z(attempt["window_end"]),
                    "marketplaceIds": [config.marketplace],
                    "maxResultsPerPage": config.max_results_per_page,
                }
                if config.included_data:
                    params["includedData"] = list(config.included_data)
                if token:
                    params["paginationToken"] = token
                response = client.search_orders(params)
                orders = response["orders"]
                fetched_at = datetime.now(UTC)
                page_counts = {
                    "inserted": 0,
                    "updated": 0,
                    "skipped": 0,
                    "errored": 0,
                }
                max_updated: datetime | None = None
                for order in orders:
                    if not isinstance(order, dict):
                        order = {"invalid_order_type": type(order).__name__}
                    try:
                        action, source_updated = _write_order(
                            conn,
                            attempt_id=attempt["id"],
                            config=config,
                            order=order,
                            fetched_at=fetched_at,
                        )
                        page_counts[action] += 1
                        if max_updated is None or source_updated > max_updated:
                            max_updated = source_updated
                    except OrderNormalizationError as exc:
                        page_counts["errored"] += 1
                        _write_reject(
                            conn,
                            attempt_id=attempt["id"],
                            config=config,
                            order=order,
                            error_code=str(exc),
                        )
                next_token = (response.get("pagination") or {}).get("nextToken")
                status = "running" if next_token else "finalizing"
                conn.execute(
                    """UPDATE amazon_sync_attempts SET
                           status=%s,
                           pagination_token=%s,
                           pages_completed=pages_completed + 1,
                           rows_pulled=rows_pulled + %s,
                           rows_inserted=rows_inserted + %s,
                           rows_updated=rows_updated + %s,
                           rows_skipped=rows_skipped + %s,
                           rows_errored=rows_errored + %s,
                           max_source_updated_at=GREATEST(
                               max_source_updated_at, %s
                           ),
                           updated_at=NOW()
                       WHERE id=%s""",
                    (
                        status,
                        next_token,
                        len(orders),
                        page_counts["inserted"],
                        page_counts["updated"],
                        page_counts["skipped"],
                        page_counts["errored"],
                        max_updated,
                        attempt["id"],
                    ),
                )
                conn.commit()
                if not next_token:
                    break
                token = next_token
            return _finalize_attempt(conn, attempt, config)
        except Exception as exc:
            conn.rollback()
            conn.execute(
                """UPDATE amazon_sync_attempts
                   SET status=CASE WHEN status='finalizing' THEN status ELSE 'failed' END,
                       error_type=%s, updated_at=NOW()
                   WHERE id=%s""",
                (type(exc).__name__, attempt["id"]),
            )
            conn.commit()
            raise
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock_key,))
