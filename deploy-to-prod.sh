#!/usr/bin/env bash
# deploy-to-prod.sh — prod 배포: 원격 main 을 당겨와 반영.
# (dev 와 동일한 구조 — prod 환경 차이는 추후 확장)
set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

git fetch origin main
git pull --ff-only origin main
echo "deployed (prod): $(git rev-parse --short HEAD)"
