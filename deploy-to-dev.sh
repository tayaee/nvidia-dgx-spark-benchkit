#!/usr/bin/env bash
# deploy-to-dev.sh — dev 배포: 원격 main 을 당겨와 로컬 웹 서버에 반영.
# web.sh 루프가 서버를 재시작하므로, 여기서는 코드만 갱신한다.
set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

git fetch origin main
git pull --ff-only origin main
echo "deployed (dev): $(git rev-parse --short HEAD)"
