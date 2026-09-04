#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
CORE_PORT=${CORE_PORT:-8080}

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for the current local runtime." >&2
  echo "Install Docker Desktop, start it, then run this installer again." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but its engine is not running." >&2
  exit 1
fi

cd "$REPO_DIR"
docker compose up --build -d

attempt=0
until curl --silent --fail "http://localhost:$CORE_PORT/health" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Amazon Data Core did not become healthy in 120 seconds." >&2
    docker compose ps >&2
    exit 1
  fi
  sleep 2
done

docker compose exec -T core amazon-data-core doctor
docker compose exec -T core python scripts/verify_mcp.py
SKILL_DIR="$HOME/.agents/skills/amazon-data-core"
mkdir -p "$SKILL_DIR"
cp "$REPO_DIR/skills/amazon-data-core/SKILL.md" "$SKILL_DIR/SKILL.md"
docker compose exec -T core amazon-data-core status

echo
echo "Amazon Data Core is ready: http://localhost:$CORE_PORT"
echo "Repository: $REPO_DIR"
echo "Skill installed: $SKILL_DIR"
echo "MCP configuration:"
python3 "$SCRIPT_DIR/configure.py" --mcp-config
echo
echo "To connect a real Amazon seller account, run: ./scripts/onboard.sh"
