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
    ProgressEvent,
    ScientificControl,
    ScientificReturn,
    description_document,
)
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.examples.dummy_provider import science
from provider_core.examples.dummy_provider.binding import sum_binding, weighted_binding
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
    return None


def _limits(**changes) -> HostLimits:
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
    return ExecutionLimits(5, 10, 5, 1024, 1024)


def _grants(binding) -> tuple[ResourceGrant, ...]:
    resident = {
        requirement.identity: requirement
        for requirement in binding.description.resident_resources
    }
    calls = {}
    for call in binding.description.calls:
        for requirement in call.resources:
            known = calls.get(requirement.identity)
            if known is None or known.amount < requirement.amount:
                calls[requirement.identity] = requirement
    result = []
    for identity in set(resident) | set(calls):
        instance = resident.get(identity)
        invocation = calls.get(identity)
        sample = instance or invocation
        result.append(ResourceGrant(
            sample.kind,
            (instance.amount if instance else 0)
            + (invocation.amount if invocation else 0),
            sample.unit,
            sample.selector,
            bool(
                (instance and instance.exclusive)
                or (invocation and invocation.exclusive)
            ),
        ))
    return tuple(result)


def _request(
    analysis,
    binding,
    operation_id: str,
    name: str,
    payload: bytes,
    states=(),
) -> Invocation:
    description = binding.description
    reservation = next(
        item
        for item in analysis.reservations
        if item.binding_id == description.binding_id
    )
    return Invocation(
        operation_id,
        CallRef(description.binding_id, description.binding_revision, name),
        payload,
        states,
        reservation,
        _execution_limits(),
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
                Allocation(name, binding, _grants(binding))
                for name, binding in bindings.items()
            ),
            limits=limits or _limits(),
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
        settings = science.Settings(scale=2, offset=1)
        coefficients = (1, 2, 3)
        model = science.create_model(settings, coefficients)
        science.load_model(model)
        direct = science.create_analysis(model)
        science.initialize_analysis(direct)
        prepared = science.prepare(
            direct,
            science.ValuesInput(values=(2.0, 4.0, 6.0)),
        )
        expected = science.calculate(
            direct,
            science.EmptyInput(),
            science.CalculationState(prepared=prepared),
        )
        science.dispose_prepared(prepared)
        science.dispose_analysis(direct)
        science.dispose_model(model)

        weighted = weighted_binding(settings, coefficients)
        ordinary = sum_binding()
        document = description_document(weighted.description)
        self.assertEqual("shared_process", document["isolation"])
        self.assertEqual(
            ["prepare", "calculate"],
            [call["name"] for call in document["calls"]],
        )

        host = self.host({"weighted": weighted, "sum": ordinary})
        analysis = host.open_analysis(
            "combined",
            allocation_names=("weighted", "sum"),
        )
        prepare_call, calculate_call = weighted.calls
        preparation, _ = await self.success(
            analysis.submit(
                _request(
                    analysis,
                    weighted,
                    "prepare",
                    "prepare",
                    b'{"values":[2,4,6]}',
                ),
                dispatch_guard=_allow,
            ),
            prepare_call.result_codec,
        )
        completed, actual = await self.success(
            analysis.submit(
                _request(
                    analysis,
                    weighted,
                    "calculate",
                    "calculate",
                    b"{}",
                    preparation.states,
                ),
                dispatch_guard=_allow,
            ),
            calculate_call.result_codec,
        )
        self.assertEqual(expected, actual)
        self.assertEqual(
            (
                ProgressEvent("calculation", 0, 1),
                ProgressEvent("calculation", 1, 1),
            ),
            completed.progress,
        )
        run_call = ordinary.calls[0]
        _, ordinary_result = await self.success(
            analysis.submit(
                _request(
                    analysis,
                    ordinary,
                    "sum",
                    "run",
                    b'{"values":[2,4,6]}',
                ),
                dispatch_guard=_allow,
            ),
            run_call.result_codec,
        )
        self.assertEqual(
            science.SumResult(total=12.0, count=3),
            ordinary_result,
        )

    async def test_permission_and_operation_identity_prevent_unowned_replay(self):
        original = sum_binding()
        call = original.calls[0]
        entered = {"decode": 0, "invoke": 0}

        @wraps(call.scientific_call)
        def scientific_call(instance, analysis, candidate, state, control):
            entered["invoke"] += 1
            return call.scientific_call(
                instance,
                analysis,
                candidate,
                state,
                control,
            )

        wrapped = replace(
            call,
            input_codec=_CountingCodec(call.input_codec, entered),
            scientific_call=scientific_call,
        )
        binding = replace(original, calls=(wrapped,))
        host = self.host({"one": binding, "two": binding})
        analysis = host.open_analysis("permission", allocation_names=("one",))
        permitted = True

        def guard() -> None:
            if not permitted:
                raise PermissionError("revoked")

        request = _request(
            analysis,
            binding,
            "same",
            "run",
            b'{"values":[1]}',
        )
        operation = analysis.submit(request, dispatch_guard=guard)
        permitted = False
        stopped = await self.finished(operation)
        self.assertIs(DispatchState.NOT_STARTED, stopped.dispatch)
        self.assertEqual({"decode": 0, "invoke": 0}, entered)
        self.assertIs(operation, analysis.submit(request, dispatch_guard=guard))
        with self.assertRaises(RequestConflict):
            analysis.submit(
                replace(request, payload=b'{"values":[2]}'),
                dispatch_guard=guard,
            )

        peer = host.open_analysis("peer-identity", allocation_names=("two",))
        _, peer_result = await self.success(
            peer.submit(
                _request(
                    peer,
                    binding,
                    "same",
                    "run",
                    b'{"values":[2]}',
                ),
                dispatch_guard=_allow,
            ),
            wrapped.result_codec,
        )
        self.assertEqual(science.SumResult(total=2.0, count=1), peer_result)

    async def test_state_is_worker_bound_consumed_once_and_correction_is_new_work(self):
        binding = weighted_binding(
            science.Settings(scale=1, offset=0),
            (2, 3),
        )
        host = self.host(
            {"one": binding, "two": binding},
            limits=_limits(state_values=2),
        )
        first = host.open_analysis("first", allocation_names=("one",))
        second = host.open_analysis("second", allocation_names=("two",))
        prepare_call, calculate_call = binding.calls
        rejected = await self.finished(
            first.submit(
                _request(
                    first,
                    binding,
                    "bad",
                    "prepare",
                    b'{"values":[1]}',
                ),
                dispatch_guard=_allow,
            )
        )
        self.assertIs(Outcome.REJECTED, rejected.outcome)
        accepted, _ = await self.success(
            first.submit(
                _request(
                    first,
                    binding,
                    "corrected",
                    "prepare",
                    b'{"values":[1,2]}',
                ),
                dispatch_guard=_allow,
            ),
            prepare_call.result_codec,
        )
        foreign = second.submit(
            _request(
                second,
                binding,
                "foreign",
                "calculate",
                b"{}",
                accepted.states,
            ),
            dispatch_guard=_allow,
        )
        self.assertIsInstance(foreign, NotAdmitted)
        calculation = first.submit(
            _request(
                first,
                binding,
                "calculate",
                "calculate",
                b"{}",
                accepted.states,
            ),
            dispatch_guard=_allow,
        )
        _, result = await self.success(
            calculation,
            calculate_call.result_codec,
        )
        self.assertEqual(
            science.PredictionResult(total=8.0, count=2),
            result,
        )
        self.assertIsInstance(
            first.submit(
                _request(
                    first,
                    binding,
                    "reuse",
                    "calculate",
                    b"{}",
                    accepted.states,
                ),
                dispatch_guard=_allow,
            ),
            NotAdmitted,
        )

    async def test_result_custody_survives_analysis_cleanup_and_encoding_failure(self):
        binding = sum_binding()
        call = binding.calls[0]
        host = self.host({"one": binding})
        analysis = host.open_analysis("result", allocation_names=("one",))
        operation = analysis.submit(
            _request(
                analysis,
                binding,
                "result",
                "run",
                b'{"values":[4,5]}',
            ),
            dispatch_guard=_allow,
        )
        completed, expected = await self.success(operation, call.result_codec)
        await self.settled(analysis.close())
        payload = await operation.read_result(completed.result)
        self.assertEqual(expected, call.result_codec.decode(payload))
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
        def scientific_call(instance, analysis, candidate, state, control):
            raw = original_call.scientific_call(
                instance,
                analysis,
                candidate,
                state,
                control,
            )
            retained.append(raw.value)
            return replace(raw, dispose_value=disposed.append)

        failure = ValueError("encoding failed")
        failing_call = replace(
            original_call,
            scientific_call=scientific_call,
            result_codec=_FailingResultCodec(
                delegate=original_call.result_codec,
                failure=failure,
            ),
        )
        binding = replace(original, calls=(failing_call,))
        host = self.host({"encoding": binding})
        analysis = host.open_analysis(
            "encoding",
            allocation_names=("encoding",),
        )
        operation = analysis.submit(
            _request(
                analysis,
                binding,
                "encoded-once",
                "run",
                b'{"values":[7]}',
            ),
            dispatch_guard=_allow,
        )
        failed = await self.finished(operation)
        self.assertIs(Outcome.SUCCEEDED, failed.outcome)
        self.assertIs(ResultState.ENCODING_FAILED, failed.result_state)
        self.assertIs(
            operation,
            analysis.submit(
                _request(
                    analysis,
                    binding,
                    "encoded-once",
                    "run",
                    b'{"values":[7]}',
                ),
                dispatch_guard=_allow,
            ),
        )
        await self.settled(analysis.close())
        self.assertEqual(retained, disposed)

    async def test_independent_allocations_progress_and_retire_exact_threads(self):
        original = sum_binding()
        call = original.calls[0]
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        workers = {}

        @wraps(call.scientific_call)
        def scientific_call(instance, analysis, candidate, state, control):
            key = candidate.values[0]
            workers[key] = threading.current_thread()
            if key == 1.0:
                loop.call_soon_threadsafe(entered.set)
                release.wait(10)
            return call.scientific_call(
                instance,
                analysis,
                candidate,
                state,
                control,
            )

        wrapped = replace(call, scientific_call=scientific_call)
        binding = replace(original, calls=(wrapped,))
        host = self.host({"held": binding, "peer": binding})
        held_analysis = host.open_analysis("held", allocation_names=("held",))
        peer_analysis = host.open_analysis("peer", allocation_names=("peer",))
        held = held_analysis.submit(
            _request(
                held_analysis,
                binding,
                "held",
                "run",
                b'{"values":[1]}',
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
        _, peer = await self.success(
            peer_analysis.submit(
                _request(
                    peer_analysis,
                    binding,
                    "peer",
                    "run",
                    b'{"values":[2]}',
                ),
                dispatch_guard=_allow,
            ),
            wrapped.result_codec,
        )
        self.assertEqual(science.SumResult(total=2.0, count=1), peer)
        release.set()
        await self.success(held, wrapped.result_codec)
        reference = next(
            item
            for item in host.allocations
            if item.allocation == "held"
        )
        await self.settled(host.retire_allocation(reference))
        self.assertFalse(workers[1.0].is_alive())

    async def test_backend_refuses_unenforceable_contracts_and_missing_grants(self):
        original = sum_binding()
        cases = (
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
        for index, description in enumerate(cases):
            calls = original.calls
            if description.calls != original.description.calls:
                calls = (
                    replace(
                        original.calls[0],
                        description=description.calls[0],
                    ),
                )
            with self.subTest(index=index), self.assertRaises(ValueError):
                Allocation(
                    f"unsupported-{index}",
                    replace(
                        original,
                        description=description,
                        calls=calls,
                    ),
                    _grants(original),
                )

        weighted = weighted_binding(
            science.Settings(scale=1, offset=0),
            (2,),
        )
        with self.assertRaises(ValueError):
            Allocation(
                "missing-memory",
                weighted,
                (ResourceGrant("cpu", 1, "thread", "generic", False),),
            )

    async def test_partial_initialization_remains_with_original_cleanup_owner(self):
        original = sum_binding()
        created = []
        disposed = []

        def construct():
            value = {"resource": object()}
            created.append(value)
            return value

        def fail_load(value) -> None:
            raise RuntimeError("load failed")

        binding = replace(
            original,
            instance=InstanceHooks(
                construct,
                fail_load,
                disposed.append,
            ),
        )
        host = self.host({"one": binding})
        analysis = host.open_analysis("partial", allocation_names=("one",))
        failed = await self.finished(
            analysis.submit(
                _request(
                    analysis,
                    binding,
                    "partial",
                    "run",
                    b'{"values":[1]}',
                ),
                dispatch_guard=_allow,
            )
        )
        self.assertEqual("instance_initialization", failed.failures[0].phase)
        await self.settled(analysis.close())
        self.assertEqual([], disposed)
        await self.settled(host.retire())
        self.assertEqual(created, disposed)

        original = sum_binding()
        created = []
        disposed = []

        def construct_analysis(instance):
            value = {"resource": object()}
            created.append(value)
            return value

        def fail_analysis(value) -> None:
            raise RuntimeError("analysis failed")

        binding = replace(
            original,
            analysis=AnalysisHooks(
                construct_analysis,
                fail_analysis,
                disposed.append,
            ),
        )
        host = self.host({"two": binding})
        analysis = host.open_analysis(
            "partial-analysis",
            allocation_names=("two",),
        )
        failed = await self.finished(
            analysis.submit(
                _request(
                    analysis,
                    binding,
                    "partial-analysis",
                    "run",
                    b'{"values":[1]}',
                ),
                dispatch_guard=_allow,
            )
        )
        self.assertEqual("analysis_initialization", failed.failures[0].phase)
        await self.settled(analysis.close())
        self.assertEqual(created, disposed)

    async def test_cooperative_stop_is_narrow_and_observable(self):
        original = sum_binding()
        call = original.calls[0]
        description = replace(
            call.description,
            interruption=InterruptionSupport.COOPERATIVE,
            progress=True,
        )
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def scientific_call(
            instance: None,
            analysis: None,
            candidate: science.ValuesInput,
            state: science.NoState,
            control: ScientificControl,
        ) -> ScientificReturn[science.SumResult]:
            loop.call_soon_threadsafe(entered.set)
            while not control.stop_requested:
                pass
            control.report_progress(ProgressEvent("stopping"))
            return ScientificReturn(
                value=science.SumResult(total=0.0, count=0),
                states=(),
                dispose_value=None,
            )

        cooperative_call = CallBinding(
            description=description,
            input_codec=call.input_codec,
            state_binding=call.state_binding,
            scientific_call=scientific_call,
            result_codec=call.result_codec,
        )
        binding = ImplementationBinding(
            description=replace(original.description, calls=(description,)),
            instance=None,
            analysis=None,
            calls=(cooperative_call,),
        )
        host = self.host({"one": binding})
        analysis = host.open_analysis(
            "cooperative",
            allocation_names=("one",),
        )
        operation = analysis.submit(
            _request(
                analysis,
                binding,
                "cooperative",
                "run",
                b'{"values":[1]}',
            ),
            dispatch_guard=_allow,
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        operation.request_stop()
        completed, result = await self.success(
            operation,
            cooperative_call.result_codec,
        )
        self.assertEqual(science.SumResult(total=0.0, count=0), result)
        self.assertEqual((ProgressEvent("stopping"),), completed.progress)
        self.assertTrue(completed.stop_requested)


if __name__ == "__main__":
    unittest.main()
