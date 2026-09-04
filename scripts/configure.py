#!/usr/bin/env python3
"""Safely create the local .env used by Amazon Data Core.

Secrets are collected from the user's terminal and are never printed.  This
script intentionally uses only the Python standard library so it can run before
the Docker image or Python package has been installed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

MARKETPLACES = {
    # marketplace ID, SP-API region, IANA timezone, currency, Ads region
    "AU": ("A39IBJ37TRP1C6", "FE", "Australia/Sydney", "AUD", "FE"),
    "BR": ("A2Q3Y263D00KWC", "NA", "America/Sao_Paulo", "BRL", "NA"),
    "CA": ("A2EUQ1WTGCTBG2", "NA", "America/Toronto", "CAD", "NA"),
    "DE": ("A1PA6795UKMFR9", "EU", "Europe/Berlin", "EUR", "EU"),
    "ES": ("A1RKKUPIHCS9HS", "EU", "Europe/Madrid", "EUR", "EU"),
    "FR": ("A13V1IB3VIYZZH", "EU", "Europe/Paris", "EUR", "EU"),
    "IN": ("A21TJRUUN4KGV", "EU", "Asia/Kolkata", "INR", "FE"),
    "IT": ("APJ6JRA9NG5V4", "EU", "Europe/Rome", "EUR", "EU"),
    "JP": ("A1VC38T7YXB528", "FE", "Asia/Tokyo", "JPY", "FE"),
    "MX": ("A1AM78C64UM0Y8", "NA", "America/Mexico_City", "MXN", "NA"),
    "NL": ("A1805IZSGTT6HS", "EU", "Europe/Amsterdam", "EUR", "EU"),
    "PL": ("A1C3SOZRARQ6R3", "EU", "Europe/Warsaw", "PLN", "EU"),
    "SE": ("A2NODRKZP88ZB9", "EU", "Europe/Stockholm", "SEK", "EU"),
    "SG": ("A19VAU5U5O7RUS", "FE", "Asia/Singapore", "SGD", "FE"),
    "UK": ("A1F83G8C2ARO7P", "EU", "Europe/London", "GBP", "EU"),
    "US": ("ATVPDKIKX0DER", "NA", "America/Los_Angeles", "USD", "NA"),
}

ENV_ORDER = (
    "DATABASE_URL",
    "LOAD_DEMO",
    "AMAZON_CLIENT_ID",
    "AMAZON_CLIENT_SECRET",
    "AMAZON_REFRESH_TOKEN",
    "AMAZON_STORE_ID",
    "AMAZON_MARKETPLACE_ID",
    "AMAZON_REGION",
    "AMAZON_TIMEZONE",
    "AMAZON_CURRENCY",
    "AMAZON_AD_CLIENT_ID",
    "AMAZON_AD_CLIENT_SECRET",
    "AMAZON_AD_REFRESH_TOKEN",
    "AMAZON_AD_PROFILE_ID",
    "AMAZON_AD_REGION",
)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        quote = value[0]
        value = value[1:-1]
        if quote == "'":
            return value.replace("\\'", "'").replace("\\\\", "\\")
        return value.replace("\\\"", "\"").replace("\\\\", "\\")
    return value


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = _unquote(value)
    return values


def _quote(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("configuration values cannot contain newlines or NUL bytes")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def write_env(path: Path, values: dict[str, str]) -> None:
    """Write an env file atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated locally by scripts/configure.py. Never commit this file.",
        "# Amazon SP-API",
    ]
    for key in ENV_ORDER:
        if key == "AMAZON_AD_CLIENT_ID":
            lines.append("")
            lines.append("# Optional Amazon Ads API")
        lines.append(f"{key}={_quote(values.get(key, ''))}")
    payload = "\n".join(lines) + "\n"

    fd, temporary_name = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def configuration_status(values: dict[str, str]) -> dict[str, object]:
    seller_fields = (
        "AMAZON_CLIENT_ID",
        "AMAZON_CLIENT_SECRET",
        "AMAZON_REFRESH_TOKEN",
        "AMAZON_STORE_ID",
        "AMAZON_MARKETPLACE_ID",
        "AMAZON_REGION",
        "AMAZON_TIMEZONE",
        "AMAZON_CURRENCY",
    )
    ads_fields = (
        "AMAZON_AD_CLIENT_ID",
        "AMAZON_AD_CLIENT_SECRET",
        "AMAZON_AD_REFRESH_TOKEN",
        "AMAZON_AD_PROFILE_ID",
    )
    seller_presence = {key: bool(values.get(key, "").strip()) for key in seller_fields}
    ads_presence = {key: bool(values.get(key, "").strip()) for key in ads_fields}
    return {
        "sp_api_configured": all(seller_presence.values()),
        "ads_configured": all(ads_presence.values()),
        "sp_api_fields": seller_presence,
        "ads_fields": ads_presence,
        "env_permissions": None,
        "env_private": False,
    }


