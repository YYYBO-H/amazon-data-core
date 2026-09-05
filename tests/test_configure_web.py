from __future__ import annotations

import importlib.util
import stat
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("configure_web_script", SCRIPTS / "configure_web.py")
assert SPEC and SPEC.loader
configure_web = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure_web)


def _sp_form(token: str) -> dict[str, str]:
    return {
        "csrf": token,
        "marketplace": "US",
        "store_id": "my-store",
        "amazon_client_id": "client-id",
        "amazon_client_secret": "client-secret",
        "amazon_refresh_token": "refresh-token",
    }


def test_sp_only_configuration_does_not_require_ads():
    values = configure_web.build_configuration(
        {key: [value] for key, value in _sp_form("unused").items()}, {}
    )

    assert values["AMAZON_MARKETPLACE_ID"] == "ATVPDKIKX0DER"
    assert values["AMAZON_TIMEZONE"] == "America/Los_Angeles"
    assert values["AMAZON_AD_CLIENT_ID"] == ""


def test_form_never_renders_existing_secret_values():
    secrets = {
        "AMAZON_CLIENT_ID": "existing-client",
        "AMAZON_CLIENT_SECRET": "existing-secret",
        "AMAZON_REFRESH_TOKEN": "existing-refresh-token",
        "AMAZON_AD_CLIENT_ID": "existing-ads-client",
        "AMAZON_AD_CLIENT_SECRET": "existing-ads-secret",
        "AMAZON_AD_REFRESH_TOKEN": "existing-ads-refresh",
        "AMAZON_AD_PROFILE_ID": "existing-profile",
    }

    page = configure_web.render_form(secrets, "page-token")

    assert "page-token" in page
    for value in secrets.values():
        assert value not in page


def test_local_http_flow_writes_private_env_without_returning_secrets(tmp_path: Path):
    env_file = tmp_path / ".env"
    server = configure_web.ConfigurationServer(("127.0.0.1", 0), env_file)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/{server.token}/"
    secret_values = ("client-id", "client-secret", "refresh-token")
    try:
        with urlopen(base_url) as response:
            page = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"

        payload = urlencode(_sp_form(server.token)).encode("utf-8")
        request = Request(
            base_url,
            data=payload,
            headers={"Origin": f"http://127.0.0.1:{server.server_port}"},
        )
        with urlopen(request) as response:
            success = response.read().decode("utf-8")
            assert response.status == 200

        assert "本地授权配置已保存" in success
        assert all(value not in page and value not in success for value in secret_values)
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        saved = configure_web.configure.read_env(env_file)
        assert saved["AMAZON_CLIENT_SECRET"] == "client-secret"
        assert saved["AMAZON_REFRESH_TOKEN"] == "refresh-token"
        assert server.completed.is_set()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_local_http_flow_rejects_cross_origin_post(tmp_path: Path):
    server = configure_web.ConfigurationServer(("127.0.0.1", 0), tmp_path / ".env")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/{server.token}/"
    request = Request(
        url,
        data=urlencode(_sp_form(server.token)).encode("utf-8"),
        headers={"Origin": "https://example.com"},
    )
    try:
        try:
            urlopen(request)
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("cross-origin request was accepted")
        assert not (tmp_path / ".env").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
