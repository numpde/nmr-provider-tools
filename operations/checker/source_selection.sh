#!/usr/bin/env bash
# Grant individual public Python files; never mount the consumer checkout itself
selection_mounts=()
selection_environment=()
if [[ -v CHECK_SOURCE_ROOT_INPUT_ORIGIN && $CHECK_SOURCE_ROOT_INPUT_ORIGIN == 'command line' ||
      -v CHECK_SOURCE_FILES_INPUT_ORIGIN && $CHECK_SOURCE_FILES_INPUT_ORIGIN == 'command line' ]]; then
    if [[ ! -v CHECK_SOURCE_ROOT_INPUT_ORIGIN || $CHECK_SOURCE_ROOT_INPUT_ORIGIN != 'command line' ||
          ! -v CHECK_SOURCE_FILES_INPUT_ORIGIN || $CHECK_SOURCE_FILES_INPUT_ORIGIN != 'command line' ||
          ! -v CHECK_SOURCE_ROOT_INPUT || ! -v CHECK_SOURCE_FILES_INPUT ||
          ! -v TEST_PATTERN_INPUT_ORIGIN || $TEST_PATTERN_INPUT_ORIGIN != 'command line' || $suite != tests/repository/quality ]]; then
        printf '%s\n' 'CHECK_SOURCE_ROOT and CHECK_SOURCE_FILES should accompany TEST_PATTERN on one quality target.' >&2
        exit 2
    fi
    source_root=$CHECK_SOURCE_ROOT_INPUT
    if [[ $source_root != /* || ! -d $source_root || $source_root =~ [[:cntrl:],] ]]; then
        printf 'CHECK_SOURCE_ROOT=%q should be an absolute directory without commas or control characters.\n' "$source_root" >&2
        exit 2
    fi
    IFS= read -r -d '' source_root < <(realpath -ez -- "$source_root")
    if [[ $source_root =~ [[:cntrl:],] ]]; then
        printf 'CHECK_SOURCE_ROOT=%q should have no commas or control characters.\n' "$source_root" >&2
        exit 2
    fi
    selected_files=()
    read -r -a selected_files <<< "$CHECK_SOURCE_FILES_INPUT"
    if [[ ${#selected_files[@]} == 0 || $CHECK_SOURCE_FILES_INPUT =~ [[:cntrl:]] ]]; then
        printf '%s\n' 'CHECK_SOURCE_FILES should name space-separated relative Python files on one line.' >&2
        exit 2
    fi
    for source_file in "${selected_files[@]}"; do
        source_path=${source_root%/}/$source_file
        case "/$source_file/" in
            */../*|*/./*|*//*)
                printf 'CHECK_SOURCE_FILES entry %q should be a public relative file without traversal.\n' "$source_file" >&2
                exit 2 ;;
        esac
        case "$source_root/$source_file/" in
            */.git/*|*/.agents/*|*/.aws/*|*/.codex/*|*/secrets/*|*/release-artifacts/*|*/bootstrap/*|*/node_modules/*|*/dependencies/*|*/.venv/*|*/__pycache__/*|*/.pytest_cache/*|*/.mypy_cache/*|*/.ruff_cache/*)
                printf 'CHECK_SOURCE_FILES entry %q should be outside private and generated source directories.\n' "$source_file" >&2
                exit 2 ;;
        esac
        if [[ $source_file == /* || $source_file != *.py || $source_file =~ [[:cntrl:],] || ! -f $source_path ]]; then
            printf 'CHECK_SOURCE_FILES entry %q should identify an existing relative .py file.\n' "$source_file" >&2
            exit 2
        fi
        resolved_source=$(realpath -e -- "$source_path")
        if [[ $resolved_source != "$source_path" ]]; then
            printf 'CHECK_SOURCE_FILES entry %q should have no symlink components.\n' "$source_file" >&2
            exit 2
        fi
        selection_mounts+=(--mount "type=bind,src=$resolved_source,dst=/selected-source/$source_file,readonly")
    done
    selection_environment=(--env CHECK_SOURCE_ROOT=/selected-source
        --env "CHECK_SOURCE_HOST_ROOT=$source_root" --env "CHECK_SOURCE_FILES=${CHECK_SOURCE_FILES_INPUT}")
fi
