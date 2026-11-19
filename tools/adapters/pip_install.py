"""Construct pip commands for resolving and installing locked local wheels."""

from tools.container_guard import require_container

require_container()

from collections.abc import Iterable
from pathlib import Path

from tools.installer import pip_command


def resolve_locked_wheels_command(python: str | Path, lock: Path, pools: Iterable[Path], report: Path) -> list[str]:
    """Build the offline dry-run command that records pip's wheel selections."""

    return [
        *pip_command(python),  # Use the worker's pinned pip in isolation
        "install",  # Invoke pip's install resolver
        "--dry-run",  # Select artifacts without installing them
        "--ignore-installed",  # Resolve the full lock even if packages are already installed
        "--quiet",  # Suppress routine resolver output
        "--disable-pip-version-check",  # Skip the pip update probe
        "--no-cache-dir",  # Select files from the supplied pools
        "--no-index",  # Resolve without contacting package indexes
        "--no-deps",  # Resolve only the explicitly locked closure
        "--require-hashes",  # Check every selected artifact against the lock
        "--only-binary=:all:",  # Accept wheels only
        *[argument for directory in pools for argument in ("--find-links", str(directory))],  # Search each admitted local pool
        "--report",  # Record the exact artifact selections
        str(report),  # Keep the selection report in private scratch
        "-r",  # Read versions and hashes from the lock
        str(lock),  # Use the validated snapshot of the lock
    ]


def install_locked_wheels_command(
    python: str | Path,
    lock: Path,
    pool: Path,
    report: Path,
    extra_pools: Iterable[Path],
) -> list[str]:
    """Build the offline installation command that records installed wheels."""

    return [
        *pip_command(python),  # Run the target's pinned pip in isolation
        "install",  # Materialize the locked environment
        "--ignore-installed",  # Require fresh selection of every locked artifact
        "--disable-pip-version-check",  # Skip the pip update probe
        "--no-cache-dir",  # Use the supplied wheels instead of pip's cache
        "--no-index",  # Resolve only from local sources
        "--no-deps",  # Install only the explicitly locked closure
        "--only-binary=:all:",  # Reject source distributions
        "--require-hashes",  # Verify the lock hashes before installation
        "--no-compile",  # Leave Python bytecode unmaterialized
        "--report",  # Record which artifacts pip actually installed
        str(report),  # Write the evidence to the caller's report
        # Give pip a find-links entry for each admitted local pool
        *[
            argument
            for directory in (pool, *extra_pools)
            for argument in ("--find-links", str(directory))
        ],
        "-r",  # Read exact versions and hashes from the lock
        str(lock),  # Select the lock for this environment
    ]
