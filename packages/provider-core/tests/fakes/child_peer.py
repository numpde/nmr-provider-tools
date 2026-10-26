"""A private scripted peer: distinguish output closure from process exit.

This is not a scientific worker or a sibling's wire protocol. Its preparation
acknowledgement establishes ordering only; no chemical admission is performed.
"""

from provider_core.container_guard import require_container

require_container()

import json
import os
import signal
import sys
from unittest.mock import patch

from provider_core.uploads import VerifiedUploadFile, read_upload_file


def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def main():
    mode = sys.argv[1]
    if mode not in {"success", "reject", "block", "close_output", "block_read"}:
        raise ValueError("Unknown scripted child mode")
    source = json.loads(sys.stdin.readline())
    if source["operation"] != "prepare":
        raise ValueError("Scripted child expected preparation before generation")
    if mode == "reject":
        emit({"state": "rejected"})
        signal.pause()
    prepared = {"state": "prepared", "source_fingerprint": source["source_fingerprint"]}
    if "upload" in source:
        receipt = VerifiedUploadFile(**source["upload"])
        if mode == "block_read":
            read = os.read

            def hold_read(descriptor, maximum):
                chunk = read(descriptor, maximum)
                emit({"state": "reading", "byte_length": len(chunk)})
                signal.pause()
                raise RuntimeError("The scripted source-read hold unexpectedly resumed")

            # Pause inside the real snapshot call, after its first actual read
            # and before source close or a prepared handle can be returned.
            with patch("provider_core.uploads.os.read", side_effect=hold_read):
                read_upload_file(receipt, directory_fd=source["directory_fd"], maximum_bytes=receipt.byte_length)
            raise RuntimeError("The scripted source-read hold unexpectedly completed")
        snapshot = read_upload_file(receipt, directory_fd=source["directory_fd"], maximum_bytes=receipt.byte_length)
        # The marker comes from the verified acquired bytes, never the request's
        # Job fingerprint or an echoed expected marker. This is JSON, not science.
        prepared.update(content_hash=snapshot.content_hash, marker=json.loads(snapshot.data)["marker"])
    emit(prepared)
    request = json.loads(sys.stdin.readline())
    if request != {"operation": "generate"}:
        raise ValueError("Scripted child expected exactly one generation request")
    emit({"state": "generating"})
    if mode == "success":
        emit({"state": "result", "source_fingerprint": source["source_fingerprint"], "value": "scripted-only"})
    elif mode == "close_output":
        # A deliberate EOF is not an exit. Keep this exact child alive until its
        # owner kills it, even when the controller closes the other pipe ends.
        os.close(sys.stdout.fileno())
    signal.pause()


if __name__ == "__main__":
    main()
