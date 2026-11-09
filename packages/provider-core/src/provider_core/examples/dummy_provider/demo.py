"""Run a finite set of fake jobs through the installed scientific interface.

This is example composition, not a Jobs service or another scheduler. The
scientific functions and binding stay unchanged when --instances changes.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import dataclass
import json

from provider_core.contribution import CallRef, ScientificRejection
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.hosting import ExecutionLimits, Invocation, NotAdmitted, Outcome

from . import science
from .binding import sum_binding, weighted_binding


@dataclass(frozen=True, slots=True)
class FakeJob:
    name: str
    values: tuple[int, ...]
    expected: int
    correct_input: bool
    cancel: bool


class Permission:
    def __init__(self):
        self.allowed = True

    def require(self):
        if not self.allowed:
            raise PermissionError("This fake job was cancelled before scientific dispatch.")


async def _finished(operation):
    if isinstance(operation, NotAdmitted):
        raise RuntimeError(f"Fake job was not admitted: {operation.reason}")
    async with asyncio.timeout(30):
        observation = await operation.observe(after=None, wait_seconds=0)
        while not observation.finished:
            observation = await operation.observe(after=observation.revision, wait_seconds=5)
    return observation


async def _settled(control):
    async with asyncio.timeout(30):
        observation = await control.observe(after=None, wait_seconds=0)
        while not observation.settled and not observation.failures:
            observation = await control.observe(after=observation.revision, wait_seconds=5)
    if observation.failures:
        raise BaseExceptionGroup("Fake-job cleanup failed.", [item.cause for item in observation.failures])
    return observation


def _submit(analysis, binding, job, name, payload, states, permission, suffix):
    description = binding.description
    reservation = next(item for item in analysis.reservations if item.binding_id == description.binding_id)
    invocation = Invocation(
        operation_id=f"{job.name}/{suffix}",
        target=CallRef(description.binding_id, description.binding_revision, name),
        payload=json.dumps(payload, allow_nan=False).encode("utf-8"),
        states=states,
        reservation=reservation,
        limits=ExecutionLimits(5, 20, 5, 2048, 2048),
    )
    return analysis.submit(invocation, dispatch_guard=permission.require)


async def _success(operation):
    observation = await _finished(operation)
    if observation.failures:
        raise BaseExceptionGroup("Fake scientific call failed.", [item.cause for item in observation.failures])
    if observation.outcome is not Outcome.SUCCEEDED or observation.result is None:
        raise RuntimeError("The original call did not establish a successful result.")
    return observation


async def _job(host, index, job, weighted, ordinary):
    analysis = host.open_analysis(job.name, allocation_names=(f"weighted-{index}", f"sum-{index}"))
    permission = Permission()
    try:
        if job.cancel:
            operation = _submit(analysis, ordinary, job, "run", {"values": job.values}, (), permission, "cancelled")
            permission.allowed = False
            observation = await _finished(operation)
            if observation.dispatched or not observation.failures:
                raise RuntimeError("Cancelled fake job entered science or lost its refusal evidence.")
            print(f"{job.name}: cancelled before scientific dispatch", flush=True)
            return

        # One analysis can call distinct registered implementations. Settings and
        # coefficients were bound at deployment below, never constructed here.
        total = _submit(analysis, ordinary, job, "run", {"values": job.values}, (), permission, "sum")
        total_observation = await _success(total)
        total_value = json.loads(await total.read_result(total_observation.result))
        await _settled(total.release_result(total_observation.result))

        if job.correct_input:
            rejected = _submit(analysis, weighted, job, "prepare", {"values": []}, (), permission, "rejected")
            observation = await _finished(rejected)
            if (observation.outcome is not Outcome.REJECTED or not observation.failures
                    or not isinstance(observation.failures[0].cause, ScientificRejection)):
                raise RuntimeError("The invalid candidate did not produce a scientific rejection.")
            print(f"{job.name}: empty input rejected; supplying corrected input", flush=True)

        preparation = _submit(analysis, weighted, job, "prepare", {"values": job.values}, (), permission, "prepare")
        accepted = await _success(preparation)
        await _settled(preparation.release_result(accepted.result))
        calculation = _submit(analysis, weighted, job, "calculate", {}, accepted.states, permission, "calculate")
        completed = await _success(calculation)

        # Keep final output after scientific analysis state has been disposed.
        await _settled(analysis.close())
        value = json.loads(await calculation.read_result(completed.result))
        if value != {"total": job.expected, "count": len(job.values)}:
            raise RuntimeError(f"Fake job {job.name} returned an unexpected result: {value!r}")
        print(f"{job.name}: sum={total_value['total']}, weighted={value['total']}; result survived cleanup", flush=True)
        await _settled(calculation.release_result(completed.result))
    finally:
        await _settled(analysis.close())


def _direct(settings, coefficients):
    model = science.create_model(settings, coefficients)
    analysis = None
    try:
        science.load_model(model)
        analysis = science.create_analysis(model)
        science.initialize_analysis(analysis)
        prepared = science.prepare(analysis, (1, 2, 3))
        try:
            result = science.calculate(analysis, prepared)
        finally:
            science.dispose_prepared(prepared)
        if result != {"total": 29, "count": 3}:
            raise RuntimeError(f"The direct scientific calculation returned an unexpected result: {result!r}")
        print("Direct scientific call: weighted=29", flush=True)
    finally:
        if analysis is not None:
            science.dispose_analysis(analysis)
        science.dispose_model(model)


async def run(instances):
    """Change allocation count while retaining the exact same scientific code."""
    settings, coefficients = science.Settings(2, 1), (1, 2, 3)
    _direct(settings, coefficients)
    weighted, ordinary = weighted_binding(settings, coefficients), sum_binding()
    # repo-check: allow-tuple-generator -- This immutable inventory is the explicit example deployment.
    allocations = tuple(
        Allocation(f"{name}-{index}", binding, ("dummy.instance", "dummy.call"))
        for index in range(instances) for name, binding in (("weighted", weighted), ("sum", ordinary))
    )
    host = EmbeddedHost(allocations, limits=HostLimits(16, 64, 65536, 65536, 32, 10, 10))
    jobs = (
        FakeJob("normal", (1, 2, 3), 29, False, False),
        FakeJob("corrected", (2, 4, 6), 57, True, False),
        FakeJob("negative-values", (-1, 0, 1), 5, False, False),
        FakeJob("cancelled", (1, 2, 3), 29, False, True),
    )

    async def worker(index):
        for job in jobs[index::instances]:
            await _job(host, index, job, weighted, ordinary)

    print(f"Hosted example: {instances} weighted-model instance(s), each with a stateless helper", flush=True)
    try:
        async with asyncio.TaskGroup() as group:
            for index in range(instances):
                group.create_task(worker(index))
    finally:
        await _settled(host.retire())
    print("DUMMY PROVIDER COMPLETE: 3 results, 1 cancelled job; all original allocations retired", flush=True)
