"""Run one typed scientific call with owned cleanup and bounded recovery."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from asyncio import get_running_loop, sleep, timeout_at
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

from provider_core.contribution import CallBinding, NoInput, ref
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.failure_collection import FailureCollector

from provider_core.hosting import (
    AdmissionCode,
    AllocationRef,
    CapacityUnavailable,
    Invocation,
    NotAdmitted,
    OperationPolicy,
    Outcome,
    ResultConsumption,
    settle_cleanup,
)

from provider_core.process_host import ProcessExecutionHost
from provider_core.submission import Submission
from provider_core.tools import require_positive_number
from provider_core.type_requirements import require_type


log = logging.getLogger(__name__)


class AdmissionRejected(RuntimeError):
    """Confirmed non-admission, retaining the SDK's original refusal record."""

    def __init__(self, refusal: NotAdmitted) -> None:
        self.refusal = refusal

        super().__init__(f"Scientific call was not admitted: {refusal.code.value=}, {refusal.message=}")


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorPolicy:
    """One attempt raises; additional attempts retry only selected failures.

    Backoff doubles up to its cap. Scientific retries explicitly authorize
    repeating a declared retryable calculation after its analysis has closed
    successfully. The operation declaration selects exact eligible failures.
    Transport uncertainty, invalid input, decoding errors, cancellation and
    cleanup failures never authorize another calculation. total_seconds bounds
    the whole invocation, including recovery and backoff. Expiration prevents
    further work but still waits for owned cleanup to settle.
    """

    max_attempts: int = 1
    backoff_initial_seconds: float = 0.1
    backoff_max_seconds: float = 5
    retry_admission_codes: frozenset[AdmissionCode] = frozenset({AdmissionCode.CAPACITY, AdmissionCode.UNAVAILABLE})
    retry_scientific_failures: bool = False
    total_seconds: float | None = None

    def __post_init__(self) -> None:
        require_type(self.max_attempts, int)

        if not (self.max_attempts >= 1):
            raise ValueError(f"{self.max_attempts=} should include at least the initial attempt")

        require_positive_number(self.backoff_initial_seconds)
        require_positive_number(self.backoff_max_seconds)

        if not (self.backoff_initial_seconds <= self.backoff_max_seconds):
            raise ValueError(f"{self.backoff_initial_seconds=} should not exceed {self.backoff_max_seconds=}")

        require_type(self.retry_admission_codes, frozenset)

        for code in self.retry_admission_codes:
            require_type(code, AdmissionCode)

        require_type(self.retry_scientific_failures, bool)

        if self.total_seconds is not None:
            require_positive_number(self.total_seconds)


