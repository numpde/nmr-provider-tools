"""Download pinned bootstrap source using the reviewed maintenance package.

Release publication has its own owner under operations/release. This maintenance
command needs only Python's standard library and a public HTTPS download.
"""

import argparse
import json
import pathlib
import tempfile

from acquisition_tool import source_update
from acquisition_tool.adapters.public_release import download_asset


def update(source_dir: pathlib.Path, pin_file: pathlib.Path) -> dict:
    """Replace clean checked-in source through the maintenance package's importer."""

    source = pathlib.Path(source_dir).absolute()

    source_update.verify(source)
    pin = source_update.read_pin(pin_file)
    name = f"acquisition-owner-{pin['version']}.tar.gz"  # machine-string: acquisition source asset filename
    published = False

    try:
        with tempfile.TemporaryDirectory(prefix="provider-tools-release-") as temporary:
            archive = pathlib.Path(temporary) / name
            captured_pin = pathlib.Path(temporary) / "release-pin.json"

            # The offline importer must use the pin selected before the transfer
            captured_pin.write_text(json.dumps(pin), encoding="ascii")

            tag = f"v{pin['version']}"  # machine-string: selected release tag

            download_asset("numpde/nmr-provider-tools", tag, name, archive, timeout_seconds=60)
            manifest = source_update.import_archive(source, captured_pin, archive)
            published = True
    except BaseException as error:
        if published:
            error.add_note(f"Acquisition source was published at {source=}; release-download cleanup failed.")

        raise

    return manifest


def main() -> None:
    """Download and authenticate the selected bootstrap update explicitly."""

    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--source-dir", type=pathlib.Path, required=True)
    parser.add_argument("--pin-file", type=pathlib.Path, required=True)
    args = parser.parse_args()

    update(args.source_dir, args.pin_file)


if __name__ == "__main__":
    main()
