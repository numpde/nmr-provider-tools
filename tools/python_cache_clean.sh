#!/usr/bin/env bash
# Opt-in removal of generated first-party Python bytecode; repository checks
# remain read-only and continue to reject caches left in the checkout.
set -euo pipefail

clean_first_party_bytecode() {
    if [[ $# != 1 ]]; then
        printf '%s\n' 'Supply one first-party checkout root.' >&2
        return 2
    fi
    local root=$1 name directory
    local -a sources=()
    if [[ ! -d $root || -L $root ]]; then
        printf '%s\n' 'Cannot clean Python bytecode: checkout root is not a real directory.' >&2
        return 2
    fi
    # Check all source roots before removing anything. find defaults to -P;
    # symbolic links within these trees are never traversed.
    for name in tools tests containers src packages; do
        directory=$root/$name
        if [[ -L $directory || -e $directory && ! -d $directory ]]; then
            printf 'Cannot clean Python bytecode: %s is a symlink or not a directory.\n' "$directory" >&2
            return 2
        fi
        if [[ -d $directory ]]; then
            sources+=("$directory")
        fi
    done
    for directory in "${sources[@]}"; do
        find -P "$directory" -type f \\( -name '*.pyc' -o -name '*.pyo' \\) -print -delete
    done
    # Retain any cache directory containing non-bytecode material, so neither
    # unusual source files nor links are discarded under the guise of cleanup.
    for directory in "${sources[@]}"; do
        find -P "$directory" -depth -type d -name '__pycache__' -empty -print -delete
    done
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    if [[ $# != 0 || $(id -u) == 0 ]]; then
        printf '%s\n' 'Run this opt-in cleanup as the checkout owner, without arguments.' >&2
        exit 2
    fi
    script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
    checkout=$(git -C "$script_directory" rev-parse --show-toplevel)
    if [[ $script_directory != "$checkout/tools" ]]; then
        printf '%s\n' 'Cannot clean Python bytecode: script is not under the selected Git checkout tools directory.' >&2
        exit 2
    fi
    clean_first_party_bytecode "$checkout"
fi
