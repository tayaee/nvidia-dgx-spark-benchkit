#!/bin/bash
# cmdc-cli.sh — routes to the default deepseek (Command Code -p mode).
# Usage: cmdc-cli.sh -p "<prompt>" [more args...]
set -euo pipefail
CMDC="$(command -v cmdc || command -v command-code || echo /home/user1/.local/bin/deepseek-cli.sh)"
if [[ "$CMDC" == *deepseek-cli.sh ]]; then
  exec "$CMDC" "$@"
fi
# cmdc 는 -p "<query>" 형식으로 프롬프트를 받는다. 인자를 그대로 전달.
exec "$CMDC" "$@"
