"""Capture interpreter grants once and create independently settled conversations."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from collections.abc import Mapping
from functools import partial
from types import MappingProxyType

from provider_core.api_config import https_trust
from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
from provider_core.conversation import independent_interruption_failure
from provider_core.failure_collection import FailureCollector
from provider_core.hosting import _join_settlement, _start_owned_task
from provider_core.interpreter import Interpreter, ProseService
from provider_core.startup_file import read_startup_file


class ManagedInterpreter(Interpreter):
    """Join the conversation and its tool dispatch before returning or retiring.

    A cancelled run stops interpretation and waits for admitted dispatch to settle.
    Tool handlers still own their scientific resources and must settle those on
    cancellation. Keep their enclosing resource scope alive until run or retire
    finishes. Accepted results and protected failures remain available afterward.
    Unlike the low-level Interpreter, dispatch failures wait for model cleanup.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._run_task = None
        self._retirement_task = None

    async def run(self, messages, tools, dispatch, *, not_after=None, describe_constraints=None):
        if not ((self._run_task is None) and (self._retirement_task is None)):
            raise RuntimeError(f"Cannot run interpretation: this conversation was already started or retired; {self._used=}; {self._stopped.is_set()=}")

        self._run_task = _start_owned_task(
            operation=partial(
                super().run,
                messages,
                tools,
                dispatch,
                not_after=not_after,
                describe_constraints=describe_constraints,
            ),
        )

        failures = FailureCollector(message="Interpretation and cleanup failed independently")

        with failures.capture():
            try:
                interpretation = await asyncio.shield(self._run_task)
            except asyncio.CancelledError:
                # shield() can create a new cancellation without the tool's cause.
                # Interpreter retains the original even after task observation.
                if self._run_task.cancelled() and isinstance(self.last_failure, asyncio.CancelledError):
                    raise self.last_failure

                raise

        with failures.capture():
            await self.retire()

        failures.raise_if_any()

        return interpretation

    async def _settle(self):
        failures = FailureCollector(message="Interpretation and model retirement failed independently")

        if (self._run_task is not None) and (not self._run_task.done()):
            requested_stop = False

            if (not self._run_task.done()) and (not self._run_task.cancelling()):
                requested_stop = self._run_task.cancel()

            with failures.capture():
                try:
                    await self._run_task
                except asyncio.CancelledError:
                    if not (requested_stop):
                        raise

                    # Requested cancellation is expected shutdown; an independent
                    # tool or I/O failure attached to it still needs to propagate.
                    retained_failure = independent_interruption_failure(self.last_failure)

                    if retained_failure is not None:
                        raise retained_failure

        with failures.capture():
            await super().retire()

        failures.raise_if_any()

    async def retire(self):
        """Stop once, join tool dispatch, then retire every visited model owner."""
        self._stopped.set()

        if self._retirement_task is None:
            self._retirement_task = _start_owned_task(operation=self._settle)

        await _join_settlement(task=self._retirement_task)

    @property
    def retirement_confirmed(self):
        return (
            ((self._run_task is None) or self._run_task.done()) and
            super().retirement_confirmed
        )


class InterpreterSettings:
    """Share retry state and captured settings without owning active conversations.

    Each new_interpreter() is single-use and owns its work through retirement.
    Services retain only retry state, so they have no shutdown resources. Keep
    each conversation inside the scope of the source/science resources its tools
    use. Supplied services can be shared across offerings; plain factories receive
    a separate service per settings object, even when their endpoint names match.
    """

    def __init__(self, endpoint_factories, *, policy: Mapping, secrets: tuple[str, ...]):
        # repo-check: allow-tuple-generator -- Interpreter requires a fixed service order for fallback and time allocation.
        self.services = tuple(
            (factory if isinstance(factory, ProseService) else ProseService(factory))
            for factory in endpoint_factories
        )

        self._policy = MappingProxyType(dict(policy))
        self._secrets = secrets

        # Interpreter is the policy authority; construction performs no model I/O.
        self.new_interpreter()

    @classmethod
    # silent-defaults: allow-contract -- File-backed startup is explicit here; snapshot owners replace the material reader.
    def from_config(cls, configuration, *, read_material=read_startup_file):
        """Capture keys and CA bytes through the caller's selected material reader.

        The default reader opens bounded startup files. Snapshot owners supply a
        reader backed by their captured bytes; model visits never reopen paths.
        Endpoint order and transport/policy limits come from the deployment.
        """
        factories = []
        secrets = []

        for endpoint in configuration["endpoints"]:
            endpoint = dict(endpoint)
            key_bytes = read_material(endpoint.pop("key_file"), maximum_bytes=16384, secret=True)

            # repo-check: allow-literal-guard-error -- The offending credential bytes must not appear in startup diagnostics.
            if not key_bytes.isascii():
                raise ValueError("Cannot configure interpreter: the API key must contain header-safe ASCII text")

            key = key_bytes.decode("ascii").rstrip("\r\n")
            ca = None

            if "ca_file" in endpoint:
                ca = read_material(endpoint.pop("ca_file"), maximum_bytes=1048576)

            # silent-defaults: allow-contract -- An omitted reasoning field requests no model reasoning override.
            # silent-defaults: allow-pop-default -- Missing reasoning requests no override; endpoint fields otherwise go to ChatEndpointSpec.
            reasoning_effort = endpoint.pop("reasoning_effort", None)

            factory = ChatEndpointFactory(
                spec=ChatEndpointSpec(reasoning_effort=reasoning_effort, **endpoint, api_key=key),
                trust=https_trust(ca, label="interpreter"),
                **configuration["transport"],
            )

            # Construction checks carrier/transport grants without starting I/O.
            factory(
                exchange_seconds=configuration["policy"]["interpretation_budget_seconds"],
                connect_seconds=configuration["policy"]["connection_timeout_seconds"],
            )

            factories.append(factory)
            secrets.append(key)

        policy = dict(configuration["policy"])

        if factories:
            # Reserve the carrier envelope inside the selected HTTP request cap.
            policy["reject_combined_messages_and_tools_over_bytes"] = min(
                policy["reject_combined_messages_and_tools_over_bytes"],
                *(factory.context_bytes for factory in factories),
            )

        return cls(tuple(factories), policy=policy, secrets=tuple(secrets))

    @property
    def diagnostic_secrets(self) -> tuple[str, ...]:
        """Supply captured keys to protected diagnostic redaction, never to reports."""

        return self._secrets

    def new_interpreter(self) -> ManagedInterpreter:
        """Create one settled conversation over this deployment's shared services."""

        return ManagedInterpreter(self.services, secrets=self._secrets, **self._policy)
