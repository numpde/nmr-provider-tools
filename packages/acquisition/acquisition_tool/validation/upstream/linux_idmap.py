"""Interpret retirement evidence reported by linux-idmap 0.3.4."""

from linux_idmap.probe import ProbeFailure, ResourceState


def probe_failure_retirement_confirmed(failure: ProbeFailure) -> bool:
    """Require both container retirement and native client retirement."""

    # The library reports native client cleanup separately from container state
    # See linux-idmap v0.3.4 _command.py and _probe_scope.py
    problems = (failure.problem, *failure.retirement.problems)

    return (
        (failure.retirement.state in (ResourceState.NOT_CREATED, ResourceState.RETIRED)) and
        all((problem.category != "client_cleanup") for problem in problems)
    )
