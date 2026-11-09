"""Run shared conformance tests against the packaged dummy example."""

import unittest

from provider_core.examples.dummy_provider.conformance import provider_fixture
from provider_core.provider_conformance import (
    ProviderConformanceTests,
    ProviderFixture,
)


class DummyProviderConformanceTests(
    ProviderConformanceTests,
    unittest.IsolatedAsyncioTestCase,
):
    def provider_fixture(self) -> ProviderFixture:
        return provider_fixture()


if __name__ == "__main__":
    unittest.main()
