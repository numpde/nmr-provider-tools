"""Execute exactly one bounded model HTTP exchange in a fresh interpreter.

Only inert request data enters this process. Conversation, tool dispatch and
scientific state stay with the parent, which also owns the original deadline
and the observation of this process's exit.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
import signal
import sys

import httpx

from provider_core.chat_tls import restore_trust
from provider_core.chat_wire import (
    METADATA_BYTES, decoded_bytes, failure_message, read_request,
    response_message, write_message,
)
from provider_core.decoded_body import require_decoded_limit
from provider_core.transport import HttpTransport
from provider_core.transport_errors import ExchangeFailed


# One outbound exchange has a closed data-only protocol schema.
# repo-check: allow-module-constant
_REQUEST_FIELDS = frozenset({
    "version", "kind", "method", "url", "headers", "body", "request_bytes",
    "trust", "exchange_seconds", "connect_seconds", "retirement_seconds",
    "not_after", "maximum_bytes", "error_maximum_bytes", "proxy_socket", "trust_directory",
})


class _Cancellation:
    """Remember cooperative interruption even before the event loop starts."""

    def __init__(self):
        self.requested = False
        self.task = None

    def request(self, _signal, _frame):
        self.requested = True
        if self.task is not None:
            self.task.cancel()

    def check(self):
        if self.requested:
            raise asyncio.CancelledError("Interpreter HTTP owner stopped the original exchange")


def _positive_seconds(value):
    if not (type(value) in (int, float) and math.isfinite(value) and value > 0):
        raise ValueError("Interpreter child requires finite positive timing settings")
    return value


def _request(document):
    if (type(document) is not dict or document.keys() != _REQUEST_FIELDS
            or type(document["version"]) is not int or document["version"] != 1
            or document["kind"] != "request"):
        raise ValueError("Interpreter child request has an unknown schema")
    for field in ("method", "url"):
        value = document[field]
        if not (type(value) is str and 0 < len(value) <= METADATA_BYTES):
            raise ValueError("Interpreter child request metadata exceeds its admitted limit")
    pairs = document["headers"]
    if not (type(pairs) is list and len(pairs) <= 65536):
        raise ValueError("Interpreter child headers exceed their admitted limit")
    headers = []
    remaining = METADATA_BYTES
    for pair in pairs:
        if type(pair) is not list or len(pair) != 2:
            raise ValueError("Interpreter child header is malformed")
        name = decoded_bytes(pair[0], remaining)
        remaining -= len(name)
        value = decoded_bytes(pair[1], remaining)
        remaining -= len(value)
        headers.append((name, value))
    limit = document["request_bytes"]
    if not (type(limit) is int and 0 <= limit):
        raise ValueError("Interpreter child request byte allowance is invalid")
    body = decoded_bytes(document["body"], limit)
    if len(body) != limit:
        raise ValueError("Interpreter child did not receive the complete admitted request")
    for field in ("exchange_seconds", "connect_seconds", "retirement_seconds", "not_after"):
        _positive_seconds(document[field])
    require_decoded_limit(document["maximum_bytes"])
    require_decoded_limit(document["error_maximum_bytes"])
    return httpx.Request(document["method"], document["url"], headers=headers, content=body)


async def _exchange(document, cancellation):
    cancellation.task = asyncio.current_task()
    owner = object()
    transport = None
    try:
        cancellation.check()
        request = _request(document)
        trust = restore_trust(document["trust"], directory=document["trust_directory"])
        transport = HttpTransport(
            trust=trust, exchange_seconds=document["exchange_seconds"],
            connect_seconds=document["connect_seconds"],
            retirement_seconds=document["retirement_seconds"],
            proxy_socket=document["proxy_socket"],
        )
        cancellation.check()
        # This is the parent's unchanged monotonic deadline. Construction and
        # trust restoration consume it; neither can grant a new exchange budget.
        response = await transport.exchange_decoded(
            request, maximum_bytes=document["maximum_bytes"],
            error_maximum_bytes=document["error_maximum_bytes"],
            owner=owner, not_after=document["not_after"],
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as cause:
        if isinstance(cause, ExchangeFailed):
            failure = cause
        elif transport is not None and transport.last_failure is not None:
            failure = transport.last_failure
        else:
            # silent-defaults: allow-contract -- Only an observed cancellation is caller interruption; other setup failures retain their original primary cause.
            failure = ExchangeFailed(
                "Cannot complete interpreter HTTP exchange: the child operation failed",
                primary=cause,
                caller_interruption=cause if isinstance(cause, asyncio.CancelledError) else None,
            )
        # Preserve original evidence before any cancellation-resistant cleanup
        # can block this interpreter. The parent still requires observed exit.
        write_message(sys.stdout.buffer, failure_message(failure))
    else:
        # HttpTransport returns a complete response only after its I/O closes.
        write_message(sys.stdout.buffer, response_message(response))
    finally:
        try:
            if transport is not None and not transport.retirement_confirmed:
                # This observes the original exchange and its fixed cleanup
                # deadline. It never creates a second HTTP attempt or budget.
                # A retirement failure propagates to main's nonzero exit; the
                # parent retains the original failure frame as child evidence.
                # asyncio.run still owns any pending task until parent TERM/KILL.
                await transport.retire(owner=owner)
        finally:
            cancellation.task = None


def main(argv=None):
    """Read one frame and write one reply; stdout is exclusively protocol data."""
    arguments = sys.argv[1:] if argv is None else argv
    cancellation = _Cancellation()
    previous = signal.signal(signal.SIGUSR1, cancellation.request)
    try:
        # A test-owned entry module may have arguments of its own. The parent
        # always appends the exact admitted request-frame allowance last.
        if not arguments or not arguments[-1].isascii() or not arguments[-1].isdecimal():
            return 1
        maximum = int(arguments[-1])
        if not 0 < maximum <= 0xffffffff:
            return 1
        document = read_request(sys.stdin.buffer, maximum)
        asyncio.run(_exchange(document, cancellation))
    # silent-defaults: allow-exception-outcome -- A failed child exits unsuccessfully; the parent must reject a missing or partial reply and observe process retirement.
    except BaseException:
        # Missing/partial replies and nonzero exit are supervised by the parent.
        # Never leak request bytes, headers or protected exceptions to stderr.
        return 1
    finally:
        signal.signal(signal.SIGUSR1, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
