"""Run the dummy provider from an offline, native-only candidate installation."""

from tools.container_guard import require_container

require_container()

import argparse
import subprocess
import tempfile
from pathlib import Path

from tools.core_environment import build_candidate, install_environment


def run_example(instances: int) -> None:
    """Keep the candidate and fake jobs private to this disposable container."""
    with tempfile.TemporaryDirectory(prefix="core-example-") as temporary:
        root = Path(temporary)
        output = root / "candidate"
        output.mkdir()
        wheel, _, _ = build_candidate(
            Path("/package"), Path("/core-runtime.lock"), Path("/core-build.lock"), Path("/wheelhouse"), output,
        )
        native_lock = output / wheel.name.replace("-py3-none-any.whl", "-native-install.lock")
        python = install_environment(
            Path("/runtime"), native_lock, Path("/wheelhouse"),
            extra_pools=(output,), required_files={wheel.name: wheel},
        )
        # -I excludes the checkout; the native lock installs no API dependencies.
        outcome = subprocess.run(
            [str(python), "-I", "-B", "-m", "provider_core.examples.dummy_provider", "--instances", str(instances)],
            cwd=root, check=False,
        )
        if outcome.returncode:
            raise SystemExit(
                f"Dummy provider example failed (exit {outcome.returncode}); "
                "see the failure diagnostic immediately above."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", type=int, choices=(1, 2), required=True)
    options = parser.parse_args()
    run_example(options.instances)
