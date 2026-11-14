"""Exercise configured methods through the installed scientific host."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import unittest

from provider_core.embedded_host import EmbeddedHost, HostLimits
from provider_core.hosting import AdmissionCode, ExecutionLimits, NotAdmitted, Outcome, ResourceGrant, observe_cleanup, observe_completion
from provider_core.scientific import AssetAccess, Contribution, NoAssets, Registration, ResourceRequirement, operation


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    scale: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Numbers:
    values: tuple[int, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Total:
    value: int


class Sum(Contribution[Settings]):
    def initialize(self, *, assets: AssetAccess) -> None:
        self.calls = 0

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


# silent-defaults: allow-contract -- Individual tests vary one part of this fixed two-times-sum contract.
def register_sum(*, contribution=Sum, scale=2, amount=1) -> Registration:
    return Registration(
        contribution=contribution, configuration=Settings(scale=scale),
        resources=(ResourceRequirement(kind="cpu", amount=amount, unit="thread"),),
    )


class RegistrationTests(unittest.TestCase):
    def test_registration_is_effect_free_and_direct_methods_remain_python(self):
        registration = register_sum()
        instance = registration.construct_instance()
        self.assertFalse(expr=hasattr(instance, "calls"))
        registration.initialize_instance(instance=instance, assets=NoAssets())
        result = instance.calculate(numbers=Numbers(values=(1, 2, 3)))
        self.assertEqual(first=Total(value=12), second=result)
        self.assertEqual(first=1, second=instance.calls)

    def test_config_and_contract_changes_have_distinct_revisions(self):
        original = register_sum()
        same = register_sum()
        changed = register_sum(scale=3)
        resources = register_sum(amount=2)
        self.assertEqual(first=original.description, second=same.description)
        self.assertNotEqual(first=original.description.binding_revision, second=changed.description.binding_revision)
        self.assertNotEqual(first=original.description.binding_revision, second=resources.description.binding_revision)

    def test_only_actual_exported_members_select_operations(self):
        registration = register_sum()
        self.assertEqual(first="calculate", second=registration.reference(method=Sum.calculate).name)
        with self.assertRaises(expected_exception=ValueError):
            registration.call(method=Sum.helper)
        with self.assertRaises(expected_exception=ValueError):
            registration.call(method=lambda: None)

    def test_registered_configuration_cannot_be_rebound(self):
        instance = register_sum().construct_instance()
        with self.assertRaises(expected_exception=AttributeError):
            instance.configuration = Settings(scale=9)
        with self.assertRaises(expected_exception=AttributeError):
            instance._configuration = Settings(scale=9)
        with self.assertRaises(expected_exception=AttributeError):
            del instance._configuration
        self.assertEqual(first=Settings(scale=2), second=instance.configuration)

    def test_missing_annotations_fail_at_registration_with_the_method_named(self):
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

        for contribution, method in ((MissingInput, "calculate"), (MissingOutput, "calculate"), (MissingAssets, "initialize")):
            with self.subTest(contribution=contribution):
                with self.assertRaisesRegex(expected_exception=TypeError, expected_regex=method):
                    register_sum(contribution=contribution)

    def test_async_and_positional_methods_are_rejected_before_initialization(self):
        class AsyncSum(Sum):
            @operation()
            async def calculate(self, *, numbers: Numbers) -> Total:
                return Total(value=0)

        class PositionalSum(Sum):
            @operation()
            def calculate(self, numbers: Numbers) -> Total:
                return Total(value=0)

        for contribution in (AsyncSum, PositionalSum):
            with self.subTest(contribution=contribution):
                with self.assertRaises(expected_exception=TypeError):
                    register_sum(contribution=contribution)

    def test_unmarked_override_removes_the_operation(self):
        class PrivateSum(Sum):
            def calculate(self, *, numbers: Numbers) -> Total:
                return Total(value=0)

        with self.assertRaises(expected_exception=ValueError):
            register_sum(contribution=PrivateSum)

    def test_registration_retains_original_lifecycle_methods(self):
        class ConfiguredSum(Sum):
            pass

        registration = register_sum(contribution=ConfiguredSum)
        original = ConfiguredSum.initialize

        def changed(self, *, assets: AssetAccess) -> None:
            raise AssertionError("A registered lifecycle method was replaced")

        ConfiguredSum.initialize = changed
        try:
            instance = registration.construct_instance()
            registration.initialize_instance(instance=instance, assets=NoAssets())
            self.assertEqual(first=0, second=instance.calls)
        finally:
            ConfiguredSum.initialize = original


class RegisteredExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reservation_selects_configuration_and_rejects_forged_ownership(self):
        host = EmbeddedHost(limits=HostLimits(
            analysis_records=1, operation_records=2, input_bytes=4096,
            result_bytes=4096, state_values=2, progress_records=2,
            initialization_seconds=5, retirement_seconds=5,
        ))
        self.addAsyncCleanup(self.retire, host=host)
        for name, scale in (("twice", 2), ("thrice", 3)):
            host.allocate(name=name, registration=register_sum(scale=scale), grants=(
                ResourceGrant(kind="cpu", amount=1, unit="thread"),
            ))
        analysis = host.open_analysis(analysis_id="configured", allocation_names=("twice", "thrice"))
        limits = ExecutionLimits(queue_seconds=5, execution_seconds=5, cleanup_seconds=5, input_bytes=1024, result_bytes=1024)
        reservation = analysis.reservations[0]
        for forged in (replace(reservation, host="other"), replace(reservation, allocation="missing"), replace(reservation, analysis="other")):
            with self.subTest(forged=forged):
                refused = analysis.submit_method(
                    method=Sum.calculate, operation_id="forged", scientific_input=Numbers(values=(1,)),
                    states=(), reservation=forged, limits=limits, dispatch_guard=lambda: None,
                )
                self.assertIsInstance(obj=refused, cls=NotAdmitted)
                self.assertIs(expr1=AdmissionCode.INVALID_REFERENCE, expr2=refused.code)
        self.assertEqual(first={}, second=host.operations)
        with self.assertRaises(expected_exception=TypeError):
            analysis.submit_method(
                method=Sum.calculate, operation_id="missing-input", states=(),
                reservation=reservation, limits=limits, dispatch_guard=lambda: None,
            )
        self.assertEqual(first={}, second=host.operations)
        for reservation, expected in zip(analysis.reservations, (2, 3), strict=True):
            operation = analysis.submit_method(
                method=Sum.calculate, operation_id=reservation.allocation, scientific_input=Numbers(values=(1,)),
                states=(), reservation=reservation, limits=limits, dispatch_guard=lambda: None,
            )
            observed = await observe_completion(operation=operation)
            self.assertEqual(first=(), second=observed.failures)
            result = json.loads(s=await operation.read_result(result=observed.result))
            self.assertEqual(first=expected, second=result["value"])

    async def test_failed_initialization_and_cleanup_retain_the_same_partial_owner(self):
        host = EmbeddedHost(limits=HostLimits(
            analysis_records=1, operation_records=1, input_bytes=4096,
            result_bytes=4096, state_values=1, progress_records=1,
            initialization_seconds=5, retirement_seconds=5,
        ))
        registration = register_sum(contribution=FailedInitialization)
        host.allocate(name="partial", registration=registration, grants=(
            ResourceGrant(kind="cpu", amount=1, unit="thread"),
        ))
        resident = host.residents["partial"]
        self.addCleanup(resident.executor.shutdown, wait=True)
        analysis = host.open_analysis(analysis_id="failure", allocation_names=("partial",))
        reservation, = analysis.reservations
        operation = analysis.submit_method(
            method=Sum.calculate, operation_id="calculate",
            scientific_input=Numbers(values=(1,)), states=(), reservation=reservation,
            limits=ExecutionLimits(queue_seconds=5, execution_seconds=5, cleanup_seconds=5, input_bytes=1024, result_bytes=1024),
            dispatch_guard=lambda: None,
        )
        completed = await observe_completion(operation=operation)
        self.assertIs(expr1=FailedInitialization.startup_failure, expr2=completed.failures[0].cause)
        partial = resident.value
        with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
            await observe_cleanup(control=host.retire())
        self.assertIs(expr1=partial, expr2=resident.value)
        self.assertIs(expr1=partial.partial_resource, expr2=partial.cleanup_resource)
        group = raised.exception
        self.assertIsInstance(obj=group, cls=BaseExceptionGroup)
        self.assertIs(expr1=FailedInitialization.cleanup_failure, expr2=group.exceptions[0])
        self.assertFalse(expr=resident.retired)
        # The fixture resource has no external lifetime. Retained failure evidence
        # is asserted above; stop its test executor without retrying disposal.

    async def test_two_allocations_share_configuration_but_not_runtime_state(self):
        host = EmbeddedHost(limits=HostLimits(
            analysis_records=2, operation_records=2, input_bytes=4096,
            result_bytes=4096, state_values=2, progress_records=2,
            initialization_seconds=5, retirement_seconds=5,
        ))
        self.addAsyncCleanup(self.retire, host=host)
        registration = Registration(
            contribution=Sum, configuration=Settings(scale=3),
            resources=(ResourceRequirement(kind="cpu", amount=1, unit="thread"),),
        )
        grants = (ResourceGrant(kind="cpu", amount=1, unit="thread"),)
        host.allocate(name="a", registration=registration, grants=grants)
        host.allocate(name="b", registration=registration, grants=grants)
        for name in ("a", "b"):
            analysis = host.open_analysis(analysis_id=name, allocation_names=(name,))
            reservation, = analysis.reservations
            operation = analysis.submit_method(
                method=Sum.calculate,
                operation_id="calculate", scientific_input=Numbers(values=(1, 2)),
                states=(), reservation=reservation,
                limits=ExecutionLimits(queue_seconds=5, execution_seconds=5, cleanup_seconds=5, input_bytes=1024, result_bytes=1024),
                dispatch_guard=lambda: None,
            )
            observed = await observe_completion(operation=operation)
            self.assertEqual(first=(), second=observed.failures)
            self.assertIs(expr1=Outcome.SUCCEEDED, expr2=observed.outcome)
            payload = await operation.read_result(result=observed.result)
            result = registration.call(method=Sum.calculate).decode_result(payload=payload)
            self.assertEqual(first=Total(value=9), second=result)
            await observe_cleanup(control=analysis.close())
        first = host.residents["a"].value
        second = host.residents["b"].value
        self.assertIsNot(expr1=first, expr2=second)
        self.assertIs(expr1=first.configuration, expr2=second.configuration)
        self.assertEqual(first=1, second=first.calls)
        self.assertEqual(first=1, second=second.calls)

    async def retire(self, *, host):
        await observe_cleanup(control=host.retire())
