#!/usr/bin/env bash
# Exercise the cold identity adapter against its exact released dependency.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/host/tools/docker_context.sh"
select_docker_context
source "$root/host/tools/base_image.sh"
source "$root/host/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
source "$root/host/tools/checker_operation.sh"
checker_begin
dependency=$wheelhouse_directory/linux_idmap-0.3.0-py3-none-any.whl
python3 -I -S - "$root/requirements/acquisition-bootstrap.pylock.toml" "$dependency" <<'PY'
import hashlib, pathlib, sys, tomllib
pin = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["packages"][0]["wheels"][0]
if hashlib.sha256(pathlib.Path(sys.argv[2]).read_bytes()).hexdigest() != pin["hashes"]["sha256"]:
    raise SystemExit("Cannot test acquisition identity: dependency wheel differs from its release pin.")
PY
checker_run 60 --pull never --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 --memory 128m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --mount "type=bind,src=$dependency,dst=/identity.whl,readonly" \
    --mount "type=bind,src=$root/acquisition_tool,dst=/source/acquisition_tool,readonly" \
    --mount "type=bind,src=$root/tests/acquisition_identity,dst=/tests,readonly" \
    --entrypoint python3 "$base_image" -I -S -B -c \
    'import sys,unittest; sys.path[:0]=["/identity.whl", "/source", "/tests"]; unittest.main(module="test_identity", verbosity=2)'
