#!/usr/bin/env bash
# Free-text operator selection follows Magnet's controlling-terminal owner:
# https://github.com/numpde/magnet-deploy/blob/0619d9ffd92a4326758b442c532aa09496161098/scripts/operator-input.sh#L19-L66
# Callers own creation and mounts. This resolves data only, without a default.

select_wheelhouse() {
    local tty_fd directory
    if [[ -v WHEELHOUSE_INPUT_ORIGIN && $WHEELHOUSE_INPUT_ORIGIN == 'command line' ]]; then
        wheelhouse_directory=
        if [[ -v WHEELHOUSE_INPUT ]]; then wheelhouse_directory=$WHEELHOUSE_INPUT; fi
    else
        if ! { exec {tty_fd}<>/dev/tty; } 2>/dev/null; then
            printf '%s\n' 'WHEELHOUSE must be set on the make command line when no interactive terminal is available: WHEELHOUSE=/absolute/cache/root' >&2
            return 2
        fi
        printf 'WHEELHOUSE (absolute or relative path): ' >&"$tty_fd"
        if ! IFS= read -r wheelhouse_directory <&"$tty_fd"; then
            exec {tty_fd}>&-
            printf '%s\n' 'WHEELHOUSE input ended before a path was read.' >&2
            return 2
        fi
        exec {tty_fd}>&-
    fi
    if [[ -z $wheelhouse_directory ]]; then
        printf '%s\n' 'WHEELHOUSE cannot be empty; select a cache path. There is no default.' >&2
        return 2
    fi
    # Resolve relative input where Make runs (after -C), before any caller cd:
    # https://www.gnu.org/software/make/manual/html_node/Recursion.html
    # Docker receives the resolved target, including operator-selected symlinks.
    # Missing cache directories are allowed; acquisition owns their creation.
    # https://www.gnu.org/software/coreutils/manual/html_node/realpath-invocation.html
    # NUL output preserves a trailing newline introduced by a symlink target,
    # so the mount-delimiter check below can reject it without changing its name.
    if ! IFS= read -r -d '' wheelhouse_directory < <(realpath -zm -- "$wheelhouse_directory"); then
        printf '%s\n' 'Cannot resolve WHEELHOUSE to an absolute cache path.' >&2
        return 2
    fi
    if [[ $wheelhouse_directory == / || $wheelhouse_directory == *','* \
        || $wheelhouse_directory == *$'\n'* || $wheelhouse_directory == *$'\r'* ]]; then
        printf 'Cannot use resolved WHEELHOUSE=%q: select a cache directory below the filesystem root without commas or line breaks.\n' "$wheelhouse_directory" >&2
        return 2
    fi
    directory=$wheelhouse_directory
    while [[ -n $directory ]]; do
        if [[ -L $directory || ( -e $directory && ! -d $directory ) ]]; then
            printf 'Cannot use WHEELHOUSE: expected a directory without a symlink at %q.\n' "$directory" >&2
            return 2
        fi
        directory=${directory%/*}
    done
}

require_wheelhouse_pool() {
    if [[ ! -d $wheelhouse_directory ]]; then
        printf 'Cannot use WHEELHOUSE=%q: the shared pool directory is missing.\n' "$wheelhouse_directory" >&2
        return 2
    fi
}
