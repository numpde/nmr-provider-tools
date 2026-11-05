"""Supervise one model HTTP exchange without moving its Attempt or science."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import dataclass, field
import math
import signal
import sys
import tempfile

from provider_core.chat_tls import capture_trust
from provider_core.chat_wire import METADATA_BYTES, encoded_bytes, framed, message_bytes, read_message, read_reply
from provider_core.decoded_body import require_decoded_limit
from provider_core.transport import require_transport_settings
from provider_core.transport_errors import ExchangeFailed


def _worker_command():
    """An isolated interpreter imports only its installed SDK, never provider code."""
    return sys.executable, "-I", "-B", "-m", "provider_core.chat_worker"


def _owned(coroutine):
    try:
        task = asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise
    task.add_done_callback(_observe_failure)
    return task


def _observe_failure(task):
    if not task.cancelled():
        task.exception()


@dataclass(frozen=True, slots=True)
class ChatProcessResult:
    """Observed process facts, independent of whether a model reply was accepted."""

    pid: int
    returncode: int | None
    signals_sent: tuple[int, ...]


class _ExitPending(TimeoutError):
    """The fixed observation budget ended; the original child is still owned."""


@dataclass(slots=True, repr=False)
class _Exchange:
    owner: object
    deadline: float
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    stopped_at: float | None = None
    task: asyncio.Task | None = None
    launch: asyncio.Task | None = None
    io: asyncio.Task | None = None
    exit: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    process: object = None
    trust_directory: object = None
    response: object = None
    child_reply: object = None
    failure: ExchangeFailed | None = None
    caller_interruption: BaseException | None = None
    cleanup_failures: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    retired: bool = False


class SupervisedChatTransport:
    """Own exec, bounded IPC, HTTP deadline and observed process retirement.

    A single retirement_seconds allowance covers cooperative cancellation,
    TERM, KILL and reaping. Its thirds are SDK shutdown policy, not new request
    time. If the allowance expires first, the same retained task keeps owning
    launch/exit observation; later callers cannot renew the allowance.
    """

    def __init__(self, *, trust, exchange_seconds, connect_seconds, retirement_seconds, proxy_socket=None):
        require_transport_settings(trust=trust, exchange_seconds=exchange_seconds,
                                   retirement_seconds=retirement_seconds, proxy_socket=proxy_socket)
        if not (type(connect_seconds) in (int, float) and math.isfinite(connect_seconds) and connect_seconds > 0):
            raise ValueError("Cannot configure interpreter child: connect_seconds must be finite positive seconds")
        self._trust = capture_trust(trust)
        self._exchange_seconds = exchange_seconds
        self._connect_seconds = connect_seconds
        self._retirement_seconds = retirement_seconds
        self._proxy_socket = proxy_socket
        self._active = None
        self.last_failure = None
        self.last_process_result = None

    @property
    def retirement_confirmed(self):
        return self._active is None

    @property
    def pending_owner(self):
        # silent-defaults: allow-contract -- None explicitly reports that no exchange currently owns this transport.
        return None if self._active is None else self._active.owner

    def _fail(self, exchange, primary, message):
        if exchange.failure is None:
            exchange.failure = ExchangeFailed(message, primary=primary)
        elif exchange.failure.primary is not primary and exchange.failure.interruption is None:
            exchange.failure.interruption = primary
        self.last_failure = exchange.failure

    def _stop(self, exchange, primary, *, at=None):
        if exchange.stopped_at is None:
            exchange.stopped_at = asyncio.get_running_loop().time() if at is None else at
            self._fail(exchange, primary, "Cannot complete interpreter HTTP exchange: the original operation was interrupted")
            exchange.stop.set()

    async def _observe(self, exchange):
        cancellation = None
        loop = asyncio.get_running_loop()
        while not exchange.task.done():
            boundary = exchange.deadline if exchange.stopped_at is None else exchange.stopped_at + self._retirement_seconds
            remaining = boundary - loop.time()
            if remaining <= 0:
                if exchange.stopped_at is None:
                    self._stop(exchange, TimeoutError("Model HTTP exchange exceeded its original absolute deadline"),
                               at=exchange.deadline)
                    continue
                self._fail(exchange, _ExitPending("Interpreter child exit remains unconfirmed; retain its original owner"),
                           "Cannot retire interpreter HTTP child: process exit remains unconfirmed")
                break
            try:
                await asyncio.wait({exchange.task}, timeout=remaining)
            except asyncio.CancelledError as cause:
                if cancellation is None:
                    cancellation = cause
                if exchange.caller_interruption is None:
                    exchange.caller_interruption = cause
                self._stop(exchange, cause)
        self._update_failure(exchange)
        return cancellation

    def _update_failure(self, exchange):
        failure = exchange.failure
        if failure is None:
            return
        cleanup = tuple(exchange.cleanup_failures)
        if not exchange.retired and exchange.stopped_at is not None:
            cleanup += (_ExitPending("Interpreter HTTP child or IPC retirement is still owned"),)
        failure.cleanup_failures = cleanup
        if exchange.caller_interruption is not None:
            failure.caller_interruption = exchange.caller_interruption
        if isinstance(exchange.child_reply, ExchangeFailed) and exchange.child_reply is not failure:
            failure.child_failure = exchange.child_reply
        process = exchange.process
        if process is not None:
            facts = ChatProcessResult(process.pid, process.returncode, tuple(exchange.signals))
            failure.process = self.last_process_result = facts
        self.last_failure = failure

    async def exchange_decoded(self, request, *, maximum_bytes, error_maximum_bytes, owner=None, not_after=None):
        if self._active is not None:
            raise ExchangeFailed("Cannot start model HTTP exchange: the original child retirement is unconfirmed")
        require_decoded_limit(maximum_bytes)
        require_decoded_limit(error_maximum_bytes)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._exchange_seconds
        if not_after is not None:
            if not (type(not_after) in (int, float) and math.isfinite(not_after) and not_after > 0):
                raise ValueError("Cannot start model HTTP exchange: not_after must be a finite positive deadline")
            deadline = min(deadline, not_after)
        if loop.time() >= deadline:
            self.last_failure = ExchangeFailed(
                "Cannot start model HTTP exchange: the original deadline expired before child startup",
                primary=TimeoutError("Model HTTP exchange expired before dispatch"),
            )
            raise self.last_failure
        content = request.content
        document = {
            "version": 1, "kind": "request", "method": request.method, "url": str(request.url),
            "headers": [[encoded_bytes(name), encoded_bytes(value)] for name, value in request.headers.raw],
            "body": encoded_bytes(content), "request_bytes": len(content), "trust": self._trust,
            "exchange_seconds": self._exchange_seconds, "connect_seconds": self._connect_seconds,
            "retirement_seconds": self._retirement_seconds, "not_after": deadline,
            "maximum_bytes": maximum_bytes, "error_maximum_bytes": error_maximum_bytes,
            "proxy_socket": self._proxy_socket,
        }
        exchange = _Exchange(owner, deadline)
        self.last_failure = None
        self._active = exchange
        try:
            exchange.task = _owned(self._run(exchange, document, maximum_bytes, error_maximum_bytes))
        except BaseException:
            self._active = None
            raise
        cancellation = await self._observe(exchange)
        if cancellation is not None:
            raise cancellation
        if exchange.failure is not None:
            if isinstance(exchange.failure.primary, asyncio.CancelledError):
                raise exchange.failure.primary
            raise exchange.failure
        if not exchange.retired or exchange.response is None:
            raise ExchangeFailed("Cannot accept model reply: original exchange retirement is unconfirmed")
        return exchange.response

    async def _io(self, exchange, raw, maximum_bytes, error_maximum_bytes):
        process = exchange.process
        process.stdin.write(framed(raw))
        await process.stdin.drain()
        process.stdin.close()
        limit = METADATA_BYTES + 4 * ((max(maximum_bytes, error_maximum_bytes) + 2) // 3)
        document = await read_message(process.stdout, limit)
        reply = read_reply(document, maximum_bytes=maximum_bytes, error_maximum_bytes=error_maximum_bytes)
        exchange.child_reply = reply
        if await process.stdout.read(1):
            raise ValueError("Interpreter child returned more than one reply")
        await asyncio.shield(exchange.exit)
        if process.returncode != 0:
            raise RuntimeError("Interpreter HTTP child exited unsuccessfully")
        return reply

    async def _until_stop(self, exchange, task):
        stop = _owned(exchange.stop.wait())
        try:
            if exchange.stopped_at is not None:
                return False
            remaining = max(0, exchange.deadline - asyncio.get_running_loop().time())
            await asyncio.wait({task, stop}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if exchange.stopped_at is None and asyncio.get_running_loop().time() >= exchange.deadline:
                self._stop(exchange, TimeoutError("Model HTTP exchange exceeded its original absolute deadline"),
                           at=exchange.deadline)
            return task.done() and exchange.stopped_at is None
        finally:
            stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)

    async def _run(self, exchange, document, maximum_bytes, error_maximum_bytes):
        try:
            if exchange.stopped_at is not None:
                return
            if asyncio.get_running_loop().time() >= exchange.deadline:
                self._stop(exchange, TimeoutError("Model HTTP exchange expired before child startup"), at=exchange.deadline)
                return
            # Lazy OpenSSL hash-directory lookup is preserved in a private
            # captured copy. Its parent owner survives even a killed child.
            exchange.trust_directory = tempfile.TemporaryDirectory(prefix="provider-model-http-")
            document["trust_directory"] = exchange.trust_directory.name
            raw = message_bytes(document)
            if asyncio.get_running_loop().time() >= exchange.deadline:
                self._stop(exchange, TimeoutError("Model HTTP exchange expired before child startup"), at=exchange.deadline)
                return
            # Only the launcher marker is inherited. Model credentials and TLS
            # material travel through the owned pipe, never argv or environment.
            exchange.launch = _owned(asyncio.create_subprocess_exec(
                *_worker_command(), str(len(raw)), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={"PROVIDER_CORE_IN_CONTAINER": "1"}, close_fds=True,
            ))
            await self._until_stop(exchange, exchange.launch)
            # A cancelled observer cannot discard a startup that may create a
            # child later. This retained task adopts and retires that exact PID.
            exchange.process = await asyncio.shield(exchange.launch)
            exchange.exit = _owned(exchange.process.wait())
            if exchange.stopped_at is None:
                exchange.io = _owned(self._io(exchange, raw, maximum_bytes, error_maximum_bytes))
                if await self._until_stop(exchange, exchange.io):
                    reply = exchange.io.result()
                    if isinstance(reply, ExchangeFailed):
                        exchange.failure = reply
                        exchange.cleanup_failures.extend(reply.cleanup_failures)
                    else:
                        exchange.response = reply
                    return
        except BaseException as cause:
            self._stop(exchange, cause)
        finally:
            exchange.cleanup_task = _owned(self._finish(exchange))
            while not exchange.cleanup_task.done():
                try:
                    await asyncio.shield(exchange.cleanup_task)
                except asyncio.CancelledError as cause:
                    self._stop(exchange, cause)
                except Exception as cause:
                    self._fail(exchange, cause, "Cannot retire interpreter HTTP child: original cleanup failed")
                    exchange.cleanup_failures.append(cause)
            if exchange.cleanup_task.cancelled():
                cause = RuntimeError("Interpreter child cleanup owner was cancelled before confirming retirement")
                self._fail(exchange, cause, "Cannot retire interpreter HTTP child: original cleanup was interrupted")
                exchange.cleanup_failures.append(cause)
            else:
                cause = exchange.cleanup_task.exception()
                if cause is not None and not any(cause is failure for failure in exchange.cleanup_failures):
                    self._fail(exchange, cause, "Cannot retire interpreter HTTP child: original cleanup failed")
                    exchange.cleanup_failures.append(cause)
            self._update_failure(exchange)
            if exchange.retired and self._active is exchange:
                self._active = None

    async def _finish(self, exchange):
        if exchange.process is None and exchange.launch is not None:
            try:
                exchange.process = await asyncio.shield(exchange.launch)
            except asyncio.CancelledError:
                # A cancelled launcher supplied no confirmed process outcome.
                # Keep the original obligation instead of guessing it created none.
                raise RuntimeError("Interpreter child startup cancellation has no confirmed process outcome") from None
            except Exception:
                # A completed, failed subprocess creation acquired no returned
                # process. _run retains that exact startup exception already.
                exchange.process = None
            else:
                exchange.exit = _owned(exchange.process.wait())
        process = exchange.process
        if process is not None:
            if process.returncode is None:
                if exchange.stopped_at is None:
                    self._stop(exchange, RuntimeError("Interpreter HTTP owner stopped before child retirement"))
                await self._retire_process(exchange)
            self._close_pipes(process)
            if exchange.io is not None and not exchange.io.done():
                exchange.io.cancel()
            if exchange.io is not None:
                await asyncio.gather(exchange.io, return_exceptions=True)
            await asyncio.shield(exchange.exit)
            self.last_process_result = ChatProcessResult(process.pid, process.returncode, tuple(exchange.signals))
        if exchange.trust_directory is not None:
            exchange.trust_directory.cleanup()
        exchange.retired = True

    def _close_pipes(self, process):
        # Process exposes no reader close operation. Close only its owned pipe
        # transports; closing the process transport itself could kill it early.
        for descriptor in (0, 1):
            pipe = process._transport.get_pipe_transport(descriptor)
            if pipe is not None:
                pipe.close()

    async def _retire_process(self, exchange):
        process = exchange.process
        # All stages share one original allowance. Its final third includes
        # kill/exit observation, so every repeated retire observes the same cap.
        for fraction, number in ((0, signal.SIGUSR1), (1 / 3, signal.SIGTERM), (2 / 3, signal.SIGKILL)):
            if process.returncode is not None:
                break
            remaining = exchange.stopped_at + fraction * self._retirement_seconds - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.wait({exchange.exit}, timeout=remaining)
            if process.returncode is not None:
                break
            try:
                process.send_signal(number)
                exchange.signals.append(int(number))
            # silent-defaults: allow-contract -- A missing PID does not confirm retirement; the original exit watcher remains mandatory below.
            except ProcessLookupError:
                # Exit still needs the original child watcher's observation.
                pass
            except OSError as cause:
                exchange.cleanup_failures.append(cause)
        self._close_pipes(process)
        # If exit cannot be confirmed inside the allowance, _observe returns a
        # held failure while this same task continues to own the process.
        await asyncio.shield(exchange.exit)

    async def retire(self, *, owner=None):
        exchange = self._active
        if exchange is None:
            return
        if exchange.owner is not owner:
            raise ExchangeFailed("Cannot retire model HTTP exchange: the original child belongs to another owner")
        if exchange.stopped_at is None:
            self._stop(exchange, asyncio.CancelledError())
        cancellation = await self._observe(exchange)
        if cancellation is not None:
            raise cancellation
        if not exchange.retired:
            raise exchange.failure
