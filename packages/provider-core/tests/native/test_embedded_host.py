"""Exercise the installed scientific interface without Jobs or API dependencies."""

import asyncio
from dataclasses import replace
import json
import threading
import unittest

from provider_core.contribution import AnalysisHooks, CallRef, InstanceHooks, ScientificRejection
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.examples.dummy_provider import science
from provider_core.examples.dummy_provider.binding import sum_binding, weighted_binding
from provider_core.hosting import (
    CapacityUnavailable, ExecutionLimits, Invocation, NotAdmitted, Outcome,
    RequestConflict,
)


def _allow():
    return None


def _host_limits():
    return HostLimits(16, 32, 32768, 32768, 16, 5, 5)


def _execution_limits():
    return ExecutionLimits(5, 10, 5, 1024, 1024)


def _request(analysis, binding, operation_id, name, payload, states):
    description = binding.description
    reservation = next(item for item in analysis.reservations if item.binding_id == description.binding_id)
    return Invocation(operation_id, CallRef(description.binding_id, description.binding_revision, name),
                      payload, states, reservation, _execution_limits())


class EmbeddedHostTests(unittest.IsolatedAsyncioTestCase):
    def host(self, bindings, limits):
        # Snapshot the explicit allocation inventory; no discovery or resizing.
        # repo-check: allow-tuple-generator -- Allocations are an immutable test deployment snapshot.
        allocations = tuple(Allocation(name, binding, ("dummy.instance", "dummy.call"))
                            for name, binding in bindings.items())
        host = EmbeddedHost(allocations, limits=limits)
        self.addAsyncCleanup(self.retire, host)
        return host

    async def retire(self, host):
        await self.settled(host.retire())

    async def settled(self, control):
        async with asyncio.timeout(10):
            observation = await control.observe(after=None, wait_seconds=0)
            while not observation.settled and not observation.failures:
                observation = await control.observe(after=observation.revision, wait_seconds=1)
        self.assertEqual((), observation.failures)
        self.assertTrue(observation.settled)
        return observation

    async def finished(self, operation):
        self.assertNotIsInstance(operation, NotAdmitted)
        async with asyncio.timeout(10):
            observation = await operation.observe(after=None, wait_seconds=0)
            while not observation.finished:
                observation = await operation.observe(after=observation.revision, wait_seconds=1)
        return observation

    async def success(self, operation):
        observation = await self.finished(operation)
        self.assertIs(Outcome.SUCCEEDED, observation.outcome)
        self.assertEqual((), observation.failures)
        self.assertIsNotNone(observation.result)
        return observation, json.loads(await operation.read_result(observation.result))

    async def test_weighted_and_stateless_science_share_one_analysis(self):
        settings = science.Settings(2, 1)
        coefficients = (1, 2, 3)
        model = science.create_model(settings, coefficients)
        science.load_model(model)
        direct = science.create_analysis(model)
        science.initialize_analysis(direct)
        prepared = science.prepare(direct, (2, 4, 6))
        try:
            expected = science.calculate(direct, prepared)
            self.assertEqual({"total": 57, "count": 3}, expected)
        finally:
            science.dispose_prepared(prepared)
            science.dispose_analysis(direct)
            science.dispose_model(model)

        weighted, ordinary = weighted_binding(settings, coefficients), sum_binding()
        host = self.host({"weighted": weighted, "sum": ordinary}, _host_limits())
        analysis = host.open_analysis("combined", allocation_names=("weighted", "sum"))
        preparation = analysis.submit(
            _request(analysis, weighted, "prepare", "prepare", b'{"values":[2,4,6]}', ()),
            dispatch_guard=_allow,
        )
        accepted, content = await self.success(preparation)
        self.assertEqual({"count": 3}, content)
        self.assertEqual(["prepared"], [item.name for item in accepted.states])
        calculation = analysis.submit(
            _request(analysis, weighted, "calculate", "calculate", b"{}", accepted.states),
            dispatch_guard=_allow,
        )
        _, actual = await self.success(calculation)
        self.assertEqual(expected, actual)
        stateless = analysis.submit(
            _request(analysis, ordinary, "sum", "run", b'{"values":[2,4,6]}', ()),
            dispatch_guard=_allow,
        )
        _, total = await self.success(stateless)
        self.assertEqual(science.sum_values((2, 4, 6)), total)

    async def test_revoked_permission_is_checked_on_the_loop_before_decoding(self):
        original = sum_binding()
        entered = {"decode": 0, "calculate": 0, "guard_loop": None, "guard_thread": None}
        call = original.calls[0]

        def decode(instance, analysis, payload):
            entered["decode"] += 1
            return call.decode_input(instance, analysis, payload)

        def calculate(instance, analysis, value, states):
            entered["calculate"] += 1
            return call.invoke(instance, analysis, value, states)

        binding = replace(original, calls=(replace(call, decode_input=decode, invoke=calculate),))
        host = self.host({"one": binding}, _host_limits())
        analysis = host.open_analysis("revoked", allocation_names=("one",))
        failure = PermissionError("The original job was revoked")
        permitted = True
        loop, thread = asyncio.get_running_loop(), threading.get_ident()

        def guard():
            entered["guard_loop"] = asyncio.get_running_loop()
            entered["guard_thread"] = threading.get_ident()
            if not permitted:
                raise failure

        operation = analysis.submit(
            _request(analysis, binding, "revoked/run", "run", b'{"values":[1]}', ()),
            dispatch_guard=guard,
        )
        # Revoke before yielding to the queued worker handoff. A submit-time
        # check alone would incorrectly let this operation enter scientific code.
        permitted = False
        observation = await self.finished(operation)
        self.assertFalse(observation.dispatched)
        self.assertEqual(0, entered["decode"])
        self.assertEqual(0, entered["calculate"])
        self.assertIs(loop, entered["guard_loop"])
        self.assertEqual(thread, entered["guard_thread"])
        self.assertIs(failure, observation.failures[0].cause)
        self.assertEqual("permission", observation.failures[0].phase)

    async def test_cancelled_observer_and_stop_keep_original_while_peer_finishes(self):
        original = sum_binding()
        call = original.calls[0]
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        executions = {1: 0, 2: 0}

        def calculate(instance, analysis, value, states):
            executions[value[0]] += 1
            if value == (1,):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(10):
                    raise TimeoutError("The test did not release the original calculation")
            return call.invoke(instance, analysis, value, states)

        binding = replace(original, calls=(replace(call, invoke=calculate),))
        host = self.host({"held": binding, "peer": binding}, _host_limits())
        held_analysis = host.open_analysis("held", allocation_names=("held",))
        peer_analysis = host.open_analysis("peer", allocation_names=("peer",))
        held = held_analysis.submit(
            _request(held_analysis, binding, "held/run", "run", b'{"values":[1]}', ()),
            dispatch_guard=_allow,
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            current = await held.observe(after=None, wait_seconds=0)
            observer_entered = asyncio.Event()

            async def observe_original():
                observer_entered.set()
                return await held.observe(after=current.revision, wait_seconds=5)

            waiter = asyncio.create_task(observe_original())
            await asyncio.wait_for(observer_entered.wait(), timeout=5)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            held.request_stop()
            peer = peer_analysis.submit(
                _request(peer_analysis, binding, "peer/run", "run", b'{"values":[2]}', ()),
                dispatch_guard=_allow,
            )
            _, value = await self.success(peer)
            self.assertEqual({"total": 2, "count": 1}, value)
            self.assertFalse(release.is_set())
            current = await held.observe(after=None, wait_seconds=0)
            self.assertFalse(current.finished)
            self.assertTrue(current.stop_requested)
            with self.assertRaises(CapacityUnavailable):
                host.open_analysis("cannot-replace-held", allocation_names=("held",))
        finally:
            # Emergency release prevents a leaked test thread; all independent
            # progress assertions above happen while the original is still held.
            release.set()
        _, value = await self.success(held)
        self.assertEqual({"total": 1, "count": 1}, value)
        self.assertEqual({1: 1, 2: 1}, executions)

    async def test_repeated_operation_id_observes_once_and_changed_work_conflicts(self):
        original = sum_binding()
        call = original.calls[0]
        executions = {"count": 0}

        def calculate(instance, analysis, value, states):
            executions["count"] += 1
            return call.invoke(instance, analysis, value, states)

        binding = replace(original, calls=(replace(call, invoke=calculate),))
        host = self.host({"one": binding}, _host_limits())
        analysis = host.open_analysis("deduplicated", allocation_names=("one",))
        request = _request(analysis, binding, "same", "run", b'{"values":[1]}', ())
        operation = analysis.submit(request, dispatch_guard=_allow)
        self.assertIs(operation, analysis.submit(request, dispatch_guard=_allow))
        first, _ = await self.success(operation)
        self.assertIs(operation, analysis.submit(request, dispatch_guard=_allow))
        repeated = await operation.observe(after=None, wait_seconds=0)
        self.assertEqual(first.result, repeated.result)
        with self.assertRaises(RequestConflict):
            analysis.submit(replace(request, payload=b'{"values":[2]}'), dispatch_guard=_allow)
        self.assertEqual(1, executions["count"])

    async def test_foreign_and_consumed_preparations_are_refused(self):
        binding = weighted_binding(science.Settings(1, 0), (2,))
        host = self.host({"one": binding, "two": binding}, _host_limits())
        first = host.open_analysis("first", allocation_names=("one",))
        second = host.open_analysis("second", allocation_names=("two",))
        prepare = first.submit(
            _request(first, binding, "first/prepare", "prepare", b'{"values":[3]}', ()),
            dispatch_guard=_allow,
        )
        accepted, _ = await self.success(prepare)
        foreign = second.submit(
            _request(second, binding, "foreign", "calculate", b"{}", accepted.states),
            dispatch_guard=_allow,
        )
        self.assertIsInstance(foreign, NotAdmitted)
        request = _request(first, binding, "first/calculate", "calculate", b"{}", accepted.states)
        calculation = first.submit(request, dispatch_guard=_allow)
        _, result = await self.success(calculation)
        self.assertEqual({"total": 6, "count": 1}, result)
        consumed = first.submit(replace(request, operation_id="reuse"), dispatch_guard=_allow)
        self.assertIsInstance(consumed, NotAdmitted)
        self.assertIs(calculation, first.submit(request, dispatch_guard=_allow))

    async def test_result_survives_analysis_close_and_exact_release_is_idempotent(self):
        binding = sum_binding()
        host = self.host({"one": binding}, _host_limits())
        analysis = host.open_analysis("result-owner", allocation_names=("one",))
        operation = analysis.submit(
            _request(analysis, binding, "result", "run", b'{"values":[4,5]}', ()),
            dispatch_guard=_allow,
        )
        completed, expected = await self.success(operation)
        cleanup = analysis.close()
        self.assertIs(cleanup, analysis.close())
        await self.settled(cleanup)
        self.assertEqual(expected, json.loads(await operation.read_result(completed.result)))
        with self.assertRaises(ValueError):
            await operation.read_result(replace(completed.result, result_id="not-this-result"))
        with self.assertRaises(ValueError):
            operation.release_result(replace(completed.result, result_id="not-this-result"))
        release = operation.release_result(completed.result)
        self.assertIs(release, operation.release_result(completed.result))
        await self.settled(release)
        self.assertIs(release, operation.release_result(completed.result))
        with self.assertRaises(ValueError):
            await operation.read_result(completed.result)
        self.assertTrue((await operation.observe(after=None, wait_seconds=0)).result_released)

    async def test_encoding_failure_keeps_scientific_outcome_and_disposal_owner(self):
        original = sum_binding()
        call = original.calls[0]
        failures = (
            ValueError("Result encoding failed after science returned"),
            ScientificRejection("invalid_encoding", "Encoding cannot reverse scientific completion"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                retained, disposed = [], []

                def dispose(value):
                    disposed.extend((value,))

                def calculate(instance, analysis, value, states):
                    raw = call.invoke(instance, analysis, value, states)
                    retained.extend((raw.value,))
                    return replace(raw, dispose_value=dispose)

                def encode(value):
                    raise failure

                binding = replace(original, calls=(replace(call, invoke=calculate, encode_result=encode),))
                host = self.host({"one": binding}, _host_limits())
                analysis = host.open_analysis("encoding", allocation_names=("one",))
                request = _request(analysis, binding, "encoded-once", "run", b'{"values":[7]}', ())
                operation = analysis.submit(request, dispatch_guard=_allow)
                completed = await self.finished(operation)
                self.assertIs(Outcome.SUCCEEDED, completed.outcome)
                self.assertIsNone(completed.result)
                self.assertEqual("encoding", completed.failures[0].phase)
                self.assertIs(failure, completed.failures[0].cause)
                self.assertEqual([], disposed)
                self.assertIs(operation, analysis.submit(request, dispatch_guard=_allow))
                await self.settled(analysis.close())
                self.assertEqual(1, len(retained))
                self.assertEqual(1, len(disposed))
                self.assertIs(retained[0], disposed[0])

    async def test_host_retirement_observes_the_actual_scientific_thread_exit(self):
        original = sum_binding()
        call = original.calls[0]
        worker = {}

        def calculate(instance, analysis, value, states):
            worker["thread"] = threading.current_thread()
            return call.invoke(instance, analysis, value, states)

        binding = replace(original, calls=(replace(call, invoke=calculate),))
        host = self.host({"one": binding}, _host_limits())
        analysis = host.open_analysis("thread-exit", allocation_names=("one",))
        operation = analysis.submit(
            _request(analysis, binding, "thread-exit/run", "run", b'{"values":[1]}', ()),
            dispatch_guard=_allow,
        )
        await self.success(operation)
        self.assertIsNot(threading.current_thread(), worker["thread"])
        self.assertTrue(worker["thread"].is_alive())
        await self.settled(host.retire())
        self.assertFalse(worker["thread"].is_alive())

    async def test_unsupported_contract_version_is_refused_before_construction(self):
        original = sum_binding()
        entered = {"construct": 0, "initialize": 0, "dispose": 0}

        def construct():
            entered["construct"] += 1
            return object()

        def initialize(value):
            entered["initialize"] += 1

        def dispose(value):
            entered["dispose"] += 1

        binding = replace(
            original,
            description=replace(original.description, contract_version=2),
            instance=InstanceHooks(construct, initialize, dispose),
        )
        with self.assertRaises(ValueError):
            self.host({"unsupported": binding}, _host_limits())
        self.assertEqual({"construct": 0, "initialize": 0, "dispose": 0}, entered)

    async def test_partial_instance_initialization_is_disposed_by_original_host(self):
        original = sum_binding()
        created, disposed = [], []
        failure = RuntimeError("Loading failed after acquiring a model allocation")

        def construct():
            value = {"allocation": None}
            created.extend((value,))
            return value

        def initialize(value):
            value["allocation"] = object()
            raise failure

        def dispose(value):
            disposed.extend((value,))
            value["allocation"] = None

        binding = replace(original, instance=InstanceHooks(construct, initialize, dispose))
        host = self.host({"one": binding}, _host_limits())
        analysis = host.open_analysis("partial-instance", allocation_names=("one",))
        operation = analysis.submit(
            _request(analysis, binding, "partial-instance/run", "run", b'{"values":[1]}', ()),
            dispatch_guard=_allow,
        )
        completed = await self.finished(operation)
        self.assertEqual("instance_initialization", completed.failures[0].phase)
        self.assertIs(failure, completed.failures[0].cause)
        await self.settled(analysis.close())
        self.assertEqual([], disposed)
        self.assertIsNotNone(created[0]["allocation"])
        with self.assertRaises(CapacityUnavailable):
            host.open_analysis("cannot-replace-instance", allocation_names=("one",))
        await self.retire(host)
        self.assertEqual(1, len(disposed))
        self.assertIs(created[0], disposed[0])
        self.assertIsNone(created[0]["allocation"])

    async def test_partial_analysis_initialization_is_reachable_before_preparation(self):
        original = sum_binding()
        created, disposed = [], []
        failure = RuntimeError("Analysis initialization failed after acquisition")

        def construct(instance):
            value = {"allocation": None}
            created.extend((value,))
            return value

        def initialize(value):
            value["allocation"] = object()
            raise failure

        def dispose(value):
            disposed.extend((value,))
            value["allocation"] = None

        binding = replace(original, analysis=AnalysisHooks(construct, initialize, dispose))
        host = self.host({"one": binding}, _host_limits())
        analysis = host.open_analysis("partial-analysis", allocation_names=("one",))
        operation = analysis.submit(
            _request(analysis, binding, "partial-analysis/run", "run", b'{"values":[1]}', ()),
            dispatch_guard=_allow,
        )
        completed = await self.finished(operation)
        self.assertEqual("analysis_initialization", completed.failures[0].phase)
        self.assertIs(failure, completed.failures[0].cause)
        self.assertEqual([], disposed)
        with self.assertRaises(CapacityUnavailable):
            host.open_analysis("cannot-replace-analysis", allocation_names=("one",))
        await self.settled(analysis.close())
        self.assertEqual(1, len(disposed))
        self.assertIs(created[0], disposed[0])
        self.assertIsNone(created[0]["allocation"])

    async def test_full_result_storage_refuses_new_work_without_evicting_output(self):
        binding = sum_binding()
        host = self.host({"one": binding, "two": binding}, replace(_host_limits(), result_bytes=1024))
        first = host.open_analysis("first", allocation_names=("one",))
        second = host.open_analysis("second", allocation_names=("two",))
        original = first.submit(
            _request(first, binding, "first/run", "run", b'{"values":[1]}', ()),
            dispatch_guard=_allow,
        )
        completed, value = await self.success(original)
        request = _request(second, binding, "second/run", "run", b'{"values":[2]}', ())
        self.assertIsInstance(second.submit(request, dispatch_guard=_allow), NotAdmitted)
        await self.settled(first.close())
        self.assertIsInstance(second.submit(request, dispatch_guard=_allow), NotAdmitted)
        self.assertEqual(value, json.loads(await original.read_result(completed.result)))
        await self.settled(original.release_result(completed.result))
        _, result = await self.success(second.submit(request, dispatch_guard=_allow))
        self.assertEqual({"total": 2, "count": 1}, result)

    async def test_rejected_preparation_can_be_corrected_within_its_state_bound(self):
        binding = weighted_binding(science.Settings(1, 0), (2, 3))
        host = self.host({"one": binding}, replace(_host_limits(), state_values=1))
        analysis = host.open_analysis("correction", allocation_names=("one",))
        rejected = analysis.submit(
            _request(analysis, binding, "wrong-count", "prepare", b'{"values":[1]}', ()),
            dispatch_guard=_allow,
        )
        observation = await self.finished(rejected)
        self.assertIs(Outcome.REJECTED, observation.outcome)
        self.assertEqual((), observation.states)
        corrected = analysis.submit(
            _request(analysis, binding, "correct-count", "prepare", b'{"values":[1,2]}', ()),
            dispatch_guard=_allow,
        )
        accepted, result = await self.success(corrected)
        self.assertEqual({"count": 2}, result)
        self.assertEqual(1, len(accepted.states))
