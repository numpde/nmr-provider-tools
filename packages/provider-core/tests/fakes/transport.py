"""Controlled HTTP streams for failure/retirement cases a finite peer cannot force."""

import asyncio

import httpcore


class Stream:
    def __init__(self, chunks, *, failure=None, close_failure=None, stall=None):
        self.chunks = chunks
        self.failure = failure
        self.close_failure = close_failure
        self.stall = stall
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.stall is not None:
            await self.stall.wait()
        if self.failure is not None:
            raise self.failure

    async def aclose(self):
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True


class Client:
    # silent-defaults: allow-contract -- The ordinary mock response is HTTP 200; refusal tests select another status.
    def __init__(self, stream, *, close_failure=None, send_gate=None, status=200, headers=None):
        self.stream = stream
        self.close_failure = close_failure
        self.send_gate = send_gate
        self.sent = asyncio.Event()
        self.closed = False
        self.calls = []
        self.status = status
        # silent-defaults: allow-contract -- None requests valid JSON headers; explicit empty or malformed header specimens stay observable.
        self.headers = [(b"content-type", b"application/json")] if headers is None else headers

    async def handle_async_request(self, request, **options):
        self.calls.append((request, options))
        self.sent.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
        return httpcore.Response(self.status, headers=self.headers, content=self.stream)

    async def aclose(self):
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True
