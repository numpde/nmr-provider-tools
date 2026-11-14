"""Prove exact-object handover and failure ownership through the installed host."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Barrier
import unittest

from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits, ResourceAllocation
from provider_core.hosting import ResourceState, CapacityUnavailable, AdmissionCode, ExecutionLimits, NotAdmitted, Outcome, ResourceGrant, observe_cleanup, observe_completion
from provider_core.scientific import AssetAccess, Contribution, HostedContribution, NoAssets, ResourceRequirement, operation


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    scale: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Numbers:
    values: tuple[int, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Total:
    value: int


class Sum(Contribution):
    def __init__(self, *, configuration: Settings) -> None:
        super().__init__()
        self.configuration = configuration
        self.calls = 0
        self.initializations = 0
        self.closes = 0
        self.marker = object()

    def initialize(self, *, assets: AssetAccess) -> None:
        self.initializations += 1

    def close(self) -> None:
        self.closes += 1

    @operation()
    def calculate(self, *, numbers: Numbers) -> Total:
        self.calls += 1
        return Total(value=self.configuration.scale * sum(numbers.values))

    def helper(self, *, numbers: Numbers) -> Total:
        return Total(value=0)


class FailedInitialization(Sum):
    startup_failure = OSError("Cannot initialize the scientific model")
    cleanup_failure = OSError("Cannot close the partially initialized model")

    def initialize(self, *, assets: AssetAccess) -> None:
        self.partial_resource = object()
        raise self.startup_failure

    def close(self) -> None:
        self.cleanup_resource = self.partial_resource
        raise self.cleanup_failure


def new_host() -> EmbeddedHost:
    return EmbeddedHost(limits=HostLimits(
          input_bytes=8192,
        result_bytes=8192, state_values=8, progress_records=8,
        initialization_seconds=5, retirement_seconds=5,
    ))


# silent-defaults: allow-contract -- Tests vary one part of the fixed CPU assignment and scientific requirement.
def adopt(*, host, contribution, name="sum", amount=1, grant=4):
    return host.adopt(
        contribution=contribution,
        allocation=ResourceAllocation(name=name, grants=(ResourceGrant(kind="cpu", amount=grant, unit="thread"),)),
        requirements=(ResourceRequirement(kind="cpu", amount=amount, unit="thread"),),
    )


async def run_sum(*, host, hosted, operation_id):
    analysis = host.open_analysis(analysis_id=operation_id, allocation_names=(hosted.allocation.allocation,))
    reservation, = analysis.reservations
    operation = analysis.submit_method(
        method=hosted.contribution.calculate,
        scientific_input=Numbers(values=(1, 2)), states=(), reservation=reservation,
        limits=ExecutionLimits(queue_seconds=5, execution_seconds=5, cleanup_seconds=5, input_bytes=1024, result_bytes=1024),
        dispatch_guard=lambda: None,
    )
    async with asyncio.timeout(10):
        observed = await observe_completion(operation=operation)
        await observe_cleanup(control=analysis.close())
    return operation, observed


class AdoptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_released_local_operations_leave_no_submission_history(self):
        hosted = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)))
        analysis = self.host.open_analysis(allocations=(hosted.allocation,))
        reservation, = analysis.reservations
        limits = ExecutionLimits(queue_seconds=5, execution_seconds=5, cleanup_seconds=5, input_bytes=1024, result_bytes=1024)
        for _ in range(20):
            operation = analysis.submit_method(
                method=hosted.contribution.calculate,
                scientific_input=Numbers(values=(1,)),
                states=(),
                reservation=reservation,
                limits=limits,
                dispatch_guard=lambda: None,
            )
            completed = await observe_completion(operation=operation)
            await observe_cleanup(control=operation.release_result(result=completed.result))
            self.assertEqual(first={}, second=self.host.operations)
            self.assertEqual(first={}, second=analysis.operations)
        await observe_cleanup(control=analysis.close())
        observed = await operation.observe(after=None, wait_seconds=0)
        self.assertIs(expr1=ResourceState.RELEASED, expr2=observed.resources)

    async def test_finished_analyses_do_not_accumulate_in_the_host(self):
        instance = Sum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)
        retained_handles = []
        for index in range(20):
            operation, completed = await run_sum(
                host=self.host,
                hosted=hosted,
                operation_id=str(index),
            )
            retained_handles.append(operation)
            self.assertEqual(first={}, second=self.host.analyses)
            self.assertEqual(first={}, second=operation.analysis.operations)
            self.assertEqual(first={}, second=operation.analysis.states)
            self.assertEqual(first=0, second=self.host.input_used)
            # The finished analysis does not own result release. The result
            # remains readable, and its unreleased bytes remain host-owned.
            self.assertTrue(expr=await operation.read_result(result=completed.result))
            self.assertEqual(first=1, second=len(self.host.operations))
            await observe_cleanup(control=operation.release_result(result=completed.result))
            self.assertEqual(first={}, second=self.host.operations)
            self.assertEqual(first=0, second=self.host.result_reserved)
        self.assertEqual(first=20, second=instance.calls)
        self.assertEqual(first=20, second=len(retained_handles))
        with self.assertRaises(expected_exception=CapacityUnavailable):
            adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)), name="late")

    async def test_state_ports_use_python_types_and_attribute_names(self):
        from provider_core.scientific import StateKind, StateUse

        kind = StateKind(value_type=Total)

        class Ports:
            produced = kind.output(dispose=lambda value: None)
            candidate = kind.input(use=StateUse.CONSUME)

        class Inherited(Ports):
            pass

        self.assertEqual(first="produced", second=Inherited.produced.description.name)
        self.assertEqual(first="candidate", second=Inherited.candidate.description.name)
        self.assertEqual(first=Total.__module__ + "." + Total.__qualname__, second=kind.name)
        with self.assertRaises(expected_exception=ValueError):
            class Ambiguous:
                first = kind.input(use=StateUse.CONSUME)
                second = first

    async def test_sdk_creates_allocation_analysis_and_operation_identities(self):
        instance = Sum(configuration=Settings(scale=2))
        hosted = self.host.adopt(
            contribution=instance,
            allocation=ResourceAllocation(grants=(ResourceGrant(kind="cpu", amount=1, unit="thread"),)),
            requirements=(ResourceRequirement(kind="cpu", amount=1, unit="thread"),),
        )
        analysis = self.host.open_analysis(allocations=(hosted.allocation,))
        reservation = analysis.reservation(allocation=hosted.allocation)
        limits = ExecutionLimits(queue_seconds=5, execution_seconds=5, cleanup_seconds=5, input_bytes=1024, result_bytes=1024)
        operations = [analysis.submit_method(
            method=instance.calculate, scientific_input=Numbers(values=(1,)),
            states=(), reservation=reservation, limits=limits, dispatch_guard=lambda: None,
        ) for _ in range(2)]
        self.assertIsNot(expr1=operations[0], expr2=operations[1])
        for operation in operations:
            await observe_completion(operation=operation)
        self.assertEqual(first=2, second=instance.calls)
        other = new_host()
        self.addAsyncCleanup(self.retire, host=other)
        with self.assertRaises(expected_exception=ValueError):
            other.open_analysis(allocations=(hosted.allocation,))
        await observe_cleanup(control=analysis.close())
        await self.retire(host=self.host)
        with self.assertRaises(expected_exception=CapacityUnavailable):
            self.host.open_analysis(allocations=(hosted.allocation,))

    async def asyncSetUp(self):
        self.host = new_host()
        self.addAsyncCleanup(self.retire, host=self.host)

    async def retire(self, *, host):
        async with asyncio.timeout(10):
            await observe_cleanup(control=host.retire())

    async def test_adoption_does_not_initialize_or_replace_the_supplied_receiver(self):
        instance = Sum(configuration=Settings(scale=2))
        marker = instance.marker
        hosted = adopt(host=self.host, contribution=instance)
        self.assertIs(expr1=instance, expr2=hosted.contribution)
        self.assertEqual(first=0, second=instance.initializations)
        operation, observed = await run_sum(host=self.host, hosted=hosted, operation_id="calculate")
        self.assertIs(expr1=Outcome.SUCCEEDED, expr2=observed.outcome)
        self.assertIs(expr1=marker, expr2=instance.marker)
        self.assertEqual(first=1, second=instance.initializations)
        self.assertEqual(first=1, second=instance.calls)
        result = hosted.call(method=instance.calculate).decode_result(payload=await operation.read_result(result=observed.result))
        self.assertEqual(first=Total(value=6), second=result)

    async def test_direct_scientific_methods_remain_ordinary_python(self):
        instance = Sum(configuration=Settings(scale=2))
        instance.initialize(assets=NoAssets())
        self.assertEqual(first=Total(value=12), second=instance.calculate(numbers=Numbers(values=(1, 2, 3))))
        instance.close()

    async def test_provider_setup_can_contain_ordinary_mutable_and_nonserializable_values(self):
        instance = Sum(configuration={"loader": lambda: None, "cache": []})
        hosted = adopt(host=self.host, contribution=instance)
        self.assertIs(expr1=instance, expr2=hosted.contribution)
        instance.configuration["cache"].append(object())
        self.assertIsNone(obj=hosted.description.configuration)

    async def test_identical_objects_cannot_exchange_bound_methods_or_portable_targets(self):
        from provider_core.hosting import Invocation

        first = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)), name="first")
        second = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)), name="second")
        analysis = self.host.open_analysis(analysis_id="identical", allocation_names=("first", "second"))
        reservation = next(item for item in analysis.reservations if item.allocation == "second")
        limits = ExecutionLimits(queue_seconds=5, execution_seconds=5, cleanup_seconds=5, input_bytes=1024, result_bytes=1024)
        for method in (first.contribution.calculate, Sum.calculate):
            with self.assertRaises(expected_exception=ValueError):
                analysis.submit_method(method=method,  scientific_input=Numbers(values=(1,)), states=(), reservation=reservation, limits=limits, dispatch_guard=lambda: None)
        refused = analysis.submit(invocation=Invocation(
            operation_id="portable", target=first.reference(method=first.contribution.calculate),
            payload=first.call(method=first.contribution.calculate).input_codec.encode(value=Numbers(values=(1,))),
            states=(), reservation=reservation, limits=limits,
        ), dispatch_guard=lambda: None)
        self.assertIsInstance(obj=refused, cls=NotAdmitted)
        self.assertEqual(first=0, second=second.contribution.calls)

    async def test_setup_is_private_and_requirements_determine_execution_contract(self):
        original = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)), name="original")
        same = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)), name="same")
        changed = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=3)), name="changed")
        requirements = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)), name="requirements", amount=2)
        self.assertNotEqual(first=original.description.binding_id, second=same.description.binding_id)
        self.assertNotEqual(first=original.allocation, second=same.allocation)
        self.assertEqual(first=original.description.binding_revision, second=changed.description.binding_revision)
        self.assertEqual(first=original.description.binding_revision, second=same.description.binding_revision)
        self.assertNotEqual(first=original.description.binding_revision, second=requirements.description.binding_revision)
        for other in (changed, requirements):
            self.assertNotEqual(first=original.description.binding_id, second=other.description.binding_id)

    async def test_only_this_receiver_or_its_exported_class_method_can_select_a_call(self):
        instance = Sum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)
        self.assertEqual(first=hosted.reference(method=Sum.calculate), second=hosted.reference(method=instance.calculate))
        for method in (Sum.helper, lambda: None, Sum(configuration=Settings(scale=2)).calculate):
            with self.subTest(method=method):
                with self.assertRaises(expected_exception=ValueError):
                    hosted.call(method=method)

    async def test_custody_cannot_be_replaced_or_deleted(self):
        instance = Sum(configuration=Settings(scale=2))
        for name in ("_custody",):
            with self.subTest(name=name):
                with self.assertRaises(expected_exception=AttributeError):
                    setattr(instance, name, object())
                with self.assertRaises(expected_exception=AttributeError):
                    delattr(instance, name)
        self.assertEqual(first=Settings(scale=2), second=instance.configuration)

    async def test_missing_annotations_fail_before_handover(self):
        class MissingInput(Sum):
            @operation()
            def calculate(self, *, numbers) -> Total:
                return Total(value=0)

        class MissingOutput(Sum):
            @operation()
            def calculate(self, *, numbers: Numbers):
                return Total(value=0)

        class MissingAssets(Sum):
            def initialize(self, *, assets) -> None:
                pass

        for contribution_type, method in ((MissingInput, "calculate"), (MissingOutput, "calculate"), (MissingAssets, "initialize")):
            instance = contribution_type(configuration=Settings(scale=2))
            with self.subTest(contribution_type=contribution_type):
                with self.assertRaisesRegex(expected_exception=TypeError, expected_regex=method):
                    adopt(host=self.host, contribution=instance)
                self.assertEqual(first=0, second=instance.closes)
        self.assertEqual(first=(), second=self.host.allocations)

    async def test_unsupported_operations_fail_before_initialization(self):
        class AsyncSum(Sum):
            @operation()
            async def calculate(self, *, numbers: Numbers) -> Total:
                return Total(value=0)

        class PositionalSum(Sum):
            @operation()
            def calculate(self, numbers: Numbers) -> Total:
                return Total(value=0)

        for contribution_type in (AsyncSum, PositionalSum):
            instance = contribution_type(configuration=Settings(scale=2))
            with self.assertRaises(expected_exception=TypeError):
                adopt(host=self.host, contribution=instance)
            self.assertEqual(first=0, second=instance.initializations)

    async def test_unmarked_override_removes_export(self):
        class PrivateSum(Sum):
            def calculate(self, *, numbers: Numbers) -> Total:
                return Total(value=0)
        with self.assertRaises(expected_exception=ValueError):
            adopt(host=self.host, contribution=PrivateSum(configuration=Settings(scale=2)))

    async def test_adoption_retains_original_lifecycle_callbacks(self):
        class ConfiguredSum(Sum):
            pass
        instance = ConfiguredSum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)
        original = ConfiguredSum.initialize
        def changed(self, *, assets: AssetAccess) -> None:
            raise AssertionError("An adopted lifecycle method was replaced")
        ConfiguredSum.initialize = changed
        try:
            _, observed = await run_sum(host=self.host, hosted=hosted, operation_id="run")
            self.assertEqual(first=(), second=observed.failures)
        finally:
            ConfiguredSum.initialize = original

    async def test_insufficient_grants_leave_object_available_for_valid_adoption(self):
        instance = Sum(configuration=Settings(scale=2))
        with self.assertRaises(expected_exception=ValueError):
            adopt(host=self.host, contribution=instance, amount=2, grant=1)
        self.assertEqual(first=0, second=instance.initializations)
        self.assertEqual(first=0, second=instance.closes)
        self.assertEqual(first=(), second=self.host.allocations)
        hosted = adopt(host=self.host, contribution=instance, amount=2, grant=2)
        self.assertIs(expr1=instance, expr2=hosted.contribution)

    async def test_one_instance_cannot_be_adopted_twice_or_reused_after_retirement(self):
        instance = Sum(configuration=Settings(scale=2))
        adopt(host=self.host, contribution=instance)
        other = new_host()
        self.addAsyncCleanup(self.retire, host=other)
        for host in (self.host, other):
            with self.assertRaises(expected_exception=ValueError):
                adopt(host=host, contribution=instance, name="duplicate")
        self.assertEqual(first=(), second=other.allocations)
        await self.retire(host=self.host)
        self.assertEqual(first=0, second=instance.initializations)
        self.assertEqual(first=1, second=instance.closes)
        with self.assertRaises(expected_exception=ValueError):
            adopt(host=other, contribution=instance)
        self.assertEqual(first=1, second=instance.closes)

    async def test_competing_event_loop_threads_cannot_adopt_the_same_object(self):
        instance = Sum(configuration=Settings(scale=2))
        barrier = Barrier(parties=2)
        allocation_counts = []
        def compete():
            async def attempt():
                host = new_host()
                barrier.wait(timeout=10)
                try:
                    return adopt(host=host, contribution=instance)
                finally:
                    allocation_counts.append(len(host.allocations))
                    await observe_cleanup(control=host.retire())
            return asyncio.run(main=attempt())
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(compete) for _ in range(2)]
            results = await asyncio.gather(
                *[asyncio.wrap_future(future=future) for future in futures], return_exceptions=True,
            )
        self.assertCountEqual(first=[HostedContribution, ValueError], second=[type(result) for result in results])
        self.assertCountEqual(first=[0, 1], second=allocation_counts)
        failure, = [result for result in results if isinstance(result, ValueError)]
        self.assertIn(member="already owns", container=str(failure))
        self.assertEqual(first=0, second=instance.initializations)
        self.assertEqual(first=1, second=instance.closes)

    async def test_legacy_allocation_cannot_bypass_object_handover(self):
        instance = Sum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)
        with self.assertRaises(expected_exception=TypeError):
            EmbeddedHost(allocations=(Allocation(name="bypass", binding=hosted, grants=(ResourceGrant(kind="cpu", amount=4, unit="thread"),)),), limits=self.host.limits)

    async def test_reservation_rejects_foreign_ownership_and_missing_input_before_dispatch(self):
        hosted = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)))
        analysis = self.host.open_analysis(analysis_id="test", allocation_names=("sum",))
        reservation, = analysis.reservations
        limits = ExecutionLimits(queue_seconds=5, execution_seconds=5, cleanup_seconds=5, input_bytes=1024, result_bytes=1024)
        for forged in (replace(reservation, host="other"), replace(reservation, allocation="missing"), replace(reservation, analysis="other")):
            refused = analysis.submit_method(method=Sum.calculate,  scientific_input=Numbers(values=(1,)), states=(), reservation=forged, limits=limits, dispatch_guard=lambda: None)
            self.assertIsInstance(obj=refused, cls=NotAdmitted)
            self.assertIs(expr1=AdmissionCode.INVALID_REFERENCE, expr2=refused.code)
        with self.assertRaises(expected_exception=TypeError):
            analysis.submit_method(method=hosted.contribution.calculate,  states=(), reservation=reservation, limits=limits, dispatch_guard=lambda: None)
        self.assertEqual(first=0, second=hosted.contribution.calls)
        self.assertEqual(first={}, second=self.host.operations)

    async def test_distinct_receivers_share_configuration_but_not_runtime_state(self):
        configuration = Settings(scale=3)
        first = Sum(configuration=configuration)
        second = Sum(configuration=configuration)
        handles = [adopt(host=self.host, contribution=instance, name=name) for instance, name in ((first, "first"), (second, "second"))]
        for hosted in handles:
            _, observed = await run_sum(host=self.host, hosted=hosted, operation_id=hosted.allocation.allocation)
            self.assertEqual(first=(), second=observed.failures)
        self.assertIs(expr1=first.configuration, expr2=second.configuration)
        self.assertEqual(first=1, second=first.calls)
        self.assertEqual(first=1, second=second.calls)

    async def test_initialization_and_cleanup_failures_retain_the_exact_partial_owner(self):
        host = new_host()
        instance = FailedInitialization(configuration=Settings(scale=2))
        hosted = adopt(host=host, contribution=instance)
        resident = host.residents["sum"]
        self.addCleanup(resident.executor.shutdown, wait=True)
        _, completed = await run_sum(host=host, hosted=hosted, operation_id="failure")
        self.assertIs(expr1=instance.startup_failure, expr2=completed.failures[0].cause)
        with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
            await self.retire(host=host)
        self.assertIs(expr1=instance, expr2=resident.value)
        self.assertIs(expr1=instance.partial_resource, expr2=instance.cleanup_resource)
        self.assertIs(expr1=instance.cleanup_failure, expr2=raised.exception.exceptions[0])
        self.assertFalse(expr=resident.retired)
        # The fixture owns no external resource; stop its executor after asserting
        # retained failure evidence, without retrying the failed disposer.
