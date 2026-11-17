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
from tools.wheelhouse import digest, requirements


def _discover_projects(*, project_root: Path) -> list[tuple[Path, str, str]]:
    projects = []
    names = set()

    for metadata in sorted(project_root.glob("*/pyproject.toml")):
        project = tomllib.loads(metadata.read_text())["project"]
        distribution_name = project["name"]
        distribution_version = project["version"]

        if not isinstance(distribution_name, (expected_type := str)):
            raise TypeError(f"{metadata.parent.name=}; {type(distribution_name)=} ≠ {expected_type=}")

        if not re.fullmatch(pattern := r"[a-z0-9]+(?:-[a-z0-9]+)*", distribution_name):
            raise ValueError(f"{metadata.parent.name!r}: {distribution_name=} does not follow {pattern=}")

        # todo: separate
        if distribution_name == "provider-core" or distribution_name in names:
            raise ValueError(
                f"Cannot install project {distribution_name!r}: distribution "
                "names must be unique and must not shadow provider-core."
            )

        if not isinstance(distribution_version, (expected_type := str)):
            raise TypeError(f"{distribution_name=}; {type(distribution_version)=} ≠ {expected_type=}")

        # todo: format like elsewhere
        if re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
            distribution_version,
        ) is None:
            raise ValueError(
                f"Cannot install project {distribution_name!r}: "
                "a three-part version is required."
            )

        names.add(distribution_name)
        projects.append((metadata.parent, distribution_name, distribution_version))

    if not (projects):
        raise ValueError(f"No standalone projects found under {project_root=}")

    return projects


def _discover_mocks(*, mocks_root: Path) -> list[tuple[Path, str, str]]:
    projects = _discover_projects(project_root=mocks_root)

    for (source, distribution_name, _) in projects:
        project = tomllib.loads((source / "pyproject.toml").read_text())["project"]

        if not distribution_name.startswith("mock-"):
            raise ValueError(f"A scientific stand-in distribution must start with mock-; {distribution_name=}")

        if not (project["dependencies"] == []):
            raise ValueError(f"Library stand-ins must remain dependency-free; {source=}")

        if (source / "requirements.lock").exists():
            raise ValueError(f"A dependency-free stand-in cannot install a dependency lock; {source=}")

    return projects


def _discover_examples(
    examples: Path,
    core_version: str,
    *,
    mock_dependencies: frozenset[str],
) -> list[tuple[Path, str, str]]:
    projects = _discover_projects(project_root=examples)
    mock_names = {dependency.split("==")[0] for dependency in mock_dependencies}

    for (source, distribution_name, _) in projects:
        project = tomllib.loads((source / "pyproject.toml").read_text())["project"]

        if distribution_name in mock_names:
            raise ValueError(f"An example cannot replace a scientific stand-in; {distribution_name=}")

        dependencies = project["dependencies"]
        sdk_dependencies = {f"provider-core=={core_version}", f"provider-core[api]=={core_version}"}
        selected_sdk_dependencies = sdk_dependencies.intersection(dependencies)

        if not (len(selected_sdk_dependencies) == 1):
            raise ValueError(f"Example must require exactly the candidate SDK or its API extra; {dependencies=}")

        scientific_dependencies = set(dependencies) - selected_sdk_dependencies - mock_dependencies

        if not scientific_dependencies and (source / "requirements.lock").exists():
            raise ValueError("An example without scientific dependencies cannot install a scientific lock")

        if scientific_dependencies:
            scientific_lock = requirements(lock=source / "requirements.lock")

            protected_projects = {"provider-core", *mock_names}

            for (locked_project, _) in scientific_lock:
                if locked_project in protected_projects:
                    raise ValueError(f"An example scientific lock cannot replace a repository-built project; {locked_project=}")

            locked_dependencies = {f"{name}=={version}" for (name, version) in scientific_lock}

            if not (scientific_dependencies <= locked_dependencies):
                raise ValueError(f"Example scientific dependencies must be pinned in its hashed lock; {scientific_dependencies=}; {locked_dependencies=}")

    return projects


def _build_and_install_project(
    *,
    root: Path,
    source: Path,
    distribution_name: str,
    distribution_version: str,
    builder_python: Path,
    python: Path,
    pool: Path,
) -> None:
    project_output = root / "release"

    project_output.mkdir(parents=True)

    build_source_wheel(
        source=source,
        staged_source=root / "source",
        python=builder_python,
        output=project_output,
    )

    wheel_distribution_name = distribution_name.replace("-", "_")
    project_wheel = project_output / f"{wheel_distribution_name}-{distribution_version}-py3-none-any.whl"

    if (
        set(project_output.iterdir()) != {project_wheel}
        or not project_wheel.is_file()
        or project_wheel.is_symlink()
    ):
        raise ValueError(f"Project {distribution_name!r} did not produce its one expected wheel.")

    project_install_lock = root / "project-install.lock"
    continuation = "\\"

    project_install_lock.write_text(
        f"{distribution_name}=={distribution_version} {continuation}\n"
        f"    --hash=sha256:{digest(project_wheel)}\n"
    )

    scientific_lock_path = source / "requirements.lock"

    if scientific_lock_path.is_file():
        with project_install_lock.open(mode="a", encoding="utf-8") as install_lock_file:
            install_lock_file.write(scientific_lock_path.read_text(encoding="utf-8"))

    install_locked(
        python,
        project_install_lock,
        pool,
        root / "project-install.json",
        extra_pools=(project_output,),
        required_files={project_wheel.name: project_wheel},
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

            builder_python = install_environment(
                Path("/runtime/example-builder"),
                Path("/core-build.lock"),
                wheelhouse,
            )

            scientific_mocks = _discover_mocks(mocks_root=Path("/mocks"))
            mock_dependencies = frozenset(f"{name}=={version}" for (_, name, version) in scientific_mocks)

            for (source, name, version) in scientific_mocks:
                _build_and_install_project(
                    root=scratch / "mocks" / name,
                    source=source,
                    distribution_name=name,
                    distribution_version=version,
                    builder_python=builder_python,
                    python=runtime_python,
                    pool=wheelhouse,
                )

                namespace = "mock_" + name.replace("-", "_")

                shutil.copytree(source / "tests", test_tree / "tests/integration" / namespace)

            examples = _discover_examples(
                examples=Path("/examples"),
                core_version=core_version,
                mock_dependencies=mock_dependencies,
            )

            for (source, name, version) in examples:
                _build_and_install_project(
                    root=scratch / "examples" / name,
                    source=source,
                    distribution_name=name,
                    distribution_version=version,
                    builder_python=builder_python,
                    python=runtime_python,
                    pool=wheelhouse,
                )

                namespace = "example_" + name.replace("-", "_")

                example_tree = test_tree / "tests/integration" / namespace

                example_tree.mkdir()
                (example_tree / "__init__.py").touch()
                shutil.copytree(source / "tests", example_tree / "tests")

                # Deployment configuration and model data stay outside wheels.
                # Preserve their relative layout when exercising installed code.
                for directory_name in ("config", "models"):
                    directory = source / directory_name

                    if directory.is_dir():
                        shutil.copytree(
                            src=directory,
                            dst=example_tree / directory_name,
                        )

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
