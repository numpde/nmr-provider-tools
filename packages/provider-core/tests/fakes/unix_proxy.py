"""Finite CONNECT relay for transport proof, not the production egress policy.

Origin selection is test-owned and fixed. Only the initial CONNECT header is
recorded; TLS and application bytes pass unchanged between real stream peers.
"""

import asyncio
import tempfile
from builtins import BaseExceptionGroup
from pathlib import Path
from urllib.parse import urlsplit


class UnixProxy:
    # silent-defaults: allow-contract -- The synthetic proxy refusal uses its explicit ordinary denial status.
    def __init__(self, origin, mode, *, refusal_status=403):
        self.origin, self.mode = urlsplit(origin), mode
        self.refusal_status = refusal_status
        self.headers = []
        self.received, self.closed = asyncio.Event(), asyncio.Event()
        self.handlers = set()

    async def __aenter__(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.path = str(Path(self.scratch.name) / "proxy.sock")
        try:
            self.server = await asyncio.start_unix_server(self.handle, self.path, limit=16384)
        except BaseException:
            self.scratch.cleanup()
            raise
        return self

    async def handle(self, reader, writer):
        self.handlers.add(asyncio.current_task())
        outbound = None
        pumps = []
        try:
            self.headers.append(await reader.readuntil(b"\r\n\r\n"))
            self.received.set()
            if self.mode == "stall":
                await reader.read()
                return
            if self.mode in ("refuse", "malformed"):
                reply = f"HTTP/1.1 {self.refusal_status} PRIVATE_PROXY_REASON\r\nContent-Length: 0\r\n\r\n".encode()
                if self.mode == "malformed":
                    reply = b"not HTTP\r\n\r\n"
                writer.write(reply)
                await writer.drain()
                return
            if self.mode != "tunnel":
                raise ValueError("Unknown Unix proxy fixture mode")
            incoming, outbound = await asyncio.open_connection("127.0.0.1", self.origin.port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()

            async def copy(source, target):
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()
                target.write_eof()

            pumps = [asyncio.create_task(copy(reader, outbound)), asyncio.create_task(copy(incoming, writer))]
            await asyncio.gather(*pumps)
        except (ConnectionError, asyncio.IncompleteReadError):
            self.closed.set()
        finally:
            for task in pumps:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            writers = [writer]
            if outbound is not None:
                writers.append(outbound)
            for stream in writers:
                stream.close()
                try:
                    await stream.wait_closed()
                except ConnectionError:
                    self.closed.set()
            self.closed.set()

    async def __aexit__(self, *_failure):
        self.server.close()
        handlers = tuple(self.handlers)
        cancelled = {task for task in handlers if not task.done()}
        for task in cancelled:
            task.cancel()
        try:
            results = await asyncio.wait_for(asyncio.gather(*handlers, return_exceptions=True), 2)
            await asyncio.wait_for(self.server.wait_closed(), 2)
        finally:
            self.scratch.cleanup()
        failures = [
            result
            for task, result in zip(handlers, results, strict=True)
            if isinstance(result, BaseException) and not (task in cancelled and isinstance(result, asyncio.CancelledError))
        ]
        if failures:
            raise BaseExceptionGroup("Unix CONNECT test peer failed", failures)
