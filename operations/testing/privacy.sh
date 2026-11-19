#!/usr/bin/env bash
# Forward only the explicit Make selection into test containers
privacy_tests=1
if [[ -v PRIVACY_TESTS_INPUT_ORIGIN && $PRIVACY_TESTS_INPUT_ORIGIN == 'command line' ]]; then
    if [[ -v PRIVACY_TESTS_INPUT && ( $PRIVACY_TESTS_INPUT == 0 || $PRIVACY_TESTS_INPUT == 1 ) ]]; then
        privacy_tests=$PRIVACY_TESTS_INPUT
    else
        printf '%s\n' 'PRIVACY_TESTS should be 0 or 1.' >&2
        exit 2
    fi
fi
