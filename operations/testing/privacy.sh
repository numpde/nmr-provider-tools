#!/usr/bin/env bash
# Forward only the explicit Make selection into test containers
privacy_tests=1
if [[ ${PRIVACY_TESTS_INPUT_ORIGIN:-undefined} == 'command line' ]]; then
    case ${PRIVACY_TESTS_INPUT:-} in
        0|1) privacy_tests=$PRIVACY_TESTS_INPUT ;;
        *) printf '%s\n' 'PRIVACY_TESTS should be 0 or 1.' >&2; exit 2 ;;
    esac
fi
