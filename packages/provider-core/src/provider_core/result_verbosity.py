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
        if not (type(value) is str):
            raise TypeError(f"Unexpected {type(value) = }")

        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"{cls.__name__} must be one of: {', '.join(x.value for x in cls)}") from None
