#!/usr/bin/env bash
# Runtime tests are mounted. Only actual image construction inputs bind its tag.
source "$root/operations/docker/image_fingerprint.sh"
source "$root/operations/docker/adapters/docker_inspection.sh"
source "$root/operations/docker/validation/upstream/image_ids.sh"
checker_input_paths=(
    operations/docker/base_image.sh operations/checker/image/dependencies.py tools/container_guard.py tools/artifacts.py
    tools/adapters/__init__.py tools/adapters/debian.py tools/adapters/http_download.py tools/adapters/pip_install.py
    tools/validation/__init__.py tools/validation/upstream/__init__.py tools/validation/upstream/debian.py
    requirements/checker-dependencies.json operations/checker/image/Dockerfile
    tools/installer.py requirements/installer-release.json packages/type-requirements/src/type_requirements.py
)

checker_fingerprint() {
    image_fingerprint "$1" "${checker_input_paths[@]}"
}

checker_image_tag() {
    case $1 in
        checker) printf 'nmr-provider-tools/checker:%s' "$2" ;;
        bootstrap) printf 'nmr-provider-tools/checker-bootstrap:%s' "$2" ;;
        *) printf '%s\n' 'Cannot select checker image: use its bootstrap or checker target.' >&2; return 2 ;;
    esac
}

checker_admit_image() {
    local fingerprint=$1 receipt=$operation_directory/image status tag
    tag=$(checker_image_tag "$2" "$fingerprint") || return "$?"
    if docker_inspect_image_id_and_label "$receipt" "$tag" org.nmr-provider-tools.inputs; then :; else
        status=$?
        printf -v operation_diagnostic '%sCannot admit prepared checker %s. Run make checks/prepare with an explicit WHEELHOUSE and NETWORK_INTERFACE; inspect Docker availability if preparation already succeeded.\n' "$operation_diagnostic" "$tag"
        return "$status"
    fi
    local observed admitted_image
    if ! IFS= read -r observed < "$receipt"; then
        operation_diagnostic+=$'Cannot admit prepared checker: image metadata is not a complete record.\n'
        return 2
    fi
    if [[ ! $fingerprint =~ ^[0-9a-f]{64}$ ]] || ! docker_image_id_with_label_matches "$observed" "$fingerprint" admitted_image; then
        printf -v operation_diagnostic '%sCannot admit prepared checker %s: immutable image ID or input label differs. Rebuild with make checks/image/build.\n' "$operation_diagnostic" "$tag"
        return 2
    fi
    if ! cmp -s "$receipt" <(printf '%s\n' "$observed"); then
        operation_diagnostic+=$'Cannot admit prepared checker: image metadata is not one exact newline-terminated record.\n'
        return 2
    fi
    checker_image=$admitted_image
}

checker_admit_tools() {
    local fingerprint
    fingerprint=$(checker_fingerprint checker) || return "$?"
    checker_admit_image "$fingerprint" checker
}
