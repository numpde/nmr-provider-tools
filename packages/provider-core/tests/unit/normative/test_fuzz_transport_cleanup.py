"""Generated cleanup schedules distinguish cooperative closure from held ownership."""

import asyncio
import random
import ssl
import unittest
from unittest.mock import patch

import httpx

from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.transport import Client, Stream


class CleanupFaultSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_and_deadline_allow_cooperative_cleanup_yields(self):
        trust = ssl.create_default_context()
        for seed in range(16):
            yields = random.Random(seed).randrange(9)
            cancelled = bool(seed % 2)
            release, entered = asyncio.Event(), asyncio.Event()

            class CooperativeClose(Stream):
                async def aclose(self):
                    entered.set()
                    for _ in range(yields):
                        await asyncio.sleep(0)
                    await super().aclose()

            client = Client(CooperativeClose([], stall=release))
            transport = HttpTransport(trust=trust, exchange_seconds=1, retirement_seconds=0.3)
            owner = object()
            with self.subTest(seed=seed, yields=yields, cancelled=cancelled), patch(
                "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client,
            ):
                # silent-defaults: allow-contract -- Caller-cancelled cases reserve time for explicit interruption to win.
                task = asyncio.create_task(transport.exchange(
                    httpx.Request("GET", "https://synthetic.example/cleanup"), maximum_bytes=32,
                    owner=owner, not_after=asyncio.get_running_loop().time() + (2 if cancelled else 0.3),
                ))
                try:
                    await asyncio.wait_for(client.sent.wait(), 1)
                    if cancelled:
                        task.cancel("synthetic caller cancellation")
                        with self.assertRaises(asyncio.CancelledError) as caught:
                            await asyncio.wait_for(task, 1)
                        self.assertEqual(caught.exception.args, ("synthetic caller cancellation",))
                        self.assertIs(transport.last_failure.caller_interruption, caught.exception)
                    else:
                        with self.assertRaises(ExchangeFailed):
                            await asyncio.wait_for(task, 1)
                    self.assertTrue(entered.is_set())
                    self.assertTrue(client.stream.closed)
                    self.assertTrue(client.closed)
                    self.assertTrue(transport.retirement_confirmed)
                    self.assertEqual(len(client.calls), 1)
                    await transport.retire(owner=owner)
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    if transport._active is not None:
                        await asyncio.wait_for(asyncio.shield(transport._active.task), 1)
                    await transport.retire(owner=owner)

    async def test_held_cleanup_cannot_renew_grace_or_allow_a_replacement(self):
        trust = ssl.create_default_context()
        for seed in range(8):
            release = asyncio.Event()

            class HeldClose(Stream):
                async def aclose(self):
                    await release.wait()
                    await super().aclose()

            client = Client(HeldClose([], stall=asyncio.Event()))
            transport = HttpTransport(trust=trust, exchange_seconds=1, retirement_seconds=0.06)
            owner = object()
            request = httpx.Request("GET", "https://synthetic.example/held")
            actions = ["retire", "replacement", "wrong-owner"] * random.Random(seed).randint(1, 3)
            random.Random(seed).shuffle(actions)
            with self.subTest(seed=seed, trace=tuple(actions)), patch(
                "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client,
            ):
                task = asyncio.create_task(transport.exchange(
                    request, maximum_bytes=32, owner=owner, not_after=asyncio.get_running_loop().time() + 5,
                ))
                try:
                    await asyncio.wait_for(client.sent.wait(), 1)
                    task.cancel("hold original cleanup")
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 1)
                    failure = transport.last_failure
                    self.assertIsNotNone(failure)
                    original = transport._active
                    self.assertIsNotNone(original)
                    for action in actions:
                        with self.assertRaises(ExchangeFailed):
                            if action == "retire":
                                await asyncio.wait_for(transport.retire(owner=owner), 0.02)
                            elif action == "wrong-owner":
                                await transport.retire(owner=object())
                            else:
                                await transport.exchange(request, maximum_bytes=32, owner=object())
                        self.assertIs(transport._active, original)
                        self.assertIs(transport.pending_owner, owner)
                        self.assertFalse(transport.retirement_confirmed)
                        self.assertIs(transport.last_failure, failure)
                    self.assertEqual(len(client.calls), 1)
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    if transport._active is not None:
                        await asyncio.wait_for(asyncio.shield(transport._active.task), 1)
                    await transport.retire(owner=owner)
                self.assertTrue(client.closed)
                self.assertTrue(transport.retirement_confirmed)
