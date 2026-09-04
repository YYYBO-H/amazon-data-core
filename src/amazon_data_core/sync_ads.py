from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from psycopg.types.json import Jsonb

from .connectors.ads_api import AdsAPIError, AmazonAdsClient, verify_profile_scope
from .contracts import DatasetRunIn
from .engine import ingest_run
from .rules import ensure_default_rules

SOURCE = "amazon_ads_reporting_v3_sp_campaigns"
DATASET = "ads_campaign_daily"
REPORTING_API_VERSION = "v3"
REPORT_TYPE_ID = "spCampaigns"
AD_PRODUCT = "SPONSORED_PRODUCTS"
GROUP_BY = "campaign"
SCHEMA_VERSION = "amazon-ads-sp-campaigns-v3-raw-v1"
FORMULA_VERSION = "amazon-ads-sp-campaigns-v3-canonical-v2"
TERMINAL_FAILURE_STATUSES = {"FAILURE", "FAILED", "CANCELLED"}
PENDING_STATUSES = {"PENDING", "PROCESSING", "IN_PROGRESS"}

SP_CAMPAIGN_COLUMNS = (
    "date",
    "campaignId",
    "campaignName",
    "campaignStatus",
    "campaignBudgetAmount",
    "campaignBudgetType",
    "campaignBiddingStrategy",
    "impressions",
    "clicks",
    "cost",
    "spend",
    "sales1d",
    "sales7d",
    "sales14d",
    "purchases1d",
    "purchases7d",
    "purchases14d",
    "unitsSoldClicks1d",
    "unitsSoldClicks7d",
    "unitsSoldClicks14d",
)


class AdsNormalizationError(ValueError):
    pass


class AdsReportPending(AdsAPIError):
    pass


@dataclass(frozen=True)
class AdsReportSpec:
    source: str
    dataset: str
    report_type_id: str
    ad_product: str
    group_by: str
    columns: tuple[str, ...]
    filters: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AdsCampaignSyncConfig:
    store_id: str
    marketplace: str
    profile_id: str
    start_date: date
    end_date: date
    region: str = "NA"
    timezone: str = "UTC"
    currency: str = "USD"
    attribution_window_days: int = 14
    poll_timeout_seconds: int = 1800
    poll_interval_seconds: int = 15

    def validate(self) -> None:
        if not self.store_id.strip():
            raise ValueError("store_id is required")
        if not self.marketplace.strip():
            raise ValueError("marketplace is required")
        if not self.profile_id.strip():
            raise ValueError("profile_id is required")
        if self.region.upper() not in {"NA", "EU", "FE"}:
            raise ValueError("region must be NA, EU or FE")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        if self.start_date > self.end_date:
            raise ValueError("start_date cannot be after end_date")
        if (self.end_date - self.start_date).days > 30:
            raise ValueError("Amazon Ads v3 reports can cover at most 31 days")
        if len(self.currency) != 3:
            raise ValueError("currency must be a three-letter code")
        if not 1 <= self.attribution_window_days <= 30:
            raise ValueError("attribution_window_days must be between 1 and 30")
        if self.poll_timeout_seconds < 0:
            raise ValueError("poll_timeout_seconds cannot be negative")
        if self.poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds cannot be negative")


