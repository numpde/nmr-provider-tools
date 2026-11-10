"""Build the shared wheel and install its closed hash lock without networking.

The pip transaction follows nmrnet-repro d09b77b tools/core_environment.py and
provider_core_tests.py. Consumers install the issued wheel; only this owner
builds it. A fixed ZIP epoch makes equal source bytes produce equal wheel bytes.
"""

from tools.container_guard import require_container

require_container()

import argparse
import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

from tools.artifacts import fsync_directory, regular_file
from tools.wheelhouse import digest, pool_read_lock, read_json, requirements, select_wheels, wheel_inputs_from_report


def pip_command(python):
    # Run pip in the target interpreter itself so its inherited pool lock stays
    # with the process reading wheels, even if the launcher is interrupted.
    import pip

    runner = Path(pip.__file__).resolve().parent / "__pip-runner__.py"
    return [str(python), "-I", str(runner), "--isolated"]


def build_source_wheel(
    *,
    source: Path,
    staged_source: Path,
    python: Path,
    output: Path,
) -> Path:
    """Build from writable scratch, never from a read-only source mount.

    The caller owns staged_source's temporary parent and its cleanup. Returning
    the stage permits a subsequent source-distribution build from the same tree.
    """
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Cannot build wheel: source must be a real directory.")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("Cannot build wheel: source must not contain symlinks.")
    if staged_source.resolve().is_relative_to(source.resolve()):
        raise ValueError("Cannot build wheel: staged source must be outside the input tree.")

    shutil.copytree(source, staged_source)
    for directory in (staged_source, *staged_source.rglob("*")):
        if directory.is_dir():
            directory.chmod(directory.stat().st_mode | stat.S_IWUSR)

    subprocess.run(
        [
            *pip_command(python),
            "wheel",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(output),
            str(staged_source),
        ],
        check=True,
        env=os.environ | {"SOURCE_DATE_EPOCH": "315532800"},
    )
    return staged_source


# silent-defaults: allow-contract -- Installation accepts explicit optional immutable extra wheel pools; an omitted pool is the empty collection, while missing required files has a separate None sentinel.
def install_locked(python, lock, wheels, report, extra_pools=(), required_files=None, lock_fd=None):
    borrowed_descriptors = ()
    if lock_fd is not None:
        borrowed_descriptors = (lock_fd,)
    subprocess.run(
        [
            *pip_command(python),
            "install",
            "--ignore-installed",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--no-deps",
            "--only-binary=:all:",
            "--require-hashes",
            "--no-compile",
            "--report",
            str(report),
            *[
                argument
                for directory in (wheels, *extra_pools)
                for argument in ("--find-links", str(directory))
            ],
            "-r",
            str(lock),
        ],
        check=True,
        pass_fds=borrowed_descriptors,
    )
    return wheel_inputs_from_report(lock, wheels, report, extra_pools, required_files)


def build_candidate(source, runtime_lock, build_lock, pool, output):
    """Freeze locks and select shared-pool wheels before building a release."""
    with pool_read_lock(pool) as lock_fd:
        return _build_candidate(source, runtime_lock, build_lock, pool, output, lock_fd)


