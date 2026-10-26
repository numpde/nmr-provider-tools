#!/usr/bin/env bash
# Runtime tests are mounted. Only actual image construction inputs bind its tag.
checker_input_paths=(
    tools/base_image.sh tools/checker_dependencies.py tools/container_guard.py
    requirements/checker-dependencies.json containers/checker/Dockerfile
)

checker_fingerprint() {
    local path mode
    for path in "${checker_input_paths[@]}"; do
        if [[ ! -f $root/$path || -L $root/$path ]]; then
            printf 'Cannot identify checker inputs: expected a regular file at %q.\n' "$root/$path" >&2
            return 2
        fi
    done
    (
        cd -- "$root" || exit "$?"
        printf '%s\0' linux/amd64 || exit "$?"
        for path in "${checker_input_paths[@]}"; do
            mode=$(stat -c %a -- "$path") || exit "$?"
            printf '%s\0%s\0' "$path" "$mode" || exit "$?"
            sha256sum -- "$path" || exit "$?"
        done
    ) | sha256sum | cut -d ' ' -f 1
}

checker_admit_image() {
    local fingerprint=$1 tag="nmr-provider-tools/checker:$1" receipt=$operation_directory/image status
    if checker_metadata "$receipt" image inspect --format '{{.Id}} {{index .Config.Labels "org.nmr-provider-tools.inputs"}}' "$tag"; then :; else
        status=$?
        printf -v operation_diagnostic '%sCannot admit prepared checker %s. Run make checks/prepare with an explicit WHEELHOUSE and NETWORK_INTERFACE; inspect Docker availability if preparation already succeeded.\n' "$operation_diagnostic" "$tag"
        return "$status"
    fi
    local observed
    if ! IFS= read -r observed < "$receipt"; then
        operation_diagnostic+=$'Cannot admit prepared checker: image metadata is not a complete record.\n'
        return 2
    fi
    if [[ ! $observed =~ ^(sha256:[0-9a-f]{64})\ ([0-9a-f]{64})$ || ${BASH_REMATCH[2]} != "$fingerprint" ]]; then
        printf -v operation_diagnostic '%sCannot admit prepared checker %s: immutable image ID or input label differs. Rebuild with make checks/image/build.\n' "$operation_diagnostic" "$tag"
        return 2
    fi
    if ! cmp -s "$receipt" <(printf '%s\n' "$observed"); then
        operation_diagnostic+=$'Cannot admit prepared checker: image metadata is not one exact newline-terminated record.\n'
        return 2
    fi
    checker_image=${BASH_REMATCH[1]}
}
