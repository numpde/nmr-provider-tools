"""One identity operation for Python and shell callers; no shell-side map parsing."""

import argparse
from dataclasses import asdict, replace
import json
import re
import signal
import sys
import threading

from .linux import LinuxError
from .model import Problem, ProbeFailure, ProbeRequest, ResourceState, Retirement, StepError
from .operation import probe_identity


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, allow_abbrev=False)

    def error(self, _message):
        raise StepError("invalid-policy", "invalid or missing arguments; use --help")


def _integer(text: str) -> int:
    if re.fullmatch(r"[0-9]{1,10}", text) is None or int(text) >= 2**32 - 1:
        raise argparse.ArgumentTypeError("expected a usable unsigned Linux ID")
    return int(text)


def _parser():
    parser = _Parser(prog="provider-deploy")
    operations = parser.add_subparsers(dest="operation", required=True, parser_class=_Parser)
    identity = operations.add_parser("identity", help="measure host IDs and retire the owned probe")
    identity.add_argument("--endpoint", required=True, help="already-selected unix:/// Docker endpoint")
    identity.add_argument("--image", required=True, help="approved image already present on that engine")
    identity.add_argument("--interpreter", required=True, help="absolute Python path inside that image")
    identity.add_argument("--user-uid", required=True, type=_integer)
    identity.add_argument("--user-gid", required=True, type=_integer)
    identity.add_argument("--userns", choices=("default", "host"))
    identity.add_argument("--uid", action="append", type=_integer)
    identity.add_argument("--gid", action="append", type=_integer)
    identity.add_argument("--values", action="store_true", help="print only projected decimal IDs for shell callers")
    identity.add_argument("--timeout", type=float, help="override the measurement time bound in seconds")
    return parser


# silent-defaults: allow-contract -- Successful JSON output is the emission baseline; callers explicitly mark failures to route values-mode diagnostics to stderr; tests/test_cli.py::CliTests.
def _emit(payload, *, values=None, failed=False):
    if values is not None and not failed:
        print(" ".join(map(str, values)))
    else:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")),
              file=sys.stderr if values is not None else sys.stdout)


def main(argv: list[str] | None = None) -> int:
    handlers = {}
    arguments = list(sys.argv[1:] if argv is None else argv)
    # silent-defaults: allow-contract -- --values routes parse errors to stderr before parsing can complete; no numbers are emitted on failure; tests/test_cli.py::CliTests.test_values_parse_failure_keeps_stdout_empty.
    values = [] if "--values" in arguments else None
    try:
        options = _parser().parse_args(arguments)
        # silent-defaults: allow-contract -- After valid parsing the boolean switch, rather than raw tokens, owns output routing; tests/test_cli.py::CliTests.test_values_are_only_requested_decimal_ids_in_order.
        values = [] if options.values else None
        # silent-defaults: allow-contract -- Absent optional projections mean no requested IDs, not an inferred mapping; tests/test_cli.py::CliTests.test_no_requested_ids_means_no_projections.
        uids = () if options.uid is None else options.uid
        # silent-defaults: allow-contract -- GID projections have the same explicit absent-means-none contract as UIDs; tests/test_cli.py::CliTests.test_no_requested_ids_means_no_projections.
        gids = () if options.gid is None else options.gid
        if len(uids) + len(gids) > 512:
            raise StepError("invalid-policy", "request at most 512 projected identities")
        request = ProbeRequest(options.endpoint, options.image, options.interpreter,
                               options.user_uid, options.user_gid)
        if options.userns is not None:
            request = replace(request, userns_mode=options.userns)
        if options.timeout is not None:
            request = replace(request, bounds=replace(request.bounds, operation_seconds=options.timeout))
        request.bounds.validate()
        cancellation = threading.Event()
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handlers[number] = signal.signal(number, lambda _number, _frame: cancellation.set())
        result = probe_identity(request, cancel=cancellation)
        try:
            projected_uids = [result.uid_map.translate(uid) for uid in uids]
            projected_gids = [result.gid_map.translate(gid) for gid in gids]
        except LinuxError as error:
            raise ProbeFailure(Problem("project", error.category, str(error)),
                Retirement(ResourceState.RETIRED, result.container_id)) from error
        payload = result.as_dict()
        payload.update(mapped_uids=projected_uids, mapped_gids=projected_gids)
        if values is not None:
            values = projected_uids + projected_gids
        _emit(payload, values=values)
        return 0
    # silent-defaults: allow-exception-outcome -- Structured operation failure always produces a positive exit status and never releases maps; tests/test_cli.py::CliTests.
    except ProbeFailure as failure:
        _emit(failure.as_dict(), values=values, failed=True)
        if failure.retirement.state == ResourceState.UNRESOLVED:
            return 3
        # silent-defaults: allow-contract -- Cancellation has shell status 130; all other settled operation failures remain nonzero status 1; tests/test_cli.py::CliTests.
        return 130 if failure.problem.category == "cancelled" else 1
    # silent-defaults: allow-exception-outcome -- Invalid caller policy is a structured refusal with status 2 and known-not-created state; tests/test_cli.py::CliTests.test_missing_arguments_fail_before_creation.
    except StepError as failure:
        _emit({"ok": False, "problem": asdict(Problem("policy", failure.category, str(failure))),
               "state": ResourceState.NOT_CREATED}, values=values, failed=True)
        return 2
    finally:
        for number, previous in handlers.items():
            signal.signal(number, previous)
