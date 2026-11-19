#!/usr/bin/env bash
# Build the selected builder's full cache prune arguments

docker_prune_all_cache_arguments() {
    local -n arguments=$1
    local builder=$2 reserved_space=$3 max_used_space=$4 min_free_space=$5
    arguments=(buildx prune --builder "$builder" --all --force --reserved-space "$reserved_space"
        --max-used-space "$max_used_space" --min-free-space "$min_free_space")
}
