"""Exercise the typed contribution/execution boundary without Jobs."""

from __future__ import annotations

import asyncio
import threading
import unittest
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from provider_core.contribution import (
    AnalysisHooks,
    CallBinding,
    CallContext,
    CallRef,
    ConcurrencyRequirement,
    ImplementationBinding,
    InstanceHooks,
    InterruptionSupport,
    IsolationRequirement,
    NoState,
    ProgressEvent,
    ResourceRequirement,
    ScientificControl,
    ScientificReturn,
    description_document,
    ref,
)

from provider_core.embedded_host import Allocation, InProcessExecutionHost

from provider_core.hosting import (
    DispatchState,
    OperationPolicy,
    Invocation,
    NotAdmitted,
    Outcome,
    RequestConflict,
    ResultRef,
    ResultState,
)

from provider_core.payload import PayloadCodec
from tests.fakes.dummy_provider import science

from tests.fakes.dummy_provider.binding import (
    sum_binding,
    weighted_sum_binding,
)


def _host_options(**changes) -> dict[str, int | float]:
    host_options = {
        "input_bytes": 32768,
        "result_bytes": 32768,
        "state_values": 16,
        "progress_records": 8,
        "initialization_seconds": 5,
        "retirement_seconds": 5,
    }

    host_options.update(changes)

    return host_options


def _operation_policy() -> OperationPolicy:
    return OperationPolicy(
        queue_stop_request_seconds=5,
        execution_stop_request_seconds=10,
        cleanup_overdue_flag_seconds=5,
        reject_encoded_input_over_bytes=1024,
        reject_encoded_result_over_bytes=1024,
    )


# silent-defaults: allow-contract -- Stateless invocation witnesses carry no input states; stateful witnesses supply their exact retained references.
def _invocation(
    analysis,
    binding,
    *,
    operation_id: str,
    call_name: str,
    payload: bytes,
    states=None,
) -> Invocation:
    description = binding.description

    reservation = next(
        reservation
        for reservation in analysis.reservations
        if reservation.binding_id == description.binding_id
    )

    return Invocation(
        operation_id=operation_id,
        target=CallRef(
            binding_id=description.binding_id,
            binding_revision=description.binding_revision,
            name=call_name,
        ),
        payload=payload,
        states={} if states is None else states,
        reservation=reservation,
        policy=_operation_policy(),
    )


@dataclass(frozen=True, slots=True)
class _CountingCodec[T]:
    delegate: PayloadCodec[T]
    counter: dict[str, int]

    @property
    def format(self) -> str:
        return self.delegate.format

    @property
    def value_type(self) -> type[T]:
        return self.delegate.value_type

    def decode(self, payload: bytes) -> T:
        self.counter["decode"] += 1

        return self.delegate.decode(payload)

    def encode(self, value: T) -> bytes:
        return self.delegate.encode(value)


@dataclass(frozen=True, slots=True)
class _FailingResultCodec[T]:
    delegate: PayloadCodec[T]
    failure: BaseException

    @property
    def format(self) -> str:
        return self.delegate.format

    @property
    def value_type(self) -> type[T]:
        return self.delegate.value_type

    def decode(self, payload: bytes) -> T:
        return self.delegate.decode(payload)

    def encode(self, value: T) -> bytes:
        raise self.failure


