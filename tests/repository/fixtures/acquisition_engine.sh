#!/bin/bash
set -eu
exec 2>>"$ENGINE_STATE/engine-error"
[[ $# -ge 5 && "$1" == --config && "$3" == --host ]] || exit 97
# Metadata inherits a per-file cap; retain only the client cleanup handle.
printf '%s %s\n' "$1" "$2" > "$ENGINE_STATE/engine-client"
shift 4
operation=$1
shift
case "$operation" in
  create)
    printf '%s\n' "$@" >> "$ENGINE_STATE/create-arguments"
    while [[ "$1" != --cidfile ]]; do shift; done
    receipt=$2
    role=${receipt%/cid}
    role=${role##*/}
    scratch=${receipt%/$role/cid}
    count=0
    [[ ! -f "$ENGINE_STATE/counter" ]] || count=$(<"$ENGINE_STATE/counter")
    count=$((count+1))
    printf '%s' "$count" > "$ENGINE_STATE/counter"
    printf -v identifier '%064x' "$count"
    printf '%s\n' "$identifier" > "$receipt"
    : > "$ENGINE_STATE/live-$identifier"
    printf '%s\n' "$role" > "$ENGINE_STATE/role-$identifier"
    printf '%s\n' "$scratch" > "$ENGINE_STATE/scratch"
    printf '%s\n' "$identifier"
    ;;
  inspect)
    identifier=${!#}
    role=$(<"$ENGINE_STATE/role-$identifier")
    case "$2" in
      '{{.HostConfig.NetworkMode}}')
        if [[ $role == worker-* && -f "$ENGINE_STATE/relay-id" ]]; then
          printf 'container:%s\n' "$(<"$ENGINE_STATE/relay-id")"
        elif [[ $role == gateway ]]; then printf 'host\n'; else printf 'none\n'; fi ;;
      '{{.HostConfig.NetworkMode}} {{.State.Running}}') printf 'none true\n' ;;
      '{{.State.Running}} {{.State.ExitCode}}') printf 'false %s\n' "${WORKER_STATUS:-0}" ;;
      *) exit 97 ;;
    esac
    ;;
  start)
    identifier=${!#}
    role=$(<"$ENGINE_STATE/role-$identifier")
    scratch=$(<"$ENGINE_STATE/scratch")
    if [[ $role == gateway ]]; then
      python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.close()' "$scratch/gateway-socket/proxy.sock"
      printf '/gateway/proxy.sock\n' > "$scratch/gateway-ready/socket"
      exit 0
    elif [[ $role == relay ]]; then
      printf '%s\n' "$identifier" > "$ENGINE_STATE/relay-id"
      printf '12345\n' > "$scratch/relay-ready/port"
      exit 0
    fi
    : > "$ENGINE_STATE/started-worker"
    if [[ ${STREAM_WITNESS:-0} == 1 ]]; then
      printf 'streamed\n'
      while [[ ! -f "$ENGINE_STATE/release-worker" ]]; do sleep 0.01; done
    fi
    if [[ -n ${WORKER_STATUS+x} ]]; then exit "$WORKER_STATUS"; fi
    while :; do sleep 1; done
    ;;
  rm)
    identifier=${!#}
    role=$(<"$ENGINE_STATE/role-$identifier")
    printf '%s\n' "$role" >> "$ENGINE_STATE/removal-attempts"
    if [[ $role == ${FAIL_REMOVE:-} ]]; then exit 1; fi
    rm -f "$ENGINE_STATE/live-$identifier"
    : > "$ENGINE_STATE/removed-worker"
    ;;
  ps)
    for arg in "$@"; do
      case "$arg" in id=*) identifier=${arg#id=} ;; esac
    done
    [[ ! -f "$ENGINE_STATE/live-$identifier" ]] || printf '%s\n' "$identifier"
    ;;
  *) exit 97 ;;
esac