def _text(value: Any, field: str, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise AdsNormalizationError(f"missing_{field}")
        return None
    if not isinstance(value, str):
        raise AdsNormalizationError(f"invalid_{field}")
    parsed = value.strip()
    if not parsed:
        if required:
            raise AdsNormalizationError(f"missing_{field}")
        return None
    return parsed


def _identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AdsNormalizationError(f"invalid_{field}")
    parsed = str(value).strip()
    if not parsed:
        raise AdsNormalizationError(f"missing_{field}")
    return parsed


def _integer(value: Any, field: str) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise AdsNormalizationError(f"invalid_{field}")
    if isinstance(value, float) and not value.is_integer():
        raise AdsNormalizationError(f"invalid_{field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AdsNormalizationError(f"invalid_{field}") from exc
    if parsed < 0:
        raise AdsNormalizationError(f"negative_{field}")
    return parsed


def _decimal(value: Any, field: str, *, optional: bool = False) -> Decimal | None:
    if value in (None, ""):
        return None if optional else Decimal("0")
    if isinstance(value, bool):
        raise AdsNormalizationError(f"invalid_{field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AdsNormalizationError(f"invalid_{field}") from exc
    if not parsed.is_finite():
        raise AdsNormalizationError(f"invalid_{field}")
    if parsed < 0:
        raise AdsNormalizationError(f"negative_{field}")
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


def normalize_campaign_row(
    row: dict[str, Any],
    *,
    start_date: date,
    end_date: date,
    attribution_window_days: int,
) -> dict[str, Any]:
    date_value = _text(row.get("date"), "date", required=True)
    try:
        report_date = date.fromisoformat(date_value or "")
    except ValueError as exc:
        raise AdsNormalizationError("invalid_date") from exc
    if not start_date <= report_date <= end_date:
        raise AdsNormalizationError("date_outside_requested_window")
    campaign_id = _identifier(row.get("campaignId"), "campaign_id")
    cost = _decimal(row.get("cost"), "cost", optional=True)
    spend = _decimal(row.get("spend"), "spend", optional=True)
    if cost is not None and spend is not None and cost != spend:
        raise AdsNormalizationError("cost_spend_mismatch")
    resolved_spend = cost if cost is not None else spend
    if resolved_spend is None:
        raise AdsNormalizationError("missing_cost_and_spend")
    row_key = hashlib.sha256(f"{report_date}|{campaign_id}".encode()).hexdigest()
    return {
        "row_key": row_key,
        "report_date": report_date,
        "campaign_id": campaign_id,
        "campaign_name": _text(row.get("campaignName"), "campaign_name"),
        "campaign_status": _text(row.get("campaignStatus"), "campaign_status"),
        "campaign_budget_amount": _decimal(
            row.get("campaignBudgetAmount"),
            "campaign_budget_amount",
            optional=True,
        ),
        "campaign_budget_type": _text(
            row.get("campaignBudgetType"), "campaign_budget_type"
        ),
        "campaign_bidding_strategy": _text(
            row.get("campaignBiddingStrategy"), "campaign_bidding_strategy"
        ),
        "impressions": _integer(row.get("impressions"), "impressions"),
        "clicks": _integer(row.get("clicks"), "clicks"),
        "spend": resolved_spend,
        "sales_1d": _decimal(row.get("sales1d"), "sales_1d"),
        "sales_7d": _decimal(row.get("sales7d"), "sales_7d"),
        "sales_14d": _decimal(row.get("sales14d"), "sales_14d"),
        "purchases_1d": _integer(row.get("purchases1d"), "purchases_1d"),
        "purchases_7d": _integer(row.get("purchases7d"), "purchases_7d"),
        "purchases_14d": _integer(row.get("purchases14d"), "purchases_14d"),
        "units_sold_clicks_1d": _integer(
            row.get("unitsSoldClicks1d"), "units_sold_clicks_1d"
        ),
        "units_sold_clicks_7d": _integer(
            row.get("unitsSoldClicks7d"), "units_sold_clicks_7d"
        ),
        "units_sold_clicks_14d": _integer(
            row.get("unitsSoldClicks14d"), "units_sold_clicks_14d"
        ),
        "provisional_until": report_date + timedelta(days=attribution_window_days),
    }


def _parse_report_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _window(config: AdsCampaignSyncConfig) -> tuple[datetime, datetime]:
    timezone = ZoneInfo(config.timezone)
    start = datetime.combine(config.start_date, datetime_time.min, timezone)
    end = datetime.combine(config.end_date + timedelta(days=1), datetime_time.min, timezone)
    return start.astimezone(UTC), end.astimezone(UTC)


CAMPAIGN_REPORT_SPEC = AdsReportSpec(
    source=SOURCE,
    dataset=DATASET,
    report_type_id=REPORT_TYPE_ID,
    ad_product=AD_PRODUCT,
    group_by=GROUP_BY,
    columns=SP_CAMPAIGN_COLUMNS,
    filters=(
        {
            "field": "campaignStatus",
            "values": ["ENABLED", "PAUSED", "ARCHIVED"],
        },
    ),
)


def build_ads_report_body(
    config: AdsCampaignSyncConfig,
    attempt_id: UUID,
    spec: AdsReportSpec,
) -> dict[str, Any]:
    configuration: dict[str, Any] = {
        "adProduct": spec.ad_product,
        "groupBy": [spec.group_by],
        "columns": list(spec.columns),
        "reportTypeId": spec.report_type_id,
        "timeUnit": "DAILY",
        "format": "GZIP_JSON",
    }
    if spec.filters:
        configuration["filters"] = list(spec.filters)
    return {
        "name": (
            f"amazon-data-core-{spec.report_type_id}-{config.start_date}-"
            f"{config.end_date}-{str(attempt_id)[:8]}"
        ),
        "startDate": config.start_date.isoformat(),
        "endDate": config.end_date.isoformat(),
        "configuration": configuration,
    }


def _register_store(conn: Connection, config: AdsCampaignSyncConfig) -> None:
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
            config.currency.upper(),
        ),
    )


def start_or_resume_ads_report(
    conn: Connection,
    config: AdsCampaignSyncConfig,
    spec: AdsReportSpec,
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = conn.execute(
        """SELECT a.*, r.report_id, r.report_status, r.request_body,
                  r.report_created_at, r.report_completed_at
           FROM amazon_sync_attempts a
           JOIN amazon_ads_reports r ON r.sync_attempt_id=a.id
           WHERE a.source=%s AND a.store_id=%s AND a.marketplace=%s
             AND a.dataset=%s AND r.start_date=%s AND r.end_date=%s
             AND r.report_type_id=%s
             AND (
                 a.status='finalizing'
                 OR (
                     a.status IN ('running', 'failed', 'partial')
                     AND r.report_status NOT IN ('FAILURE', 'FAILED', 'CANCELLED')
                 )
             )
           ORDER BY a.created_at DESC LIMIT 1""",
        (
            spec.source,
            config.store_id,
            config.marketplace,
            spec.dataset,
            config.start_date,
            config.end_date,
            spec.report_type_id,
        ),
    ).fetchone()
    if row:
        attempt = dict(row)
        report = {
            key: attempt.pop(key)
            for key in (
                "report_id",
                "report_status",
                "request_body",
                "report_created_at",
                "report_completed_at",
            )
        }
        if attempt["status"] in {"failed", "partial"}:
            conn.execute(
                """UPDATE amazon_sync_attempts
                   SET status='running', error_type=NULL, updated_at=NOW()
                   WHERE id=%s""",
                (attempt["id"],),
            )
            attempt["status"] = "running"
            conn.commit()
        return attempt, report

    conn.execute(
        """UPDATE amazon_sync_attempts
           SET status='failed', error_type='AbandonedAdsSync', updated_at=NOW()
           WHERE source=%s AND store_id=%s AND marketplace=%s AND dataset=%s
             AND status='running'""",
        (spec.source, config.store_id, config.marketplace, spec.dataset),
    )
    window_start, window_end = _window(config)
    attempt_id = uuid4()
    attempt = conn.execute(
        """INSERT INTO amazon_sync_attempts(
               id, source, store_id, marketplace, dataset, status,
               window_start, window_end
           ) VALUES (%s, %s, %s, %s, %s, 'running', %s, %s)
           RETURNING *""",
        (
            attempt_id,
            spec.source,
            config.store_id,
            config.marketplace,
            spec.dataset,
            window_start,
            window_end,
        ),
    ).fetchone()
    request_body = build_ads_report_body(config, attempt_id, spec)
    conn.execute(
        """INSERT INTO amazon_ads_reports(
               sync_attempt_id, source, store_id, marketplace, profile_id,
               reporting_api_version, report_type_id, ad_product, group_by,
               start_date, end_date, attribution_window_days,
               request_body, report_status
           ) VALUES (
               %s, %s, %s, %s, %s, %s, %s, %s, %s,
               %s, %s, %s, %s, 'NOT_REQUESTED'
           )""",
        (
            attempt_id,
            spec.source,
            config.store_id,
            config.marketplace,
            config.profile_id,
            REPORTING_API_VERSION,
            spec.report_type_id,
            spec.ad_product,
            spec.group_by,
            config.start_date,
            config.end_date,
            config.attribution_window_days,
            Jsonb(request_body),
        ),
    )
    conn.commit()
    return dict(attempt), {
        "report_id": None,
        "report_status": "NOT_REQUESTED",
        "request_body": request_body,
        "report_created_at": None,
        "report_completed_at": None,
    }


def request_and_wait_ads_report(
    conn: Connection,
    client: AmazonAdsClient,
    attempt: dict[str, Any],
    report: dict[str, Any],
    config: AdsCampaignSyncConfig,
) -> tuple[dict[str, Any], datetime]:
    report_id = report["report_id"]
    if not report_id:
        created = client.create_report(report["request_body"])
        report_id_value = created.get("reportId")
        if not isinstance(report_id_value, str) or not report_id_value.strip():
            raise AdsAPIError("Amazon Ads create-report response is missing reportId")
        report_id = report_id_value.strip()
        status = str(created.get("status") or "PENDING").upper()
        created_at = _parse_report_time(created.get("createdAt")) or datetime.now(UTC)
        conn.execute(
            """UPDATE amazon_ads_reports SET
                   report_id=%s, report_status=%s, report_created_at=%s,
                   updated_at=NOW()
               WHERE sync_attempt_id=%s""",
            (report_id, status, created_at, attempt["id"]),
        )
        conn.commit()

    deadline = time.monotonic() + config.poll_timeout_seconds
    while True:
        current = client.get_report(report_id)
        status = str(current.get("status") or "UNKNOWN").upper()
        created_at = _parse_report_time(current.get("createdAt"))
        completed_at = _parse_report_time(current.get("completedAt"))
        conn.execute(
            """UPDATE amazon_ads_reports SET
                   report_status=%s,
                   report_created_at=COALESCE(%s, report_created_at),
                   report_completed_at=COALESCE(%s, report_completed_at),
                   updated_at=NOW()
               WHERE sync_attempt_id=%s""",
            (status, created_at, completed_at, attempt["id"]),
        )
        conn.commit()
        if status == "COMPLETED":
            url = current.get("url")
            if not isinstance(url, str) or not url.strip():
                raise AdsAPIError("Amazon Ads completed report is missing download URL")
            effective_completed_at = completed_at or datetime.now(UTC)
            if completed_at is None:
                conn.execute(
                    """UPDATE amazon_ads_reports
                       SET report_completed_at=%s, updated_at=NOW()
                       WHERE sync_attempt_id=%s""",
                    (effective_completed_at, attempt["id"]),
                )
                conn.commit()
            return current, effective_completed_at
        if status in TERMINAL_FAILURE_STATUSES:
            raise AdsAPIError(f"Amazon Ads report reached terminal status {status}")
        if status not in PENDING_STATUSES:
            raise AdsAPIError(f"Amazon Ads report returned unknown status {status}")
        if time.monotonic() >= deadline:
            raise AdsReportPending(
                f"Amazon Ads report {report_id} is still {status}; rerun to resume",
                retryable=True,
            )
        client.sleep(config.poll_interval_seconds)


def _write_reject(
    conn: Connection,
    *,
    attempt_id: UUID,
    row: dict[str, Any],
    error_code: str,
) -> None:
    conn.execute(
        """INSERT INTO amazon_ads_campaign_rejects(
               sync_attempt_id, payload, payload_checksum, error_code
           ) VALUES (%s, %s, %s, %s)
           ON CONFLICT (sync_attempt_id, payload_checksum, error_code) DO NOTHING""",
        (attempt_id, Jsonb(row), payload_checksum(row), error_code),
    )


def _write_campaign(
    conn: Connection,
    *,
    attempt_id: UUID,
    report_id: str,
    report_completed_at: datetime,
    fetched_at: datetime,
    config: AdsCampaignSyncConfig,
    row: dict[str, Any],
) -> tuple[str, str]:
    normalized = normalize_campaign_row(
        row,
        start_date=config.start_date,
        end_date=config.end_date,
        attribution_window_days=config.attribution_window_days,
    )
    checksum = payload_checksum(row)
    raw = conn.execute(
        """INSERT INTO amazon_ads_campaign_raw(
               sync_attempt_id, report_id, row_key, report_date,
               campaign_id, payload, payload_checksum, fetched_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (sync_attempt_id, row_key) DO NOTHING
           RETURNING id, payload_checksum""",
        (
            attempt_id,
            report_id,
            normalized["row_key"],
            normalized["report_date"],
            normalized["campaign_id"],
            Jsonb(row),
            checksum,
            fetched_at,
        ),
    ).fetchone()
    if raw is None:
        raw = conn.execute(
            """SELECT id, payload_checksum FROM amazon_ads_campaign_raw
               WHERE sync_attempt_id=%s AND row_key=%s""",
            (attempt_id, normalized["row_key"]),
        ).fetchone()
        if raw["payload_checksum"] != checksum:
            raise AdsNormalizationError("report_row_changed_between_downloads")
    raw_id = raw["id"]
    current = conn.execute(
        """SELECT payload_checksum, report_completed_at
           FROM amazon_ads_campaign_daily
           WHERE source=%s AND store_id=%s AND marketplace=%s
             AND report_date=%s AND campaign_id=%s
           FOR UPDATE""",
        (
            SOURCE,
            config.store_id,
            config.marketplace,
            normalized["report_date"],
            normalized["campaign_id"],
        ),
    ).fetchone()
    stale = bool(current and report_completed_at < current["report_completed_at"])
    if current is None:
        action = "inserted"
    elif stale or current["payload_checksum"] == checksum:
        action = "skipped"
    else:
        action = "updated"
    if stale:
        return action, normalized["row_key"]

    values = {
        **normalized,
        "source": SOURCE,
        "store_id": config.store_id,
        "marketplace": config.marketplace,
        "currency": config.currency.upper(),
        "attribution_window_days": config.attribution_window_days,
        "report_completed_at": report_completed_at,
        "source_raw_id": raw_id,
        "payload_checksum": checksum,
        "last_seen_attempt_id": attempt_id,
    }
    conn.execute(
        """INSERT INTO amazon_ads_campaign_daily(
               source, store_id, marketplace, report_date, campaign_id,
               campaign_name, campaign_status, campaign_budget_amount,
               campaign_budget_type, campaign_bidding_strategy,
               impressions, clicks, spend, sales_1d, sales_7d, sales_14d,
               purchases_1d, purchases_7d, purchases_14d,
               units_sold_clicks_1d, units_sold_clicks_7d,
               units_sold_clicks_14d, currency, attribution_window_days,
               provisional_until, report_completed_at, source_raw_id,
               payload_checksum, last_seen_attempt_id, active
           ) VALUES (
               %(source)s, %(store_id)s, %(marketplace)s, %(report_date)s,
               %(campaign_id)s, %(campaign_name)s, %(campaign_status)s,
               %(campaign_budget_amount)s, %(campaign_budget_type)s,
               %(campaign_bidding_strategy)s, %(impressions)s, %(clicks)s,
               %(spend)s, %(sales_1d)s, %(sales_7d)s, %(sales_14d)s,
               %(purchases_1d)s, %(purchases_7d)s, %(purchases_14d)s,
               %(units_sold_clicks_1d)s, %(units_sold_clicks_7d)s,
               %(units_sold_clicks_14d)s, %(currency)s,
               %(attribution_window_days)s, %(provisional_until)s,
               %(report_completed_at)s, %(source_raw_id)s,
               %(payload_checksum)s, %(last_seen_attempt_id)s, TRUE
           )
           ON CONFLICT (source, store_id, marketplace, report_date, campaign_id)
           DO UPDATE SET
               campaign_name=EXCLUDED.campaign_name,
               campaign_status=EXCLUDED.campaign_status,
               campaign_budget_amount=EXCLUDED.campaign_budget_amount,
               campaign_budget_type=EXCLUDED.campaign_budget_type,
               campaign_bidding_strategy=EXCLUDED.campaign_bidding_strategy,
               impressions=EXCLUDED.impressions,
               clicks=EXCLUDED.clicks,
               spend=EXCLUDED.spend,
               sales_1d=EXCLUDED.sales_1d,
               sales_7d=EXCLUDED.sales_7d,
               sales_14d=EXCLUDED.sales_14d,
               purchases_1d=EXCLUDED.purchases_1d,
               purchases_7d=EXCLUDED.purchases_7d,
               purchases_14d=EXCLUDED.purchases_14d,
               units_sold_clicks_1d=EXCLUDED.units_sold_clicks_1d,
               units_sold_clicks_7d=EXCLUDED.units_sold_clicks_7d,
               units_sold_clicks_14d=EXCLUDED.units_sold_clicks_14d,
               currency=EXCLUDED.currency,
               attribution_window_days=EXCLUDED.attribution_window_days,
               provisional_until=EXCLUDED.provisional_until,
               report_completed_at=EXCLUDED.report_completed_at,
               source_raw_id=EXCLUDED.source_raw_id,
               payload_checksum=EXCLUDED.payload_checksum,
               last_seen_attempt_id=EXCLUDED.last_seen_attempt_id,
               active=TRUE,
               updated_at=NOW()""",
        values,
    )
    return action, normalized["row_key"]


def _finalize_attempt(
    conn: Connection,
    attempt: dict[str, Any],
    config: AdsCampaignSyncConfig,
    profile_scope: dict[str, str],
) -> dict[str, Any]:
    current = conn.execute(
        "SELECT * FROM amazon_sync_attempts WHERE id=%s FOR UPDATE",
        (attempt["id"],),
    ).fetchone()
    if current["status"] == "completed":
        return dict(current)
    report = conn.execute(
        "SELECT * FROM amazon_ads_reports WHERE sync_attempt_id=%s",
        (current["id"],),
    ).fetchone()
    complete = int(current["rows_errored"]) == 0
    finished_at = datetime.now(UTC)
    if complete:
        conn.execute(
            """UPDATE amazon_ads_campaign_daily SET active=FALSE, updated_at=NOW()
               WHERE source=%s AND store_id=%s AND marketplace=%s
                 AND report_date BETWEEN %s AND %s
                 AND active=TRUE AND last_seen_attempt_id<>%s
                 AND report_completed_at<=%s""",
            (
                SOURCE,
                config.store_id,
                config.marketplace,
                config.start_date,
                config.end_date,
                current["id"],
                report["report_completed_at"],
            ),
        )
    ensure_default_rules(
        conn,
        DATASET,
        max_age_minutes=2880,
        source=SOURCE,
        store_id=config.store_id,
        marketplace=config.marketplace,
    )
    today = datetime.now(ZoneInfo(config.timezone)).date()
    is_provisional = config.end_date + timedelta(
        days=config.attribution_window_days
    ) >= today
    core_result = ingest_run(
        conn,
        DatasetRunIn(
            external_run_id=f"{current['id']}:{FORMULA_VERSION}",
            source=SOURCE,
            store_id=config.store_id,
            marketplace=config.marketplace,
            dataset=DATASET,
            business_date=config.end_date,
            fetched_at=finished_at,
            source_updated_at=report["report_completed_at"] or finished_at,
            ingestion_status="complete" if complete else "partial",
            source_count=current["rows_pulled"],
            normalized_count=(
                current["rows_inserted"]
                + current["rows_updated"]
                + current["rows_skipped"]
            ),
            duplicate_count=0,
            error_count=current["rows_errored"],
            checksum=f"amazon-ads-report:{report['report_id']}",
            timezone=config.timezone,
            currency=config.currency.upper(),
            raw_reference=(
                "postgres://amazon_ads_campaign_raw"
                f"?sync_attempt_id={current['id']}"
            ),
            schema_version=SCHEMA_VERSION,
            formula_version=FORMULA_VERSION,
            is_provisional=is_provisional,
            correction_of_run_id=current["core_run_id"],
            metadata={
                "reporting_api_version": REPORTING_API_VERSION,
                "report_type_id": REPORT_TYPE_ID,
                "ad_product": AD_PRODUCT,
                "group_by": GROUP_BY,
                "start_date": config.start_date.isoformat(),
                "end_date": config.end_date.isoformat(),
                "attribution_window_days": config.attribution_window_days,
                "attribution_metrics": "sales14d/purchases14d/unitsSoldClicks14d",
                "profile_scope_verified": True,
                "profile_country_code": profile_scope["country_code"],
                "profile_currency": profile_scope["currency"],
                "profile_timezone": profile_scope["timezone"],
                "profile_marketplace": profile_scope["marketplace"],
                "unchanged_or_stale_count": current["rows_skipped"],
                "legacy_api_transition": (
                    "canonical schema is versioned independently from Ads reporting API"
                ),
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
                   cursor_value=GREATEST(
                       amazon_sync_cursors.cursor_value, EXCLUDED.cursor_value
                   ),
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


def sync_ads_campaigns(
    conn: Connection,
    client: AmazonAdsClient,
    config: AdsCampaignSyncConfig,
) -> dict[str, Any]:
    config.validate()
    profile_scope = verify_profile_scope(
        client.get_profile(),
        profile_id=config.profile_id,
        marketplace=config.marketplace,
        timezone=config.timezone,
        currency=config.currency,
    )
    lock_key = f"{SOURCE}|{config.store_id}|{config.marketplace}|{DATASET}"
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,))
    attempt: dict[str, Any] | None = None
    try:
        _register_store(conn, config)
        attempt, report = start_or_resume_ads_report(
            conn, config, CAMPAIGN_REPORT_SPEC
        )
        if attempt["status"] == "finalizing":
            return _finalize_attempt(conn, attempt, config, profile_scope)
        report_meta, completed_at = request_and_wait_ads_report(
            conn, client, attempt, report, config
        )
        report_id = str(report_meta["reportId"])
        rows = client.download_report(str(report_meta["url"]))
        fetched_at = datetime.now(UTC)
        counts = {"inserted": 0, "updated": 0, "skipped": 0, "errored": 0}
        seen_keys: set[str] = set()
        for row in rows:
            try:
                normalized = normalize_campaign_row(
                    row,
                    start_date=config.start_date,
                    end_date=config.end_date,
                    attribution_window_days=config.attribution_window_days,
                )
                if normalized["row_key"] in seen_keys:
                    raise AdsNormalizationError("duplicate_campaign_date")
                seen_keys.add(normalized["row_key"])
                action, _ = _write_campaign(
                    conn,
                    attempt_id=attempt["id"],
                    report_id=report_id,
                    report_completed_at=completed_at,
                    fetched_at=fetched_at,
                    config=config,
                    row=row,
                )
                counts[action] += 1
            except AdsNormalizationError as exc:
                counts["errored"] += 1
                _write_reject(
                    conn,
                    attempt_id=attempt["id"],
                    row=row,
                    error_code=str(exc),
                )
        conn.execute(
            """UPDATE amazon_ads_reports
               SET downloaded_at=%s, updated_at=NOW()
               WHERE sync_attempt_id=%s""",
            (fetched_at, attempt["id"]),
        )
        conn.execute(
            """UPDATE amazon_sync_attempts SET
                   status='finalizing', pages_completed=1,
                   rows_pulled=%s, rows_inserted=%s, rows_updated=%s,
                   rows_skipped=%s, rows_errored=%s,
                   max_source_updated_at=%s, updated_at=NOW()
               WHERE id=%s""",
            (
                len(rows),
                counts["inserted"],
                counts["updated"],
                counts["skipped"],
                counts["errored"],
                completed_at,
                attempt["id"],
            ),
        )
        conn.commit()
        return _finalize_attempt(conn, attempt, config, profile_scope)
    except Exception as exc:
        conn.rollback()
        if attempt is not None:
            conn.execute(
                """UPDATE amazon_sync_attempts
                   SET status=CASE
                           WHEN status='finalizing' THEN status ELSE 'failed'
                       END,
                       error_type=%s, updated_at=NOW()
                   WHERE id=%s""",
                (type(exc).__name__, attempt["id"]),
            )
            conn.commit()
        raise
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock_key,))
