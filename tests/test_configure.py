from __future__ import annotations

import importlib.util
import stat
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "configure.py"
SPEC = importlib.util.spec_from_file_location("configure_script", SCRIPT)
assert SPEC and SPEC.loader
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


def test_write_env_is_private_and_round_trips_special_characters(tmp_path: Path):
    env_file = tmp_path / ".env"
    values = {key: "" for key in configure.ENV_ORDER}
    values.update(
        {
            "DATABASE_URL": "postgresql://data_core:data_core@localhost:55432/data_core",
            "LOAD_DEMO": "false",
            "AMAZON_CLIENT_ID": "amzn1.application-oa2-client.test",
            "AMAZON_CLIENT_SECRET": "secret#$value'with\\slashes",
            "AMAZON_REFRESH_TOKEN": "test-refresh-token",
            "AMAZON_STORE_ID": "amazon-us",
            "AMAZON_MARKETPLACE_ID": "ATVPDKIKX0DER",
            "AMAZON_REGION": "NA",
            "AMAZON_TIMEZONE": "America/Los_Angeles",
            "AMAZON_CURRENCY": "USD",
            "AMAZON_AD_REGION": "NA",
        }
    )

    configure.write_env(env_file, values)

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert configure.read_env(env_file) == values


def test_configuration_status_returns_presence_not_values():
    secret = "must-never-be-returned-refresh-token"
    values = {
        "AMAZON_CLIENT_ID": "client",
        "AMAZON_CLIENT_SECRET": "secret",
        "AMAZON_REFRESH_TOKEN": secret,
        "AMAZON_STORE_ID": "amazon-us",
        "AMAZON_MARKETPLACE_ID": "ATVPDKIKX0DER",
        "AMAZON_REGION": "NA",
        "AMAZON_TIMEZONE": "America/Los_Angeles",
        "AMAZON_CURRENCY": "USD",
    }

    result = configure.configuration_status(values)

    assert result["sp_api_configured"] is True
    assert result["ads_configured"] is False
    assert secret not in repr(result)


def test_interactive_configuration_uses_preset_without_exposing_secrets():
    answers = iter(["US", "my-store", "n"])
    secrets = iter(["client-id", "client-secret", "test-refresh"])

    values = configure.interactive_configuration(
        {}, input_fn=lambda _prompt: next(answers), secret_fn=lambda _prompt: next(secrets)
    )

    assert values["LOAD_DEMO"] == "false"
    assert values["AMAZON_MARKETPLACE_ID"] == "ATVPDKIKX0DER"
    assert values["AMAZON_TIMEZONE"] == "America/Los_Angeles"
    assert values["AMAZON_REFRESH_TOKEN"] == "test-refresh"
    assert configure.configuration_status(values)["sp_api_configured"] is True


def test_validate_rejects_invalid_store_alias():
    values = {
        "AMAZON_STORE_ID": "bad store",
        "AMAZON_REGION": "NA",
        "AMAZON_AD_REGION": "NA",
        "AMAZON_CURRENCY": "USD",
        "AMAZON_TIMEZONE": "UTC",
    }

    try:
        configure._validate(values)
    except ValueError as exc:
        assert "store alias" in str(exc)
    else:
        raise AssertionError("invalid alias was accepted")
