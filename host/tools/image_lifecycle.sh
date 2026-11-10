#!/usr/bin/env bash
# Maintained main-checkout borrowers hold a shared directory lock until cleanup.
image_lifecycle_lock() {
    local mode=$1
    if [[ $mode == exclusive && ! -d $root/.git ]]; then
        printf '%s\n' 'Image retirement requires the main checkout.' >&2; return 2
    fi
    exec {image_lifecycle_fd}< "$root"
    if [[ $mode == shared ]]; then
        flock --shared --wait 60 "$image_lifecycle_fd" || return 75
    elif [[ $mode == exclusive ]]; then
        flock --exclusive --nonblock "$image_lifecycle_fd" || return 75
    else
        return 2
    fi
}

image_lifecycle_unlock() {
    exec {image_lifecycle_fd}<&-
    unset image_lifecycle_fd
}
