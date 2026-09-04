from urllib.parse import parse_qs

import httpx
import pytest

from amazon_data_core.connectors.sp_api import (
    AmazonCredentials,
    SPAPIClient,
    SPAPIError,
)
from amazon_data_core.sync_orders import (
    OrdersSyncConfig,
    normalize_order,
    sanitize_order_payload,
)


def credentials() -> AmazonCredentials:
    return AmazonCredentials("client-id", "client-secret", "refresh-token")


def test_lwa_token_is_cached_and_orders_use_current_api_without_leaking_secrets():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.amazon.com":
            form = parse_qs(request.content.decode())
            assert form["refresh_token"] == ["refresh-token"]
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        assert request.url.path == "/orders/2026-01-01/orders"
        assert request.headers["x-amz-access-token"] == "access"
        if "includedData" in request.url.params:
            assert request.url.params["marketplaceIds"] == "ATVPDKIKX0DER"
            assert request.url.params["includedData"] == "FULFILLMENT,PROCEEDS"
        return httpx.Response(200, json={"orders": []})

    with SPAPIClient(
        credentials(), region="NA", transport=httpx.MockTransport(handler)
    ) as client:
        client.search_orders(
            {
                "lastUpdatedAfter": "2026-09-01T00:00:00Z",
                "marketplaceIds": ["ATVPDKIKX0DER"],
                "includedData": ["FULFILLMENT", "PROCEEDS"],
            }
        )
        client.search_orders({"lastUpdatedAfter": "2026-09-02T00:00:00Z"})

    assert len([request for request in requests if request.url.host == "api.amazon.com"]) == 1
    assert all("client-secret" not in str(request.url) for request in requests)


def test_orders_retry_429_and_error_message_does_not_include_response_body():
    order_calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal order_calls
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        order_calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "0", "x-amzn-RequestId": "request-1"},
            json={"secret": "must-not-appear"},
        )

    with SPAPIClient(
        credentials(),
        region="NA",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep=sleeps.append,
    ) as client:
        with pytest.raises(SPAPIError) as raised:
            client.search_orders({"lastUpdatedAfter": "2026-09-01T00:00:00Z"})

    assert order_calls == 2
    assert sleeps == [0.0]
    assert "request-1" in str(raised.value)
    assert "must-not-appear" not in str(raised.value)


def test_fba_inventory_uses_documented_path_and_csv_marketplace_ids():
    inventory_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        inventory_requests.append(request)
        assert request.url.path == "/fba/inventory/v1/summaries"
        return httpx.Response(
            200,
            json={"payload": {"inventorySummaries": []}, "pagination": {}},
        )

    with SPAPIClient(
        credentials(), region="NA", transport=httpx.MockTransport(handler)
    ) as client:
        client.get_inventory_summaries(
            {
                "details": True,
                "granularityType": "Marketplace",
                "granularityId": "ATVPDKIKX0DER",
                "marketplaceIds": ["ATVPDKIKX0DER"],
            }
        )

    request = inventory_requests[0]
    assert request.url.params["details"] == "true"
    assert request.url.params["granularityType"] == "Marketplace"
    assert request.url.params["granularityId"] == "ATVPDKIKX0DER"
    assert request.url.params["marketplaceIds"] == "ATVPDKIKX0DER"


def test_reports_api_lists_metadata_and_downloads_presigned_document_without_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.url.path == "/reports/2021-06-30/reports":
            assert request.url.params["reportTypes"] == (
                "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2"
            )
            return httpx.Response(200, json={"reports": []})
        if request.url.path == "/reports/2021-06-30/documents/DOC-1":
            return httpx.Response(
                200,
                json={
                    "url": "https://download.example/report.tsv.gz",
                    "compressionAlgorithm": "GZIP",
                },
            )
        assert request.url.host == "download.example"
        assert "x-amz-access-token" not in request.headers
        return httpx.Response(200, content=b"report-bytes")

    with SPAPIClient(
        credentials(), region="NA", transport=httpx.MockTransport(handler)
    ) as client:
        client.get_reports(
            {
                "reportTypes": ["GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2"],
                "processingStatuses": ["DONE"],
            }
        )
        document = client.get_report_document("DOC-1")
        content = client.download_report_document(document["url"])

    assert content == b"report-bytes"
    assert len(requests) == 4


def test_normalization_and_recursive_pii_redaction():
    payload = {
        "orderId": "ORDER-1",
        "createdTime": "2026-09-03T01:00:00Z",
        "lastUpdatedTime": "2026-09-03T02:00:00Z",
        "salesChannel": {"marketplaceId": "ATVPDKIKX0DER"},
        "buyer": {"name": "private"},
        "orderItems": [
            {"quantityOrdered": 2, "recipient": {"name": "private"}},
            {"quantityOrdered": 1},
        ],
        "fulfillment": {"fulfillmentStatus": "SHIPPED", "fulfilledBy": "AMAZON"},
        "proceeds": {"grandTotal": {"amount": "12.30", "currencyCode": "USD"}},
    }

    sanitized = sanitize_order_payload(payload)
    normalized = normalize_order(sanitized, "ATVPDKIKX0DER")

    assert "buyer" not in sanitized
    assert "recipient" not in sanitized["orderItems"][0]
    assert normalized["item_count"] == 3
    assert str(normalized["proceeds_total_amount"]) == "12.30"
    assert normalized["currency"] == "USD"


def test_pii_datasets_are_rejected_by_configuration():
    with pytest.raises(ValueError, match="PII datasets"):
        OrdersSyncConfig(
            store_id="s",
            marketplace="m",
            included_data=("FULFILLMENT", "BUYER"),
        ).validate()
