"""Keep original Attempt owners until work and selected report duties settle."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from collections.abc import Callable, Iterable

from provider_core.controller import AttemptController
from provider_core.tools import require_positive_number


# silent-defaults: allow-contract -- Diagnostics are optional; failures always propagate after the owned duties settle.
async def drain_attempts(
    controllers: Iterable[AttemptController],
    *,
    retry_seconds: float,
    report_failure: Callable[[BaseException], None] | None = None,
) -> None:
    """Stop and settle the supplied owners, then propagate original failures.

    Callers keep native hosts, delivery and lifetime exclusion alive throughout
    this await. No deadline or cancellation authorizes losing an unaccepted
    report. Settlement observes existing work; it neither reruns science nor
    resubmits a permanently rejected report. Passive held claims can remain.

    The optional diagnostic recipient receives each retained failure once and
    owns its disclosure policy. Diagnostic failures are retained too. Repeated
    settlement failures are bounded to the first one per controller; the SDK
    controller retains subsequent cleanup evidence in its original failure.
    """
    require_positive_number(retry_seconds)
    controllers = tuple(dict.fromkeys(controllers))
    failures = []
    settlement_reported = set()
    interruption = None

    def remember(cause):
        if any(cause is previous for previous in failures):
            return

        failures.append(cause)

        if report_failure is not None:
            try:
                report_failure(cause)
            except BaseException as diagnostic:
                if all(diagnostic is not previous for previous in failures):
                    failures.append(diagnostic)

    for controller in controllers:
        try:
            controller.request_stop()
        except BaseException as cause:
            remember(cause)

    for controller in controllers:
        try:
            await controller.join()
        except BaseException as cause:
            remember(cause)

    while any((not controller.shutdown_ready) for controller in controllers):
        for controller in controllers:
            if controller.shutdown_ready:
                continue

            try:
                await controller.settle_for_shutdown()
            except BaseException as cause:
                if controller not in settlement_reported:
                    settlement_reported.add(controller)
                    remember(cause)

        if any((not controller.shutdown_ready) for controller in controllers):
            try:
                await asyncio.sleep(retry_seconds)
            except BaseException as cause:
                if interruption is None:
                    interruption = cause

                    remember(cause)

    if len(failures) == 1:
        raise failures[0]

    if failures:
        raise BaseExceptionGroup("Attempt shutdown retained original failures", failures) from None
