from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import Connection
from psycopg.types.json import Jsonb

from .connectors.ads_api import AmazonAdsClient, verify_profile_scope
from .contracts import DatasetRunIn
from .engine import ingest_run
from .rules import ensure_default_rules
from .sync_ads import (
    AD_PRODUCT,
    REPORTING_API_VERSION,
    AdsCampaignSyncConfig,
    AdsNormalizationError,
    AdsReportSpec,
    _decimal,
    _identifier,
    _integer,
    _register_store,
    _text,
    payload_checksum,
    request_and_wait_ads_report,
    start_or_resume_ads_report,
)

SEARCH_TERM_SOURCE = "amazon_ads_reporting_v3_sp_search_terms"
SEARCH_TERM_DATASET = "ads_search_term_daily"
SEARCH_TERM_FORMULA_VERSION = "amazon-ads-sp-search-terms-v3-canonical-v1"
PURCHASED_PRODUCT_SOURCE = "amazon_ads_reporting_v3_sp_purchased_products"
PURCHASED_PRODUCT_DATASET = "ads_purchased_product_daily"
PURCHASED_PRODUCT_FORMULA_VERSION = (
    "amazon-ads-sp-purchased-products-v3-canonical-v1"
)
DETAIL_SCHEMA_VERSION = "amazon-ads-sp-details-v3-raw-v1"

SP_SEARCH_TERM_COLUMNS = (
    "date",
    "campaignId",
    "campaignName",
    "adGroupId",
    "adGroupName",
    "keywordId",
    "keyword",
    "matchType",
    "searchTerm",
    "impressions",
    "clicks",
    "clickThroughRate",
    "costPerClick",
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
    "acosClicks14d",
    "roasClicks14d",
)

SP_PURCHASED_PRODUCT_COLUMNS = (
    "date",
    "campaignId",
    "campaignName",
    "adGroupId",
    "adGroupName",
    "keywordId",
    "matchType",
    "portfolioId",
    "advertisedAsin",
    "advertisedSku",
    "purchasedAsin",
    "sales1d",
    "sales7d",
    "sales14d",
    "sales30d",
    "purchases1d",
    "purchases7d",
    "purchases14d",
    "purchases30d",
    "unitsSoldClicks1d",
    "unitsSoldClicks7d",
    "unitsSoldClicks14d",
    "unitsSoldClicks30d",
)

SEARCH_TERM_REPORT_SPEC = AdsReportSpec(
    source=SEARCH_TERM_SOURCE,
    dataset=SEARCH_TERM_DATASET,
    report_type_id="spSearchTerm",
    ad_product=AD_PRODUCT,
    group_by="searchTerm",
    columns=SP_SEARCH_TERM_COLUMNS,
)

PURCHASED_PRODUCT_REPORT_SPEC = AdsReportSpec(
    source=PURCHASED_PRODUCT_SOURCE,
    dataset=PURCHASED_PRODUCT_DATASET,
    report_type_id="spPurchasedProduct",
    ad_product=AD_PRODUCT,
    group_by="asin",
    columns=SP_PURCHASED_PRODUCT_COLUMNS,
)


@dataclass(frozen=True)
class AdsDetailDefinition:
    name: str
    spec: AdsReportSpec
    raw_table: str
    reject_table: str
    current_table: str
    formula_version: str
    normalizer: Callable[..., dict[str, Any]]


