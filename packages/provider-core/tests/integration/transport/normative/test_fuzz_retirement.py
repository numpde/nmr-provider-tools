"""Generated TLS stalls must retire the original exchange after its work deadline."""

import asyncio
import random
import tempfile
import unittest

import httpx

from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.tls_peer import TlsPeer


class TransportFaultSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_tls_phases_retire_without_replaying_the_request(self):
        for seed in range(16):
            randomizer = random.Random(seed)
            phase = ("tls-stall", "headers-stall", "body-stall", "trickle")[seed % 4]
            inherited = bool((seed // 4) % 2)
            mode = randomizer.choice(("raw", "decoded", "file"))
            with self.subTest(seed=seed, phase=phase, inherited=inherited, mode=mode):
                async with TlsPeer(phase) as peer:
                    # silent-defaults: allow-contract -- The scenario chooses which request deadline expires first.
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=3 if inherited else 0.3,
                                              retirement_seconds=0.5)
                    owner = object()
                    request = httpx.Request("GET", peer.origin + "/fault-sequence")
                    # silent-defaults: allow-contract -- Aggregate-only cases intentionally omit the inherited cap.
                    deadline = asyncio.get_running_loop().time() + 0.3 if inherited else None
                    with tempfile.TemporaryFile() as output:
                        try:
                            options = {"maximum_bytes": 4096, "owner": owner, "not_after": deadline}
                            if mode == "file":
                                operation = transport.exchange_file(request, output_fd=output.fileno(), **options)
                            elif mode == "decoded":
                                operation = transport.exchange_decoded(request, error_maximum_bytes=4096, **options)
                            else:
                                operation = transport.exchange(request, **options)
                            with self.assertRaises(ExchangeFailed) as caught:
                                await asyncio.wait_for(operation, 2)
                            self.assertTrue(transport.retirement_confirmed)
                            self.assertIs(transport.last_failure, caught.exception)
                            self.assertIsInstance(caught.exception.primary, TimeoutError)
                            self.assertTrue(peer.received.is_set())
                            await asyncio.wait_for(peer.closed.wait(), 1)
                            # silent-defaults: allow-contract -- Stalled TLS cannot carry an HTTP request; later phases carry one.
                            self.assertEqual(len(peer.requests), 0 if phase == "tls-stall" else 1)
                            for _ in range(randomizer.randint(1, 4)):
                                await transport.retire(owner=owner)
                            self.assertIsNone(transport.pending_owner)
                        finally:
                            # Fixture teardown observes the original task, even on a red
                            # assertion; it must not close a lent file under pending I/O.
                            if transport._active is not None:
                                await asyncio.wait_for(asyncio.shield(transport._active.task), 2)
                            await transport.retire(owner=owner)
