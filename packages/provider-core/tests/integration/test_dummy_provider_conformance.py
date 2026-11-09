"""Run shared conformance tests with dummy-provider scientific fixtures."""

import unittest

from provider_core.provider_conformance import (
    ProviderConformanceTests,
    ProviderFixture,
)
from tests.providers.dummy import provider_fixture


class DummyProviderConformanceTests(
    ProviderConformanceTests,
    unittest.IsolatedAsyncioTestCase,
):
    def provider_fixture(self) -> ProviderFixture:
        return provider_fixture()


if __name__ == "__main__":
    unittest.main()
