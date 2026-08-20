#!/usr/bin/env bash
# deploy-to-dev.sh — dev deploy: pull remote main and update the local web server.
# The web.sh loop restarts the server, so this only refreshes the code.
set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

git fetch origin main
git pull --ff-only origin main
echo "deployed (dev): $(git rev-parse --short HEAD)"
