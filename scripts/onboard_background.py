#!/usr/bin/env python3
"""Launch browser authorization and first sync independently of an Agent shell."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import configure
import configure_web


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".amazon-data-core"
STATE_FILE = STATE_DIR / "onboard-state.json"
LOG_FILE = STATE_DIR / "onboard.log"
ACTIVE_STATES = {"starting", "awaiting_credentials", "syncing"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prepare_runtime_dir(state_dir: Path = STATE_DIR) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)


def read_state(state_file: Path = STATE_FILE) -> dict[str, object]:
    if not state_file.exists():
        return {"status": "not_started"}
    try:
        result = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown"}
    return result if isinstance(result, dict) else {"status": "unknown"}


def write_state(
    status_name: str,
    *,
    state_file: Path = STATE_FILE,
    **details: object,
) -> None:
    _prepare_runtime_dir(state_file.parent)
    previous = read_state(state_file)
    started_at = _now() if status_name == "starting" else previous.get("started_at", _now())
    payload = {
        "status": status_name,
        "pid": os.getpid(),
        "started_at": started_at,
        "updated_at": _now(),
        **details,
    }
    fd, temporary_name = tempfile.mkstemp(prefix="onboard-state.", dir=state_file.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, state_file)
        state_file.chmod(0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def process_is_running(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def spawn_detached(command: list[str], log_file: Path = LOG_FILE) -> int:
    _prepare_runtime_dir(log_file.parent)
    log_handle = log_file.open("ab", buffering=0)
    log_file.chmod(0o600)
    try:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    return process.pid


def launch() -> int:
    current = read_state()
    if current.get("status") in ACTIVE_STATES and process_is_running(current.get("pid")):
        url = current.get("authorization_url")
        print(f"Background onboarding is already running (status: {current['status']}).")
        if isinstance(url, str):
            print(f"Private local authorization page: {url}")
            webbrowser.open(url)
        return 0

    _prepare_runtime_dir()
    with LOG_FILE.open("ab") as handle:
        handle.write(f"\n== onboarding launched {_now()} ==\n".encode())
    LOG_FILE.chmod(0o600)
    pid = spawn_detached([sys.executable, str(Path(__file__).resolve()), "worker"])

    deadline = time.monotonic() + 15
    state: dict[str, object] = {"status": "starting", "pid": pid}
    while time.monotonic() < deadline:
        state = read_state()
        if state.get("pid") == pid:
            if state.get("status") != "starting" or not process_is_running(pid):
                break
        elif not process_is_running(pid):
            break
        time.sleep(0.1)

    status_name = state.get("status")
    url = state.get("authorization_url")
    if status_name in ACTIVE_STATES and process_is_running(state.get("pid")):
        print("Background onboarding launched independently of the Agent shell.")
        if isinstance(url, str):
            print(f"Private local authorization page: {url}")
        print("After submission, first synchronization will continue automatically.")
        print("Check progress: python3 scripts/onboard_background.py status")
        return 0

    print(f"Background onboarding failed to start. See {LOG_FILE}", file=sys.stderr)
    return 2


def worker() -> int:
    write_state("starting")

    def ready(url: str) -> None:
        write_state("awaiting_credentials", authorization_url=url)

    result = configure_web.run_server(
        configure.DEFAULT_ENV_PATH,
        "127.0.0.1",
        0,
        open_browser=True,
        timeout=1200,
        ready_callback=ready,
    )
    if result != 0:
        final = "cancelled" if result == 130 else "authorization_failed"
        write_state(final, exit_code=result)
        return result

    write_state("syncing")
    result = subprocess.run(
        [str(PROJECT_ROOT / "scripts" / "sync-all.sh")],
        cwd=PROJECT_ROOT,
        check=False,
    )
    final = "complete" if result.returncode == 0 else "sync_failed"
    write_state(final, exit_code=result.returncode)
    return result.returncode


def show_status() -> int:
    state = read_state()
    status_name = state.get("status", "unknown")
    running = process_is_running(state.get("pid")) if status_name in ACTIVE_STATES else False
    result = {
        "status": status_name,
        "running": running,
        "updated_at": state.get("updated_at"),
        "authorization_url": state.get("authorization_url"),
        "log": str(LOG_FILE),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if status_name == "complete":
        return 0
    if status_name in ACTIVE_STATES and running:
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("launch", "worker", "status"), default="launch")
    args = parser.parse_args()
    if args.command == "worker":
        return worker()
    if args.command == "status":
        return show_status()
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
