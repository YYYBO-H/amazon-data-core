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
        'python3 "$SCRIPT_DIR/onboard_background.py" launch'
    )
    assert '--terminal-config' in script
    assert '"$SCRIPT_DIR/sync-all.sh"' in script
    assert ".amazon-data-core/" in (ROOT / ".gitignore").read_text().splitlines()


def test_repository_is_self_describing_for_local_agents():
    instructions = (ROOT / "AGENTS.md").read_text()

    assert "AGENT_INSTALL.md" in instructions
    assert "scripts/preflight.py" in instructions
    assert "Never request secrets in chat" in instructions


def test_local_services_are_not_published_to_the_lan_by_default():
    compose = (ROOT / "compose.yaml").read_text()

    assert '127.0.0.1:${CORE_PORT:-8080}:8080' in compose
    assert '127.0.0.1:${POSTGRES_PORT:-55432}:5432' in compose
