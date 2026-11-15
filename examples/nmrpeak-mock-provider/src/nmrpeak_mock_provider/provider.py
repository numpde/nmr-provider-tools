"""Set up the provider's scientific objects and SDK execution lifetime.

The application calls open_provider explicitly and supplies its model constructor.
The application supplies language-model endpoint factories and report receivers.
This module owns provider and request lifetimes; it creates no requests or scripted
service responses. Discovery and durable delivery belong to the surrounding service.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
# Bind deployment options now; construct a fresh Interpreter for each request.
from functools import partial
from pathlib import Path
import tomllib

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.failure_collection import FailureCollector
from provider_core.hosting import OperationPolicy, settle_cleanup
from provider_core.interpreter import EndpointFactory, Interpreter
from provider_core.scientific import HostedContribution, ResourceRequirement

from .application import PredictionWorkflow
from .binding import Chf, Hf
from .science import Model, Settings


# Read the selected deployment configuration from the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """The execution manager, per-request policy and available scientific objects.

    These fields have different roles. The offerings field is the unordered
    inventory; it does not assign fixed slots to particular scientific variants.
    """

    execution_host: InProcessExecutionHost
    operation_policy: OperationPolicy
    offerings: frozenset[HostedContribution]
    create_interpreter: Callable[[], Interpreter]

    @asynccontextmanager
    async def open_prediction(
        self,
        *,
        offering: HostedContribution,
        receive: Callable[[bytes], Awaitable[None]],
    ) -> AsyncIterator[PredictionWorkflow]:
        """Own one request's interpretation, scientific execution and receiver.

        The caller selects an offering and supplies its report receiver. It can
        inspect a receiver failure while this context is open; exiting ends that
        reconciliation window and disposes remaining results. Cancellation also
        joins an already started receiver before closing its execution session.
        Independent work and cleanup errors propagate together without resending.
        """

        if offering not in self.offerings:
            raise ValueError("Cannot open a prediction for an offering outside this provider")

        input_interpreter = self.create_interpreter()
        analysis_executor = None
        prediction_workflow = None
        failure_collector = FailureCollector(message="Prediction work and cleanup failed independently")

        with failure_collector.capture():
            analysis_executor = self.execution_host.open_analysis(allocations=frozenset({offering.allocation}))

            prediction_workflow = PredictionWorkflow(
                analysis_executor=analysis_executor,
                hosted_predictor=offering,
                interpreter=input_interpreter,
                operation_policy=self.operation_policy,
                receive=receive,
            )

            yield prediction_workflow

        # Join the receiver before science and interpretation release resources it
        # may still use. Each owner settles its work; collection only preserves errors.
        if prediction_workflow is not None:
            with failure_collector.capture():
                await prediction_workflow.settle_receiver()

        if analysis_executor is not None:
            with failure_collector.capture():
                await settle_cleanup(control=analysis_executor.close())

        with failure_collector.capture():
            await input_interpreter.retire()

        failure_collector.raise_if_any()


@asynccontextmanager
async def open_provider(
    *,
    create_model: Callable[[], Model],
    endpoint_factories: tuple[EndpointFactory, ...],
    secrets: tuple[str, ...],
) -> AsyncIterator[Provider]:
    """Read deployment settings, assemble science and retire the SDK on exit.

    This is provider-written setup, not an SDK discovery hook. The application's
    request-handling code opens predictions and finishes those contexts before
    leaving the provider context. Model loading starts only when science
    is requested. Setup failure, body failure and independent cleanup failure
    retain their original causes.
    """

    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        provider_config = tomllib.load(source)

    async with InProcessExecutionHost(**provider_config["execution"]["in_process_execution_host"]) as execution_host:
        operation_policy = OperationPolicy(**provider_config["execution"]["operation_policy"])

        resource_requirements = frozenset(
            ResourceRequirement(**resource_requirement)
            for resource_requirement in provider_config["execution"]["resource_requirement"]
        )

        predictor_classes = {"hf": Hf, "chf": Chf}
        offerings = set()

        # Family selects a scientific input shape. Each entry is an independently
        # configured model; several entries can use the same contribution class.
        for (family, offering_configs) in provider_config["science"].items():
            if family not in predictor_classes:
                raise ValueError(f"Cannot configure scientific offerings: unsupported {family=}")

            predictor_class = predictor_classes[family]

            for offering_config in offering_configs:
                predictor = predictor_class(
                    configuration=Settings(**offering_config["settings"]),
                    checkpoint_path=PROVIDER_CONFIG_PATH.parent / offering_config["checkpoint_path"],
                    create_model=create_model,
                )

                offerings.add(execution_host.adopt(
                    contribution=predictor,
                    requirements=resource_requirements,
                ))

        yield Provider(
            execution_host=execution_host,
            operation_policy=operation_policy,
            offerings=frozenset(offerings),
            create_interpreter=partial(
                Interpreter,
                endpoint_factories=endpoint_factories,
                secrets=secrets,
                **provider_config["interpreter"],
            ),
        )