def _optional_identifier(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    return _identifier(value, field)


def _asin(value: Any, field: str) -> str:
    parsed = _text(value, field, required=True)
    assert parsed is not None
    parsed = parsed.upper()
    if len(parsed) != 10 or not parsed.isalnum():
        raise AdsNormalizationError(f"invalid_{field}")
    return parsed


def _report_date(row: dict[str, Any], start_date: date, end_date: date) -> date:
    value = _text(row.get("date"), "date", required=True)
    try:
        parsed = date.fromisoformat(value or "")
    except ValueError as exc:
        raise AdsNormalizationError("invalid_date") from exc
    if not start_date <= parsed <= end_date:
        raise AdsNormalizationError("date_outside_requested_window")
    return parsed


def _stable_row_key(parts: list[str | None]) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _spend(row: dict[str, Any]) -> Decimal:
    cost = _decimal(row.get("cost"), "cost", optional=True)
    spend = _decimal(row.get("spend"), "spend", optional=True)
    if cost is not None and spend is not None and cost != spend:
        raise AdsNormalizationError("cost_spend_mismatch")
    resolved = cost if cost is not None else spend
    if resolved is None:
        raise AdsNormalizationError("missing_cost_and_spend")
    return resolved


def normalize_search_term_row(
    row: dict[str, Any],
    *,
    start_date: date,
    end_date: date,
    attribution_window_days: int,
) -> dict[str, Any]:
    report_date = _report_date(row, start_date, end_date)
    campaign_id = _identifier(row.get("campaignId"), "campaign_id")
    ad_group_id = _identifier(row.get("adGroupId"), "ad_group_id")
    keyword_id = _optional_identifier(row.get("keywordId"), "keyword_id")
    search_term = _text(row.get("searchTerm"), "search_term", required=True)
    assert search_term is not None
    match_type = _text(row.get("matchType"), "match_type")
    row_key = _stable_row_key(
        [
            report_date.isoformat(),
            campaign_id,
            ad_group_id,
            keyword_id,
            match_type,
            search_term,
        ]
    )
    return {
        "row_key": row_key,
        "report_date": report_date,
        "campaign_id": campaign_id,
        "campaign_name": _text(row.get("campaignName"), "campaign_name"),
        "ad_group_id": ad_group_id,
        "ad_group_name": _text(row.get("adGroupName"), "ad_group_name"),
        "keyword_id": keyword_id,
        "keyword": _text(row.get("keyword"), "keyword"),
        "match_type": match_type,
        "search_term": search_term,
        "impressions": _integer(row.get("impressions"), "impressions"),
        "clicks": _integer(row.get("clicks"), "clicks"),
        "spend": _spend(row),
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


def normalize_purchased_product_row(
    row: dict[str, Any],
    *,
    start_date: date,
    end_date: date,
    attribution_window_days: int,
) -> dict[str, Any]:
    report_date = _report_date(row, start_date, end_date)
    campaign_id = _identifier(row.get("campaignId"), "campaign_id")
    ad_group_id = _identifier(row.get("adGroupId"), "ad_group_id")
    keyword_id = _optional_identifier(row.get("keywordId"), "keyword_id")
    match_type = _text(row.get("matchType"), "match_type")
    advertised_asin = _asin(row.get("advertisedAsin"), "advertised_asin")
    purchased_asin = _asin(row.get("purchasedAsin"), "purchased_asin")
    advertised_sku = _text(row.get("advertisedSku"), "advertised_sku")
    row_key = _stable_row_key(
        [
            report_date.isoformat(),
            campaign_id,
            ad_group_id,
            keyword_id,
            match_type,
            advertised_asin,
            advertised_sku,
            purchased_asin,
        ]
    )
    return {
        "row_key": row_key,
        "report_date": report_date,
        "campaign_id": campaign_id,
        "campaign_name": _text(row.get("campaignName"), "campaign_name"),
        "ad_group_id": ad_group_id,
        "ad_group_name": _text(row.get("adGroupName"), "ad_group_name"),
        "keyword_id": keyword_id,
        "match_type": match_type,
        "portfolio_id": _optional_identifier(row.get("portfolioId"), "portfolio_id"),
        "advertised_asin": advertised_asin,
        "advertised_sku": advertised_sku,
        "purchased_asin": purchased_asin,
        "sales_1d": _decimal(row.get("sales1d"), "sales_1d"),
        "sales_7d": _decimal(row.get("sales7d"), "sales_7d"),
        "sales_14d": _decimal(row.get("sales14d"), "sales_14d"),
        "sales_30d": _decimal(row.get("sales30d"), "sales_30d"),
        "purchases_1d": _integer(row.get("purchases1d"), "purchases_1d"),
        "purchases_7d": _integer(row.get("purchases7d"), "purchases_7d"),
        "purchases_14d": _integer(row.get("purchases14d"), "purchases_14d"),
        "purchases_30d": _integer(row.get("purchases30d"), "purchases_30d"),
        "units_sold_clicks_1d": _integer(
            row.get("unitsSoldClicks1d"), "units_sold_clicks_1d"
        ),
        "units_sold_clicks_7d": _integer(
            row.get("unitsSoldClicks7d"), "units_sold_clicks_7d"
        ),
        "units_sold_clicks_14d": _integer(
            row.get("unitsSoldClicks14d"), "units_sold_clicks_14d"
        ),
        "units_sold_clicks_30d": _integer(
            row.get("unitsSoldClicks30d"), "units_sold_clicks_30d"
        ),
        "provisional_until": report_date + timedelta(days=attribution_window_days),
    }


SEARCH_TERM_DEFINITION = AdsDetailDefinition(
    name="search_term",
    spec=SEARCH_TERM_REPORT_SPEC,
    raw_table="amazon_ads_search_term_raw",
    reject_table="amazon_ads_search_term_rejects",
    current_table="amazon_ads_search_term_daily",
    formula_version=SEARCH_TERM_FORMULA_VERSION,
    normalizer=normalize_search_term_row,
)

PURCHASED_PRODUCT_DEFINITION = AdsDetailDefinition(
    name="purchased_product",
    spec=PURCHASED_PRODUCT_REPORT_SPEC,
    raw_table="amazon_ads_purchased_product_raw",
    reject_table="amazon_ads_purchased_product_rejects",
    current_table="amazon_ads_purchased_product_daily",
    formula_version=PURCHASED_PRODUCT_FORMULA_VERSION,
    normalizer=normalize_purchased_product_row,
)


def _write_reject(
    conn: Connection,
    definition: AdsDetailDefinition,
    attempt_id: UUID,
    row: dict[str, Any],
    error_code: str,
) -> None:
    conn.execute(
        f"""INSERT INTO {definition.reject_table}(
               sync_attempt_id, payload, payload_checksum, error_code
           ) VALUES (%s, %s, %s, %s)
           ON CONFLICT (sync_attempt_id, payload_checksum, error_code) DO NOTHING""",
        (attempt_id, Jsonb(row), payload_checksum(row), error_code),
    )


def _write_raw(
    conn: Connection,
    definition: AdsDetailDefinition,
    *,
    attempt_id: UUID,
    report_id: str,
    normalized: dict[str, Any],
    row: dict[str, Any],
    fetched_at: datetime,
) -> tuple[int, str]:
    checksum = payload_checksum(row)
    identity_columns = (
        "campaign_id, ad_group_id, search_term"
        if definition.name == "search_term"
        else "campaign_id, ad_group_id, advertised_asin, purchased_asin"
    )
    identity_values = (
        (
            normalized["campaign_id"],
            normalized["ad_group_id"],
            normalized["search_term"],
        )
        if definition.name == "search_term"
        else (
            normalized["campaign_id"],
            normalized["ad_group_id"],
            normalized["advertised_asin"],
            normalized["purchased_asin"],
        )
    )
    placeholders = ", ".join(["%s"] * len(identity_values))
    raw = conn.execute(
        f"""INSERT INTO {definition.raw_table}(
               sync_attempt_id, report_id, row_key, report_date,
               {identity_columns}, payload, payload_checksum, fetched_at
           ) VALUES (%s, %s, %s, %s, {placeholders}, %s, %s, %s)
           ON CONFLICT (sync_attempt_id, row_key) DO NOTHING
           RETURNING id, payload_checksum""",
        (
            attempt_id,
            report_id,
            normalized["row_key"],
            normalized["report_date"],
            *identity_values,
            Jsonb(row),
            checksum,
            fetched_at,
        ),
    ).fetchone()
    if raw is None:
        raw = conn.execute(
            f"""SELECT id, payload_checksum FROM {definition.raw_table}
                WHERE sync_attempt_id=%s AND row_key=%s""",
            (attempt_id, normalized["row_key"]),
        ).fetchone()
        if raw["payload_checksum"] != checksum:
            raise AdsNormalizationError("report_row_changed_between_downloads")
    return raw["id"], checksum


def _write_search_term(
    conn: Connection,
    *,
    attempt_id: UUID,
    report_id: str,
    report_completed_at: datetime,
    fetched_at: datetime,
    config: AdsCampaignSyncConfig,
    normalized: dict[str, Any],
    row: dict[str, Any],
) -> str:
    raw_id, checksum = _write_raw(
        conn,
        SEARCH_TERM_DEFINITION,
        attempt_id=attempt_id,
        report_id=report_id,
        normalized=normalized,
        row=row,
        fetched_at=fetched_at,
    )
    current = conn.execute(
        """SELECT payload_checksum, report_completed_at
           FROM amazon_ads_search_term_daily
           WHERE source=%s AND store_id=%s AND marketplace=%s
             AND report_date=%s AND row_key=%s FOR UPDATE""",
        (
            SEARCH_TERM_SOURCE,
            config.store_id,
            config.marketplace,
            normalized["report_date"],
            normalized["row_key"],
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
        return action
    values = {
        **normalized,
        "source": SEARCH_TERM_SOURCE,
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
        """INSERT INTO amazon_ads_search_term_daily(
               source, store_id, marketplace, report_date, row_key,
               campaign_id, campaign_name, ad_group_id, ad_group_name,
               keyword_id, keyword, match_type, search_term,
               impressions, clicks, spend, sales_1d, sales_7d, sales_14d,
               purchases_1d, purchases_7d, purchases_14d,
               units_sold_clicks_1d, units_sold_clicks_7d,
               units_sold_clicks_14d, currency, attribution_window_days,
               provisional_until, report_completed_at, source_raw_id,
               payload_checksum, last_seen_attempt_id, active
           ) VALUES (
               %(source)s, %(store_id)s, %(marketplace)s, %(report_date)s,
               %(row_key)s, %(campaign_id)s, %(campaign_name)s,
               %(ad_group_id)s, %(ad_group_name)s, %(keyword_id)s,
               %(keyword)s, %(match_type)s, %(search_term)s,
               %(impressions)s, %(clicks)s, %(spend)s, %(sales_1d)s,
               %(sales_7d)s, %(sales_14d)s, %(purchases_1d)s,
               %(purchases_7d)s, %(purchases_14d)s,
               %(units_sold_clicks_1d)s, %(units_sold_clicks_7d)s,
               %(units_sold_clicks_14d)s, %(currency)s,
               %(attribution_window_days)s, %(provisional_until)s,
               %(report_completed_at)s, %(source_raw_id)s,
               %(payload_checksum)s, %(last_seen_attempt_id)s, TRUE
           )
           ON CONFLICT (source, store_id, marketplace, report_date, row_key)
           DO UPDATE SET
               campaign_name=EXCLUDED.campaign_name,
               ad_group_name=EXCLUDED.ad_group_name,
               keyword=EXCLUDED.keyword,
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
    return action


def _write_purchased_product(
    conn: Connection,
    *,
    attempt_id: UUID,
    report_id: str,
    report_completed_at: datetime,
    fetched_at: datetime,
    config: AdsCampaignSyncConfig,
    normalized: dict[str, Any],
    row: dict[str, Any],
) -> str:
    raw_id, checksum = _write_raw(
        conn,
        PURCHASED_PRODUCT_DEFINITION,
        attempt_id=attempt_id,
        report_id=report_id,
        normalized=normalized,
        row=row,
        fetched_at=fetched_at,
    )
    current = conn.execute(
        """SELECT payload_checksum, report_completed_at
           FROM amazon_ads_purchased_product_daily
           WHERE source=%s AND store_id=%s AND marketplace=%s
             AND report_date=%s AND row_key=%s FOR UPDATE""",
        (
            PURCHASED_PRODUCT_SOURCE,
            config.store_id,
            config.marketplace,
            normalized["report_date"],
            normalized["row_key"],
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
        return action
    values = {
        **normalized,
        "source": PURCHASED_PRODUCT_SOURCE,
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
        """INSERT INTO amazon_ads_purchased_product_daily(
               source, store_id, marketplace, report_date, row_key,
               campaign_id, campaign_name, ad_group_id, ad_group_name,
               keyword_id, match_type, portfolio_id, advertised_asin,
               advertised_sku, purchased_asin, sales_1d, sales_7d,
               sales_14d, sales_30d, purchases_1d, purchases_7d,
               purchases_14d, purchases_30d, units_sold_clicks_1d,
               units_sold_clicks_7d, units_sold_clicks_14d,
               units_sold_clicks_30d, currency, attribution_window_days,
               provisional_until, report_completed_at, source_raw_id,
               payload_checksum, last_seen_attempt_id, active
           ) VALUES (
               %(source)s, %(store_id)s, %(marketplace)s, %(report_date)s,
               %(row_key)s, %(campaign_id)s, %(campaign_name)s,
               %(ad_group_id)s, %(ad_group_name)s, %(keyword_id)s,
               %(match_type)s, %(portfolio_id)s, %(advertised_asin)s,
               %(advertised_sku)s, %(purchased_asin)s, %(sales_1d)s,
               %(sales_7d)s, %(sales_14d)s, %(sales_30d)s,
               %(purchases_1d)s, %(purchases_7d)s, %(purchases_14d)s,
               %(purchases_30d)s, %(units_sold_clicks_1d)s,
               %(units_sold_clicks_7d)s, %(units_sold_clicks_14d)s,
               %(units_sold_clicks_30d)s, %(currency)s,
               %(attribution_window_days)s, %(provisional_until)s,
               %(report_completed_at)s, %(source_raw_id)s,
               %(payload_checksum)s, %(last_seen_attempt_id)s, TRUE
           )
           ON CONFLICT (source, store_id, marketplace, report_date, row_key)
           DO UPDATE SET
               campaign_name=EXCLUDED.campaign_name,
               ad_group_name=EXCLUDED.ad_group_name,
               advertised_sku=EXCLUDED.advertised_sku,
               sales_1d=EXCLUDED.sales_1d,
               sales_7d=EXCLUDED.sales_7d,
               sales_14d=EXCLUDED.sales_14d,
               sales_30d=EXCLUDED.sales_30d,
               purchases_1d=EXCLUDED.purchases_1d,
               purchases_7d=EXCLUDED.purchases_7d,
               purchases_14d=EXCLUDED.purchases_14d,
               purchases_30d=EXCLUDED.purchases_30d,
               units_sold_clicks_1d=EXCLUDED.units_sold_clicks_1d,
               units_sold_clicks_7d=EXCLUDED.units_sold_clicks_7d,
               units_sold_clicks_14d=EXCLUDED.units_sold_clicks_14d,
               units_sold_clicks_30d=EXCLUDED.units_sold_clicks_30d,
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
    return action


def _finalize_detail_attempt(
    conn: Connection,
    attempt: dict[str, Any],
    config: AdsCampaignSyncConfig,
    profile_scope: dict[str, str],
    definition: AdsDetailDefinition,
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
            f"""UPDATE {definition.current_table}
                SET active=FALSE, updated_at=NOW()
                WHERE source=%s AND store_id=%s AND marketplace=%s
                  AND report_date BETWEEN %s AND %s
                  AND active=TRUE AND last_seen_attempt_id<>%s
                  AND report_completed_at<=%s""",
            (
                definition.spec.source,
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
        definition.spec.dataset,
        max_age_minutes=2880,
        source=definition.spec.source,
        store_id=config.store_id,
        marketplace=config.marketplace,
    )
    today = datetime.now(ZoneInfo(config.timezone)).date()
    is_provisional = config.end_date + timedelta(
        days=config.attribution_window_days
    ) >= today
    result = ingest_run(
        conn,
        DatasetRunIn(
            external_run_id=f"{current['id']}:{definition.formula_version}",
            source=definition.spec.source,
            store_id=config.store_id,
            marketplace=config.marketplace,
            dataset=definition.spec.dataset,
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
                f"postgres://{definition.raw_table}?sync_attempt_id={current['id']}"
            ),
            schema_version=DETAIL_SCHEMA_VERSION,
            formula_version=definition.formula_version,
            is_provisional=is_provisional,
            correction_of_run_id=current["core_run_id"],
            metadata={
                "reporting_api_version": REPORTING_API_VERSION,
                "report_type_id": definition.spec.report_type_id,
                "ad_product": definition.spec.ad_product,
                "group_by": definition.spec.group_by,
                "start_date": config.start_date.isoformat(),
                "end_date": config.end_date.isoformat(),
                "attribution_window_days": config.attribution_window_days,
                "profile_scope_verified": True,
                "profile_country_code": profile_scope["country_code"],
                "profile_currency": profile_scope["currency"],
                "profile_timezone": profile_scope["timezone"],
                "profile_marketplace": profile_scope["marketplace"],
                "campaign_scope": (
                    "Amazon API default eligibility; non-campaign groupBy does not "
                    "accept campaignStatus filters"
                ),
                "do_not_sum_across_report_grains": True,
                "unchanged_or_stale_count": current["rows_skipped"],
            },
        ),
    )
    final_status = "completed" if complete else "partial"
    conn.execute(
        """UPDATE amazon_sync_attempts
           SET status=%s, core_run_id=%s, error_type=NULL, updated_at=NOW()
           WHERE id=%s""",
        (final_status, UUID(result["run_id"]), current["id"]),
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
                definition.spec.source,
                config.store_id,
                config.marketplace,
                definition.spec.dataset,
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


def _sync_ads_detail(
    conn: Connection,
    client: AmazonAdsClient,
    config: AdsCampaignSyncConfig,
    definition: AdsDetailDefinition,
) -> dict[str, Any]:
    config.validate()
    profile_scope = verify_profile_scope(
        client.get_profile(),
        profile_id=config.profile_id,
        marketplace=config.marketplace,
        timezone=config.timezone,
        currency=config.currency,
    )
    lock_key = (
        f"{definition.spec.source}|{config.store_id}|{config.marketplace}|"
        f"{definition.spec.dataset}"
    )
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,))
    attempt: dict[str, Any] | None = None
    try:
        _register_store(conn, config)
        attempt, report = start_or_resume_ads_report(conn, config, definition.spec)
        if attempt["status"] == "finalizing":
            return _finalize_detail_attempt(
                conn, attempt, config, profile_scope, definition
            )
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
                normalized = definition.normalizer(
                    row,
                    start_date=config.start_date,
                    end_date=config.end_date,
                    attribution_window_days=config.attribution_window_days,
                )
                if normalized["row_key"] in seen_keys:
                    raise AdsNormalizationError("duplicate_business_key")
                seen_keys.add(normalized["row_key"])
                writer = (
                    _write_search_term
                    if definition.name == "search_term"
                    else _write_purchased_product
                )
                action = writer(
                    conn,
                    attempt_id=attempt["id"],
                    report_id=report_id,
                    report_completed_at=completed_at,
                    fetched_at=fetched_at,
                    config=config,
                    normalized=normalized,
                    row=row,
                )
                counts[action] += 1
            except AdsNormalizationError as exc:
                counts["errored"] += 1
                _write_reject(conn, definition, attempt["id"], row, str(exc))
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
        return _finalize_detail_attempt(
            conn, attempt, config, profile_scope, definition
        )
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


def sync_ads_search_terms(
    conn: Connection,
    client: AmazonAdsClient,
    config: AdsCampaignSyncConfig,
) -> dict[str, Any]:
    if config.attribution_window_days != 14:
        raise ValueError("search-term reports require a 14-day attribution window")
    return _sync_ads_detail(conn, client, config, SEARCH_TERM_DEFINITION)


def sync_ads_purchased_products(
    conn: Connection,
    client: AmazonAdsClient,
    config: AdsCampaignSyncConfig,
) -> dict[str, Any]:
    if config.attribution_window_days != 30:
        raise ValueError("purchased-product reports require a 30-day attribution window")
    return _sync_ads_detail(conn, client, config, PURCHASED_PRODUCT_DEFINITION)
