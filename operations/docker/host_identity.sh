#!/usr/bin/env bash
# Map the calling host identity into the selected local engine.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
[[ $# == 1 ]] || exit 2
source "$root/host/tools/checker_operation.sh"
checker_begin
checker_host_identity "$1" /usr/local/bin/python3 "$(id -u)" "$(id -g)"
printf '%s:%s\n' "$checker_host_uid" "$checker_host_gid"
