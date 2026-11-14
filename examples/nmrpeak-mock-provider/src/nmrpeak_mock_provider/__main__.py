"""Compose two selected requests using real SDK owners and external stand-ins."""

import asyncio
from functools import partial
from pathlib import Path
import sys
import tomllib

from provider_core.embedded_host import EmbeddedHost, HostLimits, ResourceAllocation
from provider_core.hosting import ExecutionLimits, ResourceGrant, ResultConsumption, observe_cleanup
from provider_core.interpreter import Interpreter, ProseService

from .application import Prediction
from .composition import adopt_offerings, interpretation_messages, interpretation_tools
from .fake_services import ScriptedEndpointFactory


# The example selects one deployment; startup reads its operator-owned policy.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = Path("config/deployments/localhost/provider.toml")


async def close_analysis(*, execution) -> None:
    await observe_cleanup(control=execution.close())


async def run_demo() -> None:
    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        policy = tomllib.load(source)
    host = EmbeddedHost(
        limits=HostLimits(
            **policy["execution"]["host"],
        ),
    )
    configured_grants = [
        ResourceGrant(**grant)
        for grant in policy["execution"]["grants"]
    ]
    grants = tuple(configured_grants)
    limits = ExecutionLimits(queue_seconds=5, execution_seconds=5, cleanup_seconds=5, input_bytes=8192, result_bytes=8192)
    cleanup_requests = []
    consumptions = []
    causes = []

    def require_permission():
        return None

    async def receive(payload: bytes) -> None:
        # Flushed stdout stands in for the external receiver's acknowledgement.
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()

    try:
        offerings = adopt_offerings(
            host=host,
            hf_allocation=ResourceAllocation(grants=grants),
            chf_allocation=ResourceAllocation(grants=grants),
        )
        for name, hosted, carbon in (("hf", offerings.hf, False), ("chf", offerings.chf, True)):
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
                limits=limits, require_permission=require_permission,
            )
            await prediction.interpret(
                messages=interpretation_messages(source="Formula C2H6O. Proton peak: 1.2 ppm, 3H, triplet, coupling 7 Hz. Carbon peaks: 18 and 58 ppm."),
                tools=interpretation_tools(carbon=carbon),
            )
            result = await prediction.finish()
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
