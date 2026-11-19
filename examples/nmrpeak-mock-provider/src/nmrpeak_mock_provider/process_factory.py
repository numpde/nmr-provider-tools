"""Installed construction of the NMRPeak example's selected scientific model."""

from collections.abc import Mapping
from pathlib import Path

from . import binding
from .fake_model import Model
from .science import Settings


def construct(*, startup: Mapping):
    """Construct an empty contribution in the worker; initialize loads weights."""
    contribution_type = getattr(binding, startup["contribution_class"])

    return contribution_type(
        configuration=Settings(**startup["settings"]),
        checkpoint_path=Path(startup["checkpoint_path"]),
        create_model=Model,
    )
