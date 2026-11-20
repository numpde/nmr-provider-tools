"""Set up the provider's scientific objects and SDK execution lifetime.

The application calls open_offerings explicitly and supplies its model constructor.
The application supplies language-model endpoint factories and report receivers.
This module owns provider and request lifetimes; it creates no requests or scripted
service responses. Discovery and durable delivery belong to the surrounding service.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

# Bind deployment options now; construct a fresh Interpreter for each request.
from functools import partial

import pathlib
import tomllib

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.contribution import CallBinding
from provider_core.failure_collection import FailureCollector
from provider_core.hosting import OperationPolicy, require_capacity_partition, settle_cleanup
from provider_core.interpreter import EndpointFactory, Interpreter
from provider_core.process_host import ProcessExecutionHost
from provider_core.runner_session import RunnerSession
from provider_core.scientific import HostedContribution, ResourceRequirement

from .application import PredictionWorkflow
from .binding import Chf, Hf
from .fake_model import Model as InstalledMockModel
from .science import Model, Settings


# Read the selected deployment configuration from the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = pathlib.Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Offering:
    """Open predictions for one hosted model using its selected execution host."""

    execution_host: InProcessExecutionHost | ProcessExecutionHost
    operation_policy: OperationPolicy
    hosted_predictor: HostedContribution
    create_interpreter: Callable[[], Interpreter]
    family: str
    interpretation_prompt_file: str
    preparation_call: CallBinding
    generation_call: CallBinding

    @asynccontextmanager
    async def open_prediction(
        self,
        *,
        receive: Callable[[bytes], Awaitable[None]],
    ) -> AsyncIterator[PredictionWorkflow]:
        """Own one request's interpretation, scientific execution and receiver.

        The caller supplies the report receiver. It can
        inspect a receiver failure while this context is open; exiting ends that
        reconciliation window and disposes remaining results. Cancellation also
        joins an already started receiver before closing its execution session.
        Independent work and cleanup errors propagate together without resending.
        """

        input_interpreter = self.create_interpreter()
        analysis_executor = None
        prediction_workflow = None
        failure_collector = FailureCollector(message="Prediction work and cleanup failed independently")

        with failure_collector.capture():
            analysis_executor = self.execution_host.open_analysis(allocations=frozenset({self.hosted_predictor.allocation}))
            reservation_ref = analysis_executor.reservation(allocation=self.hosted_predictor.allocation)

            prediction_workflow = PredictionWorkflow(
                analysis_executor=analysis_executor,
                preparation_call=self.preparation_call,
                generation_call=self.generation_call,
                reservation_ref=reservation_ref,
                interpretation_prompt_file=self.interpretation_prompt_file,
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


def _process_capacity_options(science_config: Mapping, host_options: Mapping) -> dict:
    """Partition aggregate capacities before any deployment session is acquired."""
    shares = {
        (family, index): configuration["process_capacity"]
        for family, configurations in science_config.items()
        for index, configuration in enumerate(configurations)
    }
    require_capacity_partition(total=host_options, shares=shares)

    # Progress is retained per operation; startup/retirement are host allowances.
    return {key: {**host_options, **share} for key, share in shares.items()}


@asynccontextmanager
async def open_offerings(
    *,
    create_model: Callable[[], Model],
    endpoint_factories: tuple[EndpointFactory, ...],
    secrets: tuple[str, ...],
    # silent-defaults: allow-contract -- Threads need no sessions; process startup requires one fresh session for each configured family/index pair.
    process_sessions: Mapping[tuple[str, int], RunnerSession] | None = None,
) -> AsyncIterator[frozenset[Offering]]:
    """Read deployment settings, assemble science and retire the SDK on exit.

    This is provider-written setup, not an SDK discovery hook. The application's
    request-handling code opens predictions and finishes those contexts before
    leaving this context. Models are loaded before offerings are yielded.
    Setup failure, body failure and independent cleanup failure
    retain their original causes.

    Process deployment supplies one native session per (family, configuration
    index). Each host takes ownership when constructed; earlier configuration
    refusals leave sessions with the caller. Already entered hosts retire if a
    later model fails during startup.
    """

    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        provider_config = tomllib.load(source)

    # silent-defaults: allow-contract -- Existing deployment files select threads by omitting backend; process execution is explicit.
    execution_mode = provider_config["execution"].get("backend", "thread")

    host_options = provider_config["execution"]["in_process_execution_host"]

    if execution_mode == "thread":
        if process_sessions is not None:
            raise ValueError("Thread execution does not use scientific process sessions")

        execution_host = InProcessExecutionHost(**host_options)
    elif execution_mode == "process":
        if process_sessions is None:
            raise ValueError("Process execution requires deployment-owned scientific runner sessions")

        process_options = _process_capacity_options(provider_config["science"], host_options)

        if set(process_sessions) != set(process_options):
            raise ValueError("Process execution requires exactly one runner session for each configured (family, index) pair")

        if len({id(session) for session in process_sessions.values()}) != len(process_sessions):
            raise ValueError("Process execution requires distinct runner sessions for independent models")

        if not (create_model is InstalledMockModel):
            raise ValueError(f"Process execution requires {create_model=} to be {InstalledMockModel=}")

    else:
        raise ValueError(f"Unsupported scientific {execution_mode=}")

    async with AsyncExitStack() as lifetime:
        if execution_mode == "thread":
            await lifetime.enter_async_context(execution_host)
        operation_policy = OperationPolicy(**provider_config["execution"]["operation_policy"])

        resource_requirements = frozenset(
            ResourceRequirement(**resource_requirement)
            for resource_requirement in provider_config["execution"]["resource_requirement"]
        )

        predictor_families = {"hf": (Hf, "hf_prompt.txt"), "chf": (Chf, "chf_prompt.txt")}
        offerings = {}

        # Family selects a scientific input shape. Each entry is an independently
        # configured model; several entries can use the same contribution class.
        for (family, offering_configs) in provider_config["science"].items():
            if not (family in predictor_families):
                raise ValueError(f"Cannot configure scientific offerings: unsupported {family=}")

            (predictor_class, interpretation_prompt_file) = predictor_families[family]

            for index, offering_config in enumerate(offering_configs):
                if execution_mode == "process":
                    execution_host = ProcessExecutionHost(session=process_sessions[family, index], **process_options[family, index])
                    await lifetime.enter_async_context(execution_host)
                    hosted_predictor = await execution_host.start(
                        contribution_type=predictor_class,
                        constructor="nmrpeak_mock_provider.process_factory:construct",
                        startup={
                            "contribution_class": predictor_class.__name__,
                            "settings": offering_config["settings"],
                            "checkpoint_path": str((PROVIDER_CONFIG_PATH.parent / offering_config["checkpoint_path"]).resolve()),
                        },
                        requirements=resource_requirements,
                    )
                else:
                    predictor = predictor_class(
                        configuration=Settings(**offering_config["settings"]),
                        checkpoint_path=PROVIDER_CONFIG_PATH.parent / offering_config["checkpoint_path"],
                        create_model=create_model,
                    )

                    hosted_predictor = execution_host.adopt(
                        contribution=predictor,
                        requirements=resource_requirements,
                    )

                preparation_call = hosted_predictor.call(method=predictor_class.prepare)
                generation_call = hosted_predictor.call(method=predictor_class.generate)

                offerings[hosted_predictor] = (execution_host, family, interpretation_prompt_file, preparation_call, generation_call)

        if execution_mode == "thread":
            for offering in offerings:
                # The SDK loads each adopted model before advertising offerings.
                # A timeout retains the original thread until host cleanup.
                await execution_host.initialize(allocation=offering.allocation)

        create_interpreter = partial(
            Interpreter,
            endpoint_factories=endpoint_factories,
            secrets=secrets,
            **provider_config["interpreter"],
        )

        yield frozenset(
            Offering(
                execution_host=execution_host,
                operation_policy=operation_policy,
                hosted_predictor=hosted_predictor,
                create_interpreter=create_interpreter,
                family=family,
                interpretation_prompt_file=interpretation_prompt_file,
                preparation_call=preparation_call,
                generation_call=generation_call,
            )
            for (hosted_predictor, (execution_host, family, interpretation_prompt_file, preparation_call, generation_call)) in offerings.items()
        )
