#!/bin/bash
set -eu
exec 2>>"$ENGINE_STATE/engine-error"
if [[ ${0##*/} == stat ]]; then
  if [[ $# == 4 && $1 == -c && $2 == '%s' && $3 == -- && $4 == */gateway/stdout ]]; then
    printf '%s\n' "${ACQUISITION_STDOUT_STAT_FAULT:?}" > "$ENGINE_STATE/stat-fault-reached"
    case "$ACQUISITION_STDOUT_STAT_FAULT" in
      failure) exit 41 ;;
      nonnumeric) printf 'unavailable\n'; exit 0 ;;
      *) exit 97 ;;
    esac
  fi
  exec /usr/bin/stat "$@"
fi
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
    if [[ -v CAP_CREATE_STDOUT_ROLE && $CAP_CREATE_STDOUT_ROLE == "$role" ]]; then
      printf '%4096s' ''
      exit 0
    fi
    if [[ -v FAIL_CREATE_ROLE && $FAIL_CREATE_ROLE == "$role" ]]; then
      case "${FAILED_CREATE_OUTPUT:?}" in
        empty) [[ -v HOLD_FAILED_CREATE && $HOLD_FAILED_CREATE == 1 ]] || exit 97 ;;
        exact) printf '%s\n' "$identifier" ;;
        disagree) printf '%064d\n' 9 ;;
        nul) printf '%s\0' "$identifier" ;;
        *) exit 97 ;;
      esac
      if [[ -v HOLD_FAILED_CREATE ]]; then
        case "$HOLD_FAILED_CREATE" in
          1)
            printf '%s\n' "$$" > "$ENGINE_STATE/create-held"
            # Bound a broken cancellation probe without changing its live PID.
            exec sleep 20
            ;;
          *) exit 97 ;;
        esac
      fi
      exit 23
    fi
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
      '{{.State.Running}} {{.State.ExitCode}}')
        if [[ -v WORKER_STATUS ]]; then
          printf 'false %s\n' "$WORKER_STATUS"
        else
          printf 'false 0\n'
        fi ;;
      *) exit 97 ;;
    esac
    ;;
  start)
    identifier=${!#}
    : > "$ENGINE_STATE/start-$identifier"
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
    if [[ -v STREAM_WITNESS && $STREAM_WITNESS == 1 ]]; then
      printf 'streamed\n'
      while [[ ! -f "$ENGINE_STATE/release-worker" ]]; do sleep 0.01; done
    fi
    if [[ -n ${WORKER_STATUS+x} ]]; then exit "$WORKER_STATUS"; fi
    while :; do sleep 1; done
    ;;
  rm)
    identifier=${!#}
    : > "$ENGINE_STATE/rm-$identifier"
    role=$(<"$ENGINE_STATE/role-$identifier")
    printf '%s\n' "$role" >> "$ENGINE_STATE/removal-attempts"
    if [[ -v FAIL_REMOVE && $role == "$FAIL_REMOVE" ]]; then exit 1; fi
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
