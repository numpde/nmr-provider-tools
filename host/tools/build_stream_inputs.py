"""Describe captured build inputs for the wheelhouse streamer."""

import hashlib
import json
import pathlib
import sys


def main():
    context = pathlib.Path(sys.argv[1])
    destination = pathlib.Path(sys.argv[2])
    build_inputs = []

    for name in sys.argv[3:]:
        source_file = context / name

        build_inputs.append(
            {
                "path": str(source_file),
                "destination": name,
                "sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
                "executable": bool(source_file.stat().st_mode & 0o111)
            }
        )

    with destination.open("x", encoding="utf-8") as stream:
        json.dump(build_inputs, stream, sort_keys=True)
        stream.write("\n")

    pin = json.loads((context / "requirements/wheelhouse-tool-release.json").read_text())

    print(pin["name"], pin["sha256"])


if __name__ == "__main__":
    main()
