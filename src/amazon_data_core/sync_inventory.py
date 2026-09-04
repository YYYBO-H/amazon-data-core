from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from psycopg.types.json import Jsonb

from .connectors.sp_api import SPAPIClient, SPAPIError
from .contracts import DatasetRunIn
from .engine import ingest_run
from .rules import ensure_default_rules

SOURCE = "amazon_sp_api_fba_inventory_v1"
DATASET = "fba_inventory"
SCHEMA_VERSION = "amazon-fba-inventory-v1-raw-v1"
FORMULA_VERSION = "amazon-fba-inventory-canonical-v1"


class InventoryNormalizationError(ValueError):
    pass


class InventoryPaginationExpired(SPAPIError):
    pass


@dataclass(frozen=True)
class InventorySyncConfig:
    store_id: str
    marketplace: str
    region: str = "NA"
    timezone: str = "UTC"
    currency: str | None = None
    max_pagination_restarts: int = 3

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
        if self.max_pagination_restarts < 0:
            raise ValueError("max_pagination_restarts cannot be negative")


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise InventoryNormalizationError("invalid_last_updated_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InventoryNormalizationError("invalid_last_updated_time") from exc
    if parsed.tzinfo is None:
        raise InventoryNormalizationError("timezone_missing_last_updated_time")
    return parsed.astimezone(UTC)


def _quantity(value: Any, field: str) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise InventoryNormalizationError(f"invalid_{field}")
    if isinstance(value, float) and not value.is_integer():
        raise InventoryNormalizationError(f"invalid_{field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InventoryNormalizationError(f"invalid_{field}") from exc
    if parsed < 0:
        raise InventoryNormalizationError(f"negative_{field}")
    return parsed


def _text(value: Any, field: str, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise InventoryNormalizationError(f"missing_{field}")
        return None
    if not isinstance(value, str):
        raise InventoryNormalizationError(f"invalid_{field}")
    parsed = value.strip()
    if not parsed:
        if required:
            raise InventoryNormalizationError(f"missing_{field}")
        return None
    return parsed


def payload_checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_inventory_summary(item: dict[str, Any]) -> dict[str, Any]:
    asin = _text(item.get("asin"), "asin", required=True)
    seller_sku = _text(item.get("sellerSku"), "seller_sku", required=True)
    fn_sku = _text(item.get("fnSku"), "fn_sku")
    condition = _text(item.get("condition"), "condition")
    key_material = json.dumps(
        [seller_sku, fn_sku or "", condition or ""],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    inventory_key = hashlib.sha256(key_material.encode()).hexdigest()
    details = item.get("inventoryDetails") or {}
    if not isinstance(details, dict):
        raise InventoryNormalizationError("invalid_inventory_details")
    reserved = details.get("reservedQuantity") or {}
    researching = details.get("researchingQuantity") or {}
    unfulfillable = details.get("unfulfillableQuantity") or {}
    if not isinstance(reserved, dict):
        raise InventoryNormalizationError("invalid_reserved_quantity")
    if not isinstance(researching, dict):
        raise InventoryNormalizationError("invalid_researching_quantity")
    if not isinstance(unfulfillable, dict):
        raise InventoryNormalizationError("invalid_unfulfillable_quantity")
    product_name = item.get("productName")
    if product_name is not None and not isinstance(product_name, str):
        raise InventoryNormalizationError("invalid_product_name")
    return {
        "inventory_key": inventory_key,
        "asin": asin,
        "seller_sku": seller_sku,
        "fn_sku": fn_sku,
        "item_condition": condition,
        "product_name": product_name,
        "total_quantity": _quantity(item.get("totalQuantity"), "total_quantity"),
        "fulfillable_quantity": _quantity(
            details.get("fulfillableQuantity"), "fulfillable_quantity"
        ),
        "inbound_working_quantity": _quantity(
            details.get("inboundWorkingQuantity"), "inbound_working_quantity"
        ),
        "inbound_shipped_quantity": _quantity(
            details.get("inboundShippedQuantity"), "inbound_shipped_quantity"
        ),
        "inbound_receiving_quantity": _quantity(
            details.get("inboundReceivingQuantity"), "inbound_receiving_quantity"
        ),
        "reserved_quantity": _quantity(
            reserved.get("totalReservedQuantity"), "reserved_quantity"
        ),
        "researching_quantity": _quantity(
            researching.get("totalResearchingQuantity"), "researching_quantity"
        ),
        "unfulfillable_quantity": _quantity(
            unfulfillable.get("totalUnfulfillableQuantity"),
            "unfulfillable_quantity",
        ),
        "source_updated_at": _parse_optional_datetime(item.get("lastUpdatedTime")),
    }


def _register_store(conn: Connection, config: InventorySyncConfig) -> None:
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
    conn: Connection,
    config: InventorySyncConfig,
    snapshot_at: datetime,
) -> dict[str, Any]:
    finalizing = conn.execute(
        """SELECT * FROM amazon_sync_attempts
           WHERE source=%s AND store_id=%s AND marketplace=%s AND dataset=%s
             AND status='finalizing'
           ORDER BY created_at DESC LIMIT 1""",
        (SOURCE, config.store_id, config.marketplace, DATASET),
    ).fetchone()
    if finalizing:
        return dict(finalizing)
    conn.execute(
        """UPDATE amazon_sync_attempts
           SET status='failed', error_type='AbandonedSnapshot', updated_at=NOW()
           WHERE source=%s AND store_id=%s AND marketplace=%s AND dataset=%s
             AND status='running'""",
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
            snapshot_at,
            snapshot_at,
        ),
    ).fetchone()
    conn.commit()
    return dict(row)


def _fetch_full_snapshot(
    client: SPAPIClient,
    config: InventorySyncConfig,
) -> tuple[list[dict[str, Any]], int]:
    for restart in range(config.max_pagination_restarts + 1):
        items: list[dict[str, Any]] = []
        next_token: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {
                "details": True,
                "granularityType": "Marketplace",
                "granularityId": config.marketplace,
                "marketplaceIds": [config.marketplace],
            }
            if next_token:
                params["nextToken"] = next_token
            try:
                response = client.get_inventory_summaries(params)
            except SPAPIError as exc:
                if next_token and exc.status_code == 400:
                    break
                raise
            pages += 1
            items.extend(response["payload"]["inventorySummaries"])
            next_token = (response.get("pagination") or {}).get("nextToken")
            if not next_token:
                return items, pages
        if restart >= config.max_pagination_restarts:
            raise InventoryPaginationExpired(
                "Amazon FBA Inventory nextToken repeatedly expired",
                retryable=True,
            )
    raise InventoryPaginationExpired(
        "Amazon FBA Inventory pagination failed",
        retryable=True,
    )


def _write_reject(
    conn: Connection,
    *,
    attempt_id: UUID,
    config: InventorySyncConfig,
    item: dict[str, Any],
    error_code: str,
) -> None:
    checksum = payload_checksum(item)
    conn.execute(
        """INSERT INTO amazon_fba_inventory_rejects(
               sync_attempt_id, store_id, marketplace,
               payload, payload_checksum, error_code
           ) VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (sync_attempt_id, payload_checksum, error_code) DO NOTHING""",
        (
            attempt_id,
            config.store_id,
            config.marketplace,
            Jsonb(item),
            checksum,
            error_code,
        ),
    )


def _write_inventory(
    conn: Connection,
    *,
    attempt_id: UUID,
    config: InventorySyncConfig,
    item: dict[str, Any],
    fetched_at: datetime,
) -> tuple[str, datetime | None, str]:
    normalized = normalize_inventory_summary(item)
    checksum = payload_checksum(item)
    raw = conn.execute(
        """INSERT INTO amazon_fba_inventory_raw(
               sync_attempt_id, source, store_id, marketplace, inventory_key,
               asin, seller_sku, fn_sku, source_updated_at, fetched_at,
               payload, payload_checksum
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (
               source, store_id, marketplace, inventory_key, payload_checksum
           ) DO NOTHING
           RETURNING id""",
        (
            attempt_id,
            SOURCE,
            config.store_id,
            config.marketplace,
            normalized["inventory_key"],
            normalized["asin"],
            normalized["seller_sku"],
            normalized["fn_sku"],
            normalized["source_updated_at"],
            fetched_at,
            Jsonb(item),
            checksum,
        ),
    ).fetchone()
    if raw:
        raw_id = raw["id"]
    else:
        raw_id = conn.execute(
            """SELECT id FROM amazon_fba_inventory_raw
               WHERE source=%s AND store_id=%s AND marketplace=%s
                 AND inventory_key=%s AND payload_checksum=%s""",
            (
                SOURCE,
                config.store_id,
                config.marketplace,
                normalized["inventory_key"],
                checksum,
            ),
        ).fetchone()["id"]
    current = conn.execute(
        """SELECT source_updated_at, payload_checksum
           FROM amazon_fba_inventory
           WHERE source=%s AND store_id=%s AND marketplace=%s AND inventory_key=%s
           FOR UPDATE""",
        (
            SOURCE,
            config.store_id,
            config.marketplace,
            normalized["inventory_key"],
        ),
    ).fetchone()
    stale = bool(
        current
        and current["source_updated_at"]
        and (
            normalized["source_updated_at"] is None
            or normalized["source_updated_at"] < current["source_updated_at"]
        )
    )
    if current is None:
        action = "inserted"
    elif stale or current["payload_checksum"] == checksum:
        action = "skipped"
    else:
        action = "updated"
    if action == "skipped":
        conn.execute(
            """UPDATE amazon_fba_inventory
               SET last_seen_attempt_id=%s, last_seen_at=%s, active=TRUE
               WHERE source=%s AND store_id=%s AND marketplace=%s
                 AND inventory_key=%s""",
            (
                attempt_id,
                fetched_at,
                SOURCE,
                config.store_id,
                config.marketplace,
                normalized["inventory_key"],
            ),
        )
    else:
        conn.execute(
            """INSERT INTO amazon_fba_inventory(
                   source, store_id, marketplace, inventory_key,
                   asin, seller_sku, fn_sku, item_condition, product_name,
                   total_quantity, fulfillable_quantity,
                   inbound_working_quantity, inbound_shipped_quantity,
                   inbound_receiving_quantity, reserved_quantity,
                   researching_quantity, unfulfillable_quantity,
                   source_updated_at, source_raw_id, payload_checksum,
                   last_seen_attempt_id, last_seen_at, active
               ) VALUES (
                   %(source)s, %(store_id)s, %(marketplace)s, %(inventory_key)s,
                   %(asin)s, %(seller_sku)s, %(fn_sku)s, %(item_condition)s,
                   %(product_name)s, %(total_quantity)s, %(fulfillable_quantity)s,
                   %(inbound_working_quantity)s, %(inbound_shipped_quantity)s,
                   %(inbound_receiving_quantity)s, %(reserved_quantity)s,
                   %(researching_quantity)s, %(unfulfillable_quantity)s,
                   %(source_updated_at)s, %(source_raw_id)s, %(payload_checksum)s,
                   %(last_seen_attempt_id)s, %(last_seen_at)s, TRUE
               )
               ON CONFLICT (source, store_id, marketplace, inventory_key) DO UPDATE SET
                   asin=EXCLUDED.asin,
                   seller_sku=EXCLUDED.seller_sku,
                   fn_sku=EXCLUDED.fn_sku,
                   item_condition=EXCLUDED.item_condition,
                   product_name=EXCLUDED.product_name,
                   total_quantity=EXCLUDED.total_quantity,
                   fulfillable_quantity=EXCLUDED.fulfillable_quantity,
                   inbound_working_quantity=EXCLUDED.inbound_working_quantity,
                   inbound_shipped_quantity=EXCLUDED.inbound_shipped_quantity,
                   inbound_receiving_quantity=EXCLUDED.inbound_receiving_quantity,
                   reserved_quantity=EXCLUDED.reserved_quantity,
                   researching_quantity=EXCLUDED.researching_quantity,
                   unfulfillable_quantity=EXCLUDED.unfulfillable_quantity,
                   source_updated_at=EXCLUDED.source_updated_at,
                   source_raw_id=EXCLUDED.source_raw_id,
                   payload_checksum=EXCLUDED.payload_checksum,
                   last_seen_attempt_id=EXCLUDED.last_seen_attempt_id,
                   last_seen_at=EXCLUDED.last_seen_at,
                   active=TRUE,
                   updated_at=NOW()""",
            {
                **normalized,
                "source": SOURCE,
                "store_id": config.store_id,
                "marketplace": config.marketplace,
                "source_raw_id": raw_id,
                "payload_checksum": checksum,
                "last_seen_attempt_id": attempt_id,
                "last_seen_at": fetched_at,
            },
        )
    conn.execute(
        """INSERT INTO amazon_fba_inventory_snapshot_rows(
               sync_attempt_id, raw_id, inventory_key, row_action
           ) VALUES (%s, %s, %s, %s)""",
        (attempt_id, raw_id, normalized["inventory_key"], action),
    )
    return action, normalized["source_updated_at"], normalized["inventory_key"]


def _finalize_attempt(
    conn: Connection,
    attempt: dict[str, Any],
    config: InventorySyncConfig,
) -> dict[str, Any]:
    current = conn.execute(
        "SELECT * FROM amazon_sync_attempts WHERE id=%s FOR UPDATE",
        (attempt["id"],),
    ).fetchone()
    if current["status"] == "completed":
        return dict(current)
    complete = int(current["rows_errored"]) == 0
    finished_at = datetime.now(UTC)
    if complete:
        conn.execute(
            """UPDATE amazon_fba_inventory
               SET active=FALSE, updated_at=NOW()
               WHERE source=%s AND store_id=%s AND marketplace=%s
                 AND active=TRUE AND last_seen_attempt_id<>%s""",
            (SOURCE, config.store_id, config.marketplace, current["id"]),
        )
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
            business_date=current["window_end"].astimezone(
                ZoneInfo(config.timezone)
            ).date(),
            fetched_at=finished_at,
            source_updated_at=current["window_end"],
            ingestion_status="complete" if complete else "partial",
            source_count=current["rows_pulled"],
            # An unchanged row is still a valid normalized member of this full
            # snapshot. "skipped" is only the canonical write action; it must
            # not be mislabeled as a duplicate source row in data quality.
            normalized_count=(
                current["rows_inserted"]
                + current["rows_updated"]
                + current["rows_skipped"]
            ),
            duplicate_count=0,
            error_count=current["rows_errored"],
            checksum=f"amazon-fba-inventory-attempt:{current['id']}",
            timezone=config.timezone,
            currency=config.currency,
            raw_reference=(
                "postgres://amazon_fba_inventory_snapshot_rows"
                f"?sync_attempt_id={current['id']}"
            ),
            schema_version=SCHEMA_VERSION,
            formula_version=FORMULA_VERSION,
            metadata={
                "api_path": "/fba/inventory/v1/summaries",
                "snapshot_mode": "full",
                "details": True,
                "pages_completed": current["pages_completed"],
                "unchanged_count": current["rows_skipped"],
                "scope": "FBA only; FBM is not included",
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


def sync_inventory(
    conn: Connection,
    client: SPAPIClient,
    config: InventorySyncConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    config.validate()
    lock_key = f"{SOURCE}|{config.store_id}|{config.marketplace}|{DATASET}"
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,))
    attempt: dict[str, Any] | None = None
    try:
        _register_store(conn, config)
        attempt = _start_attempt(conn, config, (now or datetime.now(UTC)).astimezone(UTC))
        if attempt["status"] == "finalizing":
            return _finalize_attempt(conn, attempt, config)
        items, pages = _fetch_full_snapshot(client, config)
        fetched_at = datetime.now(UTC)
        counts = {"inserted": 0, "updated": 0, "skipped": 0, "errored": 0}
        max_updated: datetime | None = None
        seen_keys: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                item = {"invalid_inventory_type": type(item).__name__}
            try:
                inventory_key = normalize_inventory_summary(item)["inventory_key"]
                if inventory_key in seen_keys:
                    raise InventoryNormalizationError("duplicate_inventory_key")
                seen_keys.add(inventory_key)
                action, source_updated, inventory_key = _write_inventory(
                    conn,
                    attempt_id=attempt["id"],
                    config=config,
                    item=item,
                    fetched_at=fetched_at,
                )
                counts[action] += 1
                if source_updated and (
                    max_updated is None or source_updated > max_updated
                ):
                    max_updated = source_updated
            except InventoryNormalizationError as exc:
                counts["errored"] += 1
                _write_reject(
                    conn,
                    attempt_id=attempt["id"],
                    config=config,
                    item=item,
                    error_code=str(exc),
                )
        conn.execute(
            """UPDATE amazon_sync_attempts SET
                   status='finalizing', pages_completed=%s,
                   rows_pulled=%s, rows_inserted=%s, rows_updated=%s,
                   rows_skipped=%s, rows_errored=%s,
                   max_source_updated_at=%s, pagination_token=NULL,
                   updated_at=NOW()
               WHERE id=%s""",
            (
                pages,
                len(items),
                counts["inserted"],
                counts["updated"],
                counts["skipped"],
                counts["errored"],
                max_updated,
                attempt["id"],
            ),
        )
        conn.commit()
        return _finalize_attempt(conn, attempt, config)
    except Exception as exc:
        conn.rollback()
        if attempt is not None:
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
