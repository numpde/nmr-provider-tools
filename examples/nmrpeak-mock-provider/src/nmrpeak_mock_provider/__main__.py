"""Read deployment settings, run HF and CHF predictions, and clean up started work."""

import asyncio
from functools import partial
from pathlib import Path
import sys
import tomllib

from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import OperationPolicy, ResultConsumption, observe_cleanup
from provider_core.interpreter import Interpreter, ProseService
from provider_core.scientific import ResourceRequirement

from .binding import Chf
from .application import Prediction
from .composition import adopt_offerings, interpretation_messages, interpretation_tools
from .fake_services import ScriptedEndpointFactory
from .fake_model import Model
from .science import Settings


# Read the selected deployment configuration from the working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = Path("config/deployments/localhost/provider.toml")


async def close_analysis(*, execution) -> None:
    """Wait for SDK analysis cleanup; an overdue flag does not end this wait."""
    await observe_cleanup(control=execution.close())


async def run_demo() -> None:
    """Run one HF and one CHF request, writing their consumed reports to stdout."""
    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        provider_config = tomllib.load(source)

    # Create the SDK execution manager. Prediction submits scientific prepare and
    # generate methods to it; the SDK also tracks their results and cleanup.
    host = EmbeddedHost(**provider_config["execution"]["embedded_host"])

    # Prediction passes this policy to the SDK with each prepare/generate submission.
    operation_policy = OperationPolicy(**provider_config["execution"]["operation_policy"])

    # HF and CHF declare this full set of resource needs for prepare and generate.
    requested_requirements = frozenset(
        ResourceRequirement(**resource_requirement)
        for resource_requirement in provider_config["execution"]["resource_requirement"]
    )

    cleanup_requests = []
    consumptions = []
    causes = []


    async def receive(payload: bytes) -> None:
        # Flushed stdout stands in for the external receiver's acknowledgement.
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()

    try:
        # Startup selects the stand-in Model implementation and scientific settings.
        # Assembly gives the HF/CHF objects to the SDK before initialize loads their models.
        offerings = adopt_offerings(
            host=host,
            requested_requirements=requested_requirements,
            hf_settings=Settings(**provider_config["science"]["hf"]["settings"]),
            chf_settings=Settings(**provider_config["science"]["chf"]["settings"]),
            create_model=Model,
        )
        for hosted in offerings:
            carbon = isinstance(hosted.contribution, Chf)
            candidate = {
                "formula": "C2H6O",
                "proton_peaks": [{"centroid": "1.2", "integral": 3, "multiplicity": "triplet", "couplings": ["7"]}],
            }
            if carbon:
                candidate["carbon_peaks"] = ["18", "58"]
            factory = ScriptedEndpointFactory(proposals=(candidate,), context_bytes=32768)
            interpreter = Interpreter(
                endpoint_factories=(ProseService(factory=factory),),
                timeout_seconds=15, context_bytes=factory.context_bytes, secrets=(),
            )
            cleanup_requests.append(interpreter.retire)
            execution = host.open_analysis(allocations=(hosted.allocation,))
            cleanup_requests.append(partial(close_analysis, execution=execution))
            prediction = Prediction(
                execution=execution, hosted=hosted, interpreter=interpreter,
                policy=operation_policy,
            )
            # Interpreter proposes input; Prediction submits prepare to validate it.
            await prediction.interpret(
                messages=interpretation_messages(source="Formula C2H6O. Proton peak: 1.2 ppm, 3H, triplet, coupling 7 Hz. Carbon peaks: 18 and 58 ppm."),
                tools=interpretation_tools(carbon=carbon),
            )
            # Prediction submits generate once and waits on its retained operation.
            result = await prediction.finish()
            # Retain delivery and result release together so cancellation cannot lose them.
            consumption = ResultConsumption(operation=prediction.generation, result=result, receive=receive)
            consumptions.append(consumption)
            await consumption.consume()
            await observe_cleanup(control=execution.close())
    except BaseException as cause:
        causes.append(cause)
    finally:
        for consumption in consumptions:
            try:
                await consumption.settle()
            except BaseException as cause:
                if not any(original is cause for original in causes):
                    causes.append(cause)
        cleanup_requests.append(lambda: observe_cleanup(control=host.retire()))
        for cleanup in cleanup_requests:
            try:
                await cleanup()
            except BaseException as cause:
                causes.append(cause)
    if len(causes) == 1:
        raise causes[0]
    if causes:
        raise BaseExceptionGroup("Prediction and cleanup failures", causes)


if __name__ == "__main__":
    asyncio.run(
        main=run_demo(),
    )
