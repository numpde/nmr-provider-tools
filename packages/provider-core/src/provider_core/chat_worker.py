"""Execute exactly one bounded model HTTP exchange in a fresh interpreter.

Only inert request data enters this process. Conversation, tool dispatch and
scientific state stay with the parent, which also owns the original deadline
and the observation of this process's exit.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import signal
import sys

import httpx

from provider_core.chat_tls import restore_trust

from provider_core.chat_wire import (
    METADATA_BYTES,
    decoded_bytes,
    failure_message,
    read_request,
    response_message,
    write_message,
)

from provider_core.decoded_body import require_decoded_limit
from provider_core.transport import HttpTransport
from provider_core.transport_errors import ExchangeFailed
from provider_core.tools import require_positive_number
from provider_core.validation.upstream.chat_completions.response import COMPLETION_SUCCESS_STATUS
from provider_core.type_requirements import require_type


# One outbound exchange has a closed data-only protocol schema.
# repo-check: allow-module-constant
_REQUEST_FIELDS = frozenset(
    {
        "version",
        "kind",
        "method",
        "url",
        "headers",
        "body",
        "request_bytes",
        "trust",
        "exchange_seconds",
        "connect_seconds",
        "retirement_seconds",
        "not_after",
        "maximum_bytes",
        "error_maximum_bytes",
        "proxy_socket",
        "trust_directory",
    }
)


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


def _decode_http_request(request_message):
    require_type(request_message, dict)

    if request_message.keys() != _REQUEST_FIELDS:
        raise ValueError("Interpreter child request has an unknown schema")

    document_version = request_message["version"]

    require_type(document_version, int)

    if (request_message["version"] != 1) or (request_message["kind"] != "request"):
        raise ValueError("Interpreter child request has an unknown schema")

    for field in ("method", "url"):
        request_field_text = request_message[field]

        require_type(request_field_text, str)

        if not (0 < len(request_field_text) <= METADATA_BYTES):
            raise ValueError("Interpreter child request metadata exceeds its admitted limit")

    pairs = request_message["headers"]

    require_type(pairs, list)

    if not (len(pairs) <= 65536):
        raise ValueError("Interpreter child headers exceed their admitted limit")

    headers = []
    remaining = METADATA_BYTES

    for pair in pairs:
        require_type(pair, list)

        if len(pair) != 2:
            raise ValueError("Interpreter child header is malformed")

        name = decoded_bytes(pair[0], remaining)
        remaining -= len(name)
        header_value = decoded_bytes(pair[1], remaining)
        remaining -= len(header_value)

        headers.append((name, header_value))

    expected_request_bytes = request_message["request_bytes"]

    require_type(expected_request_bytes, int)

    if not (0 <= expected_request_bytes):
        raise ValueError(f"{expected_request_bytes=} should be nonnegative")

    body = decoded_bytes(request_message["body"], expected_request_bytes)

    if len(body) != expected_request_bytes:
        raise ValueError(f"{len(body)=} should match {expected_request_bytes=}")

    for field in ("exchange_seconds", "connect_seconds", "retirement_seconds", "not_after"):
        require_positive_number(request_message[field])

    require_decoded_limit(request_message["maximum_bytes"])
    require_decoded_limit(request_message["error_maximum_bytes"])

    return httpx.Request(request_message["method"], request_message["url"], headers=headers, content=body)


async def _exchange(request_message, cancellation):
    cancellation.task = asyncio.current_task()
    owner = object()
    transport = None

    try:
        cancellation.check()
        http_request = _decode_http_request(request_message)
        trust = restore_trust(request_message["trust"], directory=request_message["trust_directory"])

        transport = HttpTransport(
            trust=trust,
            exchange_seconds=request_message["exchange_seconds"],
            connect_seconds=request_message["connect_seconds"],
            retirement_seconds=request_message["retirement_seconds"],
            proxy_socket=request_message["proxy_socket"],
        )

        cancellation.check()

        # This is the parent's unchanged monotonic deadline. Construction and
        # trust restoration consume it; neither can grant a new exchange budget.
        response = await transport.exchange_decoded(
            http_request,
            maximum_bytes=request_message["maximum_bytes"],
            error_maximum_bytes=request_message["error_maximum_bytes"],
            success_status=COMPLETION_SUCCESS_STATUS,
            owner=owner,
            not_after=request_message["not_after"],
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as cause:
        if isinstance(cause, ExchangeFailed):
            failure = cause
        elif (transport is not None) and (transport.last_failure is not None):
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
            if (transport is not None) and (not transport.retirement_confirmed):
                # This observes the original exchange and its fixed cleanup
                # deadline. It never creates a second HTTP attempt or budget.
                # A retirement failure propagates to main's nonzero exit;
                # the parent retains the original failure frame as child evidence.
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
        if (not arguments) or (not arguments[-1].isascii()) or (not arguments[-1].isdecimal()):
            return 1

        max_request_message_bytes = int(arguments[-1])

        if not 0 < max_request_message_bytes <= 0xffffffff:
            return 1

        request_message = read_request(sys.stdin.buffer, max_request_message_bytes)

        asyncio.run(_exchange(request_message, cancellation))
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
