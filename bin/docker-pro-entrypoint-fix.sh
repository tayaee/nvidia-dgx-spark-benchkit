#!/usr/bin/env bash
# docker-pro-entrypoint-fix.sh — mini-swe-agent docker wrapper
#
# SWE-bench Pro images (jefzda/sweap-images) ship with ENTRYPOINT=/bin/bash, so
# `docker run ... <image> sleep 2h` invoked by mini-swe-agent fails. This wrapper
# injects --entrypoint /usr/bin/sleep into `run` invocations and converts the
# trailing `sleep <timeout>` to seconds so the container stays alive.
#
# Use: MSWEA_DOCKER_EXECUTABLE=/path/to/this/script
set -Eeuo pipefail

# sleep time conversion: 2h -> 7200, 30m -> 1800, 45s -> 45 (default 2h=7200)
to_seconds(){
  local v="$1"
  case "$v" in
    *h) echo $(( ${v%h} * 3600 ));;
    *m) echo $(( ${v%m} * 60 ));;
    *s) echo ${v%s};;
    *)  echo "$v";;
  esac
}

args=("$@")
cmd="${1:-}"

if [[ "$cmd" == "run" ]]; then
  out=("${args[@]:1}")
  n=${#out[@]}
  # trailing `sleep <timeout>` -> `--entrypoint /usr/bin/sleep <image> <seconds>`
  if (( n >= 2 )) && [[ "${out[$((n-2))]}" == "sleep" ]]; then
    timeout_sec="$(to_seconds "${out[$((n-1))]}")"
    # image index: first non-flag arg (skip option values)
    img_idx=-1
    skip_next=0
    for ((i=0; i<n-2; i++)); do
      a="${out[$i]}"
      if (( skip_next )); then skip_next=0; continue; fi
      case "$a" in
        --) img_idx=$((i+1)); break;;
        --name|--workdir|-w|--user|-u|--network|--env|-e|--label|-l|--volume|-v|--mount|--platform|--cpus|--memory|-m|--entrypoint) skip_next=1;;
        -*) ;;
        *) img_idx=$i; break;;
      esac
    done
    if (( img_idx >= 0 )); then
      # out: [opts..., <image>, sleep, <timeout>]
      #      -> [opts..., --entrypoint /usr/bin/sleep, <image>, <seconds>]
      out=(
        "${out[@]:0:$((n-3))}"                       # opts (through before image)
        "--entrypoint" "/usr/bin/sleep"              # entrypoint override
        "${out[@]:$((n-3)):1}"                       # image
        "$timeout_sec"                               # timeout in seconds
      )
    fi
  fi
  exec docker run "${out[@]}"
fi

exec docker "$@"
