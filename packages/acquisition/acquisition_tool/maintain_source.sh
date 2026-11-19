#!/usr/bin/env bash
# Run the reviewed maintenance package; the target snapshot is data
set -euo pipefail
operation=${1:?Select update, verify or import}
case "$operation" in
    update) maintenance_module=acquisition_tool.source_download; shift ;;
    verify|import) maintenance_module=acquisition_tool.source_update ;;
    *) printf 'Unknown source operation: %s. Expected update, verify or import.\n' "$operation" >&2; exit 2 ;;
esac
package_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
exec python3 -I -S -B -c '
import runpy
import sys
sys.path.insert(0, sys.argv.pop(1))
runpy.run_module(sys.argv.pop(1), run_name="__main__", alter_sys=True)
' "$package_root" "$maintenance_module" "$@"