def _prompt(
    label: str,
    default: str,
    input_fn: Callable[[str], str],
    *,
    required: bool = True,
) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input_fn(f"{label}{suffix}: ").strip()
        value = value or default
        if value or not required:
            return value
        print("This value is required.")


def _secret(
    label: str,
    existing: str,
    secret_fn: Callable[[str], str],
) -> str:
    suffix = " [press Enter to keep the saved value]" if existing else ""
    while True:
        value = secret_fn(f"{label}{suffix}: ").strip()
        if value:
            return value
        if existing:
            return existing
        print("This value is required.")


def _yes_no(
    label: str,
    default: bool,
    input_fn: Callable[[str], str],
) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input_fn(f"{label} [{suffix}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer y or n.")


def _validate(values: dict[str, str]) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", values["AMAZON_STORE_ID"]):
        raise ValueError("store alias must use only letters, numbers, dot, underscore or dash")
    if values["AMAZON_REGION"] not in {"NA", "EU", "FE"}:
        raise ValueError("Amazon region must be NA, EU or FE")
    if values["AMAZON_AD_REGION"] not in {"NA", "EU", "FE"}:
        raise ValueError("Amazon Ads region must be NA, EU or FE")
    if not re.fullmatch(r"[A-Z]{3}", values["AMAZON_CURRENCY"]):
        raise ValueError("currency must be a three-letter ISO code")
    try:
        ZoneInfo(values["AMAZON_TIMEZONE"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def interactive_configuration(
    existing: dict[str, str],
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
) -> dict[str, str]:
    print("\nAmazon Data Core seller authorization")
    print("Secrets entered here stay in this terminal and are saved only to local .env (0600).")
    print("Use a private SP-API app for your own organization; public seller access requires Amazon OAuth approval.")

    print("\nMarketplace presets: " + ", ".join(MARKETPLACES) + ", CUSTOM")
    preset_default = "US"
    current_marketplace = existing.get("AMAZON_MARKETPLACE_ID", "")
    for code, (marketplace, *_rest) in MARKETPLACES.items():
        if marketplace == current_marketplace:
            preset_default = code
            break
    preset = _prompt("Marketplace", preset_default, input_fn).upper()
    while preset not in MARKETPLACES and preset != "CUSTOM":
        print("Choose a listed country code or CUSTOM.")
        preset = _prompt("Marketplace", preset_default, input_fn).upper()

    if preset == "CUSTOM":
        marketplace = _prompt("Marketplace ID", current_marketplace, input_fn)
        region = _prompt("SP-API region (NA/EU/FE)", existing.get("AMAZON_REGION", "NA"), input_fn).upper()
        timezone = _prompt("IANA timezone", existing.get("AMAZON_TIMEZONE", "UTC"), input_fn)
        currency = _prompt("ISO currency", existing.get("AMAZON_CURRENCY", "USD"), input_fn).upper()
        ads_region = existing.get("AMAZON_AD_REGION", "NA") or "NA"
    else:
        marketplace, region, timezone, currency, ads_region = MARKETPLACES[preset]

    values = dict(existing)
    values.update(
        {
            "DATABASE_URL": existing.get(
                "DATABASE_URL", "postgresql://data_core:data_core@localhost:55432/data_core"
            ),
            "LOAD_DEMO": "false",
            "AMAZON_STORE_ID": _prompt(
                "Local store alias", existing.get("AMAZON_STORE_ID", f"amazon-{preset.lower()}"), input_fn
            ),
            "AMAZON_MARKETPLACE_ID": marketplace,
            "AMAZON_REGION": region,
            "AMAZON_TIMEZONE": timezone,
            "AMAZON_CURRENCY": currency,
            "AMAZON_CLIENT_ID": _secret("LWA client ID", existing.get("AMAZON_CLIENT_ID", ""), secret_fn),
            "AMAZON_CLIENT_SECRET": _secret(
                "LWA client secret", existing.get("AMAZON_CLIENT_SECRET", ""), secret_fn
            ),
            "AMAZON_REFRESH_TOKEN": _secret(
                "Self-authorization refresh token", existing.get("AMAZON_REFRESH_TOKEN", ""), secret_fn
            ),
            "AMAZON_AD_REGION": existing.get("AMAZON_AD_REGION", ads_region) or ads_region,
        }
    )

    ads_already_configured = configuration_status(existing)["ads_configured"]
    if _yes_no("Configure optional Amazon Ads now?", bool(ads_already_configured), input_fn):
        values.update(
            {
                "AMAZON_AD_CLIENT_ID": _secret(
                    "Ads client ID", existing.get("AMAZON_AD_CLIENT_ID", ""), secret_fn
                ),
                "AMAZON_AD_CLIENT_SECRET": _secret(
                    "Ads client secret", existing.get("AMAZON_AD_CLIENT_SECRET", ""), secret_fn
                ),
                "AMAZON_AD_REFRESH_TOKEN": _secret(
                    "Ads refresh token", existing.get("AMAZON_AD_REFRESH_TOKEN", ""), secret_fn
                ),
                "AMAZON_AD_PROFILE_ID": _secret(
                    "Ads profile ID", existing.get("AMAZON_AD_PROFILE_ID", ""), secret_fn
                ),
                "AMAZON_AD_REGION": _prompt(
                    "Ads region (NA/EU/FE)",
                    existing.get("AMAZON_AD_REGION", ads_region) or ads_region,
                    input_fn,
                ).upper(),
            }
        )
    else:
        for key in (
            "AMAZON_AD_CLIENT_ID",
            "AMAZON_AD_CLIENT_SECRET",
            "AMAZON_AD_REFRESH_TOKEN",
            "AMAZON_AD_PROFILE_ID",
        ):
            values[key] = ""
        values["AMAZON_AD_REGION"] = ads_region

    _validate(values)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument("--status", action="store_true", help="show presence only; never show values")
    parser.add_argument("--require-sp-api", action="store_true")
    parser.add_argument("--ads-configured", action="store_true", help="exit 0 only when all Ads fields exist")
    parser.add_argument("--mcp-config", action="store_true", help="print a generic stdio MCP configuration")
    args = parser.parse_args()

    if args.mcp_config:
        print(
            json.dumps(
                {
                    "mcpServers": {
                        "amazon-data-core": {
                            "command": "docker",
                            "args": ["compose", "exec", "-T", "core", "amazon-data-core", "mcp"],
                            "cwd": str(PROJECT_ROOT),
                        }
                    }
                },
                indent=2,
            )
        )
        return 0

    existing = read_env(args.env_file)
    status_result = configuration_status(existing)
    if args.env_file.exists():
        mode = stat.S_IMODE(args.env_file.stat().st_mode)
        status_result["env_permissions"] = oct(mode)
        status_result["env_private"] = mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    if args.status or args.require_sp_api or args.ads_configured:
        print(json.dumps(status_result, indent=2))
        if args.require_sp_api and (
            not status_result["sp_api_configured"] or not status_result["env_private"]
        ):
            return 1
        if args.ads_configured and not status_result["ads_configured"]:
            return 1
        return 0

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Interactive configuration requires a terminal. Run: ./scripts/onboard.sh", file=sys.stderr)
        return 2
    try:
        configured = interactive_configuration(existing)
        write_env(args.env_file, configured)
    except (EOFError, KeyboardInterrupt):
        print("\nConfiguration cancelled; no credentials were changed.", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"Configuration rejected: {exc}", file=sys.stderr)
        return 2

    print(f"\nSaved local configuration to {args.env_file} with permissions 0600.")
    print("No credential value was printed or sent to the Agent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
