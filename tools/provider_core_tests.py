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


def run_suite(suite, patterns):
    with tempfile.TemporaryDirectory(prefix="core-tests-") as temporary:
        root = Path(temporary)
        output = root / "release"
        output.mkdir()
        wheel, lock, _ = build_candidate(
            Path("/package"), Path("/core-runtime.lock"), Path("/core-build.lock"), Path("/wheelhouse"), output,
        )
        python = install_environment(Path("/runtime"), lock, Path("/wheelhouse"),
                                     extra_pools=(output,), required_files={wheel.name: wheel})
        run = root / "run"
        shutil.copytree(Path("/package/tests"), run / "tests")
        # silent-defaults: allow-contract -- Fuzz selects both owned suites; each other admitted selector names one suite.
        selections = ("integration", "unit") if suite == "fuzz" else (suite,)
        for selection in selections:
            subprocess.run([str(python), "-I", "-B", str(run / "tests/run_suite.py"), selection, *patterns], cwd=run, check=True)
        if suite == "unit":
            native_lock = output / wheel.name.replace("-py3-none-any.whl", "-native-install.lock")
            native_python = install_environment(Path("/runtime/native-proof"), native_lock, Path("/wheelhouse"),
                                                extra_pools=(output,), required_files={wheel.name: wheel})
            subprocess.run([str(native_python), "-I", "-B", str(run / "tests/run_suite.py"), "native", "test*.py"], cwd=run, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("unit", "contract", "integration", "integration/delivery", "fuzz"))
    parser.add_argument("--pattern", action="append", required=True)
    args = parser.parse_args()
    run_suite(args.suite, args.pattern)
