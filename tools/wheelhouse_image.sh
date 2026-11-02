#!/usr/bin/env bash
# Build and test the shared tool without installing provider-core.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
action=${1:?Select build or test}
source "$root/tools/base_image.sh"
source "$root/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
source "$root/tools/checker_operation.sh"
checker_begin
image=nmr-provider-tools/wheelhouse-tool:local
case "$action" in
    build)
        checker_call 'offline wheelhouse tool build' 900 stdio "${engine[@]}" build \
            --builder default --network none --pull=false \
            --build-context "wheelhouse=$wheelhouse_directory" \
            --build-arg "BASE_IMAGE=$base_image" \
            --file "$root/containers/wheelhouse/Dockerfile" --tag "$image" "$root"
        ;;
    test)
        checker_run 180 --pull never --network none --read-only --cap-drop ALL \
            --security-opt no-new-privileges:true --pids-limit 128 --memory 512m \
            --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
            --mount "type=bind,src=$root/wheelhouse_tool,dst=/opt/wheelhouse_tool,readonly" \
            --mount "type=bind,src=$root/tests/test_shared_wheelhouse.py,dst=/tests/test_shared_wheelhouse.py,readonly" \
            --entrypoint python "$image" -m unittest discover -s /tests -p test_shared_wheelhouse.py -v
        ;;
    *) printf 'Unknown wheelhouse image operation: %s\n' "$action" >&2; exit 2 ;;
esac
