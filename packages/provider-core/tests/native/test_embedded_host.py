"""Exercise the typed contribution/execution boundary without Jobs."""

from __future__ import annotations

import asyncio
import threading
import unittest
from dataclasses import dataclass, replace
from functools import wraps

from provider_core.contribution import (
    AnalysisHooks,
    CallBinding,
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
)
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.examples.dummy_provider import science
from provider_core.examples.dummy_provider.binding import (
    sum_binding,
    weighted_sum_binding,
)
from provider_core.hosting import (
    DispatchState,
    ExecutionLimits,
    Invocation,
    NotAdmitted,
    Outcome,
    RequestConflict,
    ResourceGrant,
    ResultState,
)
from provider_core.payload import PayloadCodec


def _allow() -> None:
    pass


def _host_limits(**changes) -> HostLimits:
    values = {
        "analysis_records": 16,
        "operation_records": 32,
        "input_bytes": 32768,
        "result_bytes": 32768,
        "state_values": 16,
        "progress_records": 8,
        "initialization_seconds": 5,
        "retirement_seconds": 5,
    }
    values.update(changes)
    return HostLimits(**values)


def _execution_limits() -> ExecutionLimits:
    return ExecutionLimits(
        queue_seconds=5,
        execution_seconds=10,
        cleanup_seconds=5,
        input_bytes=1024,
        result_bytes=1024,
    )


def _grants(binding) -> tuple[ResourceGrant, ...]:
    resident = {
        requirement.identity: requirement
        for requirement in binding.description.resident_resources
    }
    active = {}
    for call in binding.description.calls:
        for requirement in call.resources:
            known = active.get(requirement.identity)
            if known is None or known.amount < requirement.amount:
                active[requirement.identity] = requirement

    grants = []
    for identity in set(resident) | set(active):
        resident_requirement = resident.get(identity)
        active_requirement = active.get(identity)
        requirement = resident_requirement or active_requirement
        grants.append(
            ResourceGrant(
                kind=requirement.kind,
                amount=(
                    (resident_requirement.amount if resident_requirement else 0)
                    + (active_requirement.amount if active_requirement else 0)
                ),
                unit=requirement.unit,
                selector=requirement.selector,
                exclusive=bool(
                    (resident_requirement and resident_requirement.exclusive)
                    or (active_requirement and active_requirement.exclusive)
                ),
            )
        )
    return tuple(grants)


