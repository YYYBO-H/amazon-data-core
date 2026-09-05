from __future__ import annotations

import importlib.util
import json
import stat
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "onboard_background_script", SCRIPTS / "onboard_background.py"
)
assert SPEC and SPEC.loader
onboard_background = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(onboard_background)


def test_state_file_is_private_and_contains_no_credentials(tmp_path: Path):
    state_file = tmp_path / "runtime" / "state.json"
    onboard_background.write_state(
        "awaiting_credentials",
        state_file=state_file,
        authorization_url="http://127.0.0.1:1234/token/",
    )

    state = json.loads(state_file.read_text())
    assert state["status"] == "awaiting_credentials"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700
    assert "secret" not in state and "refresh_token" not in state


def test_detached_child_survives_launcher_return(tmp_path: Path):
    marker = tmp_path / "child-finished"
    log_file = tmp_path / "runtime" / "child.log"
    command = [
        sys.executable,
        "-c",
        "import pathlib,time; time.sleep(0.25); pathlib.Path(r'%s').write_text('ok')"
        % marker,
    ]

    pid = onboard_background.spawn_detached(command, log_file)

    assert onboard_background.process_is_running(pid)
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.read_text() == "ok"
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
