#!/usr/bin/env bash
# docker-pro-entrypoint-fix.sh — mini-swe-agent용 docker 래퍼
#
# SWE-bench Pro 이미지(jefzda/sweap-images)는 ENTRYPOINT=/bin/bash 라서
# mini-swe-agent 가 실행하는 `docker run ... <image> sleep 2h` 가 실패한다.
# 이 래퍼는 run 명령에서 --entrypoint /usr/bin/sleep 를 삽입하고
# trailing `sleep <timeout>` 을 초 단위로 변환해 컨테이너가 정상 유지되게 한다.
#
# 사용: MSWEA_DOCKER_EXECUTABLE=/path/to/this/script
set -Eeuo pipefail

# sleep 시간 변환: 2h -> 7200, 30m -> 1800, 45s -> 45 (기본 2h=7200)
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
    # 이미지 인덱스: 첫 번째 non-flag 인자 (옵션 값은 건너뜀)
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
      # out: [옵션들..., <image>, sleep, <timeout>]
      #      -> [옵션들..., --entrypoint /usr/bin/sleep, <image>, <seconds>]
      out=(
        "${out[@]:0:$((n-3))}"                       # 옵션들 (image 앞까지)
        "--entrypoint" "/usr/bin/sleep"              # entrypoint 오버라이드
        "${out[@]:$((n-3)):1}"                       # 이미지
        "$timeout_sec"                               # 초 단위 timeout
      )
    fi
  fi
  exec docker run "${out[@]}"
fi

exec docker "$@"
