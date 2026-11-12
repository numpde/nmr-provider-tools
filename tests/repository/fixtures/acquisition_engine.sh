#!/bin/bash
set -eu
if [[ ${0##*/} == python3 ]]; then
  [[ $# == 14 && $1 == -I && $2 == -S && $3 == -B ]] || exit 97
  [[ $4 == "$ACQUISITION_SOURCE_ROOT/acquisition_tool/identity.py" ]] || exit 97
  [[ $5 == --endpoint && $7 == --image && $9 == --host-uid ]] || exit 97
  [[ ${11} == --host-gid && ${13} == --retired-receipt ]] || exit 97
  [[ -n $6 && -n $8 && -n ${10} && -n ${12} ]] || exit 97
  printf '%s\n' "$6" "$8" "${10}" "${12}" >> "$ENGINE_STATE/identity-arguments"
  (set -C; printf 'retired\n' > "${14}")
  printf '17 19\n'
  exit 0
fi
if [[ ${0##*/} == rm ]]; then
  if [[ $# == 3 && $1 == -rf && $2 == -- && $3 == /tmp/nmrnet-docker-client.* ]]; then
    printf '%s\n' "$3" > "$ENGINE_STATE/client-cleanup-path"
    case "${CLIENT_CLEANUP_FAILURE:?}" in
      retain|capture-directory) printf 'fixture cleanup could not remove the selected directory: Permission denied\n' >&2 ;;
      delete) /bin/rm "$@"; printf 'fixture cleanup failed after deleting the selected directory\n' >&2 ;;
      binary-cause) printf 'cleanup\0cause\r\n\n' >&2 ;;
      oversized-cause)
        printf 'cleanup-cause-canary\n' >&2
        printf '%8192s' '' >&2
        printf 'cleanup-output-tail\n' >&2
        ;;
      *) exit 97 ;;
    esac
    exit 41
  fi
  exec /bin/rm "$@"
fi
if [[ ! -v READINESS_ROLE ]]; then exec 2>>"$ENGINE_STATE/engine-error"; fi
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
  compose)
    [[ $1 == --project-name ]] || exit 97
    project=$2
    shift 2
    while [[ $1 != run && $1 != down && $1 != up ]]; do shift; done
    action=$1
    shift
    printf '%s %s\n' "$project" "$action" >> "$ENGINE_STATE/compose-calls"
    case "$action" in
      run)
        while [[ $1 != --name ]]; do shift; done
        worker=$2
        printf '%s\n' "$project" > "$ENGINE_STATE/compose-project"
        printf '%s\n' "$worker" > "$ENGINE_STATE/compose-worker"
        printf 'package-output\n'
        if [[ ${HOLD_COMPOSE_WORKER:-0} == 1 ]]; then
          touch "$ENGINE_STATE/compose-held"
          exec sleep 20
        fi
        exit "${ACQUISITION_COMPOSE_EXIT:-0}"
        ;;
      down)
        if [[ ${FAIL_COMPOSE_DOWN:-0} == 1 ]]; then exit 41; fi
        /bin/rm -f "$ENGINE_STATE/compose-project" "$ENGINE_STATE/compose-worker"
        ;;
      *) exit 97 ;;
    esac
    ;;
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
    if [[ -f $ENGINE_STATE/compose-worker && ${!#} == "$(<"$ENGINE_STATE/compose-worker")" ]]; then
      printf 'false %s\n' "${ACQUISITION_COMPOSE_EXIT:-0}"
      exit 0
    fi
    identifier=${!#}
    role=$(<"$ENGINE_STATE/role-$identifier")
    case "$2" in
      '{{.State.Running}}')
        printf '%s\n' "$role" >> "$ENGINE_STATE/readiness-inspections"
        case "${READINESS_SCENARIO:?}" in
          stopped) printf 'false\n' ;;
          inspect-failed) printf 'private inspect cause\n' >&2; exit 37 ;;
          malformed) printf 'private malformed state\n' ;;
          exhausted) printf 'true\n' ;;
          *) exit 97 ;;
        esac ;;
      '{{.HostConfig.NetworkMode}}')
        if [[ $role == worker-* && -f "$ENGINE_STATE/relay-id" ]]; then
          printf 'container:%s\n' "$(<"$ENGINE_STATE/relay-id")"
        elif [[ $role == gateway ]]; then printf 'host\n'; else printf 'none\n'; fi ;;
      '{{.HostConfig.NetworkMode}} {{.State.Running}}')
        if [[ -v INVALID_RELAY_STATE && $role == relay ]]; then printf 'none false\n'; else printf 'none true\n'; fi ;;
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
    if [[ -v READINESS_ROLE && $role == "$READINESS_ROLE" ]]; then exit 0; fi
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
    if [[ -v CORRUPT_COMPLETED_WORKER_CID ]]; then
      printf '%s\n' "$CORRUPT_COMPLETED_WORKER_CID" > "$scratch/$role/cid"
    fi
    if [[ -v STREAM_WITNESS && $STREAM_WITNESS == 1 ]]; then
      printf 'streamed\n'
      while [[ ! -f "$ENGINE_STATE/release-worker" ]]; do sleep 0.01; done
    fi
    if [[ -n ${WORKER_STATUS+x} ]]; then exit "$WORKER_STATUS"; fi
    while :; do sleep 1; done
    ;;
  logs)
    identifier=${!#}
    role=$(<"$ENGINE_STATE/role-$identifier")
    printf '%s\n' "$role" >> "$ENGINE_STATE/readiness-logs"
    [[ ! -f "$ENGINE_STATE/rm-$identifier" ]] || exit 98
    printf 'private container stdout\n'
    printf 'private container stderr\n' >&2
    exit "${READINESS_LOG_STATUS:?}"
    ;;
  rm)
    identifier=${!#}
    : > "$ENGINE_STATE/rm-$identifier"
    role=$(<"$ENGINE_STATE/role-$identifier")
    printf '%s\n' "$role" >> "$ENGINE_STATE/removal-attempts"
    if [[ -v FAIL_REMOVE_AFTER_EFFECT && $role == "$FAIL_REMOVE_AFTER_EFFECT" ]]; then
      rm -f "$ENGINE_STATE/live-$identifier"
      exit 23
    fi
    if [[ -v FAIL_REMOVE && $role == "$FAIL_REMOVE" ]]; then exit 1; fi
    rm -f "$ENGINE_STATE/live-$identifier"
    : > "$ENGINE_STATE/removed-worker"
    ;;
  ps)
    if [[ $* == *label=com.docker.compose.project=* ]]; then
      [[ ! -f $ENGINE_STATE/compose-worker ]] || printf '%064d\n' 1
      exit 0
    fi
    printf '%s\n' "$@" >> "$ENGINE_STATE/absence-probes"
    for arg in "$@"; do
      case "$arg" in id=*) identifier=${arg#id=} ;; esac
    done
    [[ ! -f "$ENGINE_STATE/live-$identifier" ]] || printf '%s\n' "$identifier"
    ;;
  *) exit 97 ;;
esac
