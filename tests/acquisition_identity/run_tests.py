"""Run cold identity proofs from the complete checked bootstrap selection."""

import json
import pathlib
import subprocess
import sys
import unittest


selection = subprocess.run(
    [
        sys.executable,  # Match the bootstrap lock's target interpreter.
        "-I",  # Ignore user Python paths and environment settings.
        "-B",  # Keep bytecode out of the read-only test source.
        "/wheelhouse.pyz",  # Run the independently verified tool bundle.
        "check",  # Verify the complete bootstrap selection offline.
        "--lock",  # Read the selected bootstrap environment lock.
        "/bootstrap.wheelhouse.json",
        "--pool",  # Read the caller's shared wheel directory.
        "/wheelhouse",
    ],
    check=True,
    stdout=subprocess.PIPE,
    text=True,
)

(identity,) = (wheel for wheel in json.loads(selection.stdout)["artifact_entries"] if wheel["package"] == "linux-idmap")
sys.path[:0] = [identity["source_path"], "/source", "/tests"]

unittest.main(module="test_identity", verbosity=2)
