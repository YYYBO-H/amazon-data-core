from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
assert SPEC and SPEC.loader
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


def test_ready_macos_returns_onboarding_action():
    with (
        patch.object(PREFLIGHT.platform, "system", return_value="Darwin"),
        patch.object(PREFLIGHT.platform, "machine", return_value="arm64"),
        patch.object(PREFLIGHT.shutil, "which", return_value="/usr/local/bin/docker"),
        patch.object(PREFLIGHT, "_run", side_effect=[(True, "27.0"), (True, "2.29")]),
    ):
        report = PREFLIGHT.build_report()

    assert report["runtime_ready"] is True
    assert report["next_action"] == "run_onboarding"
    assert report["onboard_command"] == "./scripts/onboard.sh"


def test_missing_docker_returns_install_action():
    with (
        patch.object(PREFLIGHT.platform, "system", return_value="Linux"),
        patch.object(PREFLIGHT.shutil, "which", return_value=None),
    ):
        report = PREFLIGHT.build_report()

    assert report["runtime_ready"] is False
    assert report["next_action"] == "install_docker"


def test_native_windows_is_honest_about_wsl2_requirement():
    with (
        patch.object(PREFLIGHT.platform, "system", return_value="Windows"),
        patch.object(PREFLIGHT.shutil, "which", return_value="C:/docker.exe"),
        patch.object(PREFLIGHT, "_run", side_effect=[(True, "27.0"), (True, "2.29")]),
    ):
        report = PREFLIGHT.build_report()

    assert report["runtime_ready"] is False
    assert report["support"]["native_windows_verified"] is False
    assert report["next_action"] == "use_wsl2"


def test_unknown_platform_is_not_sent_into_onboarding():
    with (
        patch.object(PREFLIGHT.platform, "system", return_value="FreeBSD"),
        patch.object(PREFLIGHT.shutil, "which", return_value="/usr/local/bin/docker"),
        patch.object(PREFLIGHT, "_run", side_effect=[(True, "27.0"), (True, "2.29")]),
    ):
        report = PREFLIGHT.build_report()

    assert report["runtime_ready"] is False
    assert report["next_action"] == "unsupported_platform"
    assert report["onboard_command"] == ""
