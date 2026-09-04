import gzip
import json
from urllib.parse import parse_qs

import httpx
import pytest

from amazon_data_core.connectors.ads_api import (
    AdsAPIError,
    AdsCredentials,
    AmazonAdsClient,
    verify_profile_scope,
)


def credentials() -> AdsCredentials:
    return AdsCredentials("ads-client-id", "ads-client-secret", "ads-refresh-token")


def test_ads_client_auth_headers_report_lifecycle_and_gzip_download():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.amazon.com":
            form = parse_qs(request.content.decode())
            assert form["refresh_token"] == ["ads-refresh-token"]
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.url.host == "reports.example":
            return httpx.Response(
                200,
                content=gzip.compress(json.dumps([{"campaignId": "1"}]).encode()),
            )
        assert request.headers["Authorization"] == "Bearer access"
        assert request.headers["Amazon-Advertising-API-ClientId"] == "ads-client-id"
        assert request.headers["Amazon-Advertising-API-Scope"] == "profile-1"
        if request.url.path == "/v2/profiles/profile-1":
            return httpx.Response(
                200,
                json={
                    "profileId": "profile-1",
                    "countryCode": "US",
                    "currencyCode": "USD",
                    "timezone": "America/Los_Angeles",
                    "accountInfo": {"marketplaceStringId": "market-1"},
                },
            )
        if request.method == "POST":
            assert request.url.path == "/reporting/reports"
            return httpx.Response(202, json={"reportId": "report-1", "status": "PENDING"})
        assert request.url.path == "/reporting/reports/report-1"
        return httpx.Response(
            200,
            json={
                "reportId": "report-1",
                "status": "COMPLETED",
                "url": "https://reports.example/report.gz",
            },
        )

    with AmazonAdsClient(
        credentials(),
        profile_id="profile-1",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    ) as client:
        created = client.create_report({"name": "test"})
        profile = client.get_profile()
        report = client.get_report(created["reportId"])
        rows = client.download_report(report["url"])

    assert rows == [{"campaignId": "1"}]
    assert profile["currencyCode"] == "USD"
    assert len([request for request in requests if request.url.host == "api.amazon.com"]) == 1
    assert all("ads-client-secret" not in str(request.url) for request in requests)


def test_verify_profile_scope_rejects_mismatched_store_metadata():
    profile = {
        "profileId": "profile-1",
        "countryCode": "US",
        "currencyCode": "USD",
        "timezone": "America/Los_Angeles",
        "accountInfo": {"marketplaceStringId": "market-1"},
    }
    assert verify_profile_scope(
        profile,
        profile_id="profile-1",
        marketplace="market-1",
        timezone="America/Los_Angeles",
        currency="usd",
    )["country_code"] == "US"

    with pytest.raises(AdsAPIError, match="currency"):
        verify_profile_scope(
            profile,
            profile_id="profile-1",
            marketplace="market-1",
            timezone="America/Los_Angeles",
            currency="EUR",
        )
