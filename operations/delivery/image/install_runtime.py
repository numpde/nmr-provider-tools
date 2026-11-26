"""Install the complete delivery projection with the image-owned pip."""

import pathlib
import shutil
import subprocess

from tools.installer import create_environment


root = pathlib.Path("/opt/provider-core")
python = create_environment(root / "environment")
lock = pathlib.Path("/opt/build/provider-core-install.lock")

subprocess.run(
    [
        str(python),  # Install into the image-owned environment.
        "-I",  # Ignore ambient Python settings.
        "-B",  # Suppress bytecode writes.
        "-m",  # Use the environment's own pinned installer.
        "pip",  # Select pinned pip.
        "install",  # Install the complete rendered selection.
        "--no-index",  # Forbid publisher access.
        "--no-deps",  # Preserve the checked complete selection.
        "--require-hashes",  # Verify bytes again at installation.
        "--no-cache-dir",  # Keep image layers free of installer caches.
        "--requirement",  # Consume Wheelhouse-rendered direct references.
        str(lock),  # Read the captured installation projection.
    ],
    check=True,
)

subprocess.run([str(python), "-I", "-B", "-m", "pip", "check"], check=True)
shutil.copyfile(lock, root / "provider-core-install.lock")
