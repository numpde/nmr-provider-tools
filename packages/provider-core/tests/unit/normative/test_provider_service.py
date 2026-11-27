"""Provider exclusion survives service, resource and cancellation failures."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from provider_core.job_service import serve_provider
from provider_core.lifetime import ProviderLifetime


class ProviderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_exclusion_covers_startup_service_and_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lifetime = ProviderLifetime(root)
            phases = []

            def observe(phase):
                phases.append(phase)
                with self.assertRaises(BlockingIOError):
                    ProviderLifetime(root).open()

            @asynccontextmanager
            async def resources():
                observe("start")
                yield "ready"
                observe("retire")

            async def serve():
                observe("serve")

            def new_service(started, *, stopping):
                self.assertEqual(started, "ready")
                self.assertFalse(stopping.is_set())
                return SimpleNamespace(serve=serve)

            await serve_provider(lifetime=lifetime, resources=resources(), new_service=new_service)
            self.assertEqual(phases, ["start", "serve", "retire"])
            ProviderLifetime(root).open().close()

    async def test_failures_keep_exclusion_and_original_failure(self):
        for phase in ("startup", "construction", "service", "retirement", "cancellation"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory)
                lifetime = ProviderLifetime(root)
                failure = asyncio.CancelledError() if phase == "cancellation" else RuntimeError(phase)
                retired = []

                @asynccontextmanager
                async def resources():
                    try:
                        if phase == "startup":
                            raise failure
                        yield "ready"
                    finally:
                        retired.append(True)
                        if phase == "retirement":
                            raise failure

                async def serve():
                    if phase in ("service", "cancellation"):
                        raise failure

                def new_service(started, *, stopping):
                    if phase == "construction":
                        raise failure
                    return SimpleNamespace(serve=serve)

                self.addCleanup(lifetime.close)
                with self.assertRaises(type(failure)) as caught:
                    await serve_provider(lifetime=lifetime, resources=resources(), new_service=new_service)
                self.assertIs(caught.exception, failure)
                self.assertEqual(retired, [True])
                with self.assertRaises(BlockingIOError):
                    ProviderLifetime(root).open()
                lifetime.close()

    async def test_stop_signal_reaches_service_without_cancelling_resource_entry(self):
        with TemporaryDirectory() as directory:
            phases = []
            signal = []

            @contextmanager
            def signals(request_stop):
                signal.append(request_stop)
                yield

            @asynccontextmanager
            async def resources():
                signal[0]()
                phases.append("started")
                yield None
                phases.append("retired")

            def new_service(started, *, stopping):
                async def serve():
                    self.assertTrue(stopping.is_set())
                    phases.append("served")
                return SimpleNamespace(serve=serve)

            with patch("provider_core.job_service.termination_signals", signals):
                await serve_provider(lifetime=ProviderLifetime(Path(directory)), resources=resources(), new_service=new_service)
            self.assertEqual(phases, ["started", "served", "retired"])
