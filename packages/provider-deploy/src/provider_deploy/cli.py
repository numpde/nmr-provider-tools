"""Thin structured CLI for the same complete Python operation."""

import argparse
import json
from pathlib import Path
import re
import signal
import sys
import threading

from .model import Bounds, ProbeFailure, ProbeRequest, StepError
from .operation import probe_identity


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse includes only caller-owned argument details; keep the external
        # failure schema stable and do not include any arbitrary engine output.
        raise StepError("invalid-policy", "invalid or missing command arguments; use --help")


def _integer(text: str) -> int:
    if re.fullmatch(r"[0-9]+", text) is None or len(text) > 10:
        raise argparse.ArgumentTypeError("expected a bounded unsigned decimal integer")
    return int(text)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="provider-deploy", description="Bounded host-side deployment operations")
    operations = parser.add_subparsers(dest="operation", required=True, parser_class=_Parser)
    identity = operations.add_parser("identity", help="observe requested IDs and retire the exact owned probe")
    identity.add_argument("--context", required=True)
    identity.add_argument("--image", required=True, help="approved image already present on the selected engine")
    identity.add_argument("--user-uid", required=True, type=_integer)
    identity.add_argument("--user-gid", required=True, type=_integer)
    identity.add_argument("--userns", required=True, choices=("default", "host"))
    identity.add_argument("--map-uid", required=True, action="append", type=_integer)
    identity.add_argument("--map-gid", required=True, action="append", type=_integer)
    identity.add_argument("--journal", required=True, type=Path, help="new absolute file in an existing caller-owned 0700 directory")
    identity.add_argument("--operation-seconds", required=True, type=float)
    identity.add_argument("--command-seconds", required=True, type=float)
    identity.add_argument("--cleanup-seconds", required=True, type=float)
    identity.add_argument("--lifetime-seconds", required=True, type=_integer)
    return parser


def main(argv: list[str] | None = None) -> int:
    handlers = {}
    try:
        options = _parser().parse_args(argv)
        cancellation = threading.Event()
        def request_cancellation(_number, _frame):
            cancellation.set()
        for number in (signal.SIGINT, signal.SIGTERM):
            handlers[number] = signal.signal(number, request_cancellation)
        request = ProbeRequest(options.context, options.image, options.user_uid, options.user_gid,
            options.userns, tuple(options.map_uid), tuple(options.map_gid), options.journal,
            Bounds(options.operation_seconds, options.command_seconds, options.cleanup_seconds,
                   options.lifetime_seconds))
        payload = probe_identity(request, cancel=cancellation).as_dict()
        status = 0
    except ProbeFailure as failure:
        payload = failure.as_dict()
        status = 130 if failure.category == "cancelled" else 3 if failure.state == "uncertain-outcome" else 1
        print("Identity probe failed; no mapping was released. Inspect the structured recovery evidence.", file=sys.stderr)
    except StepError as failure:
        payload = {"ok": False, "category": failure.category, "message": str(failure), "state": "known-not-created"}
        status = 2
    finally:
        for number, previous in handlers.items():
            signal.signal(number, previous)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 131072:
        # All inputs/maps have tighter bounds; this is a last serialization guard.
        was_successful = payload["ok"] is True
        limited = {"ok": False, "category": "output-limit",
                   "state": "confirmed-retired" if was_successful else payload["state"],
                   "operation_token": payload.get("operation_token"),
                   "container_id": payload.get("container_id"),
                   "journal_path_omitted": True}
        print(json.dumps(limited, sort_keys=True, separators=(",", ":")))
        return 1 if was_successful else status
    print(encoded)
    return status
