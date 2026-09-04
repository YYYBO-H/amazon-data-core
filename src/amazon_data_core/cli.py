from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .contracts import DatasetRunIn
from .db import connect, migrate
from .engine import health_summary, ingest_run, run_checks
from .rules import ensure_default_rules


def _redacted_database_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.password:
        return value
    username = parsed.username or ""
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{username}:***@{hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def doctor() -> tuple[dict, int]:
    from .config import database_url

    checks: list[dict[str, object]] = []
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
            checks.append({"name": "database_connection", "status": "passed"})
            tables = conn.execute(
                """SELECT COUNT(*) AS n FROM information_schema.tables
                   WHERE table_schema='public'
                     AND table_name IN (
                         'data_scopes', 'quality_rules', 'dataset_runs',
                         'current_dataset_state', 'quality_check_events'
                     )"""
            ).fetchone()["n"]
            schema_ok = tables == 5
            checks.append(
                {
                    "name": "database_schema",
                    "status": "passed" if schema_ok else "failed",
                    "found": tables,
                    "expected": 5,
                    "fix": None if schema_ok else "run: amazon-data-core migrate",
                }
            )
            connector_tables = conn.execute(
                """SELECT COUNT(*) AS n FROM information_schema.tables
                   WHERE table_schema='public'
                     AND table_name IN (
                         'amazon_store_connections', 'amazon_sync_cursors',
                         'amazon_sync_attempts', 'amazon_orders_raw',
                         'amazon_orders'
                     )"""
            ).fetchone()["n"]
            connector_schema_ok = connector_tables == 5
            checks.append(
                {
                    "name": "orders_connector_schema",
                    "status": "passed" if connector_schema_ok else "failed",
                    "found": connector_tables,
                    "expected": 5,
                    "fix": None
                    if connector_schema_ok
                    else "run: amazon-data-core migrate",
                }
            )
            inventory_tables = conn.execute(
                """SELECT COUNT(*) AS n FROM information_schema.tables
                   WHERE table_schema='public'
                     AND table_name IN (
                         'amazon_fba_inventory_raw',
                         'amazon_fba_inventory_snapshot_rows',
                         'amazon_fba_inventory_rejects',
                         'amazon_fba_inventory'
                     )"""
            ).fetchone()["n"]
            inventory_schema_ok = inventory_tables == 4
            checks.append(
                {
                    "name": "fba_inventory_connector_schema",
                    "status": "passed" if inventory_schema_ok else "failed",
                    "found": inventory_tables,
                    "expected": 4,
                    "fix": None
                    if inventory_schema_ok
                    else "run: amazon-data-core migrate",
                }
            )
            ads_tables = conn.execute(
                """SELECT COUNT(*) AS n FROM information_schema.tables
                   WHERE table_schema='public'
                     AND table_name IN (
                         'amazon_ads_reports', 'amazon_ads_campaign_raw',
                         'amazon_ads_campaign_rejects',
                         'amazon_ads_campaign_daily'
                     )"""
            ).fetchone()["n"]
            ads_schema_ok = ads_tables == 4
            checks.append(
                {
                    "name": "ads_campaign_connector_schema",
                    "status": "passed" if ads_schema_ok else "failed",
                    "found": ads_tables,
                    "expected": 4,
                    "fix": None if ads_schema_ok else "run: amazon-data-core migrate",
                }
            )
            ads_detail_tables = conn.execute(
                """SELECT COUNT(*) AS n FROM information_schema.tables
                   WHERE table_schema='public'
                     AND table_name IN (
                         'amazon_ads_search_term_raw',
                         'amazon_ads_search_term_rejects',
                         'amazon_ads_search_term_daily',
                         'amazon_ads_purchased_product_raw',
                         'amazon_ads_purchased_product_rejects',
                         'amazon_ads_purchased_product_daily'
                     )"""
            ).fetchone()["n"]
            ads_detail_schema_ok = ads_detail_tables == 6
            checks.append(
                {
                    "name": "ads_detail_connector_schema",
                    "status": "passed" if ads_detail_schema_ok else "failed",
                    "found": ads_detail_tables,
                    "expected": 6,
                    "fix": None
                    if ads_detail_schema_ok
                    else "run: amazon-data-core migrate",
                }
            )
            settlement_tables = conn.execute(
                """SELECT COUNT(*) AS n FROM information_schema.tables
                   WHERE table_schema='public'
                     AND table_name IN (
                         'amazon_settlement_reports', 'amazon_settlement_raw',
                         'amazon_settlement_rejects', 'amazon_settlement_lines',
                         'amazon_settlement_periods'
                     )"""
            ).fetchone()["n"]
            settlement_schema_ok = settlement_tables == 5
            checks.append(
                {
                    "name": "settlement_connector_schema",
                    "status": "passed" if settlement_schema_ok else "failed",
                    "found": settlement_tables,
                    "expected": 5,
                    "fix": None
                    if settlement_schema_ok
                    else "run: amazon-data-core migrate",
                }
            )
    except Exception as exc:
        checks.append(
            {
                "name": "database_connection",
                "status": "failed",
                "error_type": type(exc).__name__,
                "fix": "start the local stack or set DATABASE_URL",
            }
        )
    ready = all(check["status"] == "passed" for check in checks)
    return (
        {
            "ready": ready,
            "database_url": _redacted_database_url(database_url()),
            "checks": checks,
        },
        0 if ready else 1,
    )


SKILL_HOST_DIRS = {
    "generic": Path(".agents/skills"),
    "kimi": Path(".kimi-code/skills"),
    "qoder": Path(".qoder/skills"),
    "lingma": Path(".lingma/skills"),
}


def install_skill(host: str, target_dir: Path | None = None) -> dict[str, str]:
    base = target_dir or (Path.home() / SKILL_HOST_DIRS[host])
    destination = base / "amazon-data-core"
    destination.mkdir(parents=True, exist_ok=True)
    source = files("amazon_data_core").joinpath(
        "resources/amazon-data-core/SKILL.md"
    )
    with source.open("rb") as input_file, (destination / "SKILL.md").open("wb") as output_file:
        shutil.copyfileobj(input_file, output_file)
    return {"installed": str(destination), "host": host}


def load_demo(path: Path) -> dict[str, int]:
    loaded = 0
    with connect() as conn:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            run = DatasetRunIn.model_validate_json(line)
            ingest_run(conn, run)
            ensure_default_rules(
                conn,
                run.dataset,
                max_age_minutes=10080,
                source=run.source,
                store_id=run.store_id,
                marketplace=run.marketplace,
            )
            loaded += 1
    return {"loaded": loaded}


def amazon_auth_status(*, verify: bool = False, region: str = "NA") -> tuple[dict, int]:
    """Report credential presence without ever returning credential values."""
    from .connectors.sp_api import AmazonCredentials, SPAPIClient, SPAPIError

    names = ("AMAZON_CLIENT_ID", "AMAZON_CLIENT_SECRET", "AMAZON_REFRESH_TOKEN")
    configured = {name: bool(os.getenv(name, "").strip()) for name in names}
    result: dict[str, object] = {
        "configured": all(configured.values()),
        "fields": configured,
        "region": region.upper(),
        "verified": False,
    }
    if not result["configured"]:
        result["missing"] = [name for name, present in configured.items() if not present]
        return result, 1
    if not verify:
        return result, 0
    try:
        credentials = AmazonCredentials.from_env()
        with SPAPIClient(credentials, region=region) as client:
            client.get_access_token()
    except SPAPIError as exc:
        result["error_type"] = type(exc).__name__
        result["message"] = str(exc)
        return result, 1
    result["verified"] = True
    return result, 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="amazon-data-core")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    demo = sub.add_parser("load-demo")
    demo.add_argument("--file", type=Path, required=True)
    sub.add_parser("check")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("mcp")
    skill = sub.add_parser("install-skill")
    skill.add_argument("--host", choices=sorted(SKILL_HOST_DIRS), default="generic")
    skill.add_argument("--target-dir", type=Path)
    auth = sub.add_parser("amazon-auth")
    auth.add_argument("--verify", action="store_true")
    auth.add_argument("--region", default=os.getenv("AMAZON_REGION", "NA"))
    orders = sub.add_parser("sync-orders")
    orders.add_argument("--store-id", default=os.getenv("AMAZON_STORE_ID"))
    orders.add_argument("--marketplace", default=os.getenv("AMAZON_MARKETPLACE_ID"))
    orders.add_argument("--region", default=os.getenv("AMAZON_REGION", "NA"))
    orders.add_argument("--timezone", default=os.getenv("AMAZON_TIMEZONE", "UTC"))
    orders.add_argument("--currency", default=os.getenv("AMAZON_CURRENCY"))
    orders.add_argument("--initial-days", type=int, default=60)
    orders.add_argument("--lookback-minutes", type=int, default=5)
    orders.add_argument("--no-resume", action="store_true")
    inventory = sub.add_parser("sync-inventory")
    inventory.add_argument("--store-id", default=os.getenv("AMAZON_STORE_ID"))
    inventory.add_argument(
        "--marketplace", default=os.getenv("AMAZON_MARKETPLACE_ID")
    )
    inventory.add_argument("--region", default=os.getenv("AMAZON_REGION", "NA"))
    inventory.add_argument("--timezone", default=os.getenv("AMAZON_TIMEZONE", "UTC"))
    inventory.add_argument("--currency", default=os.getenv("AMAZON_CURRENCY"))
    settlements = sub.add_parser("sync-settlements")
    settlements.add_argument("--store-id", default=os.getenv("AMAZON_STORE_ID"))
    settlements.add_argument(
        "--marketplace", default=os.getenv("AMAZON_MARKETPLACE_ID")
    )
    settlements.add_argument("--region", default=os.getenv("AMAZON_REGION", "NA"))
    settlements.add_argument(
        "--timezone", default=os.getenv("AMAZON_TIMEZONE", "UTC")
    )
    settlements.add_argument("--currency", default=os.getenv("AMAZON_CURRENCY"))
    settlements.add_argument("--created-since-days", type=int, default=90)
    ads_auth = sub.add_parser("amazon-ads-auth")
    ads_auth.add_argument("--verify", action="store_true")
    ads_auth.add_argument("--region", default=os.getenv("AMAZON_AD_REGION", "NA"))
    ads_auth.add_argument(
        "--profile-id", default=os.getenv("AMAZON_AD_PROFILE_ID")
    )
    ads = sub.add_parser("sync-ads-campaigns")
    ads.add_argument("--store-id", default=os.getenv("AMAZON_STORE_ID"))
    ads.add_argument("--marketplace", default=os.getenv("AMAZON_MARKETPLACE_ID"))
    ads.add_argument("--profile-id", default=os.getenv("AMAZON_AD_PROFILE_ID"))
    ads.add_argument("--region", default=os.getenv("AMAZON_AD_REGION", "NA"))
    ads.add_argument("--timezone", default=os.getenv("AMAZON_TIMEZONE", "UTC"))
    ads.add_argument("--currency", default=os.getenv("AMAZON_CURRENCY", "USD"))
    ads.add_argument("--start-date", type=lambda value: datetime.fromisoformat(value).date())
    ads.add_argument("--end-date", type=lambda value: datetime.fromisoformat(value).date())
    ads.add_argument("--poll-timeout-seconds", type=int, default=1800)
    ads.add_argument("--poll-interval-seconds", type=int, default=15)
    for command in ("sync-ads-search-terms", "sync-ads-purchased-products"):
        detail = sub.add_parser(command)
        detail.add_argument("--store-id", default=os.getenv("AMAZON_STORE_ID"))
        detail.add_argument(
            "--marketplace", default=os.getenv("AMAZON_MARKETPLACE_ID")
        )
        detail.add_argument(
            "--profile-id", default=os.getenv("AMAZON_AD_PROFILE_ID")
        )
        detail.add_argument("--region", default=os.getenv("AMAZON_AD_REGION", "NA"))
        detail.add_argument("--timezone", default=os.getenv("AMAZON_TIMEZONE", "UTC"))
        detail.add_argument("--currency", default=os.getenv("AMAZON_CURRENCY", "USD"))
        detail.add_argument(
            "--start-date", type=lambda value: datetime.fromisoformat(value).date()
        )
        detail.add_argument(
            "--end-date", type=lambda value: datetime.fromisoformat(value).date()
        )
        detail.add_argument("--poll-timeout-seconds", type=int, default=1800)
        detail.add_argument("--poll-interval-seconds", type=int, default=15)
    args = parser.parse_args()

    exit_code = 0
    if args.command == "migrate":
        migrate()
        result = {"migrated": True}
    elif args.command == "load-demo":
        result = load_demo(args.file)
    elif args.command == "check":
        with connect() as conn:
            result = run_checks(conn)
    elif args.command == "amazon-auth":
        result, exit_code = amazon_auth_status(verify=args.verify, region=args.region)
    elif args.command == "sync-orders":
        if not args.store_id or not args.marketplace:
            parser.error(
                "sync-orders requires --store-id/AMAZON_STORE_ID and "
                "--marketplace/AMAZON_MARKETPLACE_ID"
            )
        from .connectors.sp_api import AmazonCredentials, SPAPIClient, SPAPIError
        from .sync_orders import OrdersSyncConfig, sync_orders

        config = OrdersSyncConfig(
            store_id=args.store_id,
            marketplace=args.marketplace,
            region=args.region,
            timezone=args.timezone,
            currency=args.currency,
            initial_days=args.initial_days,
            lookback_minutes=args.lookback_minutes,
        )
        try:
            credentials = AmazonCredentials.from_env()
            with SPAPIClient(credentials, region=config.region) as client, connect() as conn:
                synced = sync_orders(conn, client, config, resume=not args.no_resume)
                result = {"sync": synced, "checks": run_checks(conn)}
        except SPAPIError as exc:
            result = {
                "sync": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            exit_code = 1
    elif args.command == "sync-inventory":
        if not args.store_id or not args.marketplace:
            parser.error(
                "sync-inventory requires --store-id/AMAZON_STORE_ID and "
                "--marketplace/AMAZON_MARKETPLACE_ID"
            )
        from .connectors.sp_api import AmazonCredentials, SPAPIClient, SPAPIError
        from .sync_inventory import InventorySyncConfig, sync_inventory

        config = InventorySyncConfig(
            store_id=args.store_id,
            marketplace=args.marketplace,
            region=args.region,
            timezone=args.timezone,
            currency=args.currency,
        )
        try:
            credentials = AmazonCredentials.from_env()
            with SPAPIClient(credentials, region=config.region) as client, connect() as conn:
                synced = sync_inventory(conn, client, config)
                result = {"sync": synced, "checks": run_checks(conn)}
        except SPAPIError as exc:
            result = {
                "sync": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            exit_code = 1
    elif args.command == "sync-settlements":
        if not args.store_id or not args.marketplace:
            parser.error(
                "sync-settlements requires --store-id/AMAZON_STORE_ID and "
                "--marketplace/AMAZON_MARKETPLACE_ID"
            )
        from .connectors.sp_api import AmazonCredentials, SPAPIClient, SPAPIError
        from .sync_settlements import SettlementSyncConfig, sync_settlements

        config = SettlementSyncConfig(
            store_id=args.store_id,
            marketplace=args.marketplace,
            region=args.region,
            timezone=args.timezone,
            currency=args.currency,
            created_since_days=args.created_since_days,
        )
        try:
            credentials = AmazonCredentials.from_env()
            with SPAPIClient(credentials, region=config.region) as client, connect() as conn:
                synced = sync_settlements(conn, client, config)
                result = {"sync": synced, "checks": run_checks(conn)}
        except (SPAPIError, ValueError) as exc:
            result = {
                "sync": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "retryable": getattr(exc, "retryable", False),
            }
            exit_code = 1
    elif args.command == "amazon-ads-auth":
        from .connectors.ads_api import AdsAPIError, AdsCredentials, AmazonAdsClient

        names = (
            "AMAZON_AD_CLIENT_ID",
            "AMAZON_AD_CLIENT_SECRET",
            "AMAZON_AD_REFRESH_TOKEN",
        )
        configured = {name: bool(os.getenv(name, "").strip()) for name in names}
        result = {
            "configured": all(configured.values()) and bool(args.profile_id),
            "fields": configured,
            "profile_id_configured": bool(args.profile_id),
            "region": args.region.upper(),
            "verified": False,
        }
        if not result["configured"]:
            result["missing"] = [
                name for name, present in configured.items() if not present
            ]
            if not args.profile_id:
                result["missing"].append("AMAZON_AD_PROFILE_ID")
            exit_code = 1
        elif args.verify:
            try:
                credentials = AdsCredentials.from_env()
                with AmazonAdsClient(
                    credentials, profile_id=args.profile_id, region=args.region
                ) as client:
                    profile = client.get_profile()
                result["verified"] = True
                result["profile"] = {
                    "country_code": profile.get("countryCode"),
                    "currency": profile.get("currencyCode"),
                    "timezone": profile.get("timezone"),
                    "marketplace": (
                        profile.get("accountInfo", {}).get("marketplaceStringId")
                        if isinstance(profile.get("accountInfo"), dict)
                        else None
                    ),
                }
            except AdsAPIError as exc:
                result["error_type"] = type(exc).__name__
                result["message"] = str(exc)
                exit_code = 1
    elif args.command == "sync-ads-campaigns":
        if not args.store_id or not args.marketplace or not args.profile_id:
            parser.error(
                "sync-ads-campaigns requires store, marketplace and Ads profile ID"
            )
        from .connectors.ads_api import AdsAPIError, AdsCredentials, AmazonAdsClient
        from .sync_ads import AdsCampaignSyncConfig, sync_ads_campaigns

        local_today = datetime.now(ZoneInfo(args.timezone)).date()
        end_date = args.end_date or (local_today - timedelta(days=1))
        start_date = args.start_date or end_date
        config = AdsCampaignSyncConfig(
            store_id=args.store_id,
            marketplace=args.marketplace,
            profile_id=args.profile_id,
            start_date=start_date,
            end_date=end_date,
            region=args.region,
            timezone=args.timezone,
            currency=args.currency,
            poll_timeout_seconds=args.poll_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        try:
            credentials = AdsCredentials.from_env()
            with AmazonAdsClient(
                credentials,
                profile_id=config.profile_id,
                region=config.region,
            ) as client, connect() as conn:
                synced = sync_ads_campaigns(conn, client, config)
                result = {"sync": synced, "checks": run_checks(conn)}
        except AdsAPIError as exc:
            result = {
                "sync": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "retryable": exc.retryable,
            }
            exit_code = 1
    elif args.command in {"sync-ads-search-terms", "sync-ads-purchased-products"}:
        if not args.store_id or not args.marketplace or not args.profile_id:
            parser.error(
                f"{args.command} requires store, marketplace and Ads profile ID"
            )
        from .connectors.ads_api import AdsAPIError, AdsCredentials, AmazonAdsClient
        from .sync_ads import AdsCampaignSyncConfig
        from .sync_ads_details import (
            sync_ads_purchased_products,
            sync_ads_search_terms,
        )

        local_today = datetime.now(ZoneInfo(args.timezone)).date()
        end_date = args.end_date or (local_today - timedelta(days=1))
        start_date = args.start_date or end_date
        purchased_products = args.command == "sync-ads-purchased-products"
        config = AdsCampaignSyncConfig(
            store_id=args.store_id,
            marketplace=args.marketplace,
            profile_id=args.profile_id,
            start_date=start_date,
            end_date=end_date,
            region=args.region,
            timezone=args.timezone,
            currency=args.currency,
            attribution_window_days=30 if purchased_products else 14,
            poll_timeout_seconds=args.poll_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        sync_function = (
            sync_ads_purchased_products
            if purchased_products
            else sync_ads_search_terms
        )
        try:
            credentials = AdsCredentials.from_env()
            with AmazonAdsClient(
                credentials,
                profile_id=config.profile_id,
                region=config.region,
            ) as client, connect() as conn:
                synced = sync_function(conn, client, config)
                result = {"sync": synced, "checks": run_checks(conn)}
        except AdsAPIError as exc:
            result = {
                "sync": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "retryable": exc.retryable,
            }
            exit_code = 1
    elif args.command == "doctor":
        result, exit_code = doctor()
    elif args.command == "install-skill":
        result = install_skill(args.host, args.target_dir)
    elif args.command == "mcp":
        try:
            from .mcp_server import run
        except ImportError:
            parser.error("MCP support is not installed; install amazon-data-core[agent]")
        run()
        return
    else:
        with connect() as conn:
            result = health_summary(conn)
    print(json.dumps(result, ensure_ascii=False, default=str))
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
