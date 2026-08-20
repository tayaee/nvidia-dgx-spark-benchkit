#!/bin/bash
# cmdc-cli.sh — routes to the default deepseek (Command Code -p mode).
# Usage: cmdc-cli.sh -p "<prompt>" [more args...]
set -euo pipefail
CMDC="$(command -v cmdc || command -v command-code || echo /home/user1/.local/bin/deepseek-cli.sh)"
if [[ "$CMDC" == *deepseek-cli.sh ]]; then
  exec "$CMDC" "$@"
fi
# cmdc takes the prompt as -p "<query>"; pass args through unchanged.
exec "$CMDC" "$@"