class ScientificCall[InputT, OutputT]:
    """A reusable typed method whose invocations each own one analysis.

    The host retains the loaded contribution between invocations. This interface
    returns a decoded value only after its analysis and results are released.
    Calls exchanging live state handles need an explicitly shared analysis and
    use the lower-level submission API instead.
    """

    def __init__(
        self,
        *,
        host: InProcessExecutionHost | ProcessExecutionHost,
        allocation: AllocationRef,
        binding: CallBinding[InputT, object, OutputT],
        operation_policy: OperationPolicy,
        error_policy: ErrorPolicy,
    ) -> None:
        if not (binding.description.state_inputs == binding.description.state_outputs == ()):
            raise ValueError(f"{binding.description.name=} exchanges state handles and requires a shared analysis")

        self.host = host
        self.allocation = allocation
        self.binding = binding
        self.operation_policy = operation_policy
        self.error_policy = error_policy

    # silent-defaults: allow-contract -- NoInput denotes absent payload; absent receiver requests a typed result; absent permission callback is for calls outside an API Attempt
    async def __call__(
        self,
        *,
        scientific_input: InputT | NoInput = NoInput(),
        receive: Callable[[bytes], Awaitable[None]] | None = None,
        require_execution: Callable[[], None] | None = None,
    ) -> OutputT:
        """Return one decoded result after cleanup, or raise the original failures.

        Encode once so retries use the same input snapshot. A retry starts only
        after successful cleanup; cancellation also waits for cleanup before
        propagating. An optional receiver accepts the encoded result once; its
        failure never authorizes resend, and cancellation joins its original work.
        Cleanup allowances flag overdue work rather than abandon it. An optional
        permission check runs before acquiring each analysis, including retries.
        """
        deadline = None

        if self.error_policy.total_seconds is not None:
            deadline = get_running_loop().time() + self.error_policy.total_seconds

        payload = self.binding.encode_input(value=scientific_input)

        return await self._invoke(
            payload=payload,
            receive=receive,
            require_execution=require_execution,
            deadline=deadline,
        )

    async def _invoke(self, *, payload, receive, require_execution, deadline):
        delay = self.error_policy.backoff_initial_seconds
        previous_failures = []

        try:
            for attempt in range(1, self.error_policy.max_attempts + 1):
                failures = FailureCollector(message="Scientific call and cleanup failed")
                analysis = None
                retry_failure = None
                cleanup_succeeded = False
                scientific_failure = False

                with failures.capture():
                    async with timeout_at(deadline):
                        self._require_budget(deadline=deadline)
                        # An API job can be cancelled while this call waits to retry.
                        # Its Attempt owner supplies this check, so every attempt confirms
                        # permission before acquiring resources or submitting science.
                        # A refused check propagates without another retry
                        if require_execution is not None:
                            require_execution()

                        self._require_budget(deadline=deadline)

                        try:
                            analysis = self.host.open_analysis(allocations=frozenset({self.allocation}), retain_results=False)
                        except CapacityUnavailable as error:
                            if AdmissionCode.CAPACITY in self.error_policy.retry_admission_codes:
                                retry_failure = error

                            raise

                        submission = Submission(
                            analysis_executor=analysis,
                            invocation=Invocation(
                                target=ref(call=self.binding),
                                payload=payload,
                                states={},
                                reservation=analysis.reservation(allocation=self.allocation),
                                policy=self.operation_policy,
                            ),
                        )

                        self._require_budget(deadline=deadline)

                        operation = submission.submit()

                        match operation:
                            case NotAdmitted() as refusal:
                                rejection = AdmissionRejected(refusal)

                                if refusal.code in self.error_policy.retry_admission_codes:
                                    retry_failure = rejection

                                raise rejection

                        observation = await submission.wait()

                        try:
                            result_ref = observation.require_result()
                        except Exception as error:
                            if (
                                self.error_policy.retry_scientific_failures and
                                (observation.outcome is Outcome.FAILED) and
                                observation.failures and
                                all(((failure.phase == "calculation") and failure.retryable) for failure in observation.failures)
                            ):
                                retry_failure = error
                                scientific_failure = True

                            raise

                        if receive is None:
                            encoded = await operation.read_result(result_ref)
                            decoded = self.binding.result_codec.decode(encoded)
                        else:
                            async def accept(encoded: bytes) -> None:
                                nonlocal decoded

                                decoded = self.binding.result_codec.decode(encoded)

                                await receive(encoded)

                            async with ResultConsumption(operation=operation, result=result_ref, receive=accept) as consumption:
                                await consumption.consume()

                with failures.capture():
                    if analysis is not None:
                        await settle_cleanup(control=analysis.close())

                    cleanup_succeeded = True

                try:
                    failures.raise_if_any()
                except Exception as error:
                    if not (
                        (error is retry_failure) and
                        (cleanup_succeeded) and
                        (attempt < self.error_policy.max_attempts)
                    ):
                        raise

                    log.warning(
                        f"Retrying scientific call {self.binding.description.name=}: "
                        f"{attempt=}, {self.error_policy.max_attempts=}, {delay=}; {error=}"
                    )

                    previous_failures.append(error)

                    await self._recover(
                        delay=delay,
                        deadline=deadline,
                        require_execution=require_execution,
                        scientific_failure=scientific_failure,
                    )

                    delay = min(delay * 2, self.error_policy.backoff_max_seconds)
                else:
                    return decoded

        except BaseException as terminal:
            # Preserve the terminal exception's type and identity, including
            # cancellation, while retaining earlier attempts and its own cause
            if not previous_failures:
                raise

            cause = terminal.__cause__

            if (cause is None) and (not terminal.__suppress_context__):
                cause = terminal.__context__

            earlier = tuple({
                id(failure): failure
                for failure in (*previous_failures, cause)
                if (failure is not None) and (failure is not terminal)
            }.values())

            if len(earlier) == 1:
                raise terminal from earlier[0]

            if earlier:
                raise terminal from BaseExceptionGroup("Earlier scientific attempts and terminal failure cause", earlier)

            raise

    async def _recover(self, *, delay, deadline, require_execution, scientific_failure):
        async with timeout_at(deadline):
            self._require_budget(deadline=deadline)

            await sleep(delay)

    @staticmethod
    def _require_budget(*, deadline: float | None) -> None:
        # An expired asyncio timeout cancels only at the next suspension; check
        # before synchronous admission can start another scientific operation
        if (deadline is not None) and ((now := get_running_loop().time()) >= deadline):
            raise TimeoutError(f"Scientific call budget expired: {now=}, {deadline=}")
