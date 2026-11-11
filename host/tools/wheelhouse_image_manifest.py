"""Project the target pip selection to streamed wheelhouse inputs."""

import json
import sys
from pathlib import Path


def main():
    selection = json.loads(Path(sys.argv[1]).read_text())
    installer = json.loads(Path(sys.argv[3]).read_text())
    with Path(sys.argv[2]).open("x", encoding="utf-8") as stream:
        json.dump([{"directory": "wheelhouse", "filename": name, "sha256": checksum}
                   for (name, checksum) in sorted(selection["files"].items())]
                  + [{"directory": "installer", "filename": installer["name"], "sha256": installer["sha256"]}], stream, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main()
