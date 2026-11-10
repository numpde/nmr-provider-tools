"""Build and qualify the installed core and standalone consumer example.

Provider-core tests run from the candidate wheel with no checkout on its import
path. The integration lane separately builds and installs the dummy-provider
example, then runs the example's own tests outside both source trees.
"""

from tools.container_guard import require_container

require_container()

import argparse
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from tools.core_environment import (
    behavioral_install_lock,
    build_candidate,
    build_source_wheel,
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
    example_source = Path("/example")
    project = tomllib.loads((example_source / "pyproject.toml").read_text())["project"]
    distribution_name = project["name"]
    distribution_version = project["version"]
    core_version = tomllib.loads(
        (Path("/package") / "pyproject.toml").read_text()
    )["project"]["version"]

    if distribution_name != "dummy-provider-example":
        raise ValueError("The installed example must identify dummy-provider-example")
    if re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
        distribution_version,
    ) is None:
        raise ValueError("The installed example requires a three-part version")
    if project.get("dependencies") != [f"provider-core=={core_version}"]:
        raise ValueError(
            "The example must depend on the candidate provider-core version"
        )

    example_output = root / "example-release"
    example_output.mkdir()

    builder_python = install_environment(
        Path("/runtime/example-builder"),
        Path("/core-build.lock"),
        pool,
    )
    build_source_wheel(
        source=example_source,
        staged_source=root / "example-source",
        python=builder_python,
        output=example_output,
    )

    wheel_distribution_name = re.sub(r"[-_.]+", "_", distribution_name)
    example_wheel = example_output / f"{wheel_distribution_name}-{distribution_version}-py3-none-any.whl"

    if set(example_output.iterdir()) != {example_wheel} or not example_wheel.is_file():
        raise ValueError("The example build did not produce its one expected wheel")

    example_install_lock = root / "example-install.lock"
    continuation = "\\"
    example_install_lock.write_text(
        f"{distribution_name}=={distribution_version} {continuation}\n"
        f"    --hash=sha256:{digest(example_wheel)}\n"
    )
    install_locked(
        python,
        example_install_lock,
        pool,
        root / "example-install.json",
        extra_pools=(example_output,),
        required_files={example_wheel.name: example_wheel},
    )
    subprocess.run([*pip_command(python), "--no-cache-dir", "check"], check=True)


def _run_installed_suite(
    *,
    python: Path,
    run: Path,
    selection: str,
    patterns: tuple[str, ...],
) -> None:
    test_process = subprocess.run(
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
    if test_process.returncode:
        raise SystemExit(
            f"Installed provider-core {selection} tests failed "
            f"(exit {test_process.returncode}); see the failure summary or "
            "diagnostic immediately above."
        )


def run_suite(suite: str, patterns: tuple[str, ...]) -> None:
    with tempfile.TemporaryDirectory(prefix="core-tests-") as temporary:
        scratch = Path(temporary)
        wheelhouse = Path("/wheelhouse")
        candidate_output = scratch / "release"
        candidate_output.mkdir()

        (candidate_wheel, candidate_lock, _) = build_candidate(
            Path("/package"),
            Path("/core-runtime.lock"),
            Path("/core-build.lock"),
            wheelhouse,
            candidate_output,
        )

        # Behavioral suites need their pinned test closure. Contract and native
        # installation proofs remain dependency-pure.
        install_lock = candidate_lock
        if suite in ("unit", "integration", "integration/delivery", "fuzz"):
            install_lock = behavioral_install_lock(
                candidate_lock,
                Path("/package/tests/requirements.lock"),
                scratch / "behavioral-install.lock",
            )

        runtime_python = install_environment(
            Path("/runtime"),
            install_lock,
            wheelhouse,
            extra_pools=(candidate_output,),
            required_files={candidate_wheel.name: candidate_wheel},
        )

        test_tree = scratch / "run"
        shutil.copytree(Path("/package/tests"), test_tree / "tests")
        if suite == "integration":
            _build_and_install_example(
                root=scratch,
                python=runtime_python,
                pool=wheelhouse,
            )
            shutil.copytree(
                Path("/example/tests"),
                test_tree / "tests/integration/dummy_provider_example",
            )

        # silent-defaults: allow-contract -- Fuzz selects both owned suites;
        # every other admitted selector names one suite.
        selections = ("integration", "unit") if suite == "fuzz" else (suite,)
        for selection in selections:
            _run_installed_suite(
                python=runtime_python,
                run=test_tree,
                selection=selection,
                patterns=patterns,
            )

        if suite == "unit":
            native_lock = candidate_output / candidate_wheel.name.replace(
                "-py3-none-any.whl",
                "-native-install.lock",
            )
            native_python = install_environment(
                Path("/runtime/native-proof"),
                native_lock,
                wheelhouse,
                extra_pools=(candidate_output,),
                required_files={candidate_wheel.name: candidate_wheel},
            )
            _run_installed_suite(
                python=native_python,
                run=test_tree,
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
