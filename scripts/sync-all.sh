#!/bin/sh
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
CORE_PORT=${CORE_PORT:-8080}

cd "$REPO_DIR" || exit 1

if ! python3 "$SCRIPT_DIR/configure.py" --status --require-sp-api >/dev/null; then
  echo "Amazon SP-API configuration is incomplete or .env is not private (expected 0600)." >&2
  echo "Run ./scripts/onboard.sh first." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop must be installed and running." >&2
  exit 1
fi

docker compose up --build -d --force-recreate core || exit 1

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

failures=""

run_step() {
  label=$1
  shift
  echo
  echo "==> $label"
  if ! docker compose exec -T core amazon-data-core "$@"; then
    failures="$failures $label"
  fi
}

run_step "SP-API authorization" amazon-auth --verify
if [ -z "$failures" ]; then
  run_step "orders" sync-orders
  run_step "FBA inventory" sync-inventory
  run_step "settlements" sync-settlements
else
  echo "Seller datasets were not requested because SP-API authorization failed." >&2
fi

if python3 "$SCRIPT_DIR/configure.py" --ads-configured >/dev/null 2>&1; then
  failures_before_ads=$failures
  run_step "Ads authorization" amazon-ads-auth --verify
  if [ "$failures" = "$failures_before_ads" ]; then
    run_step "Ads campaigns" sync-ads-campaigns
    run_step "Ads search terms" sync-ads-search-terms
    run_step "Ads purchased products" sync-ads-purchased-products
  else
    echo "Ads datasets were not requested because Ads authorization failed." >&2
  fi
else
  echo
  echo "==> Amazon Ads skipped (optional credentials are not configured)"
fi

run_step "data quality checks" check
echo
echo "==> MCP contract"
if ! docker compose exec -T core python scripts/verify_mcp.py; then
  failures="$failures MCP contract"
fi

echo
docker compose exec -T core amazon-data-core status || failures="$failures status"

if [ -n "$failures" ]; then
  echo >&2
  echo "First sync finished with failures:$failures" >&2
  echo "Successful datasets remain available; rerun ./scripts/sync-all.sh after fixing the reported authorization or role." >&2
  exit 1
fi

echo
echo "First sync passed. Amazon Data Core is ready at http://localhost:$CORE_PORT"
