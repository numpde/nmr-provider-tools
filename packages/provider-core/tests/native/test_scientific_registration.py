"""Prove exact-object handover and failure ownership through the installed host."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Barrier, Event, current_thread
import unittest

from provider_core.embedded_host import Allocation, InProcessExecutionHost
from provider_core.hosting import ResourceState, CapacityUnavailable, AdmissionCode, OperationPolicy, NotAdmitted, Outcome, RequestConflict, StartDeadlineExpired, DispatchState, Invocation, observe_cleanup, observe_completion
from provider_core.scientific import AssetAccess, Contribution, HostedContribution, IsolationRequirement, NoAssets, ResourceRequirement, operation


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


def new_host() -> InProcessExecutionHost:
    return InProcessExecutionHost(
        input_bytes=8192,
        result_bytes=8192,
        state_values=8,
        progress_records=8,
        initialization_seconds=5,
        retirement_seconds=5,
    )


def deadline_policy() -> OperationPolicy:
    return OperationPolicy(
        queue_stop_request_seconds=5,
        execution_stop_request_seconds=5,
        cleanup_overdue_flag_seconds=5,
        reject_encoded_input_over_bytes=1024,
        reject_encoded_result_over_bytes=1024,
    )


# silent-defaults: allow-contract -- Tests vary the scientific CPU requirement.
def adopt(*, host, contribution, amount=1):
    return host.adopt(
        contribution=contribution,
        requirements=frozenset({ResourceRequirement(kind="cpu", amount=amount, unit="thread")}),
    )


async def run_sum(*, host, hosted, operation_id):
    analysis = host.open_analysis(analysis_id=operation_id, allocation_names=(hosted.allocation.allocation,))
    (reservation,) = analysis.reservations

    operation = analysis.submit_method(
        method=hosted.contribution.calculate,
        scientific_input=Numbers(values=(1, 2)),
        states={},
        reservation=reservation,
        policy=OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=5,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=1024,
            reject_encoded_result_over_bytes=1024,
        ),
    )

    async with asyncio.timeout(10):
        observed = await observe_completion(operation=operation)

        await observe_cleanup(control=analysis.close())

    return (operation, observed)


class AdoptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_constructor_validates_its_explicit_policy_arguments(self):
        options = {
            "input_bytes": 8192,
            "result_bytes": 8192,
            "state_values": 8,
            "progress_records": 8,
            "initialization_seconds": 5,
            "retirement_seconds": 5,
        }

        for policy_name in options:
            for policy_setting in (True, "5"):
                with self.subTest(name=policy_name, value=policy_setting):
                    with self.assertRaises(expected_exception=TypeError):
                        InProcessExecutionHost(**(options | {policy_name: policy_setting}))

            for policy_setting in (0, -1):
                with self.subTest(name=policy_name, value=policy_setting):
                    with self.assertRaises(expected_exception=ValueError):
                        InProcessExecutionHost(**(options | {policy_name: policy_setting}))

        for policy_name in ("initialization_seconds", "retirement_seconds"):
            for policy_setting in (float("inf"), float("nan")):
                with self.subTest(name=policy_name, value=policy_setting):
                    with self.assertRaises(expected_exception=ValueError):
                        InProcessExecutionHost(**(options | {policy_name: policy_setting}))

    async def test_released_local_operations_leave_no_submission_history(self):
        hosted = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)))
        analysis = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        (reservation,) = analysis.reservations

        operation_policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=5,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=1024,
            reject_encoded_result_over_bytes=1024,
        )

        for _ in range(20):
            operation = analysis.submit_method(
                method=hosted.contribution.calculate,
                scientific_input=Numbers(values=(1,)),
                states={},
                reservation=reservation,
                policy=operation_policy,
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
            (operation, completed) = await run_sum(
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

        replacement = Sum(configuration=Settings(scale=2))
        hosted_replacement = adopt(host=self.host, contribution=replacement)

        self.assertIs(expr1=replacement, expr2=hosted_replacement.contribution)
        self.assertEqual(first={}, second=self.host.analyses)

    async def test_state_ports_use_python_types_and_attribute_names(self):
        from provider_core.scientific import StateType, StateUse

        kind = StateType(value_type=Total)

        class Ports:
            produced = kind.output(dispose=lambda unused_total: None)
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
            requirements=frozenset({ResourceRequirement(kind="cpu", amount=1, unit="thread")}),
        )

        analysis = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        reservation = analysis.reservation(allocation=hosted.allocation)

        operation_policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=5,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=1024,
            reject_encoded_result_over_bytes=1024,
        )

        operations = [
            analysis.submit_method(
                method=instance.calculate,
                scientific_input=Numbers(values=(1,)),
                states={},
                reservation=reservation,
                policy=operation_policy,
            )
            for _ in range(2)
        ]

        self.assertIsNot(expr1=operations[0], expr2=operations[1])

        for operation in operations:
            await observe_completion(operation=operation)

        self.assertEqual(first=2, second=instance.calls)
        other = new_host()

        self.addAsyncCleanup(self.retire, host=other)

        with self.assertRaises(expected_exception=ValueError):
            other.open_analysis(allocations=frozenset({hosted.allocation}))

        await observe_cleanup(control=analysis.close())
        await self.retire(host=self.host)

        with self.assertRaises(expected_exception=CapacityUnavailable):
            self.host.open_analysis(allocations=frozenset({hosted.allocation}))

    async def asyncSetUp(self):
        self.host = new_host()

        self.addAsyncCleanup(self.retire, host=self.host)

    async def retire(self, *, host):
        async with asyncio.timeout(10):
            await observe_cleanup(control=host.retire())

    async def test_class_declaration_compiles_without_constructing_a_receiver(self):
        requirements = frozenset({ResourceRequirement(kind="cpu", amount=1, unit="thread")})
        class_only = HostedContribution(
            contribution=None,
            contribution_type=Sum,
            host="remote-host",
            allocation_name="prediction",
            requirements=requirements,
        )
        instance = Sum(configuration=Settings(scale=2))
        adopted = self.host.adopt(contribution=instance, requirements=requirements)

        self.assertIsNone(obj=class_only.contribution)
        self.assertEqual(first=adopted.description.binding_revision, second=class_only.description.binding_revision)
        self.assertEqual(
            first=adopted.call(method=Sum.calculate).description,
            second=class_only.call(method=Sum.calculate).description,
        )

        with self.assertRaises(expected_exception=ValueError):
            class_only.call(method=instance.calculate)

    async def test_class_handle_does_not_turn_an_unbound_method_into_a_receiver(self):
        instance = Sum(configuration=Settings(scale=2))
        hosted = self.host.adopt_class(
            contribution_type=Sum,
            constructor=lambda: instance,
            requirements=frozenset(),
        )
        analysis = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        reservation = analysis.reservation(allocation=hosted.allocation)

        with self.assertRaises(expected_exception=ValueError):
            analysis.submit_method(
                method=Sum.calculate,
                scientific_input=Numbers(values=(1, 2)),
                states={},
                reservation=reservation,
                policy=deadline_policy(),
            )

        self.assertEqual(first=0, second=instance.calls)

        call = hosted.call(method=Sum.calculate)
        operation = analysis.submit_call(
            call=call,
            payload=call.encode_input(value=Numbers(values=(1, 2))),
            states={},
            reservation=reservation,
            policy=deadline_policy(),
        )
        observed = await observe_completion(operation=operation)

        self.assertIs(expr1=Outcome.SUCCEEDED, expr2=observed.outcome)
        self.assertEqual(first=1, second=instance.calls)

        await observe_cleanup(control=operation.release_result(result=observed.require_result()))
        await observe_cleanup(control=analysis.close())

    async def test_host_owned_construction_starts_after_factory_custody(self):
        constructed_instances = []
        worker_threads = []

        def construct() -> Sum:
            worker_threads.append(current_thread())

            instance = Sum(configuration=Settings(scale=2))

            constructed_instances.append(instance)

            return instance

        hosted = self.host.adopt_class(
            contribution_type=Sum,
            constructor=construct,
            requirements=frozenset({ResourceRequirement(kind="cpu", amount=1, unit="thread")}),
        )

        self.assertEqual(first=[], second=constructed_instances)

        await self.host.initialize(allocation=hosted.allocation)

        (instance,) = constructed_instances

        self.assertIsNot(expr1=current_thread(), expr2=worker_threads[0])
        self.assertEqual(first=1, second=instance.initializations)

        with self.assertRaises(expected_exception=ValueError):
            self.host.adopt(
                contribution=instance,
                requirements=frozenset({ResourceRequirement(kind="cpu", amount=1, unit="thread")}),
            )

    async def test_adoption_does_not_initialize_or_replace_the_supplied_receiver(self):
        instance = Sum(configuration=Settings(scale=2))
        marker = instance.marker
        hosted = adopt(host=self.host, contribution=instance)

        self.assertIs(expr1=instance, expr2=hosted.contribution)
        self.assertEqual(first=0, second=instance.initializations)
        (operation, observed) = await run_sum(host=self.host, hosted=hosted, operation_id="calculate")

        self.assertIs(expr1=Outcome.SUCCEEDED, expr2=observed.outcome)
        self.assertIs(expr1=marker, expr2=instance.marker)
        self.assertEqual(first=1, second=instance.initializations)
        self.assertEqual(first=1, second=instance.calls)

        calculated_total = (
            hosted
            .call(method=instance.calculate)
            .decode_result(payload=await operation.read_result(result=observed.result))
        )

        self.assertEqual(first=Total(value=6), second=calculated_total)

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

        first = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)))
        second = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)))
        analysis = self.host.open_analysis(allocations=frozenset({first.allocation, second.allocation}))
        reservation = analysis.reservation(allocation=second.allocation)

        operation_policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=5,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=1024,
            reject_encoded_result_over_bytes=1024,
        )

        for method in (first.contribution.calculate, Sum.calculate):
            with self.assertRaises(expected_exception=ValueError):
                analysis.submit_method(method=method,  scientific_input=Numbers(values=(1,)), states={}, reservation=reservation, policy=operation_policy)

        refused = analysis.submit(
            invocation=Invocation(
                operation_id="portable",
                target=first.reference(method=first.contribution.calculate),
                payload=first.call(method=first.contribution.calculate).input_codec.encode(value=Numbers(values=(1,))),
                states={},
                reservation=reservation,
                policy=operation_policy,
            )
        )

        self.assertIsInstance(obj=refused, cls=NotAdmitted)
        self.assertEqual(first=0, second=second.contribution.calls)

    async def test_requirements_export_in_canonical_order_and_ignore_identical_repeats(self):
        from provider_core.contribution import description_document

        cpu = ResourceRequirement(kind="cpu", amount=1, unit="thread")
        selected_cpu = ResourceRequirement(kind="cpu", amount=2, unit="thread", selector="avx")
        memory = ResourceRequirement(kind="host-memory", amount=4096, unit="byte")

        first = self.host.adopt(
            contribution=Sum(configuration=Settings(scale=2)),
            requirements=frozenset({memory, selected_cpu, cpu}),
            resident_resources=frozenset({memory, selected_cpu, cpu}),
        )

        repeated = self.host.adopt(
            contribution=Sum(configuration=Settings(scale=2)),
            requirements=frozenset([cpu, selected_cpu, memory, cpu]),
            resident_resources=frozenset([cpu, selected_cpu, memory, cpu]),
        )

        self.assertEqual(first=first.description.binding_revision, second=repeated.description.binding_revision)
        description_fields = description_document(description=first.description)

        for exported_requirements in (
            description_fields["resident_resources"],
            description_fields["calls"][0]["resources"],
        ):
            self.assertEqual(
                first=[("cpu", None), ("cpu", "avx"), ("host-memory", None)],
                second=[
                    (requirement_fields["kind"], requirement_fields["selector"])
                    for requirement_fields in exported_requirements
                ],
            )

    async def test_requirement_conflicts_refuse_adoption_without_claiming_the_object(self):
        cpu = ResourceRequirement(kind="cpu", amount=1, unit="thread")

        for conflict in (replace(cpu, amount=2), replace(cpu, exclusive=True)):
            for field in ("requirements", "resident_resources"):
                with self.subTest(conflict=conflict, field=field):
                    instance = Sum(configuration=Settings(scale=2))
                    declaration = {"requirements": frozenset({cpu}), field: frozenset({cpu, conflict})}

                    with self.assertRaises(expected_exception=ValueError):
                        self.host.adopt(contribution=instance, **declaration)

                    adopted = adopt(host=self.host, contribution=instance)

                    self.assertIs(expr1=instance, expr2=adopted.contribution)

    async def test_requirements_reject_ordered_or_mutable_collections_and_wrong_records(self):
        cpu = ResourceRequirement(kind="cpu", amount=1, unit="thread")

        for invalid in ((cpu,), [cpu], {cpu}, frozenset({"cpu"})):
            with self.subTest(invalid=invalid):
                instance = Sum(configuration=Settings(scale=2))

                with self.assertRaises(expected_exception=TypeError):
                    self.host.adopt(contribution=instance, requirements=invalid)

                adopted = adopt(host=self.host, contribution=instance)

                self.assertIs(expr1=instance, expr2=adopted.contribution)

    async def test_explicit_empty_operation_requirements_override_common_requirements(self):
        class UnrestrictedSum(Sum):
            @operation(requirements=frozenset())
            def calculate(self, *, numbers: Numbers) -> Total:
                return super().calculate(numbers=numbers)

        hosted = adopt(host=self.host, contribution=UnrestrictedSum(configuration=Settings(scale=2)))
        (call,) = hosted.description.calls

        self.assertEqual(first=frozenset(), second=call.resources)
        self.assertEqual(first=1, second=len(hosted.requirements))

    async def test_hosted_handles_form_stable_sets_for_unhashable_science(self):
        @dataclass(init=False)
        class EqualSum(Sum):
            configuration: Settings
            calls: int

        first = EqualSum(configuration=Settings(scale=2))
        second = EqualSum(configuration=Settings(scale=2))

        self.assertEqual(first=first, second=second)
        first_handle = adopt(host=self.host, contribution=first)
        second_handle = adopt(host=self.host, contribution=second)
        offerings = frozenset({first_handle, second_handle})
        first.calls += 1

        self.assertEqual(first=2, second=len(offerings))
        self.assertIn(member=first_handle, container=offerings)
        self.assertIn(member=second_handle, container=offerings)

    async def test_allocation_inventory_can_be_selected_without_an_ordered_conversion(self):
        first = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)))
        second = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=3)))

        self.assertEqual(first=frozenset({first.allocation, second.allocation}), second=self.host.allocations)
        execution = self.host.open_analysis(allocations=self.host.allocations)

        self.assertEqual(first=2, second=len(execution.reservations))
        self.assertIsInstance(obj=execution.reservations, cls=frozenset)

        for hosted in (first, second):
            reservation = execution.reservation(allocation=hosted.allocation)

            self.assertEqual(first=hosted.allocation.binding_id, second=reservation.binding_id)

        await observe_cleanup(control=execution.close())

    async def test_session_stop_preserves_results_and_prevents_more_science(self):
        instance = Sum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)
        execution = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        reservation = execution.reservation(allocation=hosted.allocation)

        first = execution.submit_method(
            method=instance.calculate,
            scientific_input=Numbers(values=(1,)),
            states={},
            reservation=reservation,
            policy=deadline_policy(),
        )

        completed = await observe_completion(operation=first)
        retained = await first.read_result(result=completed.result)

        waiting = execution.submit_method(
            method=instance.calculate,
            scientific_input=Numbers(values=(2,)),
            states={},
            reservation=reservation,
            policy=deadline_policy(),
        )

        execution.request_stop()

        refused = execution.submit_method(
            method=instance.calculate,
            scientific_input=Numbers(values=(3,)),
            states={},
            reservation=reservation,
            policy=deadline_policy(),
        )

        self.assertIsInstance(obj=refused, cls=NotAdmitted)
        self.assertIs(expr1=AdmissionCode.CLOSED, expr2=refused.code)
        stopped = await observe_completion(operation=waiting)

        self.assertIs(expr1=DispatchState.NOT_STARTED, expr2=stopped.dispatch)
        self.assertEqual(first=1, second=instance.calls)
        self.assertEqual(first=retained, second=await first.read_result(result=completed.result))
        self.assertEqual(first=0, second=instance.closes)
        await observe_cleanup(control=first.release_result(result=completed.result))
        await observe_cleanup(control=execution.close())

    async def test_invalid_or_expired_start_deadlines_admit_no_work(self):
        instance = Sum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)
        execution = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        reservation = execution.reservation(allocation=hosted.allocation)

        for invalid in (True, "later", float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                expected = TypeError if type(invalid) in (bool, str) else ValueError

                with self.assertRaises(expected_exception=expected):
                    execution.submit_method(
                        method=instance.calculate,
                        scientific_input=Numbers(values=(1,)),
                        states={},
                        reservation=reservation,
                        policy=deadline_policy(),
                        start_not_after=invalid,
                    )

        with self.assertRaises(expected_exception=StartDeadlineExpired):
            execution.submit_method(
                method=instance.calculate,
                scientific_input=Numbers(values=(1,)),
                states={},
                reservation=reservation,
                policy=deadline_policy(),
                start_not_after=asyncio.get_running_loop().time() - 1,
            )

        self.assertEqual(first={}, second=execution.operations)
        self.assertEqual(first=0, second=self.host.input_used)
        self.assertEqual(first=0, second=self.host.result_reserved)
        self.assertEqual(first=0, second=instance.initializations)
        self.assertEqual(first=0, second=instance.calls)
        await observe_cleanup(control=execution.close())

    async def test_expired_start_deadline_is_not_masked_by_full_result_capacity(self):
        instance = Sum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)
        execution = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        reservation = execution.reservation(allocation=hosted.allocation)

        held = execution.submit_method(
            method=instance.calculate,
            scientific_input=Numbers(values=(1,)),
            states={},
            reservation=reservation,
            policy=replace(deadline_policy(), reject_encoded_result_over_bytes=self.host.result_bytes),
        )

        completed = await observe_completion(operation=held)

        self.assertEqual(first=self.host.result_bytes, second=self.host.result_reserved)

        with self.assertRaises(expected_exception=StartDeadlineExpired):
            execution.submit_method(
                method=instance.calculate,
                scientific_input=Numbers(values=(2,)),
                states={},
                reservation=reservation,
                policy=deadline_policy(),
                start_not_after=asyncio.get_running_loop().time() - 1,
            )

        self.assertEqual(first=1, second=instance.calls)
        self.assertEqual(first=1, second=len(execution.operations))
        await observe_cleanup(control=held.release_result(result=completed.result))
        await observe_cleanup(control=execution.close())

    async def test_queued_deadline_and_replay_keep_the_original_start_window(self):
        entered = Event()
        release = Event()

        class HeldSum(Sum):
            @operation()
            def calculate(self, *, numbers: Numbers) -> Total:
                self.calls += 1

                if self.calls == 1:
                    entered.set()
                    release.wait(timeout=5)

                return Total(value=sum(numbers.values))

        instance = HeldSum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)
        execution = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        reservation = execution.reservation(allocation=hosted.allocation)

        first = execution.submit_method(
            method=instance.calculate,
            scientific_input=Numbers(values=(1,)),
            states={},
            reservation=reservation,
            policy=deadline_policy(),
        )

        try:
            self.assertTrue(expr=await asyncio.to_thread(entered.wait, 2))
            deadline = asyncio.get_running_loop().time() + 0.05

            waiting = execution.submit(
                invocation=Invocation(
                    target=hosted.reference(method=instance.calculate),
                    payload=hosted.call(method=instance.calculate).input_codec.encode(value=Numbers(values=(2,))),
                    states={},
                    reservation=reservation,
                    policy=deadline_policy(),
                    start_not_after=deadline,
                )
            )

            await asyncio.sleep(delay=max(0, deadline - asyncio.get_running_loop().time()) + 0.01)
        finally:
            release.set()

        await observe_completion(operation=first)
        completed = await observe_completion(operation=waiting)

        self.assertIs(expr1=DispatchState.NOT_STARTED, expr2=completed.dispatch)
        self.assertIsInstance(obj=completed.failures[0].cause, cls=StartDeadlineExpired)
        self.assertEqual(first=1, second=instance.calls)
        self.assertIs(expr1=waiting, expr2=execution.submit(invocation=waiting.invocation))

        with self.assertRaises(expected_exception=RequestConflict):
            execution.submit(invocation=replace(waiting.invocation, start_not_after=deadline + 10))

        await observe_cleanup(control=execution.close())

    async def test_deadline_expiring_during_initialization_prevents_calculation(self):
        entered = Event()
        release = Event()

        class LoadingSum(Sum):
            def initialize(self, *, assets: AssetAccess) -> None:
                self.initializations += 1

                entered.set()
                release.wait(timeout=5)

        instance = LoadingSum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)
        execution = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        deadline = asyncio.get_running_loop().time() + 1

        operation = execution.submit_method(
            method=instance.calculate,
            scientific_input=Numbers(values=(1,)),
            states={},
            reservation=execution.reservation(allocation=hosted.allocation),
            policy=deadline_policy(),
            start_not_after=deadline,
        )

        try:
            self.assertTrue(expr=await asyncio.to_thread(entered.wait, 2))
            await asyncio.sleep(delay=max(0, deadline - asyncio.get_running_loop().time()) + 0.01)
        finally:
            release.set()

        completed = await observe_completion(operation=operation)

        self.assertIsInstance(obj=completed.failures[0].cause, cls=StartDeadlineExpired)
        self.assertEqual(first=1, second=instance.initializations)
        self.assertEqual(first=0, second=instance.calls)
        await observe_cleanup(control=execution.close())
        await observe_cleanup(control=self.host.retire())
        self.assertEqual(first=1, second=instance.closes)

    async def test_setup_is_private_and_requirements_determine_execution_contract(self):
        original = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)))
        same = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)))
        changed = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=3)))
        requirements = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)), amount=2)

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

        for (contribution_type, method) in ((MissingInput, "calculate"), (MissingOutput, "calculate"), (MissingAssets, "initialize")):
            instance = contribution_type(configuration=Settings(scale=2))

            with self.subTest(contribution_type=contribution_type):
                with self.assertRaisesRegex(expected_exception=TypeError, expected_regex=method):
                    adopt(host=self.host, contribution=instance)

                self.assertEqual(first=0, second=instance.closes)

        self.assertEqual(first=frozenset(), second=self.host.allocations)

    async def test_invalid_analysis_class_rejected_before_handover(self):
        class UnrelatedAnalysis(Sum):
            analysis_type = str

        class NonClassAnalysis(Sum):
            analysis_type = "not-a-class"

        for contribution_type in (UnrelatedAnalysis, NonClassAnalysis):
            instance = contribution_type(configuration=Settings(scale=2))

            with self.subTest(contribution_type=contribution_type):
                with self.assertRaises(TypeError):
                    adopt(host=self.host, contribution=instance)

                self.assertEqual(0, instance.initializations)
                self.assertEqual(0, instance.closes)
                self.assertEqual(frozenset(), self.host.allocations)

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

    async def test_operation_aliases_fail_before_host_custody(self):
        class AliasedSum(Sum):
            calculate_again = Sum.calculate

        instance = AliasedSum(configuration=Settings(scale=2))

        with self.assertRaises(expected_exception=ValueError) as rejected:
            adopt(host=self.host, contribution=instance)

        self.assertIn(member="calculate", container=str(rejected.exception))
        self.assertIn(member="calculate_again", container=str(rejected.exception))
        self.assertEqual(first=0, second=instance.initializations)
        self.assertEqual(first=0, second=instance.closes)
        self.assertEqual(first=frozenset(), second=self.host.allocations)

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
            (_, observed) = await run_sum(host=self.host, hosted=hosted, operation_id="run")

            self.assertEqual(first=(), second=observed.failures)
        finally:
            ConfiguredSum.initialize = original

    async def test_invalid_requirements_leave_object_available_for_valid_adoption(self):
        instance = Sum(configuration=Settings(scale=2))

        with self.assertRaises(expected_exception=ValueError):
            adopt(host=self.host, contribution=instance, amount=0)

        self.assertEqual(first=0, second=instance.initializations)
        self.assertEqual(first=0, second=instance.closes)
        self.assertEqual(first=frozenset(), second=self.host.allocations)
        hosted = adopt(host=self.host, contribution=instance, amount=2)

        self.assertIs(expr1=instance, expr2=hosted.contribution)

    async def test_unsupported_isolation_leaves_object_available_for_valid_adoption(self):
        instance = Sum(configuration=Settings(scale=2))

        with self.assertRaises(expected_exception=ValueError):
            self.host.adopt(
                contribution=instance,
                requirements=frozenset({ResourceRequirement(kind="cpu", amount=1, unit="thread")}),
                isolation=IsolationRequirement.DEDICATED_PROCESS,
            )

        self.assertEqual(first=0, second=instance.initializations)
        self.assertEqual(first=0, second=instance.closes)
        self.assertEqual(first=frozenset(), second=self.host.allocations)
        hosted = adopt(host=self.host, contribution=instance)

        self.assertIs(expr1=instance, expr2=hosted.contribution)

    async def test_one_instance_cannot_be_adopted_twice_or_reused_after_retirement(self):
        instance = Sum(configuration=Settings(scale=2))

        adopt(host=self.host, contribution=instance)
        other = new_host()

        self.addAsyncCleanup(self.retire, host=other)

        for host in (self.host, other):
            with self.assertRaises(expected_exception=ValueError):
                adopt(host=host, contribution=instance)

        self.assertEqual(first=frozenset(), second=other.allocations)
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

            adoption_outcomes = await asyncio.gather(
                *[asyncio.wrap_future(future=future) for future in futures],
                return_exceptions=True,
            )

        self.assertCountEqual(
            first=[HostedContribution, ValueError],
            second=[type(adoption_outcome) for adoption_outcome in adoption_outcomes],
        )

        self.assertCountEqual(first=[0, 1], second=allocation_counts)

        (failure,) = [
            adoption_outcome
            for adoption_outcome in adoption_outcomes
            if isinstance(adoption_outcome, ValueError)
        ]

        self.assertIn(member="already owns", container=str(failure))
        self.assertEqual(first=0, second=instance.initializations)
        self.assertEqual(first=1, second=instance.closes)

    async def test_legacy_allocation_cannot_bypass_object_handover(self):
        instance = Sum(configuration=Settings(scale=2))
        hosted = adopt(host=self.host, contribution=instance)

        with self.assertRaises(expected_exception=TypeError):
            InProcessExecutionHost(
                allocations=(Allocation(name="bypass", binding=hosted),),
                input_bytes=8192,
                result_bytes=8192,
                state_values=8,
                progress_records=8,
                initialization_seconds=5,
                retirement_seconds=5,
            )

    async def test_reservation_rejects_foreign_ownership_and_missing_input_before_dispatch(self):
        hosted = adopt(host=self.host, contribution=Sum(configuration=Settings(scale=2)))
        analysis = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        (reservation,) = analysis.reservations

        operation_policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=5,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=1024,
            reject_encoded_result_over_bytes=1024,
        )

        for forged in (replace(reservation, host="other"), replace(reservation, allocation="missing"), replace(reservation, analysis="other")):
            refused = analysis.submit_method(method=Sum.calculate,  scientific_input=Numbers(values=(1,)), states={}, reservation=forged, policy=operation_policy)

            self.assertIsInstance(obj=refused, cls=NotAdmitted)
            self.assertIs(expr1=AdmissionCode.INVALID_REFERENCE, expr2=refused.code)

        with self.assertRaises(expected_exception=TypeError):
            analysis.submit_method(method=hosted.contribution.calculate,  states={}, reservation=reservation, policy=operation_policy)

        self.assertEqual(first=0, second=hosted.contribution.calls)
        self.assertEqual(first={}, second=self.host.operations)

    async def test_distinct_receivers_share_configuration_but_not_runtime_state(self):
        configuration = Settings(scale=3)
        first = Sum(configuration=configuration)
        second = Sum(configuration=configuration)
        handles = [adopt(host=self.host, contribution=instance) for instance in (first, second)]

        for hosted in handles:
            (_, observed) = await run_sum(host=self.host, hosted=hosted, operation_id=hosted.allocation.allocation)

            self.assertEqual(first=(), second=observed.failures)

        self.assertIs(expr1=first.configuration, expr2=second.configuration)
        self.assertEqual(first=1, second=first.calls)
        self.assertEqual(first=1, second=second.calls)

    async def test_initialization_and_cleanup_failures_retain_the_exact_partial_owner(self):
        host = new_host()
        instance = FailedInitialization(configuration=Settings(scale=2))
        hosted = adopt(host=host, contribution=instance)
        resident = host.residents[hosted.allocation.allocation]

        self.addCleanup(resident.executor.shutdown, wait=True)
        (_, completed) = await run_sum(host=host, hosted=hosted, operation_id="failure")

        self.assertIs(expr1=instance.startup_failure, expr2=completed.failures[0].cause)

        with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
            await self.retire(host=host)

        self.assertIs(expr1=instance, expr2=resident.instance)
        self.assertIs(expr1=instance.partial_resource, expr2=instance.cleanup_resource)
        self.assertIs(expr1=instance.cleanup_failure, expr2=raised.exception.exceptions[0])
        self.assertFalse(expr=resident.retired)
        # The fixture owns no external resource; stop its executor after asserting
        # retained failure evidence, without retrying the failed disposer.
