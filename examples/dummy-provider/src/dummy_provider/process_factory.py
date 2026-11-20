"""Installed construction of one worker-owned weighted contribution."""

from collections.abc import Mapping

import mock_numpy as np

from . import science
from .binding import WeightedSum, WeightedSumConfig


def construct(*, startup: Mapping) -> WeightedSum:
    """Capture explicit startup values; SDK initialization loads the model."""

    return WeightedSum(
        configuration=WeightedSumConfig(
            settings=science.WeightedSumSettings(**startup["settings"]),
            coefficients=science.CoefficientSet(weights=np.array(object=startup["coefficients"], dtype=float)),
        ),
    )