def _invocation(
    analysis,
    binding,
    *,
    operation_id: str,
    call_name: str,
    payload: bytes,
    states=(),
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
        states=states,
        reservation=reservation,
        limits=_execution_limits(),
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
    def host(self, bindings, *, limits=None):
        host = EmbeddedHost(
            tuple(
                Allocation(
                    name=name,
                    binding=binding,
                    grants=_grants(binding),
                )
                for (name, binding) in bindings.items()
            ),
            limits=limits or _host_limits(),
        )
        self.addAsyncCleanup(self.retire_host, host)
        return host

    async def retire_host(self, host) -> None:
        await self.settled(host.retire())

    async def settled(self, control):
        async with asyncio.timeout(10):
            observation = await control.observe(after=None, wait_seconds=0)
            while not observation.settled and not observation.failures:
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
        return observation, codec.decode(payload)

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
        self.assertEqual(
            ["prepare", "calculate"],
            [call["name"] for call in description["calls"]],
        )
        self.assertIsNone(ordinary.description.configuration)

        host = self.host({"weighted": weighted, "sum": ordinary})
        analysis = host.open_analysis(
            "combined",
            allocation_names=("weighted", "sum"),
        )
        (prepare_call, calculate_call) = weighted.calls
        preparation, _ = await self.success(
            analysis.submit(
                _invocation(
                    analysis,
                    weighted,
                    operation_id="prepare",
                    call_name="prepare",
                    payload=b'{"values":[2,4,6]}',
                ),
                dispatch_guard=_allow,
            ),
            prepare_call.result_codec,
        )
        calculation, actual_result = await self.success(
            analysis.submit(
                _invocation(
                    analysis,
                    weighted,
                    operation_id="calculate",
                    call_name="calculate",
                    payload=b"{}",
                    states=preparation.states,
                ),
                dispatch_guard=_allow,
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

        sum_call = ordinary.calls[0]
        _, ordinary_result = await self.success(
            analysis.submit(
                _invocation(
                    analysis,
                    ordinary,
                    operation_id="sum",
                    call_name="run",
                    payload=b'{"values":[2,4,6]}',
                ),
                dispatch_guard=_allow,
            ),
            sum_call.result_codec,
        )
        self.assertEqual(science.SumResult(total=12, count=3), ordinary_result)

    async def test_permission_and_operation_identity_prevent_unowned_replay(self):
        original = sum_binding()
        original_call = original.calls[0]
        entered = {"decode": 0, "invoke": 0}

        @wraps(original_call.scientific_call)
        def scientific_call(instance, analysis, summation, state, control):
            entered["invoke"] += 1
            return original_call.scientific_call(
                instance,
                analysis,
                summation,
                state,
                control,
            )

        wrapped_call = replace(
            original_call,
            input_codec=_CountingCodec(
                delegate=original_call.input_codec,
                counter=entered,
            ),
            scientific_call=scientific_call,
        )
        binding = replace(original, calls=(wrapped_call,))
        host = self.host({"one": binding, "two": binding})
        analysis = host.open_analysis("permission", allocation_names=("one",))
        permitted = True

        def guard() -> None:
            if not permitted:
                raise PermissionError("revoked")

        invocation = _invocation(
            analysis,
            binding,
            operation_id="same",
            call_name="run",
            payload=b'{"values":[1]}',
        )
        operation = analysis.submit(invocation, dispatch_guard=guard)
        permitted = False
        stopped = await self.finished(operation)

        self.assertIs(DispatchState.NOT_STARTED, stopped.dispatch)
        self.assertEqual({"decode": 0, "invoke": 0}, entered)
        self.assertIs(operation, analysis.submit(invocation, dispatch_guard=guard))
        with self.assertRaises(RequestConflict):
            analysis.submit(
                replace(invocation, payload=b'{"values":[2]}'),
                dispatch_guard=guard,
            )

        peer_analysis = host.open_analysis(
            "peer-identity",
            allocation_names=("two",),
        )
        _, peer_result = await self.success(
            peer_analysis.submit(
                _invocation(
                    peer_analysis,
                    binding,
                    operation_id="same",
                    call_name="run",
                    payload=b'{"values":[2]}',
                ),
                dispatch_guard=_allow,
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
            limits=_host_limits(state_values=2),
        )
        first = host.open_analysis("first", allocation_names=("one",))
        second = host.open_analysis("second", allocation_names=("two",))
        (prepare_call, calculate_call) = binding.calls

        rejected = await self.finished(
            first.submit(
                _invocation(
                    first,
                    binding,
                    operation_id="bad",
                    call_name="prepare",
                    payload=b'{"values":[1]}',
                ),
                dispatch_guard=_allow,
            )
        )
        self.assertIs(Outcome.REJECTED, rejected.outcome)

        accepted, _ = await self.success(
            first.submit(
                _invocation(
                    first,
                    binding,
                    operation_id="corrected",
                    call_name="prepare",
                    payload=b'{"values":[1,2]}',
                ),
                dispatch_guard=_allow,
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
                states=accepted.states,
            ),
            dispatch_guard=_allow,
        )
        self.assertIsInstance(foreign, NotAdmitted)

        _, result = await self.success(
            first.submit(
                _invocation(
                    first,
                    binding,
                    operation_id="calculate",
                    call_name="calculate",
                    payload=b"{}",
                    states=accepted.states,
                ),
                dispatch_guard=_allow,
            ),
            calculate_call.result_codec,
        )
        self.assertEqual(science.PredictionResult(total=8, count=2), result)
        self.assertIsInstance(
            first.submit(
                _invocation(
                    first,
                    binding,
                    operation_id="reuse",
                    call_name="calculate",
                    payload=b"{}",
                    states=accepted.states,
                ),
                dispatch_guard=_allow,
            ),
            NotAdmitted,
        )

    async def test_result_custody_survives_cleanup_and_encoding_failure(self):
        binding = sum_binding()
        call = binding.calls[0]
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
            dispatch_guard=_allow,
        )
        completed, expected_result = await self.success(operation, call.result_codec)
        await self.settled(analysis.close())

        retained_payload = await operation.read_result(completed.result)
        self.assertEqual(expected_result, call.result_codec.decode(retained_payload))
        release = operation.release_result(completed.result)
        self.assertIs(release, operation.release_result(completed.result))
        await self.settled(release)
        self.assertIs(
            ResultState.RELEASED,
            (await operation.observe(after=None, wait_seconds=0)).result_state,
        )

        original = sum_binding()
        original_call = original.calls[0]
        retained = []
        disposed = []

        @wraps(original_call.scientific_call)
        def scientific_call(instance, analysis, summation, state, control):
            returned = original_call.scientific_call(
                instance,
                analysis,
                summation,
                state,
                control,
            )
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
        failing_binding = replace(original, calls=(failing_call,))
        host = self.host({"encoding": failing_binding})
        analysis = host.open_analysis("encoding", allocation_names=("encoding",))
        invocation = _invocation(
            analysis,
            failing_binding,
            operation_id="encoded-once",
            call_name="run",
            payload=b'{"values":[7]}',
        )
        operation = analysis.submit(invocation, dispatch_guard=_allow)
        failed = await self.finished(operation)

        self.assertIs(Outcome.SUCCEEDED, failed.outcome)
        self.assertIs(ResultState.ENCODING_FAILED, failed.result_state)
        self.assertIs(operation, analysis.submit(invocation, dispatch_guard=_allow))
        await self.settled(analysis.close())
        self.assertEqual(retained, disposed)

    async def test_independent_allocations_progress_and_retire_exact_threads(self):
        original = sum_binding()
        original_call = original.calls[0]
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        workers = {}

        @wraps(original_call.scientific_call)
        def scientific_call(instance, analysis, summation, state, control):
            first_value = summation.values[0]
            workers[first_value] = threading.current_thread()
            if first_value == 1.0:
                loop.call_soon_threadsafe(entered.set)
                release.wait(10)
            return original_call.scientific_call(
                instance,
                analysis,
                summation,
                state,
                control,
            )

        wrapped_call = replace(original_call, scientific_call=scientific_call)
        binding = replace(original, calls=(wrapped_call,))
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
            dispatch_guard=_allow,
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        current = await held.observe(after=None, wait_seconds=0)
        waiter = asyncio.create_task(
            held.observe(after=current.revision, wait_seconds=5)
        )
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        _, peer_result = await self.success(
            peer_analysis.submit(
                _invocation(
                    peer_analysis,
                    binding,
                    operation_id="peer",
                    call_name="run",
                    payload=b'{"values":[2]}',
                ),
                dispatch_guard=_allow,
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

    async def test_backend_refuses_unenforceable_contracts_and_missing_grants(self):
        original = sum_binding()
        unsupported_descriptions = (
            replace(original.description, contract_version=2),
            replace(
                original.description,
                isolation=IsolationRequirement.DEDICATED_PROCESS,
            ),
            replace(
                original.description,
                calls=(
                    replace(
                        original.description.calls[0],
                        concurrency=ConcurrencyRequirement.PROCESS_SERIAL,
                    ),
                ),
            ),
        )

        for (index, description) in enumerate(unsupported_descriptions):
            calls = original.calls
            if description.calls != original.description.calls:
                calls = (
                    replace(
                        original.calls[0],
                        description=description.calls[0],
                    ),
                )
            unsupported_binding = replace(
                original,
                description=description,
                calls=calls,
            )
            with self.subTest(index=index), self.assertRaises(ValueError):
                Allocation(
                    name=f"unsupported-{index}",
                    binding=unsupported_binding,
                    grants=_grants(original),
                )

        resident_requirement = ResourceRequirement(
            kind="host-memory",
            amount=1,
            unit="byte",
            selector="test",
            exclusive=False,
        )
        resident_binding = replace(
            original,
            description=replace(
                original.description,
                resident_resources=(resident_requirement,),
            ),
        )
        with self.assertRaises(ValueError):
            Allocation(
                name="missing-resident-grant",
                binding=resident_binding,
                grants=_grants(original),
            )

    async def test_partial_initialization_keeps_the_original_cleanup_owner(self):
        original = sum_binding()
        created_instances = []
        disposed_instances = []

        def construct_instance():
            instance = {"resource": object()}
            created_instances.append(instance)
            return instance

        def fail_instance_initialization(instance) -> None:
            raise RuntimeError("load failed")

        instance_binding = replace(
            original,
            instance=InstanceHooks(
                construct=construct_instance,
                initialize=fail_instance_initialization,
                dispose=disposed_instances.append,
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
                    call_name="run",
                    payload=b'{"values":[1]}',
                ),
                dispatch_guard=_allow,
            )
        )
        self.assertEqual("instance_initialization", failed.failures[0].phase)
        await self.settled(analysis.close())
        self.assertEqual([], disposed_instances)
        await self.settled(host.retire())
        self.assertEqual(created_instances, disposed_instances)

        original = sum_binding()
        created_analyses = []
        disposed_analyses = []

        def construct_analysis(instance):
            analysis_state = {"resource": object()}
            created_analyses.append(analysis_state)
            return analysis_state

        def fail_analysis_initialization(analysis_state) -> None:
            raise RuntimeError("analysis failed")

        analysis_binding = replace(
            original,
            analysis=AnalysisHooks(
                construct=construct_analysis,
                initialize=fail_analysis_initialization,
                dispose=disposed_analyses.append,
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
                    call_name="run",
                    payload=b'{"values":[1]}',
                ),
                dispatch_guard=_allow,
            )
        )
        self.assertEqual("analysis_initialization", failed.failures[0].phase)
        await self.settled(analysis.close())
        self.assertEqual(created_analyses, disposed_analyses)

    async def test_cooperative_stop_is_narrow_and_observable(self):
        original = sum_binding()
        original_call = original.calls[0]
        description = replace(
            original_call.description,
            interruption=InterruptionSupport.COOPERATIVE,
            progress=True,
        )
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def scientific_call(
            instance: None,
            analysis: None,
            summation: science.SumInput,
            state: NoState,
            control: ScientificControl,
        ) -> ScientificReturn[science.SumResult]:
            loop.call_soon_threadsafe(entered.set)
            while not control.stop_requested:
                pass
            control.report_progress(ProgressEvent(stage="stopping"))
            return ScientificReturn(
                value=science.SumResult(total=0, count=len(summation.values)),
                states=(),
                dispose_value=None,
            )

        cooperative_call = CallBinding(
            description=description,
            input_codec=original_call.input_codec,
            state_binding=original_call.state_binding,
            scientific_call=scientific_call,
            result_codec=original_call.result_codec,
        )
        binding = ImplementationBinding(
            description=replace(original.description, calls=(description,)),
            instance=None,
            analysis=None,
            calls=(cooperative_call,),
        )
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
            dispatch_guard=_allow,
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        operation.request_stop()
        completed, result = await self.success(
            operation,
            cooperative_call.result_codec,
        )

        self.assertEqual(science.SumResult(total=0, count=1), result)
        self.assertEqual((ProgressEvent(stage="stopping"),), completed.progress)
        self.assertTrue(completed.stop_requested)


if __name__ == "__main__":
    unittest.main()
