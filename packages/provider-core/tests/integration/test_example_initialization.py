"""Apply the host's startup and retirement contract to real example contributions."""

from provider_core.container_guard import require_container

require_container()

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mock_numpy as np

from dummy_provider.binding import Sum, WeightedSum, WeightedSumConfig
from dummy_provider.science import CoefficientSet, WeightedSumSettings
from nmrpeak_mock_provider.binding import Chf, Hf
from nmrpeak_mock_provider.fake_model import Model
from nmrpeak_mock_provider.science import Settings
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import observe_cleanup
from provider_core.scientific import AssetAccess
from reference_provider.binding import ReferenceConfig, ReferenceMatcher
from reference_provider.science import MatchSettings, Reference, ReferenceSet
from spectrum_provider.binding import BaselineCorrector, PeakDetector
from spectrum_provider.science import BaselineSettings, PeakSettings


class ExampleInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_contribution_initializes_once_and_cleans_up_after_failure(self):
        with TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint_bytes = b'{"candidates": ["CCO"]}'

            checkpoint.write_bytes(checkpoint_bytes)

            peak_settings = Settings(
                checkpoint_sha256=sha256(checkpoint_bytes).hexdigest(),
                maximum_input_tokens=100,
                beam_size=1,
                maximum_candidate_characters=100,
            )

            weighted_configuration = WeightedSumConfig(
                settings=WeightedSumSettings(scale=1, offset=0),
                coefficients=CoefficientSet(weights=np.array(object=[1], dtype=float)),
            )

            reference_configuration = ReferenceConfig(
                settings=MatchSettings(minimum_score=0.5),
                references=ReferenceSet(entries=(Reference(label="sample", values=(1,)),)),
            )

            contribution_factories = (
                Sum,
                lambda: WeightedSum(configuration=weighted_configuration),
                lambda: BaselineCorrector(configuration=BaselineSettings(baseline=0)),
                lambda: PeakDetector(configuration=PeakSettings(minimum_height=1)),
                lambda: ReferenceMatcher(configuration=reference_configuration),
                lambda: Hf(configuration=peak_settings, checkpoint_path=checkpoint, create_model=Model),
                lambda: Chf(configuration=peak_settings, checkpoint_path=checkpoint, create_model=Model),
            )

            for create_contribution in contribution_factories:
                contribution = create_contribution()

                with self.subTest(contribution=type(contribution).__name__):
                    initialize = type(contribution).initialize
                    initialization_calls = []

                    def record_initialization(self, *, assets: AssetAccess) -> None:
                        initialization_calls.append(self)
                        initialize(self=self, assets=assets)

                    with patch.object(target=type(contribution), attribute="initialize", new=record_initialization):
                        async with InProcessExecutionHost(
                            input_bytes=4096,
                            result_bytes=4096,
                            state_values=1,
                            progress_records=1,
                            initialization_seconds=5,
                            retirement_seconds=5,
                        ) as host:
                            offering = host.adopt(contribution=contribution, requirements=frozenset())

                            await host.initialize(allocation=offering.allocation)
                            await host.initialize(allocation=offering.allocation)
                            self.assertEqual(first=[contribution], second=initialization_calls)

                        retirement = await observe_cleanup(control=host.retire())

                        self.assertTrue(expr=retirement.settled)

                    failed_contribution = create_contribution()
                    original_failure = OSError("Injected loading failure")
                    closed = []
                    close = type(failed_contribution).close

                    def fail_after_initialization(self, *, assets: AssetAccess) -> None:
                        initialize(self=self, assets=assets)
                        raise original_failure

                    def record_close(self) -> None:
                        closed.append(self)
                        close(self=self)

                    with patch.object(target=type(failed_contribution), attribute="initialize", new=fail_after_initialization):
                        with patch.object(target=type(failed_contribution), attribute="close", new=record_close):
                            async with InProcessExecutionHost(
                                input_bytes=4096,
                                result_bytes=4096,
                                state_values=1,
                                progress_records=1,
                                initialization_seconds=5,
                                retirement_seconds=5,
                            ) as host:
                                offering = host.adopt(contribution=failed_contribution, requirements=frozenset())

                                with self.assertRaises(expected_exception=OSError) as raised:
                                    await host.initialize(allocation=offering.allocation)

                                self.assertIs(expr1=original_failure, expr2=raised.exception)

                            self.assertEqual(first=[failed_contribution], second=closed)