def _build_candidate(source, runtime_lock, build_lock, pool, output, lock_fd):
    if output.is_symlink() or not output.is_dir() or any(output.iterdir()):
        raise ValueError("Cannot build provider-core: output must be a private empty directory.")

    with tempfile.TemporaryDirectory(prefix="core-build-") as temporary:
        scratch = Path(temporary)
        identities = {}
        locks = {}
        for (role, lock) in (("runtime", runtime_lock), ("build", build_lock)):
            frozen = scratch / f"{role}.lock"
            shutil.copyfile(lock, frozen)
            requirements(frozen)
            identities[role] = select_wheels(frozen, pool, lock_fd=lock_fd)
            locks[role] = frozen

        project_metadata = tomllib.loads((source / "pyproject.toml").read_text())
        release_version = project_metadata["project"]["version"]
        if not (
            project_metadata["project"]["name"] == "provider-core"
            and type(release_version) is str
            and re.fullmatch(
                r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                release_version,
            ) is not None
        ):
            raise ValueError(
                "Cannot build provider-core release: metadata must identify "
                "provider-core and a three-part release version."
            )

        builder = scratch / "builder"
        subprocess.run(
            [sys.executable, "-I", "-m", "venv", "--without-pip", str(builder)],
            check=True,
        )
        builder_python = builder / "bin/python"
        installed_build_inputs = install_locked(
            python=builder_python,
            lock=locks["build"],
            wheels=pool,
            report=scratch / "build-install.json",
            lock_fd=lock_fd,
        )
        if installed_build_inputs != identities["build"]:
            raise ValueError("Cannot build provider-core: installed build inputs differ from selected inputs.")

        staged_package = build_source_wheel(
            source=source,
            staged_source=scratch / "package",
            python=builder_python,
            output=output,
        )
        sdist_builder = Path(__file__).with_name("build_sdist.py")
        subprocess.run(
            [
                str(builder_python),
                "-I",
                "-B",
                str(sdist_builder),
                str(output),
            ],
            cwd=staged_package,
            env=os.environ | {"SOURCE_DATE_EPOCH": "315532800"},
            check=True,
        )
        wheel = output / f"provider_core-{release_version}-py3-none-any.whl"
        sdist = output / f"provider_core-{release_version}.tar.gz"
        if set(output.iterdir()) != {wheel, sdist} or any(
            not path.is_file() or path.is_symlink() for path in (wheel, sdist)
        ):
            raise ValueError(
                "Cannot publish provider-core: build must produce exactly "
                "its declared wheel and source distribution."
            )

        normalize_sdist(sdist)

        install_lock = output / f"provider_core-{release_version}-install.lock"
        runtime_lock_text = locks["runtime"].read_text()
        requirements(locks["runtime"])
        install_lock.write_text(
            runtime_lock_text.rstrip()
            + f"\nprovider-core=={release_version} \\\n    --hash=sha256:{digest(wheel)}\n"
        )
        expected_requirements = requirements(locks["runtime"]) | {
            ("provider-core", release_version): {digest(wheel)}
        }
        observed_requirements = requirements(install_lock)
        if observed_requirements != expected_requirements:
            raise ValueError(
                "Cannot publish provider-core: installation lock differs "
                "from the runtime closure and built wheel."
            )

        native_lock = output / f"provider_core-{release_version}-native-install.lock"
        native_lock.write_text(
            f"provider-core=={release_version} \\\n    --hash=sha256:{digest(wheel)}\n"
        )
        if requirements(native_lock) != {("provider-core", release_version): {digest(wheel)}}:
            raise ValueError("Cannot publish provider-core: native installation lock differs from the built wheel.")

    return wheel, install_lock, identities


# silent-defaults: allow-contract -- An optional caller-supplied wheel pool is immutable and empty by default; the expected-inputs and required-files None values signal distinct checks.
def install_environment(root, lock, pool, expected_inputs=None, extra_pools=(), required_files=None):
    """Create a pip-free runtime from one complete installation lock."""
    with pool_read_lock(pool) as lock_fd:
        return _install_environment(root, lock, pool, expected_inputs, extra_pools, required_files, lock_fd)


def _install_environment(root, lock, pool, expected_inputs, extra_pools, required_files, lock_fd):
    python = root / "environment/bin/python"
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(python.parent.parent)], check=True)
    selected = select_wheels(lock, pool, extra_pools, required_files, lock_fd)
    if expected_inputs is not None and selected != expected_inputs:
        raise ValueError("Cannot admit provider-core installation: selected wheel identities differ from the pinned inputs.")
    with tempfile.NamedTemporaryFile(prefix="core-install-report-", suffix=".json") as report:
        observed = install_locked(python, lock, pool, Path(report.name), extra_pools, required_files, lock_fd)
        if observed != selected:
            raise ValueError("Cannot admit provider-core installation: installed wheel identities differ from captured inputs.")
    subprocess.run([*pip_command(python), "--no-cache-dir", "check"], check=True)
    shutil.copyfile(lock, root / "provider-core-install.lock")
    return python


def behavioral_install_lock(runtime_lock, test_lock, destination):
    """Combine the exact installed runtime wheel lock with test-only wheels.

    Issued runtime and native locks are never mutated. Consumers receive a
    temporary complete lock, and duplicate package identities are refused.
    """
    base = requirements(runtime_lock)
    tests = requirements(test_lock)
    overlap = set(base).intersection(tests)
    if overlap:
        raise ValueError(
            "Cannot install provider-core behavioral tests: test requirements "
            f"overlap runtime packages: {sorted(overlap)!r}"
        )
    destination.write_text(runtime_lock.read_text().rstrip() + "\n" + test_lock.read_text().strip() + "\n")
    if requirements(destination) != base | tests:
        raise ValueError("Cannot install provider-core behavioral tests: merged wheel lock differs from its inputs")
    return destination


