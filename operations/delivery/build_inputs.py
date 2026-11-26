"""Capture one complete delivery lock and stream only its verified inputs."""

from tools.container_guard import require_container

require_container()

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

from tools.installer import release as installer_release
from sdk_build.provider_core_acquire import acquire_tool, tool_present


def read_json(json_path):
    with json_path.open(encoding="utf-8") as source:
        return json.load(source)


def wheelhouse(tool, action, lock, pool, *options):
    command_result = subprocess.run(
        [
            sys.executable,  # Use the caller's target interpreter.
            "-I",  # Ignore ambient Python settings.
            "-B",  # Keep captured inputs free of bytecode.
            str(tool),  # Execute the independently pinned bundle.
            action,  # Consume the complete selection.
            "--lock",  # Supply the authoritative captured lock.
            str(lock),  # Preserve this operation's selection.
            "--pool",  # Choose the shared wheel store.
            str(pool),  # Reuse verified pooled bytes.
            *map(str, options),  # Request optional Wheelhouse projections.
        ],
        check=True,
        stdout=subprocess.PIPE,
    )

    return json.loads(command_result.stdout)


def lock_release(tool, project, pool, lock, offline):
    """Resolve the declared delivery role on the actual delivery interpreter."""

    if not offline:
        subprocess.run(
            [
                sys.executable,  # Use the delivery target interpreter.
                "-I",  # Ignore ambient Python settings.
                "-B",  # Suppress bytecode writes.
                str(tool),  # Execute the pinned external bundle.
                "acquire",  # Prepare the exact declared SDK wheel.
                "--project",  # Read the consumer-owned declaration.
                str(project),  # Select the complete delivery role intent.
                "--package",  # Prepare one exact prerequisite before resolution.
                "provider-core",  # Read the SDK's actual wheel metadata.
                "--pool",  # Select the shared wheel store.
                str(pool),  # Reuse or publish verified exact bytes.
            ],
            check=True,
        )

    command = [
        sys.executable,  # Use the delivery target interpreter.
        "-I",  # Ignore ambient Python settings.
        "-B",  # Suppress bytecode writes.
        str(tool),  # Execute the independently pinned bundle.
        "lock",  # Resolve one complete installation environment.
        "--project",  # Read the consumer-owned dependency declaration.
        str(project),  # Preserve the captured role intent.
        "--pool",  # Select prepared wheel metadata.
        str(pool),  # Reuse verified available bytes.
        "--lock",  # Write the authoritative complete lock.
        str(lock),  # Publish only after successful resolution.
    ]

    if offline:
        command.append("--offline")

    subprocess.run(command, check=True)


def capture_image_inputs(lock, pool, destination, tool):
    captured_lock = destination / "delivery.wheelhouse.json"

    shutil.copyfile(lock, captured_lock)
    selected = wheelhouse(tool, "check", captured_lock, pool)
    artifact_entries = selected["artifact_entries"]

    mappings = {
        "artifacts": [
            {"source_path": entry["source_path"], "sha256": entry["sha256"], "destination": "/wheels/" + entry["filename"]}
            for entry in artifact_entries
        ]
    }

    map_path = destination / "provider-core-destinations.json"

    map_path.write_text(json.dumps(mappings, sort_keys=True) + "\n")

    projected = wheelhouse(
        tool,
        "check",
        captured_lock,
        pool,
        "--requirements",
        destination / "provider-core-install.lock",
        "--destination-map",
        map_path,
    )

    if not (projected["artifact_entries"] == artifact_entries):
        raise ValueError(f"Cannot capture delivery inputs: {captured_lock = !s} changed artifact identities while rendering installation requirements.")

    template = destination / "operations/delivery/image/Dockerfile"
    marker = "# selected-wheel-inputs"
    contents = template.read_text()

    if not (contents.count(marker) == 1):
        raise ValueError(f"Cannot capture delivery Dockerfile: {template = !s} must contain exactly one {marker = !r} marker.")

    installer = installer_release(destination / "requirements/installer-release.json")
    artifacts = []
    copies = []

    for entry in artifact_entries:
        source = pathlib.Path(entry["source_path"])

        relative = "wheelhouse/" + entry["filename"]

        artifacts.append({"path": str(source), "destination": relative, "sha256": entry["sha256"], "executable": False})
        copies.append("COPY " + json.dumps([relative, "/" + entry["filename"]]))

    installer_destination = "installer/" + installer["name"]
    installer_source = pool / installer["name"]

    artifacts.append({"path": str(installer_source), "destination": installer_destination, "sha256": installer["sha256"], "executable": False})
    copies.append("COPY " + json.dumps([installer_destination, "/" + installer_destination]))
    (template.parent / "Dockerfile.selected").write_text(contents.replace(marker, "\n".join(copies)))
    (destination / "provider-core-stream.json").write_text(json.dumps(artifacts, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("action", choices=("check", "acquire", "capture", "lock"))
    parser.add_argument("--lock", type=pathlib.Path)
    parser.add_argument("--pool", type=pathlib.Path)
    parser.add_argument("--tool-pin", type=pathlib.Path)
    parser.add_argument("--destination", type=pathlib.Path)
    parser.add_argument("--project", type=pathlib.Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    if any((argument is None) for argument in (args.pool, args.tool_pin, args.lock)):
        parser.error("check, acquire, capture and lock require --pool, --tool-pin and --lock")

    pin = read_json(args.tool_pin)

    if args.action in ("acquire", "lock"):
        tool = acquire_tool(args.pool, pin)
    elif not tool_present(args.pool, pin):
        raise FileNotFoundError(f"Cannot verify delivery inputs: acquire the pinned Wheelhouse bundle {pin['name'] = !r} first.")
    else:
        tool = args.pool / pin["name"]

    if args.action == "check":
        wheelhouse(tool, "check", args.lock, args.pool)
        print("complete")
    elif args.action == "acquire":
        wheelhouse(tool, "acquire", args.lock, args.pool)
    elif args.action == "lock":
        if args.project is None:
            parser.error("lock requires --project")

        lock_release(tool, args.project, args.pool, args.lock, args.offline)
    else:
        if args.destination is None:
            parser.error("capture requires --destination")

        capture_image_inputs(args.lock, args.pool, args.destination, tool)


if __name__ == "__main__":
    main()
