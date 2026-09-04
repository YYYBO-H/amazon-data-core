from __future__ import annotations

import gzip
import json
import os
import platform
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from amazon_data_core import __version__

from .sp_api import LWA_TOKEN_URL, RETRYABLE_STATUS_CODES, SPAPIError, TokenBucket

ADS_REGION_ENDPOINTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}
MAX_COMPRESSED_REPORT_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_REPORT_BYTES = 500 * 1024 * 1024


class AdsAPIError(SPAPIError):
    pass


@dataclass(frozen=True)
class AdsCredentials:
    client_id: str
    client_secret: str
    refresh_token: str

    @classmethod
    def from_env(cls) -> "AdsCredentials":
        names = (
            "AMAZON_AD_CLIENT_ID",
            "AMAZON_AD_CLIENT_SECRET",
            "AMAZON_AD_REFRESH_TOKEN",
        )
        values = {name: os.getenv(name, "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise AdsAPIError(
                f"missing Amazon Ads credentials: {', '.join(missing)}",
                retryable=False,
            )
        return cls(
            client_id=values["AMAZON_AD_CLIENT_ID"],
            client_secret=values["AMAZON_AD_CLIENT_SECRET"],
            refresh_token=values["AMAZON_AD_REFRESH_TOKEN"],
        )


class AmazonAdsClient:
    """Minimal Amazon Ads Reporting v3 client with safe retries."""

    def __init__(
        self,
        credentials: AdsCredentials,
        *,
        profile_id: str,
        region: str = "NA",
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        limiter: TokenBucket | None = None,
    ) -> None:
        normalized_region = region.upper()
        if normalized_region not in ADS_REGION_ENDPOINTS:
            raise AdsAPIError(f"unsupported Amazon Ads region: {region}")
        if not profile_id.strip():
            raise AdsAPIError("Amazon Ads profile_id is required")
        self.credentials = credentials
        self.profile_id = profile_id.strip()
        self.endpoint = ADS_REGION_ENDPOINTS[normalized_region]
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.sleep = sleep
        self.limiter = limiter or TokenBucket(
            capacity=1,
            requests_per_second=0.5,
            sleep=sleep,
        )
        self.http = httpx.Client(transport=transport, timeout=timeout_seconds)
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "AmazonAdsClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_access_token(self) -> str:
        now = time.time()
        if self._access_token and self._token_expires_at > now + 60:
            return self._access_token
        try:
            response = self.http.post(
                LWA_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.credentials.refresh_token,
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TransportError as exc:
            raise AdsAPIError(
                f"Amazon Ads LWA connection failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.status_code != 200:
            raise AdsAPIError(
                f"Amazon Ads LWA authorization failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
            token = body["access_token"]
            expires_in = int(body.get("expires_in", 3600))
        except (KeyError, TypeError, ValueError) as exc:
            raise AdsAPIError("Amazon Ads LWA returned an invalid token response") from exc
        if not isinstance(token, str) or not token:
            raise AdsAPIError("Amazon Ads LWA returned an empty access token")
        self._access_token = token
        self._token_expires_at = now + max(expires_in, 1)
        return token

    def create_report(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", "/reporting/reports", json_body=body)

    def get_profile(self) -> dict[str, Any]:
        """Return the scoped advertising profile used to verify store metadata."""
        return self._request_json("GET", f"/v2/profiles/{self.profile_id}")

    def get_report(self, report_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/reporting/reports/{report_id}")

    def download_report(self, url: str) -> list[dict[str, Any]]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise AdsAPIError("Amazon Ads report returned an invalid download URL")
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.http.get(url, timeout=max(self.timeout_seconds, 180.0))
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise AdsAPIError(
                        f"Amazon Ads report download failed: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
                self.sleep(min(2**attempt, 60))
                continue
            if response.status_code not in RETRYABLE_STATUS_CODES:
                break
            if attempt >= self.max_retries:
                break
            self.sleep(self._retry_delay(response, attempt))
        if response is None:  # pragma: no cover
            raise AdsAPIError("Amazon Ads report download returned no response")
        if response.status_code != 200:
            raise AdsAPIError(
                f"Amazon Ads report download failed with HTTP {response.status_code}",
                retryable=response.status_code in RETRYABLE_STATUS_CODES,
                status_code=response.status_code,
            )
        if len(response.content) > MAX_COMPRESSED_REPORT_BYTES:
            raise AdsAPIError("Amazon Ads compressed report exceeds the safety limit")
        raw = response.content
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except OSError as exc:
                raise AdsAPIError("Amazon Ads report contains invalid gzip data") from exc
        if len(raw) > MAX_UNCOMPRESSED_REPORT_BYTES:
            raise AdsAPIError("Amazon Ads report exceeds the safety limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdsAPIError("Amazon Ads report contains invalid JSON") from exc
        if isinstance(payload, dict):
            payload = (payload.get("report") or {}).get("rows", payload.get("rows"))
        if not isinstance(payload, list) or not all(
            isinstance(row, dict) for row in payload
        ):
            raise AdsAPIError("Amazon Ads report is not a JSON row list")
        return payload

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Amazon-Advertising-API-ClientId": self.credentials.client_id,
            "Amazon-Advertising-API-Scope": self.profile_id,
            "Content-Type": "application/json",
            "accept": "application/json",
            "user-agent": (
                f"AmazonDataCore/{__version__} "
                f"(Language=Python/{platform.python_version()})"
            ),
        }
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                response = self.http.request(
                    method,
                    f"{self.endpoint}{path}",
                    headers=headers,
                    json=json_body,
                )
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise AdsAPIError(
                        f"Amazon Ads API connection failed: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
                self.sleep(min(2**attempt, 60))
                continue
            if response.status_code not in RETRYABLE_STATUS_CODES:
                break
            if attempt >= self.max_retries:
                break
            self.sleep(self._retry_delay(response, attempt))
        if response is None:  # pragma: no cover
            raise AdsAPIError("Amazon Ads API returned no response", retryable=True)
        if response.status_code not in {200, 201, 202}:
            request_id = response.headers.get("Amazon-Advertising-API-RequestId", "unknown")
            raise AdsAPIError(
                f"Amazon Ads API failed with HTTP {response.status_code}; "
                f"request_id={request_id}",
                retryable=response.status_code in RETRYABLE_STATUS_CODES,
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AdsAPIError("Amazon Ads API returned non-JSON data") from exc
        if not isinstance(body, dict):
            raise AdsAPIError("Amazon Ads API returned an invalid JSON object")
        return body

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else float(2**attempt)
        except ValueError:
            delay = float(2**attempt)
        return max(0.0, min(delay, 180.0))


def verify_profile_scope(
    profile: dict[str, Any],
    *,
    profile_id: str,
    marketplace: str,
    timezone: str,
    currency: str,
) -> dict[str, str]:
    """Fail closed when the Ads profile conflicts with configured store scope."""
    account_info = profile.get("accountInfo")
    if not isinstance(account_info, dict):
        account_info = {}
    actual = {
        "profile_id": str(profile.get("profileId") or "").strip(),
        "marketplace": str(account_info.get("marketplaceStringId") or "").strip(),
        "timezone": str(profile.get("timezone") or "").strip(),
        "currency": str(profile.get("currencyCode") or "").strip().upper(),
        "country_code": str(profile.get("countryCode") or "").strip().upper(),
    }
    expected = {
        "profile_id": profile_id.strip(),
        "marketplace": marketplace.strip(),
        "timezone": timezone.strip(),
        "currency": currency.strip().upper(),
    }
    missing = [field for field in expected if not actual[field]]
    if missing:
        raise AdsAPIError(
            f"Amazon Ads profile is missing required metadata: {', '.join(missing)}"
        )
    mismatches = [
        field
        for field, expected_value in expected.items()
        if actual[field] != expected_value
    ]
    if mismatches:
        raise AdsAPIError(
            "Amazon Ads profile does not match configured store scope: "
            + ", ".join(mismatches)
        )
    return actual
