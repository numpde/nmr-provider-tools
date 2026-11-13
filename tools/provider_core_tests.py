"""Build and qualify the installed core and standalone consumer examples.

Provider-core tests run from the candidate wheel with no checkout on its import
path. The integration lane separately builds and installs each standalone
example, then runs its own tests outside all source trees.
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
)
from tools.installer import pip_command
from tools.wheelhouse import digest


def _discover_examples(examples: Path, core_version: str) -> list[tuple[Path, str, str]]:
    projects = []
    names = set()
    for metadata in sorted(examples.glob("*/pyproject.toml")):
        project = tomllib.loads(metadata.read_text())["project"]
        distribution_name = project["name"]
        distribution_version = project["version"]
        if not isinstance(distribution_name, str) or re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", distribution_name
        ) is None:
            raise ValueError(
                f"Cannot install example {metadata.parent.name!r}: distribution "
                "name must use lowercase letters, digits and single hyphens."
            )
        if distribution_name == "provider-core" or distribution_name in names:
            raise ValueError(
                f"Cannot install example {distribution_name!r}: distribution "
                "names must be unique and must not shadow provider-core."
            )
        if not isinstance(distribution_version, str) or re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
            distribution_version,
        ) is None:
            raise ValueError(
                f"Cannot install example {distribution_name!r}: "
                "a three-part version is required."
            )
        if project.get("dependencies") != [f"provider-core=={core_version}"]:
            raise ValueError(
                f"Cannot install example {distribution_name!r}: its only dependency "
                f"must be provider-core=={core_version}."
            )
        names.add(distribution_name)
        projects.append((metadata.parent, distribution_name, distribution_version))

    if not projects:
        raise ValueError("Cannot qualify installed examples: /examples contains no standalone projects.")
    return projects


def _build_and_install_example(
    *,
    root: Path,
    source: Path,
    distribution_name: str,
    distribution_version: str,
    builder_python: Path,
    python: Path,
    pool: Path,
) -> None:
    example_output = root / "release"
    example_output.mkdir(parents=True)
    build_source_wheel(
        source=source,
        staged_source=root / "source",
        python=builder_python,
        output=example_output,
    )

    wheel_distribution_name = distribution_name.replace("-", "_")
    example_wheel = example_output / f"{wheel_distribution_name}-{distribution_version}-py3-none-any.whl"

    if (
        set(example_output.iterdir()) != {example_wheel}
        or not example_wheel.is_file()
        or example_wheel.is_symlink()
    ):
        raise ValueError(f"Example {distribution_name!r} did not produce its one expected wheel.")

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
            core_version = tomllib.loads(
                (Path("/package") / "pyproject.toml").read_text()
            )["project"]["version"]
            examples = _discover_examples(Path("/examples"), core_version)
            builder_python = install_environment(
                Path("/runtime/example-builder"),
                Path("/core-build.lock"),
                wheelhouse,
            )
            for source, name, version in examples:
                _build_and_install_example(
                    root=scratch / "examples" / name,
                    source=source,
                    distribution_name=name,
                    distribution_version=version,
                    builder_python=builder_python,
                    python=runtime_python,
                    pool=wheelhouse,
                )
                namespace = "example_" + name.replace("-", "_")
                shutil.copytree(source / "tests", test_tree / "tests/integration" / namespace)

        # silent-defaults: allow-contract -- Fuzz selects both owned suites; every other admitted selector names one suite.
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
