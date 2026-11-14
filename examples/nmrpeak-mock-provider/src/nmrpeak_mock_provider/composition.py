"""Construct the provider-selected HF/CHF objects and hand them to the SDK."""

from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from provider_core.embedded_host import EmbeddedHost
from provider_core.scientific import HostedContribution, ResourceRequirement

from .binding import Chf, Hf
from .science import Model, Settings


@dataclass(frozen=True, slots=True, kw_only=True)
class Offerings:
    hf: HostedContribution
    chf: HostedContribution


def adopt_offerings(
    *,
    host: EmbeddedHost,
    requested_requirements: frozenset[ResourceRequirement],
    hf_settings: Settings,
    chf_settings: Settings,
    create_model: Callable[[], Model],
) -> Offerings:
    """Assemble science from the selected deployment's settings and model constructor.

    Provider startup supplies typed settings and create_model. This helper builds
    Hf and Chf around that constructor and the packaged checkpoint substitutes,
    then asks EmbeddedHost to retain each exact object before initialization.
    """
    package = files(anchor=__package__)
    # This example uses packaged JSON checkpoint substitutes. Provider startup
    # selected their expected digests in hf_settings and chf_settings.
    hf = Hf(
        configuration=hf_settings,
        checkpoint_path=Path(str(package.joinpath("hf.json"))),
        create_model=create_model,
    )
    chf = Chf(
        configuration=chf_settings,
        checkpoint_path=Path(str(package.joinpath("chf.json"))),
        create_model=create_model,
    )
    return Offerings(
        hf=host.adopt(contribution=hf, requirements=requested_requirements),
        chf=host.adopt(contribution=chf, requirements=requested_requirements),
    )


def interpretation_messages(*, source: str) -> list[dict]:
    instructions = files(anchor=__package__).joinpath("interpreter.md").read_text(encoding="utf-8")
    return [{"role": "system", "content": instructions}, {"role": "user", "content": source}]


def interpretation_tools(*, carbon: bool) -> list[dict]:
    """Describe the selected offering; the typed input codec admits its values."""
    peak = {"type": "object", "properties": {
        "centroid": {"type": "string"}, "integral": {"type": "integer"},
        "multiplicity": {"type": "string"},
        "couplings": {"type": "array", "items": {"type": "string"}},
    }, "required": ["centroid", "integral", "multiplicity", "couplings"], "additionalProperties": False}
    properties = {"formula": {"type": "string"}, "proton_peaks": {"type": "array", "items": peak}}
    if carbon:
        properties["carbon_peaks"] = {"type": "array", "items": {"type": "string"}}
    value = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    return [{"type": "function", "function": {
        "name": "submit_interpretation",
        "description": "Submit the formula and source-faithful NMR peaks for validation.",
        "parameters": {"type": "object", "properties": {"value": value}, "required": ["value"], "additionalProperties": False},
    }}]
