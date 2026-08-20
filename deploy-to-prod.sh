#!/usr/bin/env bash
# deploy-to-prod.sh — prod deploy: pull remote main and update.
# (Same structure as dev — prod-specific differences can be added later.)
set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

git fetch origin main
git pull --ff-only origin main
echo "deployed (prod): $(git rev-parse --short HEAD)"
