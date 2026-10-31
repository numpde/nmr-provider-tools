#!/usr/bin/env bash
# Select one local engine for the whole Make operation and its child processes.
set -euo pipefail
select_docker_context() {
    local name endpoint listing tty_fd answer index origin
    local -a choices=() endpoints=()
    if [[ -v DOCKER_CONTEXT_INPUT_ORIGIN ]]; then origin=$DOCKER_CONTEXT_INPUT_ORIGIN; else origin=undefined; fi
    if [[ -v NMR_SELECTED_DOCKER_ENDPOINT ]]; then
        endpoint=$NMR_SELECTED_DOCKER_ENDPOINT
        [[ $endpoint == unix:///* && ! $endpoint =~ [[:cntrl:]] ]] || { printf '%s\n' 'Selected Docker endpoint is not a local Unix socket.' >&2; return 2; }
        return 0
    fi
    if [[ $origin == 'command line' ]]; then
        name=
        if [[ -v DOCKER_CONTEXT_INPUT ]]; then name=$DOCKER_CONTEXT_INPUT; fi
        [[ -n $name ]] || { printf '%s\n' 'DOCKER_CONTEXT cannot be empty; select a local Docker context.' >&2; return 2; }
    else
        if ! { exec {tty_fd}<>/dev/tty; } 2>/dev/null; then
            printf '%s\n' 'Select a local Docker context with DOCKER_CONTEXT=<name> on the make command line; no interactive terminal is available.' >&2
            return 2
        fi
        if ! listing=$(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker context ls --format '{{.Name}}'); then
            exec {tty_fd}>&-
            printf '%s\n' 'Cannot list Docker contexts; inspect Docker client configuration.' >&2
            return 2
        fi
        while IFS= read -r name; do
            [[ -n $name ]] || continue
            if endpoint=$(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker context inspect --format '{{.Endpoints.docker.Host}}' "$name" 2>/dev/null) \
                && [[ $endpoint == unix:///* && ! $endpoint =~ [[:cntrl:]] ]]; then
                choices+=("$name")
                endpoints+=("$endpoint")
            fi
        done <<< "$listing"
        if [[ ${#choices[@]} == 0 ]]; then
            exec {tty_fd}>&-
            printf '%s\n' 'No local Unix-socket Docker contexts are available.' >&2
            return 2
        fi
        printf 'Select a local Docker context (no default):\n' >&"$tty_fd"
        for index in "${!choices[@]}"; do printf '  %d) %s\n' "$((index + 1))" "${choices[index]}" >&"$tty_fd"; done
        printf 'Choice [1-%d]: ' "${#choices[@]}" >&"$tty_fd"
        if ! IFS= read -r answer <&"$tty_fd"; then exec {tty_fd}>&-; printf '%s\n' 'Docker context selection ended before a choice was read.' >&2; return 2; fi
        exec {tty_fd}>&-
        name=
        for index in "${!choices[@]}"; do
            if [[ $answer == "$((index + 1))" ]]; then name=${choices[index]}; endpoint=${endpoints[index]}; break; fi
        done
        [[ -n $name ]] || { printf '%s\n' 'Select one of the listed Docker contexts.' >&2; return 2; }
    fi
    if [[ $origin == 'command line' ]]; then
        if ! endpoint=$(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker context inspect --format '{{.Endpoints.docker.Host}}' "$name" 2>/dev/null); then
            printf 'Cannot inspect Docker context %q.\n' "$name" >&2; return 2
        fi
    fi
    if [[ $endpoint != unix:///* || $endpoint =~ [[:cntrl:]] ]]; then
        printf 'Docker context %q must use a local Unix socket.\n' "$name" >&2; return 2
    fi
    export NMR_SELECTED_DOCKER_ENDPOINT=$endpoint
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    [[ $# == 1 && $1 == --endpoint ]] || exit 2
    select_docker_context
    printf '%s\n' "$NMR_SELECTED_DOCKER_ENDPOINT"
fi
