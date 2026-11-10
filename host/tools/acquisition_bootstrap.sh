#!/usr/bin/env bash
# Refresh checked-in tool source from an exact reviewed release; never at runtime.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
action=${1:?Select check or refresh}
case "$action" in
    check)
        exec python3 -I -S -B "$root/host/tools/acquisition_bootstrap.py" \
            "$root/bootstrap/acquisition" "$root/requirements/acquisition-owner-release.json"
        ;;
    refresh)
        [[ -v ARTIFACTS_INPUT_ORIGIN && $ARTIFACTS_INPUT_ORIGIN == 'command line' && -v ARTIFACTS_INPUT && -n $ARTIFACTS_INPUT ]] || {
            printf '%s\n' 'Supply ARTIFACTS as the exact release artifact directory.' >&2; exit 2;
        }
        shopt -s nullglob
        records=("$ARTIFACTS_INPUT"/provider_core-*-release.json)
        archives=("$ARTIFACTS_INPUT"/acquisition-owner-*.tar.gz)
        [[ ${#records[@]} == 1 && ${#archives[@]} == 1 ]] || {
            printf '%s\n' 'Acquisition bootstrap refresh requires one issuer record and one source archive.' >&2; exit 2;
        }
        exec python3 -I -S -B "$root/host/tools/acquisition_bootstrap.py" \
            "$root/bootstrap/acquisition" "${records[0]}" --archive "${archives[0]}" \
            --record-output "$root/requirements/acquisition-owner-release.json"
        ;;
    *) printf '%s\n' 'Select check or refresh for the acquisition bootstrap.' >&2; exit 2 ;;
esac
