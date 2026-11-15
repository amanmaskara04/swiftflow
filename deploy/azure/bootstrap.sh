#!/usr/bin/env bash
# Sets up or updates SwiftFlow on a fresh Ubuntu VM. Safe to re-run.
#   sudo bash deploy/azure/bootstrap.sh <public-dns-name>
set -euo pipefail

FQDN="${1:?usage: bootstrap.sh <public-dns-name>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$ROOT/deploy/compose/.env"
COMPOSE=(docker compose -f "$ROOT/deploy/compose/docker-compose.yml" -f "$ROOT/deploy/azure/docker-compose.azure.yml" --env-file "$ENV_FILE")

# Swap: image builds on a 4 GB VM need the headroom.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

# Settings: created once with random secrets. Postgres stores the passwords on first start,
# so an existing file is kept as it is.
if [ ! -f "$ENV_FILE" ]; then
  cp "$ROOT/deploy/compose/.env.example" "$ENV_FILE"
  for key in POSTGRES_PASSWORD ORDER_DB_PASSWORD INVENTORY_DB_PASSWORD NOTIFICATION_DB_PASSWORD PROVIDER_API_KEY GRAFANA_ADMIN_PASSWORD; do
    sed -i "s|^$key=.*|$key=$(openssl rand -hex 24)|" "$ENV_FILE"
  done
  sed -i "s|^CORS_ORIGINS=.*|CORS_ORIGINS=https://$FQDN|" "$ENV_FILE"
  echo "SITE_ADDRESS=$FQDN" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

"${COMPOSE[@]}" up -d --build --wait --wait-timeout 600

# Demo tenants, clients, warehouses and stock (re-running changes nothing that exists).
docker run --rm --network host -v "$ROOT:/src:ro" -w /src python:3.12-slim \
  sh -c "pip install -q --no-cache-dir -r requirements-dev.lock.txt && python scripts/seed.py"

docker image prune -f >/dev/null
echo "SwiftFlow is up at https://$FQDN"
