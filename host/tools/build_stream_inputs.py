"""Describe captured build inputs for the wheelhouse streamer."""

import hashlib
import json
import sys
from pathlib import Path


def main():
    context = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    entries = []
    for name in sys.argv[3:]:
        path = context / name
        entries.append({"path": str(path), "destination": name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "executable": bool(path.stat().st_mode & 0o111)})
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(entries, stream, sort_keys=True)
        stream.write("\n")
    pin = json.loads((context / "requirements/wheelhouse-tool-release.json").read_text())
    print(pin["name"], pin["sha256"])


if __name__ == "__main__":
    main()
