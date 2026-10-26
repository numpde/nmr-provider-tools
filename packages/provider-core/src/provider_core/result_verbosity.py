"""Shared output vocabulary; bindings own defaults and which public fields it selects.

The established names come from Magnet's consumer-owned projection:
https://github.com/numpde/magnet-deploy/blob/0619d9ffd92a4326758b442c532aa09496161098/magnet_provider/result_verbosity.py#L6-L18
No level grants access to private diagnostics or changes scientific computation.
"""

from provider_core.container_guard import require_container

require_container()

from enum import StrEnum


class ResultVerbosity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    MAX = "max"

    @classmethod
    def parse(cls, value):
        message = "The verbosity field must be low, medium or max."
        if type(value) is not str:
            raise ValueError(message)
        try:
            return cls(value)
        except ValueError:
            raise ValueError(message) from None
