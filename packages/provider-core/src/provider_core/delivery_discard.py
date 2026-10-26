"""Inspect or discard held payloads offline, under the existing queue lock.

The operator must stop shared delivery first. Producer sockets deliberately have
no deletion route. No API request, retry, scientific execution or archive occurs.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.diagnostics import failure_evidence
from provider_core.startup_file import read_startup_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--api-origin", required=True)
    parser.add_argument("--provider-ref", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list-held", action="store_true")
    action.add_argument("--execution-attempt-ref")
    parser.add_argument("--body-sha256")
    options = parser.parse_args()
    if (not options.list_held and options.body_sha256 is None) or (options.list_held and options.body_sha256 is not None):
        parser.error("Supply the exact body SHA-256 for discard; list-held needs no payload selector.")
    os.umask(0o077)
    document = tomllib.loads(read_startup_file(
        options.config, maximum_bytes=65536, label="delivery configuration"
    ).decode("utf-8"))
    if document["schema_id"] != "provider-core.delivery.v1":
        parser.error("Cannot inspect or discard held results: use the installed delivery configuration (schema provider-core.delivery.v1).")
    matches = [binding for binding in document["bindings"]
               if (binding["api"]["origin"], binding["api"]["provider_ref"]) == (options.api_origin, options.provider_ref)]
    if len(matches) != 1:
        parser.error("Cannot inspect or discard held results: the supplied API/provider pair must match exactly one configured binding.")
    queue = DeliveryQueue(Path(document["state_directory"]), limits=QueueLimits(**document["limits"]))
    try:
        if options.list_held:
            items = queue.inventory(options.api_origin, options.provider_ref)
            print(json.dumps({"api_origin": options.api_origin, "provider_ref": options.provider_ref,
                              "outstanding_items": len(items),
                              "held": [item for item in items
                                       if item["state"] == "held"]}, sort_keys=True))
        else:
            queue.discard_held(options.api_origin, options.provider_ref, options.execution_attempt_ref, options.body_sha256)
    finally:
        queue.close()
    if not options.list_held:
        print("Discarded the matching held payload. This does not establish API acceptance or resolve the remote Attempt.")


if __name__ == "__main__":
    try:
        main()
    except Exception as failure:
        print("Cannot inspect or discard held delivery data. Explicitly stop shared delivery first; check the configured binding and any supplied Attempt/digest. No API request was sent.", file=sys.stderr)
        print(json.dumps(failure_evidence(failure, secrets=())), file=sys.stderr)
        raise SystemExit(2) from None
