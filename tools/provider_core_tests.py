"""Build and qualify the installed core with no consumer checkout on its path.

The isolated runner and executable runtime tmpfs follow nmrnet-repro d09b77b.
Package integration fixtures are self-contained; consumer tests stay with their
provider repositories.
"""

from tools.container_guard import require_container

require_container()

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from tools.core_environment import build_candidate, install_environment
from tools.artifacts import regular_file
from tools.wheelhouse import capture


def run_suite(suite, patterns):
    with tempfile.TemporaryDirectory(prefix="core-tests-") as temporary:
        root = Path(temporary)
        output = root / "release"
        output.mkdir()
        wheel, lock, _ = build_candidate(
            Path("/package"), Path("/core-runtime.lock"), Path("/core-build.lock"), Path("/wheelhouse"), output,
        )
        # Reuse the selected shared dependency files, adding the candidate wheel
        # only to this private installation pool, never to the shared cache.
        from_tools_pool = root / "pool"
        from_tools_pool.mkdir()
        capture(Path("/core-runtime.lock"), Path("/wheelhouse"), from_tools_pool)
        with regular_file(wheel) as source, (from_tools_pool / wheel.name).open("xb") as target:
            shutil.copyfileobj(source, target)
        python = install_environment(Path("/runtime"), lock, from_tools_pool)
        run = root / "run"
        shutil.copytree(Path("/package/tests"), run / "tests")
        subprocess.run([str(python), "-I", "-B", str(run / "tests/run_suite.py"), suite, *patterns], cwd=run, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("unit", "contract", "integration", "integration/delivery"))
    parser.add_argument("--pattern", action="append", required=True)
    args = parser.parse_args()
    run_suite(args.suite, args.pattern)
