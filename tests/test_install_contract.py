from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_secret_files_are_excluded_from_git_and_docker_context():
    gitignore = (ROOT / ".gitignore").read_text()
    dockerignore = (ROOT / ".dockerignore").read_text()

    assert ".env" in gitignore.splitlines()
    assert ".env" in dockerignore.splitlines()
    assert ".env.*" in dockerignore.splitlines()


def test_onboarding_runs_install_before_collecting_credentials():
    script = (ROOT / "scripts" / "onboard.sh").read_text()

    assert script.index('"$SCRIPT_DIR/install.sh"') < script.index(
        'python3 "$SCRIPT_DIR/configure.py"'
    )
    assert '"$SCRIPT_DIR/sync-all.sh"' in script
