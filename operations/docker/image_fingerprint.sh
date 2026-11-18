#!/usr/bin/env bash
image_fingerprint() {
    local path mode target=$1
    shift
    for path in "$@"; do
        if [[ ! -f $root/$path || -L $root/$path ]]; then
            printf 'Cannot identify image inputs: expected a regular file at %q.\n' "$root/$path" >&2
            return 2
        fi
    done
    (
        cd -- "$root" || exit "$?"
        printf '%s\0' linux/amd64 "$target" || exit "$?"
        for path in "$@"; do
            mode=$(stat -c %a -- "$path") || exit "$?"
            printf '%s\0%s\0' "$path" "$mode" || exit "$?"
            sha256sum -- "$path" || exit "$?"
        done
    ) | sha256sum | cut -d ' ' -f 1
}
