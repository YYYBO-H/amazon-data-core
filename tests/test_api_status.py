from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from amazon_data_core.api import app, index


def test_status_page_uses_per_response_csp_nonce():
    response = index()
    body = response.body.decode("utf-8")
    csp = response.headers["content-security-policy"]
    match = re.search(r"script-src 'nonce-([^']+)'", csp)

    assert match
    assert f'nonce="{match.group(1)}"' in body
    assert "__NONCE__" not in body
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL is required for integration test"
)
def test_sync_status_api_is_credential_free():
    response = TestClient(app).get("/v1/sync-status")

    assert response.status_code == 200
    result = response.json()
    assert result["schema_version"] == 1
    assert len(result["datasets"]) == 6
    assert "safe_to_analyze" in result["overall"]
    serialized = response.text
    assert "AMAZON_CLIENT_SECRET" not in serialized
    assert "AMAZON_REFRESH_TOKEN" not in serialized
