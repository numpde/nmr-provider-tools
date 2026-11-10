"""Build and qualify the installed core and standalone consumer example.

Provider-core tests run from the candidate wheel with no checkout on its import
path. The integration lane separately builds and installs the dummy-provider
example, then runs the example's own tests outside both source trees.
"""

from tools.container_guard import require_container

require_container()

import argparse
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from tools.core_environment import (
    behavioral_install_lock,
    build_candidate,
    install_environment,
    install_locked,
    pip_command,
)
from tools.wheelhouse import digest


def _build_and_install_example(
    *,
    root: Path,
    python: Path,
    pool: Path,
) -> None:
    source = Path("/example")
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    distribution = project["name"]
    version = project["version"]
    core_version = tomllib.loads(
        (Path("/package") / "pyproject.toml").read_text()
    )["project"]["version"]

    if distribution != "dummy-provider-example":
        raise ValueError(
            "The installed example must identify dummy-provider-example"
        )
    if re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
        version,
    ) is None:
        raise ValueError("The installed example requires a three-part version")
    if project.get("dependencies") != [f"provider-core=={core_version}"]:
        raise ValueError(
            "The example must depend on the candidate provider-core version"
        )

    output = root / "example-release"
    output.mkdir()
    builder = install_environment(
        Path("/runtime/example-builder"),
        Path("/core-build.lock"),
        pool,
    )
    subprocess.run(
        [
            *pip_command(builder),
            "wheel",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(output),
            str(source),
        ],
        check=True,
        env=os.environ | {"SOURCE_DATE_EPOCH": "315532800"},
    )

    normalized_distribution = re.sub(r"[-_.]+", "_", distribution)
    wheel = output / f"{normalized_distribution}-{version}-py3-none-any.whl"
    if set(output.iterdir()) != {wheel} or not wheel.is_file():
        raise ValueError("The example build did not produce its one expected wheel")

    lock = root / "example-install.lock"
    continuation = "\\"
    lock.write_text(
        f"{distribution}=={version} {continuation}\n"
        f"    --hash=sha256:{digest(wheel)}\n"
    )
    install_locked(
        python,
        lock,
        pool,
        root / "example-install.json",
        extra_pools=(output,),
        required_files={wheel.name: wheel},
    )
    subprocess.run(
        [
            *pip_command(python),
            "--no-cache-dir",
            "check",
        ],
        check=True,
    )


def _run_installed_suite(
    *,
    python: Path,
    run: Path,
    selection: str,
    patterns: tuple[str, ...],
) -> None:
    outcome = subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            str(run / "tests/run_suite.py"),
            selection,
            *patterns,
        ],
        cwd=run,
        check=False,
    )
    if outcome.returncode:
        raise SystemExit(
            f"Installed provider-core {selection} tests failed "
            f"(exit {outcome.returncode}); see the failure summary or "
            "diagnostic immediately above."
        )


def run_suite(suite: str, patterns: tuple[str, ...]) -> None:
    with tempfile.TemporaryDirectory(prefix="core-tests-") as temporary:
        root = Path(temporary)
        pool = Path("/wheelhouse")
        output = root / "release"
        output.mkdir()

        (wheel, lock, _) = build_candidate(
            Path("/package"),
            Path("/core-runtime.lock"),
            Path("/core-build.lock"),
            pool,
            output,
        )

        # Behavioral suites need their pinned test closure. Contract and native
        # installation proofs remain dependency-pure.
        install_lock = lock
        if suite in ("unit", "integration", "integration/delivery", "fuzz"):
            install_lock = behavioral_install_lock(
                lock,
                Path("/package/tests/requirements.lock"),
                root / "behavioral-install.lock",
            )
        python = install_environment(
            Path("/runtime"),
            install_lock,
            pool,
            extra_pools=(output,),
            required_files={wheel.name: wheel},
        )

        run = root / "run"
        shutil.copytree(Path("/package/tests"), run / "tests")
        if suite == "integration":
            _build_and_install_example(
                root=root,
                python=python,
                pool=pool,
            )
            shutil.copytree(
                Path("/example/tests"),
                run / "tests/integration/dummy_provider_example",
            )

        # silent-defaults: allow-contract -- Fuzz selects both owned suites;
        # every other admitted selector names one suite.
        selections = ("integration", "unit") if suite == "fuzz" else (suite,)
        for selection in selections:
            _run_installed_suite(
                python=python,
                run=run,
                selection=selection,
                patterns=patterns,
            )

        if suite == "unit":
            native_lock = output / wheel.name.replace(
                "-py3-none-any.whl",
                "-native-install.lock",
            )
            native_python = install_environment(
                Path("/runtime/native-proof"),
                native_lock,
                pool,
                extra_pools=(output,),
                required_files={wheel.name: wheel},
            )
            _run_installed_suite(
                python=native_python,
                run=run,
                selection="native",
                patterns=("test*.py",),
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite",
        choices=(
            "unit",
            "contract",
            "integration",
            "integration/delivery",
            "fuzz",
        ),
    )
    parser.add_argument(
        "--pattern",
        action="append",
        required=True,
    )
    arguments = parser.parse_args()
    run_suite(
        arguments.suite,
        tuple(arguments.pattern),
    )
