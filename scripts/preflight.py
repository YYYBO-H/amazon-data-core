#!/usr/bin/env python3
"""Return a secret-free, machine-readable onboarding preflight report."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MINIMUM_HOST_PYTHON = (3, 9)


def _run(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, type(exc).__name__
    raw_output = result.stdout.strip() or result.stderr.strip()
    output = raw_output.splitlines()
    return result.returncode == 0, output[0] if output else ""


def _environment(system: str) -> str:
    if system == "linux" and (
        os.getenv("WSL_DISTRO_NAME")
        or "microsoft" in platform.release().lower()
    ):
        return "wsl2"
    return "native"


def build_report() -> dict[str, Any]:
    system = platform.system().lower()
    environment = _environment(system)
    python_ok = sys.version_info >= MINIMUM_HOST_PYTHON
    docker_path = shutil.which("docker")
    docker_installed = docker_path is not None
    docker_running, docker_version = (
        _run([docker_path, "info", "--format", "{{.ServerVersion}}"])
        if docker_path
        else (False, "")
    )
    compose_ok, compose_version = (
        _run([docker_path, "compose", "version", "--short"])
        if docker_path
        else (False, "")
    )

    native_windows = system == "windows"
    supported = system in {"darwin", "linux"}

    if native_windows:
        next_action = "use_wsl2"
        onboard_command = "./scripts/onboard.sh (inside WSL2)"
    elif not supported:
        next_action = "unsupported_platform"
        onboard_command = ""
    elif not python_ok:
        next_action = "install_python_3_9_or_newer"
        onboard_command = "./scripts/onboard.sh"
    elif not docker_installed:
        next_action = "install_docker"
        onboard_command = "./scripts/onboard.sh"
    elif not docker_running:
        next_action = "start_docker"
        onboard_command = "./scripts/onboard.sh"
    elif not compose_ok:
        next_action = "install_docker_compose_v2"
        onboard_command = "./scripts/onboard.sh"
    else:
        next_action = "run_onboarding"
        onboard_command = "./scripts/onboard.sh"

    return {
        "schema_version": 1,
        "runtime_ready": bool(
            supported and python_ok and docker_installed and docker_running and compose_ok
        ),
        "platform": system,
        "environment": environment,
        "architecture": platform.machine().lower(),
        "support": {
            "supported": supported,
            "native_windows_verified": False,
        },
        "checks": {
            "python": {
                "ok": python_ok,
                "version": platform.python_version(),
                "minimum": ".".join(map(str, MINIMUM_HOST_PYTHON)),
            },
            "docker_installed": {"ok": docker_installed},
            "docker_running": {"ok": docker_running, "version": docker_version},
            "docker_compose_v2": {"ok": compose_ok, "version": compose_version},
        },
        "onboard_command": onboard_command,
        "next_action": next_action,
    }


def main() -> int:
    report = build_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["runtime_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
