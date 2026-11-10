"""End-to-end conformance scenarios owned by the dummy provider example."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import provider_core.contribution as contribution
from dummy_provider import science
from dummy_provider.binding import (
    create_sum_binding,
    create_weighted_sum_binding,
)
from provider_core.testing.conformance import (
    AllocationFixture,
    AnalysisFixture,
    CallFixture,
    ExpectedScientificRejection,
    ExpectedSuccess,
    ImplementationFixture,
    ProviderFixture,
)


def _create_weighted_sum_binding() -> contribution.ImplementationBinding[
    science.WeightedSumModel,
    science.WeightedSumAnalysis,
]:
    """Adapt the configured binding factory to the runner’s zero-argument interface."""
    return create_weighted_sum_binding(
        settings=science.WeightedSumSettings(
            scale=2,
            offset=1,
        ),
        coefficients=science.CoefficientSet(
            values=(1, 2, 3),
        ),
    )


def provider_fixture() -> ProviderFixture:
    """Describe scientific inputs and outcomes; shared code runs the scenarios."""
    calculation_progress = (
        contribution.ProgressEvent(
            stage="calculation",
            completed=0,
            total=1,
        ),
        contribution.ProgressEvent(
            stage="calculation",
            completed=1,
            total=1,
        ),
    )

    return ProviderFixture(
        name="dummy-provider",
        implementations=(
            ImplementationFixture(
                name="weighted-sum",
                create_binding=_create_weighted_sum_binding,
            ),
            ImplementationFixture(
                name="sum",
                create_binding=create_sum_binding,
            ),
        ),
        allocations=(
            AllocationFixture(
                name="weighted-a",
                implementation="weighted-sum",
            ),
            AllocationFixture(
                name="weighted-b",
                implementation="weighted-sum",
            ),
            AllocationFixture(
                name="sum",
                implementation="sum",
            ),
        ),
        analyses=(
            AnalysisFixture(
                name="single-allocation",
                allocations=("weighted-a",),
                calls=(
                    CallFixture(
                        operation_id="single/prepare",
                        allocation="weighted-a",
                        call="prepare",
                        payload=b'{"values":[1,2,3]}',
                        expected=ExpectedSuccess(
                            result=b'{"count":3}',
                            save_states_as="single/prepared",
                        ),
                    ),
                    CallFixture(
                        operation_id="single/calculate",
                        allocation="weighted-a",
                        call="calculate",
                        payload=b"{}",
                        states_from="single/prepared",
                        expected=ExpectedSuccess(
                            result=b'{"count":3,"total":29.0}',
                            progress=calculation_progress,
                        ),
                    ),
                ),
            ),
            AnalysisFixture(
                name="multiple-allocations",
                allocations=(
                    "weighted-a",
                    "weighted-b",
                    "sum",
                ),
                calls=(
                    CallFixture(
                        operation_id="weighted-a/rejected",
                        allocation="weighted-a",
                        call="prepare",
                        payload=b'{"values":[1,2]}',
                        expected=ExpectedScientificRejection(
                            code="wrong_count",
                        ),
                    ),
                    CallFixture(
                        operation_id="weighted-a/prepare",
                        allocation="weighted-a",
                        call="prepare",
                        payload=b'{"values":[2,4,6]}',
                        expected=ExpectedSuccess(
                            result=b'{"count":3}',
                            save_states_as="weighted-a/prepared",
                        ),
                    ),
                    CallFixture(
                        operation_id="weighted-a/calculate",
                        allocation="weighted-a",
                        call="calculate",
                        payload=b"{}",
                        states_from="weighted-a/prepared",
                        expected=ExpectedSuccess(
                            result=b'{"count":3,"total":57.0}',
                            progress=calculation_progress,
                        ),
                    ),
                    CallFixture(
                        operation_id="weighted-b/prepare",
                        allocation="weighted-b",
                        call="prepare",
                        payload=b'{"values":[1,2,3]}',
                        expected=ExpectedSuccess(
                            result=b'{"count":3}',
                            save_states_as="weighted-b/prepared",
                        ),
                    ),
                    CallFixture(
                        operation_id="weighted-b/calculate",
                        allocation="weighted-b",
                        call="calculate",
                        payload=b"{}",
                        states_from="weighted-b/prepared",
                        expected=ExpectedSuccess(
                            result=b'{"count":3,"total":29.0}',
                            progress=calculation_progress,
                        ),
                    ),
                    CallFixture(
                        operation_id="sum/run",
                        allocation="sum",
                        call="run",
                        payload=b'{"values":[2,4,6]}',
                        expected=ExpectedSuccess(
                            result=b'{"count":3,"total":12.0}',
                        ),
                    ),
                ),
            ),
        ),
    )
