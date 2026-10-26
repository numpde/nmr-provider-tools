"""Private plain-HTTP peer for the delivery client's actual Unix transport."""

import asyncio
import tempfile
from pathlib import Path


class UnixHTTPPeer:
    # silent-defaults: allow-contract -- The ordinary peer responds; the explicit stall scenario supplies the partial reply.
    def __init__(self, *, stall=False):
        self.stall = stall
        self.requests = []
        self.head_sent, self.closed = asyncio.Event(), asyncio.Event()
        self.tasks = set()

    async def __aenter__(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.path = str(Path(self.scratch.name) / "http.sock")
        self.server = await asyncio.start_unix_server(self.handle, self.path)
        return self

    async def handle(self, reader, writer):
        self.tasks.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            fields = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:] if line)
            # silent-defaults: allow-contract -- Absent Content-Length means the fixture request has no framed body.
            length = int(fields.get(b"Content-Length", b"0"))
            body = await reader.readexactly(length)
            self.requests.append((head, body))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n")
            await writer.drain()
            self.head_sent.set()
            if self.stall:
                await reader.read()
            else:
                writer.write(b"ok")
                await writer.drain()
        except ConnectionResetError:
            # Cancellation deliberately closes a client with an unread reply.
            self.closed.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                self.closed.set()
            self.closed.set()

    async def __aexit__(self, *_failure):
        self.server.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.server.wait_closed()
        self.scratch.cleanup()
        failures = [failure for failure in results if isinstance(failure, Exception)]
        if failures:
            raise ExceptionGroup("Unix HTTP peer failed", failures)