class EmbeddedHostTests(unittest.IsolatedAsyncioTestCase):
    def host(self, bindings, *, options=None):
        host = InProcessExecutionHost(
            # repo-check: allow-tuple-generator -- Host validation and installation make separate passes; fixed membership prevents installation of an unvalidated binding
            allocations=tuple(
                Allocation(
                    name=name,
                    binding=binding,
                )
                for (name, binding) in bindings.items()
            ),
            **(options or _host_options()),
        )

        self.addAsyncCleanup(self.retire_host, host)

        return host

    async def retire_host(self, host) -> None:
        await self.settled(host.retire())

    async def settled(self, control):
        async with asyncio.timeout(10):
            observation = await control.observe(after=None, wait_seconds=0)

            while (not observation.settled) and (not observation.failures):
                observation = await control.observe(
                    after=observation.revision,
                    wait_seconds=1,
                )

        self.assertEqual((), observation.failures)
        self.assertTrue(observation.settled)

        return observation

    async def finished(self, operation):
        self.assertNotIsInstance(operation, NotAdmitted)

        async with asyncio.timeout(10):
            observation = await operation.observe(after=None, wait_seconds=0)

            while not observation.finished:
                observation = await operation.observe(
                    after=observation.revision,
                    wait_seconds=1,
                )

        return observation

    async def success(self, operation, codec: PayloadCodec):
        observation = await self.finished(operation)

        self.assertIs(Outcome.SUCCEEDED, observation.outcome)
        self.assertEqual((), observation.failures)
        self.assertIs(ResultState.AVAILABLE, observation.result_state)
        payload = await operation.read_result(observation.result)

        return (observation, codec.decode(payload))

    async def test_local_bound_submission_and_portable_submission_share_one_owner(self):
        binding = sum_binding()
        host = self.host({"one": binding})
        analysis = host.open_analysis("local-call", allocation_names=("one",))

        invocation = _invocation(
            analysis=analysis,
            binding=binding,
            operation_id="sum",
            call_name="run",
            payload=b'{"values":[4,5]}',
        )

        operation = analysis.submit_call(
            binding.run,
            payload=invocation.payload,
            states=invocation.states,
            reservation=invocation.reservation,
            policy=invocation.policy,
        )

        original_invocation = operation.invocation

        self.assertIs(operation, analysis.submit(original_invocation))

        another = analysis.submit_call(
            binding.run,
            payload=b'{"values":[6]}',
            states={},
            reservation=invocation.reservation,
            policy=invocation.policy,
        )

        self.assertIsNot(operation, another)
        (_, sum_result) = await self.success(operation, binding.run.result_codec)

        self.assertEqual(science.SumResult(total=9, count=2), sum_result)

    async def test_local_submission_rejects_unbound_and_nonmember_targets(self):
        binding = sum_binding()
        host = self.host({"one": binding})
        analysis = host.open_analysis("unbound", allocation_names=("one",))
        (reservation,) = analysis.reservations

        with self.assertRaisesRegex(ValueError, "before a configured binding owns it"):
            analysis.submit_call(
                replace(binding.run),
                payload=b'{"values":[1]}',
                states={},
                reservation=reservation,
                policy=_operation_policy(),
            )

        for target in ("run", ref(binding.run)):
            with self.subTest(target=target), self.assertRaises(TypeError):
                analysis.submit_call(
                    target,
                    payload=b'{"values":[1]}',
                    states={},
                    reservation=reservation,
                    policy=_operation_policy(),
                )

        self.assertEqual({}, host.operations)
        self.assertEqual(0, host.input_used)
        self.assertEqual(0, host.result_reserved)

    async def test_local_submission_preserves_revision_refusal_and_execution_stop(self):
        binding = sum_binding()
        host = self.host({"one": binding})
        analysis = host.open_analysis("local-stop", allocation_names=("one",))
        (reservation,) = analysis.reservations

        wrong_revision = replace(
            binding,
            identity=replace(
                binding.identity,
                implementation="dummy.sum.another",
            )
        )

        foreign = analysis.submit_call(
            wrong_revision.run,
            payload=b'{"values":[1]}',
            states={},
            reservation=reservation,
            policy=_operation_policy(),
        )

        self.assertIsInstance(foreign, NotAdmitted)
        self.assertEqual({}, host.operations)

        operation = analysis.submit_call(
            binding.run,
            payload=b'{"values":[1]}',
            states={},
            reservation=reservation,
            policy=_operation_policy(),
        )

        operation.request_stop()
        observed = await self.finished(operation)

        self.assertIs(DispatchState.NOT_STARTED, observed.dispatch)
        self.assertIsNone(observed.outcome)
        self.assertTrue(any(isinstance(failure.cause, PermissionError) for failure in observed.failures))

    async def test_description_and_direct_science_match_hosted_calls(self):
        settings = science.WeightedSumSettings(scale=2, offset=1)
        coefficients = science.CoefficientSet(values=(1, 2, 3))

        model = science.create_model(
            settings=settings,
            coefficients=coefficients,
        )

        science.load_model(model)
        direct_analysis = science.create_analysis(model)

        science.initialize_analysis(direct_analysis)

        prepared = science.prepare(
            analysis=direct_analysis,
            candidate=science.PreparationInput(values=(2, 4, 6)),
        )

        expected_result = science.calculate(
            analysis=direct_analysis,
            state=science.CalculationState(prepared=prepared),
        )

        science.dispose_analysis(direct_analysis)
        science.dispose_model(model)

        weighted = weighted_sum_binding(
            settings=settings,
            coefficients=coefficients,
        )

        ordinary = sum_binding()
        description = description_document(weighted.description)

        self.assertEqual("shared_process", description["isolation"])
        self.assertEqual(["calculate", "prepare"], [c["name"] for c in description["calls"]])
        self.assertIsNone(ordinary.description.configuration)

        host = self.host({"weighted": weighted, "sum": ordinary})

        analysis = host.open_analysis(
            "combined",
            allocation_names=("weighted", "sum"),
        )

        (prepare_call, calculate_call) = (weighted.prepare, weighted.calculate)

        (preparation, _) = await self.success(
            analysis.submit(
                _invocation(
                    analysis,
                    weighted,
                    operation_id="prepare",
                    call_name="prepare",
                    payload=b'{"values":[2,4,6]}',
                ),
            ),
            prepare_call.result_codec,
        )

        (calculation, actual_result) = await self.success(
            analysis.submit(
                _invocation(
                    analysis,
                    weighted,
                    operation_id="calculate",
                    call_name="calculate",
                    payload=b"{}",
                    states={"candidate": preparation.states[0].reference},
                ),
            ),
            calculate_call.result_codec,
        )

        self.assertEqual(expected_result, actual_result)

        self.assertEqual(
            (
                ProgressEvent(stage="calculation", completed=0, total=1),
                ProgressEvent(stage="calculation", completed=1, total=1),
            ),
            calculation.progress,
        )

        sum_call = ordinary.run

        (_, ordinary_result) = await self.success(
            analysis.submit(
                _invocation(
                    analysis,
                    ordinary,
                    operation_id="sum",
                    call_name="run",
                    payload=b'{"values":[2,4,6]}',
                ),
            ),
            sum_call.result_codec,
        )

        self.assertEqual(science.SumResult(total=12, count=3), ordinary_result)

    async def test_stop_and_operation_identity_prevent_unowned_replay(self):
        original = sum_binding()
        original_call = original.run
        entered = {"decode": 0, "invoke": 0}

        def scientific_call(
            context: CallContext[None, None, science.SumInput, NoState],
        ) -> ScientificReturn[science.SumResult]:
            entered["invoke"] += 1

            return original_call.scientific_call(context)

        wrapped_call = replace(
            original_call,
            input_codec=_CountingCodec(
                delegate=original_call.input_codec,
                counter=entered,
            ),
            scientific_call=scientific_call,
        )

        binding = replace(original, run=wrapped_call)
        host = self.host({"one": binding, "two": binding})
        analysis = host.open_analysis("permission", allocation_names=("one",))

        invocation = _invocation(
            analysis,
            binding,
            operation_id="same",
            call_name="run",
            payload=b'{"values":[1]}',
        )

        operation = analysis.submit(invocation)

        operation.request_stop()
        stopped = await self.finished(operation)

        self.assertIs(DispatchState.NOT_STARTED, stopped.dispatch)
        self.assertEqual({"decode": 0, "invoke": 0}, entered)
        self.assertIs(operation, analysis.submit(invocation))

        with self.assertRaises(RequestConflict):
            analysis.submit(
                replace(invocation, payload=b'{"values":[2]}'),
            )

        peer_analysis = host.open_analysis(
            "peer-identity",
            allocation_names=("two",),
        )

        (_, peer_result) = await self.success(
            peer_analysis.submit(
                _invocation(
                    peer_analysis,
                    binding,
                    operation_id="same",
                    call_name="run",
                    payload=b'{"values":[2]}',
                ),
            ),
            wrapped_call.result_codec,
        )

        self.assertEqual(science.SumResult(total=2, count=1), peer_result)

    async def test_state_is_worker_bound_consumed_once_and_correction_is_new_work(self):
        binding = weighted_sum_binding(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(values=(2, 3)),
        )

        host = self.host(
            {"one": binding, "two": binding},
            options=_host_options(state_values=2),
        )

        first = host.open_analysis("first", allocation_names=("one",))
        second = host.open_analysis("second", allocation_names=("two",))
        (prepare_call, calculate_call) = (binding.prepare, binding.calculate)

        rejected = await self.finished(
            first.submit(
                _invocation(
                    first,
                    binding,
                    operation_id="bad",
                    call_name="prepare",
                    payload=b'{"values":[1]}',
                ),
            )
        )

        self.assertIs(Outcome.REJECTED, rejected.outcome)

        (accepted, _) = await self.success(
            first.submit(
                _invocation(
                    first,
                    binding,
                    operation_id="corrected",
                    call_name="prepare",
                    payload=b'{"values":[1,2]}',
                ),
            ),
            prepare_call.result_codec,
        )

        foreign = second.submit(
            _invocation(
                second,
                binding,
                operation_id="foreign",
                call_name="calculate",
                payload=b"{}",
                states={"candidate": accepted.states[0].reference},
            ),
        )

        self.assertIsInstance(foreign, NotAdmitted)

        (_, prediction) = await self.success(
            first.submit(
                _invocation(
                    first,
                    binding,
                    operation_id="calculate",
                    call_name="calculate",
                    payload=b"{}",
                    states={"candidate": accepted.states[0].reference},
                ),
            ),
            calculate_call.result_codec,
        )

        self.assertEqual(science.PredictionResult(total=8, count=2), prediction)

        self.assertIsInstance(
            first.submit(
                _invocation(
                    first,
                    binding,
                    operation_id="reuse",
                    call_name="calculate",
                    payload=b"{}",
                    states={"candidate": accepted.states[0].reference},
                ),
            ),
            NotAdmitted,
        )

    async def test_typed_projection_checks_producer_correlation_before_consumption(self):
        binding = weighted_sum_binding(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(values=(2, 3)),
        )

        host = self.host({"weighted": binding})
        analysis = host.open_analysis("typed-state", allocation_names=("weighted",))

        operation = analysis.submit(
            _invocation(
                analysis,
                binding,
                operation_id="prepare",
                call_name="prepare",
                payload=b'{"values":[1,2]}',
            ),
        )

        (observed, _) = await self.success(operation, binding.prepare.result_codec)
        slot = binding.prepared_output

        wrong_producer = SimpleNamespace(
            reference=operation.reference,
            observe=AsyncMock(),
        )

        with self.assertRaises(ValueError):
            await slot.reference(binding.calculate, wrong_producer)

        wrong_producer.observe.assert_not_awaited()

        changed_identity = replace(binding.identity, implementation="dummy.weighted-sum.v3")
        other_operation = replace(operation.reference, operation_id="another-prepare")

        mismatches = (
            (
                "call",
                operation.reference,
                replace(observed, target=ref(binding.calculate)),
                "another producer call or configured revision",
            ),
            (
                "revision",
                operation.reference,
                replace(observed, target=replace(observed.target, binding_revision=changed_identity.revision)),
                "another producer call or configured revision",
            ),
            (
                "operation observation",
                other_operation,
                observed,
                "another producer operation",
            ),
            (
                "same call swapped state",
                other_operation,
                replace(observed, operation=other_operation),
                "provenance differs from its producer observation",
            ),
        )

        for (mismatch_name, reference, observation, message) in mismatches:
            with self.subTest(mismatch=mismatch_name):
                view = SimpleNamespace(
                    reference=reference,
                    observe=AsyncMock(return_value=observation),
                )

                with self.assertRaisesRegex(ValueError, message):
                    await slot.reference(binding.prepare, view)

        handle = await slot.reference(binding.prepare, operation)

        self.assertEqual(operation.reference, handle.producer)
        self.assertEqual("normalized", observed.states[0].name)
        self.assertEqual("candidate", binding.prepared_input.name)
        requested_states = {"candidate": handle.reference}

        invocation = _invocation(
            analysis,
            binding,
            operation_id="calculate",
            call_name="calculate",
            payload=b"{}",
            states=requested_states,
        )

        requested_states.clear()

        self.assertEqual(first={"candidate": handle.reference}, second=invocation.states)

        with self.assertRaises(TypeError):
            invocation.states["candidate"] = handle.reference

        other_reference = replace(handle.reference, state_id="other")

        first_order = replace(invocation, states={"candidate": handle.reference, "other": other_reference})
        second_order = replace(invocation, states={"other": other_reference, "candidate": handle.reference})

        self.assertEqual(first=first_order, second=second_order)

        (_, prediction) = await self.success(
            analysis.submit(invocation),
            binding.calculate.result_codec,
        )

        self.assertEqual(science.PredictionResult(total=8, count=2), prediction)

    async def test_stopped_consumer_dispatch_preserves_the_original_typed_state(self):
        binding = weighted_sum_binding(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(values=(2, 3)),
        )

        host = self.host({"weighted": binding})
        analysis = host.open_analysis("denied-consume", allocation_names=("weighted",))

        producer = analysis.submit(
            _invocation(
                analysis,
                binding,
                operation_id="prepare",
                call_name="prepare",
                payload=b'{"values":[1,2]}',
            ),
        )

        await self.success(producer, binding.prepare.result_codec)
        handle = await binding.prepared_output.reference(binding.prepare, producer)

        consumer = analysis.submit(
            _invocation(
                analysis,
                binding,
                operation_id="stopped-consumer",
                call_name="calculate",
                payload=b"{}",
                states={"candidate": handle.reference},
            ),
        )

        consumer.request_stop()
        denied = await self.finished(consumer)

        self.assertIs(DispatchState.NOT_STARTED, denied.dispatch)
        self.assertIsNone(denied.outcome)
        self.assertTrue(any(isinstance(failure.cause, PermissionError) for failure in denied.failures))

        self.assertEqual(
            handle,
            await binding.prepared_output.reference(binding.prepare, producer),
        )

        (_, prediction) = await self.success(
            analysis.submit(
                _invocation(
                    analysis,
                    binding,
                    operation_id="permitted",
                    call_name="calculate",
                    payload=b"{}",
                    states={"candidate": handle.reference},
                ),
            ),
            binding.calculate.result_codec,
        )

        self.assertEqual(science.PredictionResult(total=8, count=2), prediction)

    async def test_wrong_concrete_returned_state_is_owned_and_projection_preserves_failure(self):
        original = weighted_sum_binding(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(values=(2, 3)),
        )

        invalid_value = object()
        disposed = []

        def prepare(
            context: CallContext[
                science.WeightedSumModel,
                science.WeightedSumAnalysis,
                science.PreparationInput,
                NoState,
            ],
        ) -> ScientificReturn[science.PreparationResult]:
            malformed_state = replace(
                original.prepared_output.returned(invalid_value),
                dispose=disposed.append,
            )

            return ScientificReturn(
                value=science.PreparationResult(count=len(context.scientific_input.values)),
                states=(malformed_state,),
            )

        binding = replace(
            original,
            prepare=replace(original.prepare, scientific_call=prepare),
        )

        host = self.host({"weighted": binding})
        analysis = host.open_analysis("malformed-state", allocation_names=("weighted",))

        operation = analysis.submit(
            _invocation(
                analysis,
                binding,
                operation_id="prepare",
                call_name="prepare",
                payload=b'{"values":[1,2]}',
            ),
        )

        observed = await self.finished(operation)

        adoption_failure = next(
            failure.cause for failure in observed.failures
            if failure.phase == "state_adoption"
        )

        self.assertIsInstance(adoption_failure, ExceptionGroup)
        self.assertTrue(any(isinstance(cause, TypeError) for cause in adoption_failure.exceptions))
        self.assertEqual(1, len(observed.states))
        self.assertEqual("normalized", observed.states[0].name)
        self.assertEqual([], disposed)

        with self.assertRaises(ExceptionGroup) as raised:
            await binding.prepared_output.reference(binding.prepare, operation)

        self.assertIs(adoption_failure, raised.exception)

        await self.settled(analysis.close())
        self.assertEqual([invalid_value], disposed)

    async def test_result_custody_survives_cleanup_and_encoding_failure(self):
        binding = sum_binding()
        call = binding.run
        host = self.host({"one": binding})
        analysis = host.open_analysis("result", allocation_names=("one",))

        operation = analysis.submit(
            _invocation(
                analysis,
                binding,
                operation_id="result",
                call_name="run",
                payload=b'{"values":[4,5]}',
            ),
        )

        (completed, expected_result) = await self.success(operation, call.result_codec)

        await self.settled(analysis.close())

        retained_payload = await operation.read_result(completed.result)

        self.assertEqual(expected_result, call.result_codec.decode(retained_payload))
        release = operation.release_result(completed.result)

        self.assertIs(release, operation.release_result(completed.result))
        await self.settled(release)
        self.assertIs(release, operation.release_result(completed.result))

        with self.assertRaises(ValueError):
            await operation.read_result(completed.result)

        with self.assertRaises(ValueError):
            operation.release_result(replace(completed.result, result_id="another-result"))

        with self.assertRaises(ValueError):
            operation.release_result(
                ResultRef(
                    operation=replace(operation.reference, operation_id="another-operation"),
                    result_id=completed.result.result_id,
                )
            )

        self.assertIs(
            ResultState.RELEASED,
            (await operation.observe(after=None, wait_seconds=0)).result_state,
        )

        original = sum_binding()
        original_call = original.run
        retained = []
        disposed = []

        def scientific_call(
            context: CallContext[None, None, science.SumInput, NoState],
        ) -> ScientificReturn[science.SumResult]:
            returned = original_call.scientific_call(context)

            retained.append(returned.value)

            return replace(returned, dispose_value=disposed.append)

        failing_call = replace(
            original_call,
            scientific_call=scientific_call,
            result_codec=_FailingResultCodec(
                delegate=original_call.result_codec,
                failure=ValueError("encoding failed"),
            ),
        )

        failing_binding = replace(original, run=failing_call)
        host = self.host({"encoding": failing_binding})
        analysis = host.open_analysis("encoding", allocation_names=("encoding",))

        invocation = _invocation(
            analysis,
            failing_binding,
            operation_id="encoded-once",
            call_name="run",
            payload=b'{"values":[7]}',
        )

        operation = analysis.submit(invocation)
        failed = await self.finished(operation)

        self.assertIs(Outcome.SUCCEEDED, failed.outcome)
        self.assertIs(ResultState.ENCODING_FAILED, failed.result_state)
        self.assertIs(operation, analysis.submit(invocation))
        await self.settled(analysis.close())
        self.assertEqual(retained, disposed)

    async def test_independent_allocations_progress_and_retire_exact_threads(self):
        original = sum_binding()
        original_call = original.run
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        workers = {}

        def scientific_call(
            context: CallContext[None, None, science.SumInput, NoState],
        ) -> ScientificReturn[science.SumResult]:
            first_value = context.scientific_input.values[0]
            workers[first_value] = threading.current_thread()

            if first_value == 1.0:
                loop.call_soon_threadsafe(entered.set)
                release.wait(10)

            return original_call.scientific_call(context)

        wrapped_call = replace(original_call, scientific_call=scientific_call)
        binding = replace(original, run=wrapped_call)
        host = self.host({"held": binding, "peer": binding})
        held_analysis = host.open_analysis("held", allocation_names=("held",))
        peer_analysis = host.open_analysis("peer", allocation_names=("peer",))

        held = held_analysis.submit(
            _invocation(
                held_analysis,
                binding,
                operation_id="held",
                call_name="run",
                payload=b'{"values":[1]}',
            ),
        )

        await asyncio.wait_for(entered.wait(), timeout=5)
        current = await held.observe(after=None, wait_seconds=0)

        waiter = asyncio.create_task(
            held.observe(after=current.revision, wait_seconds=5)
        )

        waiter.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await waiter

        (_, peer_result) = await self.success(
            peer_analysis.submit(
                _invocation(
                    peer_analysis,
                    binding,
                    operation_id="peer",
                    call_name="run",
                    payload=b'{"values":[2]}',
                ),
            ),
            wrapped_call.result_codec,
        )

        self.assertEqual(science.SumResult(total=2, count=1), peer_result)

        release.set()
        await self.success(held, wrapped_call.result_codec)

        held_allocation = next(
            allocation
            for allocation in host.allocations
            if allocation.allocation == "held"
        )

        await self.settled(host.retire_allocation(held_allocation))
        self.assertFalse(workers[1.0].is_alive())

    async def test_backend_refuses_unenforceable_contracts(self):
        original = sum_binding()

        unsupported_bindings = (
            replace(original, identity=replace(original.identity, contract_version=2)),
            replace(original, isolation=IsolationRequirement.DEDICATED_PROCESS),
            replace(
                original,
                run=replace(
                    original.run,
                    description=replace(
                        original.run.description,
                        concurrency=ConcurrencyRequirement.PROCESS_SERIAL,
                    )
                )
            ),
        )

        for (index, unsupported_binding) in enumerate(unsupported_bindings):
            with self.subTest(index=index), self.assertRaises(ValueError):
                Allocation(
                    name=f"unsupported-{index}",
                    binding=unsupported_binding,
                )

    async def test_declared_requirements_are_retained_without_a_capacity_assertion(self):
        resident_requirement = ResourceRequirement(
            kind="host-memory",
            amount=1,
            unit="byte",
            selector="test",
            exclusive=False,
        )

        binding = replace(sum_binding(), resident_resources=frozenset({resident_requirement}))
        host = self.host(bindings={"sum": binding})
        retained_binding = host.residents["sum"].binding

        self.assertEqual(
            first=frozenset({resident_requirement}),
            second=retained_binding.description.resident_resources,
        )

    async def test_partial_initialization_keeps_the_original_cleanup_owner(self):
        settings = science.WeightedSumSettings(scale=1, offset=0)
        coefficients = science.CoefficientSet(values=(1,))

        original = weighted_sum_binding(
            settings=settings,
            coefficients=coefficients,
        )

        created_instances = []
        disposed_instances = []

        def construct_instance() -> science.WeightedSumModel:
            instance = science.create_model(
                settings=settings,
                coefficients=coefficients,
            )

            created_instances.append(instance)

            return instance

        def fail_instance_initialization(
            instance: science.WeightedSumModel,
        ) -> None:
            raise RuntimeError("load failed")

        def dispose_instance(instance: science.WeightedSumModel) -> None:
            disposed_instances.append(instance)

        instance_binding = replace(
            original,
            instance=InstanceHooks(
                construct=construct_instance,
                initialize=fail_instance_initialization,
                dispose=dispose_instance,
            ),
        )

        host = self.host({"one": instance_binding})
        analysis = host.open_analysis("partial-instance", allocation_names=("one",))

        failed = await self.finished(
            analysis.submit(
                _invocation(
                    analysis,
                    instance_binding,
                    operation_id="partial-instance",
                    call_name="prepare",
                    payload=b'{"values":[1]}',
                ),
            )
        )

        self.assertEqual("instance_initialization", failed.failures[0].phase)
        await self.settled(analysis.close())
        self.assertEqual([], disposed_instances)
        await self.settled(host.retire())
        self.assertEqual(created_instances, disposed_instances)

        original = weighted_sum_binding(
            settings=settings,
            coefficients=coefficients,
        )

        created_analyses = []
        disposed_analyses = []

        def construct_analysis(
            instance: science.WeightedSumModel,
        ) -> science.WeightedSumAnalysis:
            analysis_state = science.create_analysis(instance)

            created_analyses.append(analysis_state)

            return analysis_state

        def fail_analysis_initialization(
            analysis_state: science.WeightedSumAnalysis,
        ) -> None:
            raise RuntimeError("analysis failed")

        def dispose_analysis(
            analysis_state: science.WeightedSumAnalysis,
        ) -> None:
            disposed_analyses.append(analysis_state)

        analysis_binding = replace(
            original,
            analysis=AnalysisHooks(
                construct=construct_analysis,
                initialize=fail_analysis_initialization,
                dispose=dispose_analysis,
            ),
        )

        host = self.host({"two": analysis_binding})
        analysis = host.open_analysis("partial-analysis", allocation_names=("two",))

        failed = await self.finished(
            analysis.submit(
                _invocation(
                    analysis,
                    analysis_binding,
                    operation_id="partial-analysis",
                    call_name="prepare",
                    payload=b'{"values":[1]}',
                ),
            )
        )

        self.assertEqual("analysis_initialization", failed.failures[0].phase)
        await self.settled(analysis.close())
        self.assertEqual(created_analyses, disposed_analyses)

    async def test_cooperative_stop_is_narrow_and_observable(self):
        original = sum_binding()
        original_call = original.run

        description = replace(
            original_call.description,
            interruption=InterruptionSupport.COOPERATIVE,
            progress=True,
        )

        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def scientific_call(
            context: CallContext[None, None, science.SumInput, NoState],
        ) -> ScientificReturn[science.SumResult]:
            control = context.require_control()

            loop.call_soon_threadsafe(entered.set)

            while not control.stop_requested:
                pass

            control.report_progress(ProgressEvent(stage="stopping"))

            return ScientificReturn(
                value=science.SumResult(
                    total=0,
                    count=len(context.scientific_input.values),
                ),
            )

        cooperative_call = CallBinding(
            description=description,
            input_codec=original_call.input_codec,
            state_binding=original_call.state_binding,
            scientific_call=scientific_call,
            result_codec=original_call.result_codec,
        )

        binding = replace(original, run=cooperative_call)
        host = self.host({"one": binding})
        analysis = host.open_analysis("cooperative", allocation_names=("one",))

        operation = analysis.submit(
            _invocation(
                analysis,
                binding,
                operation_id="cooperative",
                call_name="run",
                payload=b'{"values":[1]}',
            ),
        )

        await asyncio.wait_for(entered.wait(), timeout=5)
        operation.request_stop()

        (completed, cooperative_sum) = await self.success(
            operation,
            cooperative_call.result_codec,
        )

        self.assertEqual(science.SumResult(total=0, count=1), cooperative_sum)
        self.assertEqual((ProgressEvent(stage="stopping"),), completed.progress)
        self.assertTrue(completed.stop_requested)


if __name__ == "__main__":
    unittest.main()
