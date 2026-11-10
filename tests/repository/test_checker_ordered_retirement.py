"""Test dependency ordering of the real checker owner without an engine."""
from tools.container_guard import require_container

require_container()

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.repository.quality.sources import ROOT


class OrderedRetirementTests(unittest.TestCase):
    # silent-defaults: allow-contract -- Ordered-cleanup fixtures start without unexpected borrowers, pending operations or prior errors unless a test injects one.
    def exercise(self, fail, *, unexpected=False, pending=False, primary=0):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script = r'''set -eu
root=$1
source "$root/host/tools/checker_operation.sh"
operation_directory=$2
mkdir -p "$operation_directory"
operation_label=owned
operation_diagnostic=
operation_success=
docker_client_directory=$2
inference_retirement_confirmed=true
retain_operation=false
create_pending=$PENDING
ordered_container_retirement=true
worker=$(printf 'a%.0s' {1..64})
relay=$(printf 'b%.0s' {1..64})
gateway=$(printf 'c%.0s' {1..64})
unknown=$(printf 'd%.0s' {1..64})
operation_retire_order=("$worker" "$relay" "$gateway")
checker_teardown() {
  case "$1" in
    ps)
      if [[ "$*" == *"label="* ]]; then
        for id in "$gateway" "$worker" "$relay" "$unknown"; do [[ ! -e "$operation_directory/$id" ]] || printf '%s\n' "$id"; done
      else
        id=${*: -1}; id=${id#id=}
        [[ ! -e "$operation_directory/$id" ]] || printf '%s\n' "$id"
      fi ;;
    rm)
      id=${*: -1}
      printf '%s\n' "${id:0:1}" >> "$CALLS"
      [[ "$id" != "$relay" || "$FAIL" != yes ]] || return 1
      rm -f "$operation_directory/$id" ;;
  esac
}
docker_client_cleanup() { :; }
image_lifecycle_unlock() { :; }
image_retire_after_success=false
touch "$operation_directory/$worker" "$operation_directory/$relay" "$operation_directory/$gateway"
if [[ $UNEXPECTED == yes ]]; then touch "$operation_directory/$unknown"; fi
set +e
(exit "$PRIMARY")
checker_finish
'''
            fail_text = "no"
            unexpected_text = "no"
            pending_text = "false"
            if fail:
                fail_text = "yes"
            if unexpected:
                unexpected_text = "yes"
            if pending:
                pending_text = "true"
            env = os.environ | {"CALLS": str(directory / "calls"), "FAIL": fail_text,
                               "UNEXPECTED": unexpected_text,
                               "PENDING": pending_text, "PRIMARY": str(primary)}
            result = subprocess.run(["bash", "-c", script, "bash", str(ROOT), str(directory / "operation")],
                                    env=env, capture_output=True, text=True, timeout=10)
            calls = []
            if (directory / "calls").exists():
                calls = (directory / "calls").read_text().splitlines()
            return result, calls

    def test_removes_in_dependency_order(self):
        result, calls = self.exercise(False)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(["a", "b", "c"], calls)

    def test_failed_relay_keeps_gateway(self):
        result, calls = self.exercise(True)
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertEqual(["a", "b"], calls)

    def test_unknown_container_or_unconfirmed_create_preserves_everything(self):
        for options in ({"unexpected": True}, {"pending": True}):
            with self.subTest(options=options):
                result, calls = self.exercise(False, **options)
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertEqual([], calls)

    def test_primary_failure_is_not_replaced_by_cleanup_failure(self):
        result, calls = self.exercise(True, primary=23)
        self.assertEqual(23, result.returncode, msg=result.stderr)
        self.assertEqual(["a", "b"], calls)


if __name__ == "__main__":
    unittest.main()
