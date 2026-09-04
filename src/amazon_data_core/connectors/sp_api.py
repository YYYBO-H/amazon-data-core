from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from amazon_data_core import __version__

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
REGION_ENDPOINTS = {
    "NA": "https://sellingpartnerapi-na.amazon.com",
    "EU": "https://sellingpartnerapi-eu.amazon.com",
    "FE": "https://sellingpartnerapi-fe.amazon.com",
}
ORDERS_PATH = "/orders/2026-01-01/orders"
FBA_INVENTORY_PATH = "/fba/inventory/v1/summaries"
REPORTS_PATH = "/reports/2021-06-30/reports"
REPORT_DOCUMENT_PATH = "/reports/2021-06-30/documents"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class SPAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class AmazonCredentials:
    client_id: str
    client_secret: str
    refresh_token: str

    @classmethod
    def from_env(cls) -> "AmazonCredentials":
        names = (
            "AMAZON_CLIENT_ID",
            "AMAZON_CLIENT_SECRET",
            "AMAZON_REFRESH_TOKEN",
        )
        values = {name: os.getenv(name, "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise SPAPIError(
                f"missing Amazon credentials: {', '.join(missing)}",
                retryable=False,
            )
        return cls(
            client_id=values["AMAZON_CLIENT_ID"],
            client_secret=values["AMAZON_CLIENT_SECRET"],
            refresh_token=values["AMAZON_REFRESH_TOKEN"],
        )


class TokenBucket:
    """Simple limiter configured per SP-API operation."""

    def __init__(
        self,
        *,
        capacity: int = 20,
        requests_per_second: float = 0.0056,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.refill_per_second = requests_per_second
        self.monotonic = monotonic
        self.sleep = sleep
        self.last_refill = monotonic()

    def acquire(self) -> None:
        now = self.monotonic()
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.refill_per_second,
        )
        self.last_refill = now
        if self.tokens < 1.0:
            wait_seconds = (1.0 - self.tokens) / self.refill_per_second
            self.sleep(wait_seconds)
            self.last_refill = self.monotonic()
            self.tokens = 0.0
        else:
            self.tokens -= 1.0


class SPAPIClient:
    def __init__(
        self,
        credentials: AmazonCredentials,
        *,
        region: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        limiter: TokenBucket | None = None,
    ) -> None:
        normalized_region = region.upper()
        if normalized_region not in REGION_ENDPOINTS:
            raise SPAPIError(f"unsupported Amazon region: {region}")
        self.credentials = credentials
        self.endpoint = REGION_ENDPOINTS[normalized_region]
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.sleep = sleep
        self.limiter = limiter or TokenBucket(sleep=sleep)
        self.inventory_limiter = TokenBucket(
            capacity=2,
            requests_per_second=2.0,
            sleep=sleep,
        )
        self.reports_limiter = TokenBucket(
            capacity=10,
            requests_per_second=0.0222,
            sleep=sleep,
        )
        self.http = httpx.Client(transport=transport, timeout=timeout_seconds)
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "SPAPIClient":
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
            raise SPAPIError(
                f"Amazon LWA connection failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.status_code != 200:
            raise SPAPIError(
                f"Amazon LWA authorization failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
            token = body["access_token"]
            expires_in = int(body.get("expires_in", 3600))
        except (KeyError, TypeError, ValueError) as exc:
            raise SPAPIError("Amazon LWA returned an invalid token response") from exc
        if not isinstance(token, str) or not token:
            raise SPAPIError("Amazon LWA returned an empty access token")
        self._access_token = token
        self._token_expires_at = now + max(expires_in, 1)
        return token

    def search_orders(self, params: dict[str, Any]) -> dict[str, Any]:
        body = self._get_json(
            ORDERS_PATH,
            params,
            operation="Amazon Orders",
            limiter=self.limiter,
        )
        if not isinstance(body.get("orders"), list):
            raise SPAPIError("Amazon Orders response is missing the orders list")
        return body

    def get_inventory_summaries(self, params: dict[str, Any]) -> dict[str, Any]:
        body = self._get_json(
            FBA_INVENTORY_PATH,
            params,
            operation="Amazon FBA Inventory",
            limiter=self.inventory_limiter,
        )
        payload = body.get("payload")
        if not isinstance(payload, dict) or not isinstance(
            payload.get("inventorySummaries"), list
        ):
            raise SPAPIError(
                "Amazon FBA Inventory response is missing inventorySummaries"
            )
        return body

    def get_reports(self, params: dict[str, Any]) -> dict[str, Any]:
        body = self._get_json(
            REPORTS_PATH,
            params,
            operation="Amazon Reports getReports",
            limiter=self.reports_limiter,
        )
        if not isinstance(body.get("reports"), list):
            raise SPAPIError("Amazon Reports response is missing the reports list")
        return body

    def get_report_document(self, report_document_id: str) -> dict[str, Any]:
        body = self._get_json(
            f"{REPORT_DOCUMENT_PATH}/{report_document_id}",
            {},
            operation="Amazon Reports getReportDocument",
            limiter=self.reports_limiter,
        )
        if not isinstance(body.get("url"), str) or not body["url"]:
            raise SPAPIError("Amazon report document response is missing the URL")
        compression = body.get("compressionAlgorithm")
        if compression not in (None, "GZIP"):
            raise SPAPIError(
                f"unsupported Amazon report compression: {compression}",
                retryable=False,
            )
        return body

    def download_report_document(self, url: str) -> bytes:
        try:
            response = self.http.get(url)
        except httpx.TransportError as exc:
            raise SPAPIError(
                f"Amazon report download failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.status_code != 200:
            raise SPAPIError(
                f"Amazon report download failed with HTTP {response.status_code}",
                retryable=response.status_code in RETRYABLE_STATUS_CODES,
                status_code=response.status_code,
            )
        return response.content

    def _get_json(
        self,
        path: str,
        params: dict[str, Any],
        *,
        operation: str,
        limiter: TokenBucket,
    ) -> dict[str, Any]:
        # Official SP-API Swagger 2.0 models use CSV array query encoding unless
        # collectionFormat=multi is explicitly declared.
        query_params = {
            key: ",".join(str(item) for item in value)
            if isinstance(value, (list, tuple))
            else value
            for key, value in params.items()
        }
        headers = {
            "x-amz-access-token": self.get_access_token(),
            "user-agent": (
                f"AmazonDataCore/{__version__} "
                f"(Language=Python/{platform.python_version()})"
            ),
            "accept": "application/json",
        }
        url = f"{self.endpoint}{path}"
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            limiter.acquire()
            try:
                response = self.http.get(url, params=query_params, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise SPAPIError(
                        f"{operation} connection failed: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
                self.sleep(min(2**attempt, 60))
                continue
            if response.status_code not in RETRYABLE_STATUS_CODES:
                break
            if attempt >= self.max_retries:
                break
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            self.sleep(max(0.0, min(delay, 180.0)))
        if response is None:  # pragma: no cover - loop returns or raises
            raise SPAPIError(f"{operation} returned no response", retryable=True)
        if response.status_code != 200:
            request_id = response.headers.get("x-amzn-RequestId", "unknown")
            raise SPAPIError(
                f"{operation} failed with HTTP {response.status_code}; "
                f"request_id={request_id}",
                retryable=response.status_code in RETRYABLE_STATUS_CODES,
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise SPAPIError(f"{operation} returned non-JSON data") from exc
        if not isinstance(body, dict):
            raise SPAPIError(f"{operation} returned an invalid JSON object")
        return body
