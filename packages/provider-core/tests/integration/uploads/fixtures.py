"""Small authored ZIPs test source handling, without scientific interpretation."""

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile


def zip_bytes(entries):
    with BytesIO() as output:
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for name, contents in entries:
                archive.writestr(name, contents)
        return output.getvalue()
