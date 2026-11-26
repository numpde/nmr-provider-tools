"""Resolve complete SDK roles from their maintained direct dependency intent."""

from tools.container_guard import require_container

require_container()

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import tomllib

from sdk_build.provider_core_acquire import acquire_tool


def projects(source):
    """Derive runtime, build and test roles; keep exact bootstrap intent intact."""
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

    for (role, dependencies) in (("runtime", runtime), ("build", build), ("test", runtime + test)):
        roles["provider-core-" + role] = (
            "[project]\n"
            + "dependencies = " + json.dumps(dependencies) + "\n"
            + "requires-python = " + json.dumps(project["requires-python"]) + "\n"
            + "\n"
            + "[tool.wheelhouse]\n"
            + 'index = "https://pypi.org/simple"\n'
        )

    roles["acquisition-bootstrap"] = (source / "requirements/acquisition-bootstrap.toml").read_text()

    return roles


def refresh(source, pool, output):
    """Stage every complete role before the host publishes any maintained lock."""
    pin = json.loads((source / "requirements/wheelhouse-tool-release.json").read_text())
    tool = acquire_tool(pool, pin)
    command = [sys.executable, "-I", "-B", str(tool)]

    with tempfile.TemporaryDirectory(prefix="sdk-lock-intent-") as temporary:
        work = pathlib.Path(temporary)

        for (role, content) in projects(source).items():
            project = work / (role + ".toml")

            project.write_text(content)

            # Exact bootstrap wheels must be prepared before metadata-only locking.
            if role == "acquisition-bootstrap":
                bootstrap_sources = tomllib.loads(content)["tool"]["wheelhouse"]["sources"]

                for name in bootstrap_sources:
                    subprocess.run(
                        [
                            *command,
                            "acquire",
                            "--project",
                            str(project),
                            "--package",
                            name,
                            "--pool",
                            str(pool),
                        ],
                        check=True,
                    )

            subprocess.run(
                [
                    *command,
                    "lock",
                    "--project",
                    str(project),
                    "--pool",
                    str(pool),
                    "--lock",
                    str(output / (role + ".wheelhouse.json")),
                ],
                check=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--pool", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    arguments = parser.parse_args()

    refresh(arguments.source, arguments.pool, arguments.output)


if __name__ == "__main__":
    main()