def source_inventory(source):
    inventory = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Cannot capture package inventory: source links are not release inputs.")
        if path.is_file():
            inventory[path.relative_to(source).as_posix()] = {
                "sha256": digest(path), "size": path.stat().st_size, "mode": path.stat().st_mode & 0o777,
            }
        elif not path.is_dir():
            raise ValueError("Cannot capture package inventory: source must contain regular files and directories.")
    return inventory


def wheelhouse_source_inventory(source):
    from tools.build_wheelhouse_bundle import SOURCE_MODULES

    return {name: {"sha256": digest(source / name), "size": (source / name).stat().st_size,
                   "mode": (source / name).stat().st_mode & 0o777}
            for name in SOURCE_MODULES}


def normalize_sdist(path):
    """Remove host account metadata and use the same fixed epoch as the wheel."""
    # Keep replacement on the same filesystem, with automatic failure cleanup.
    with tempfile.TemporaryDirectory(prefix="sdist-normalize-", dir=path.parent) as temporary:
        normalized = Path(temporary) / "archive.tar.gz"
        with tarfile.open(path, "r:gz") as original, normalized.open("xb") as output:
            with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=315532800) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as target:
                    for member in sorted(original.getmembers(), key=lambda item: item.name):
                        if not (member.isfile() or member.isdir()):
                            raise ValueError("Cannot admit source distribution: archive contains an unsupported link or special file.")
                        member.uid = member.gid = 0
                        member.uname = member.gname = ""
                        member.mtime = 315532800
                        member.pax_headers = {}
                        if member.isfile():
                            with original.extractfile(member) as content:
                                target.addfile(member, content)
                        else:
                            target.addfile(member)
        normalized.replace(path)


def release(source, runtime_lock, build_lock, pool, output, revision):
    from tools.build_wheelhouse_bundle import build as build_wheelhouse_bundle, dependency_files
    from tools.build_acquisition_bundle import build as build_acquisition_bundle

    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Cannot build release artifacts: source revision must identify the captured Git commit.")
    dependency_files(pool, Path("/requirements/wheelhouse-tool.lock"))
    inventory = source_inventory(source)
    (wheel, lock, identities) = build_candidate(source, runtime_lock, build_lock, pool, output)
    prefix = wheel.name.removesuffix("-py3-none-any.whl")
    sdist = output / (prefix + ".tar.gz")
    native_lock = output / (prefix + "-native-install.lock")
    bundle = output / ("wheelhouse-tool-" + prefix.removeprefix("provider_core-") + ".pyz")
    build_wheelhouse_bundle(pool, bundle, Path("/workspace"), Path("/requirements/wheelhouse-tool.lock"))
    acquisition = output / ("acquisition-owner-" + prefix.removeprefix("provider_core-") + ".tar.gz")
    acquisition_record = build_acquisition_bundle(pool, acquisition, Path("/workspace"), wheel,
                                                  Path("/requirements/acquisition-bootstrap.pylock.toml"))
    payload = (wheel, sdist, lock, native_lock, bundle, acquisition)
    record = output / (prefix + "-release.json")
    record.write_text(json.dumps({
        "source_revision": revision, "dependency_inputs": identities,
        "source_files": inventory, "tool_source_files": wheelhouse_source_inventory(Path("/workspace/wheelhouse_tool")),
        "acquisition_source_files": acquisition_record["source_files"],
        "acquisition_dependency_inputs": acquisition_record["dependency_inputs"],
        "files": {path.name: digest(path) for path in payload},
    }, sort_keys=True, indent=2) + "\n")
    manifest = output / (prefix + "-sha256.json")
    manifest.write_text(json.dumps({path.name: digest(path) for path in (*payload, record)}, sort_keys=True, indent=2) + "\n")
    for path in (*payload, record, manifest):
        with regular_file(path) as stream:
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
    fsync_directory(output)
    print(json.dumps({"source_revision": revision, "wheel": wheel.name, "sha256": digest(wheel)}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("release", "install"))
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--runtime-lock", type=Path)
    parser.add_argument("--build-lock", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--expected-inputs", type=Path)
    args = parser.parse_args()
    if args.action == "install":
        if args.root is None or args.lock is None:
            parser.error("install requires --root and --lock")
        expected_inputs = None
        if args.expected_inputs is not None:
            expected_inputs = read_json(args.expected_inputs)
        install_environment(args.root, args.lock, args.pool, expected_inputs)
    else:
        if any(value is None for value in (args.source, args.runtime_lock, args.build_lock, args.output, args.revision)):
            parser.error("release requires --source, --runtime-lock, --build-lock, --output and --revision")
        release(args.source, args.runtime_lock, args.build_lock, args.pool, args.output, args.revision)


if __name__ == "__main__":
    main()
