"""Capture source and artifact entries for the shared context exporter."""

import hashlib
import json
import pathlib
import sys

def read_json(json_path):
    return json.loads(json_path.read_text())


def context_manifest(context, destination, pool, inputs):
    """Combine captured source and checked artifact entries without copying wheels."""
    context_entries = read_json(context / "provider-core-stream.json")

    # The checker sees the shared pool at /wheelhouse. Translate only that mount
    # into the host's source path after fingerprinting the portable capture.
    for entry in context_entries:
        source = pathlib.Path(entry["path"])

        if source.is_relative_to("/wheelhouse"):
            entry["path"] = str(pool / source.relative_to("/wheelhouse"))

    for name in inputs:
        source = context / name
        checksum = hashlib.sha256()

        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                checksum.update(chunk)

        context_entries.append({"path": str(source), "destination": name, "sha256": checksum.hexdigest(), "executable": bool(source.stat().st_mode & 0o111)})

    destination.write_text(json.dumps(context_entries, sort_keys=True) + "\n")



if __name__ == "__main__":
    context_manifest(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), sys.argv[4:])
