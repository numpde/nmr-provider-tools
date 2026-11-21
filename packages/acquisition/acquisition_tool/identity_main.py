"""Project identities through one retired probe on the selected local engine."""

import argparse
import reprlib
import signal
import sys
import pathlib
from dataclasses import replace

from linux_idmap import Identifier
from linux_idmap.probe import ProbeFailure, ProbeRequest, ProbeResources, StepError, probe_identity

from acquisition_tool.validation.upstream.linux_idmap import probe_failure_retirement_confirmed


class Cancellation:
    def __init__(self):
        self.signal = 0

    def request(self, number, _frame):
        if not self.signal:
            self.signal = number

    def is_set(self):
        return bool(self.signal)


def _inputs(args):
    policy = {
        "interpreter": args.interpreter,
        "user_uid": args.user_uid,
        "user_gid": args.user_gid,
        "memory_bytes": args.memory_bytes,
        "pids_limit": args.pids_limit,
        "nano_cpus": args.nano_cpus,
    }

    if args.maps is None:
        supplied_policy_fields = [policy_name for (policy_name, policy_setting) in policy.items() if policy_setting is not None]

        if (
            (args.host_uid is None) or
            (args.host_gid is None) or
            (args.userns is not None) or
            supplied_policy_fields
        ):
            raise ValueError(
                f"{args.host_uid=} and {args.host_gid=} should be set; "
                f"{args.userns=} should be None; {supplied_policy_fields=} should be empty"
            )

        probe_request = ProbeRequest(args.endpoint, args.image, "/usr/local/bin/python3", 0, 0)

        return (
            probe_request,
            (
                ("uid", "inverse", Identifier(args.host_uid)),
                ("gid", "inverse", Identifier(args.host_gid))
            )
        )

    if (args.host_uid is not None) or (args.host_gid is not None):
        raise ValueError(f"{args.host_uid=} and {args.host_gid=} should both be None with --map")

    missing_policy_fields = [policy_name for (policy_name, policy_setting) in policy.items() if policy_setting is None]

    if missing_policy_fields:
        raise ValueError(f"{missing_policy_fields=} should be empty with --map")

    max_projections = 512

    if not (len(args.maps) <= max_projections):
        raise ValueError(f"{len(args.maps)=} should be at most {max_projections=}")

    projections = []
    expected_axes = ("uid", "gid")
    expected_directions = ("forward", "inverse")

    for (axis, direction, identifier) in args.maps:
        if not (axis in expected_axes):
            raise ValueError(f"{reprlib.repr(axis)=} should be one of {expected_axes=}")

        if not (direction in expected_directions):
            raise ValueError(f"{reprlib.repr(direction)=} should be one of {expected_directions=}")

        projections.append((axis, direction, Identifier.of(identifier)))

    probe_request = ProbeRequest(
        args.endpoint,
        args.image,
        args.interpreter,
        args.user_uid,
        args.user_gid,
        resources=ProbeResources(args.memory_bytes, args.pids_limit, args.nano_cpus),
    )

    if args.userns is not None:
        probe_request = replace(probe_request, userns_mode=args.userns)

    return (probe_request, projections)


def _project(observation, projections):
    translated_ids = []

    for (axis, direction, identifier) in projections:
        if axis == "uid":
            mapping = observation.uid_map
        else:
            mapping = observation.gid_map

        if direction == "forward":
            translated_ids.append(mapping.translate(identifier))
        else:
            translated_ids.append(mapping.translate_inverse(identifier))

    return translated_ids


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--host-uid", type=int)
    parser.add_argument("--host-gid", type=int)
    parser.add_argument("--map", dest="maps", action="append", nargs=3, metavar=("AXIS", "DIRECTION", "ID"))
    parser.add_argument("--interpreter")
    parser.add_argument("--user-uid", type=int)
    parser.add_argument("--user-gid", type=int)
    parser.add_argument("--userns", choices=("default", "host"))
    parser.add_argument("--memory-bytes", type=int)
    parser.add_argument("--pids-limit", type=int)
    parser.add_argument("--nano-cpus", type=int)
    parser.add_argument("--retired-receipt", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    cancellation = Cancellation()
    previous = {number: signal.signal(number, cancellation.request) for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
    retired = False
    status = 0

    try:
        retired = True
        (probe_request, projections) = _inputs(args)
        retired = False
        observation = probe_identity(probe_request, cancel=cancellation)
        retired = True
        translated_ids = _project(observation, projections)
    except ProbeFailure as failure:
        retired = probe_failure_retirement_confirmed(failure)

        print(f"Cannot map engine identity: {failure.problem.message=}", file=sys.stderr)

        for problem in failure.retirement.problems:
            print(f"Probe cleanup: {problem.message=}", file=sys.stderr)

        if failure.handle is not None:
            container_id = failure.handle.container_id

            if not container_id:
                container_id = "not confirmed"

            print(
                f"{failure.handle.operation_token=}; "
                f"{container_id=}; "
                f"{failure.retirement.state=}",
                file=sys.stderr,
            )

        status = 2
    except (ValueError, StepError) as failure:
        print(f"Cannot map engine identity: {failure=!s}", file=sys.stderr)
        status = 2
    finally:
        for (number, handler) in previous.items():
            signal.signal(number, handler)

        if retired:
            with args.retired_receipt.open("x", encoding="ascii") as receipt:
                receipt.write("retired\n")

    # Receipt and restored caller handlers precede any usable projection output.
    if (status == 0) and (not cancellation.is_set()):
        sys.stdout.write(" ".join(map(str, translated_ids)) + "\n")

    return 128 + cancellation.signal if cancellation.is_set() else status


if __name__ == "__main__":
    raise SystemExit(main())
