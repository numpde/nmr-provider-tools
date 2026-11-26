#!/usr/bin/env bash
# One declared Linux/amd64 base serves acquisition and the offline checker build.
readonly base_image='docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487'
# Linux/amd64 image configuration selected by the registry manifest above.
readonly base_image_config='sha256:37ccb536022bb36c4570856b55ebfa85bf07827e3c2fa3eba37f0dac62eebc05'

# Dockerfile remains the authority for the resolver binary's immutable origin.
checker_resolver_image() {
    local recipe=$1 image
    image=$(sed -n 's|^COPY --from=\([^[:space:]]*\) /uv /usr/local/bin/uv$|\1|p' "$recipe") || return "$?"
    if [[ ! $image =~ ^ghcr\.io/astral-sh/uv:[0-9.]+@sha256:[0-9a-f]{64}$ ]]; then
        printf '%s\n' 'Cannot preload the checker resolver: its Dockerfile must declare one pinned uv image copy.' >&2
        return 2
    fi
    printf '%s\n' "$image"
}
