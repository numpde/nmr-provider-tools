"""Own successive scientific processes without overlapping their owners."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from asyncio import Lock, timeout_at
from collections.abc import Awaitable, Callable
from copy import deepcopy
from reprlib import repr as abbreviated

from provider_core.contribution import NoInput
from provider_core.hosting import _raise_context_failure, settle_cleanup
from provider_core.runner_session import RunnerSession
from provider_core.scientific_call import ErrorPolicy, ScientificCall


class ProcessCall:
    """An initially unstarted scientific call with SDK-owned process custody.

    Enter as an async context manager, or explicitly start and close from a
    surrounding lifecycle owner. Closing also settles interrupted startup and
    replacement; repeated closes retain any unconfirmed retirement failure.
    The host is exposed for service health observation. Scientific invocation
    goes through this owner; lending its host to other callers breaks custody.
    """

    def __init__(self, *, settings, runner, contribution_type, constructor, startup, method, identity_method):
        self.settings = settings
        self.runner = deepcopy(dict(runner))
        self.contribution_type = contribution_type
        self.constructor = constructor
        self.startup = deepcopy(dict(startup))
        self.method = method
        self.identity_method = identity_method
        self.host = None
        self.closed = False
        self._invocation = None
        self._batch_owner = None
        self._active_calls = 0

    def require_open(self) -> None:
        if self.closed:
            raise RuntimeError(f"Scientific process owner has closed: {self.method=}")

    async def __aenter__(self):
        try:
            return await self.start()
        except BaseException as original:
            await self.__aexit__(type(original), original, original.__traceback__)

            raise

    async def __aexit__(self, exception_type, exception, traceback):
        try:
            await self.close()
        except BaseException as cleanup:
            _raise_context_failure(original=exception, cleanup=cleanup)

    async def start(self):
        """Initialize once; this handle retains any partially started worker."""
        self.require_open()

        if self._invocation is None:
            if self.host is not None:
                raise RuntimeError(f"Scientific startup was already requested: {self.method=}")

            (contribution, identity) = await self._start_worker()
            self._invocation = _ProcessInvocation(owner=self, contribution=contribution, identity=identity)

        return self

    @property
    def error_policy(self):
        """Expose the configured retry policy before the worker starts."""

        return self.settings.error_policy

    @property
    def binding(self):
        """Expose the started method's codecs to manual scientific callers."""
        return self._started_call().binding

    @property
    def runtime_identity(self):
        """Return a detached copy of the first worker's declared runtime evidence."""
        return self._started_call().runtime_identity

    def _started_call(self):
        if self._invocation is None:
            raise RuntimeError(f"Scientific call should be started before use: {self.method=}")

        return self._invocation

    def _claim_batch(self, owner):
        self.require_open()
        self._started_call()

        if not ((self._batch_owner is None) and (self._active_calls == 0)):
            raise RuntimeError(f"Cannot attach a batch dispatcher: {self._active_calls=} or an existing dispatcher owns this process call")

        self._batch_owner = owner

    def _release_batch(self, owner):
        if self._batch_owner is None:
            return

        if not ((self._batch_owner is owner) and (self._active_calls == 0)):
            raise RuntimeError(f"Cannot release the batch reservation: {self._batch_owner is owner=}, {self._active_calls=}")

        self._batch_owner = None

    async def __call__(
        self,
        *,
        scientific_input=NoInput(),
        receive: Callable[[bytes], Awaitable[None]] | None = None,
        require_execution: Callable[[], None] | None = None,
        _batch_owner: object | None = None,
    ):
        self.require_open()

        if not (self._batch_owner is _batch_owner):
            raise RuntimeError(f"Cannot invoke this process call outside its active batch dispatcher: {self._batch_owner is _batch_owner=}")

        self._active_calls += 1

        try:
            return await self._started_call()(
                scientific_input=scientific_input,
                receive=receive,
                require_execution=require_execution,
            )
        finally:
            self._active_calls -= 1

    async def _start_worker(self):
        self.require_open()

        session = RunnerSession.from_config(self.runner)

        try:
            self.host = self.settings.create_host(process_session=session)
        except BaseException:
            session.close()

            raise

        # Adopt the host before startup can suspend; retirement joins that
        # original startup even if its caller is cancelled or times out
        contribution = await self.settings.start_contribution(
            host=self.host,
            contribution_type=self.contribution_type,
            constructor=self.constructor,
            startup=deepcopy(self.startup),
        )
        self.require_open()
        identity = None

        if self.identity_method is not None:
            identity_call = ScientificCall(
                host=self.host,
                allocation=contribution.allocation,
                binding=contribution.call(method=self.identity_method),
                operation_policy=self.settings.operation_policy,
                error_policy=ErrorPolicy(),
            )
            value = await identity_call()
            identity = identity_call.binding.result_codec.encode(value)

        self.require_open()

        return (contribution, identity)

    async def close(self) -> None:
        """Forbid new work and settle the current host's original retirement."""
        self.closed = True

        if self.host is not None:
            await settle_cleanup(control=self.host.retire())


class _ProcessInvocation(ScientificCall):
    """Serialize invocations across replacement of their shared scientific host."""

    def __init__(self, *, owner, contribution, identity):
        super().__init__(
            host=owner.host,
            allocation=contribution.allocation,
            binding=contribution.call(method=owner.method),
            operation_policy=owner.settings.operation_policy,
            error_policy=owner.settings.error_policy,
        )

        self.owner = owner
        self._identity = identity
        self._identity_codec = (
            None if owner.identity_method is None else contribution.call(method=owner.identity_method).result_codec
        )
        self._invocation = Lock()

    @property
    def runtime_identity(self):
        """Return a detached copy of the first worker's declared runtime evidence."""
        if self._identity is None:
            return None

        return self._identity_codec.decode(self._identity)

    async def _invoke(self, *, payload, receive, require_execution, deadline):
        async with timeout_at(deadline):
            self._require_budget(deadline=deadline)

            await self._invocation.acquire()

        try:
            self.owner.require_open()

            return await super()._invoke(
                payload=payload,
                receive=receive,
                require_execution=require_execution,
                deadline=deadline,
            )
        finally:
            self._invocation.release()

    async def _recover(self, *, delay, deadline, require_execution, scientific_failure):
        try:
            if scientific_failure:
                await settle_cleanup(control=self.host.retire())

            await super()._recover(
                delay=delay,
                deadline=deadline,
                require_execution=require_execution,
                scientific_failure=scientific_failure,
            )

            if not scientific_failure:
                return

            async with timeout_at(deadline):
                self._require_budget(deadline=deadline)

                if require_execution is not None:
                    require_execution()

                self._require_budget(deadline=deadline)

                (contribution, identity) = await self.owner._start_worker()

                expected = self.runtime_identity
                observed = None if identity is None else self._identity_codec.decode(identity)

                if not (observed == expected):
                    raise ValueError(
                        f"Replacement scientific runtime differs from the initially admitted runtime identity: "
                        f"{abbreviated(expected)=}, {abbreviated(observed)=}"
                    )

                self.host = self.owner.host
                self.allocation = contribution.allocation
                self.binding = contribution.call(method=self.owner.method)
        except BaseException:
            if scientific_failure:
                self.owner.closed = True

            raise
