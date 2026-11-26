"""Refresh SDK requirements locks from their maintained direct dependency intent."""

from tools.container_guard import require_container

require_container()

import argparse
import pathlib
import subprocess
import sys
import tempfile
import tomllib


def projects(source):
    """Derive each environment from its maintained direct requirements."""
    package = tomllib.loads((source / "packages/provider-core/pyproject.toml").read_text())
    project = package["project"]
    runtime = project["dependencies"] + project["optional-dependencies"]["api"]
    build = package["build-system"]["requires"]

    test = [
        line.strip()
        for line in (source / "packages/provider-core/tests/requirements.in").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    roles = {}

    for (role, dependencies) in (("runtime", runtime), ("build", build), ("test", test)):
        roles["provider-core-" + role] = "\n".join(dependencies) + "\n"

    return roles


def refresh(source, output):
    """Stage every requirements lock before the host publishes any of them."""
    with tempfile.TemporaryDirectory(prefix="sdk-lock-intent-") as temporary:
        work = pathlib.Path(temporary)

        for (role, content) in projects(source).items():
            project = work / (role + ".in")
            project.write_text(content)
            subprocess.run(
                [
                    "uv",
                    "--no-config",
                    "--no-cache",
                    "pip",
                    "compile",
                    str(project),
                    "--python",
                    sys.executable,
                    "--no-python-downloads",
                    "--only-binary=:all:",
                    "--default-index",
                    "https://pypi.org/simple",
                    "--generate-hashes",
                    "--no-header",
                    "--no-annotate",
                    "--output-file",
                    str(output / (role + ".lock")),
                ],
                check=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    arguments = parser.parse_args()

    refresh(arguments.source, arguments.output)


if __name__ == "__main__":
    main()
