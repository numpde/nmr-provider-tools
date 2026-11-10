"""Run shared execution conformance against this provider's scenarios."""

from provider_core.container_guard import require_container

require_container()

import unittest

from provider_core.testing.conformance import (
    ProviderConformanceTests,
    ProviderFixture,
)

from .conformance import provider_fixture


class DummyProviderConformanceTests(
    ProviderConformanceTests,
    unittest.IsolatedAsyncioTestCase,
):
    def provider_fixture(self) -> ProviderFixture:
        return provider_fixture()


if __name__ == "__main__":
    unittest.main()
