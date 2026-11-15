"""Construct the provider-selected HF/CHF objects and hand them to the SDK."""

from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from provider_core.embedded_host import EmbeddedHost
from provider_core.scientific import HostedContribution, ResourceRequirement

from .binding import Chf, Hf
from .science import Model, Settings


def adopt_offerings(
    *,
    host: EmbeddedHost,
    requested_requirements: frozenset[ResourceRequirement],
    hf_settings: Settings,
    chf_settings: Settings,
    hf_checkpoint_path: Path,
    chf_checkpoint_path: Path,
    create_model: Callable[[], Model],
) -> frozenset[HostedContribution]:
    """Assemble science from the selected deployment's settings and model constructor.

    Provider startup supplies typed settings and create_model. This helper builds
    Hf and Chf around that constructor and deployment-selected checkpoints,
    then asks EmbeddedHost to retain each exact object before initialization.
    """
    hf = Hf(
        configuration=hf_settings,
        checkpoint_path=hf_checkpoint_path,
        create_model=create_model,
    )
    chf = Chf(
        configuration=chf_settings,
        checkpoint_path=chf_checkpoint_path,
        create_model=create_model,
    )
    return frozenset({
        host.adopt(contribution=hf, requirements=requested_requirements),
        host.adopt(contribution=chf, requirements=requested_requirements),
    })


def interpretation_messages(*, source: str) -> list[dict]:
    instructions = files(anchor=__package__).joinpath("interpreter.md").read_text(encoding="utf-8")
    return [{"role": "system", "content": instructions}, {"role": "user", "content": source}]
