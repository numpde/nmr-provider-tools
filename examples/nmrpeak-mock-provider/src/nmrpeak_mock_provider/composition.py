"""Provider-owned assembly of scientific objects and their execution requirements."""

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
import tomllib

from provider_core.embedded_host import EmbeddedHost
from provider_core.scientific import HostedContribution, ResourceRequirement

from .binding import Chf, Hf
from .fake_model import Model
from .science import Settings


@dataclass(frozen=True, slots=True, kw_only=True)
class Offerings:
    hf: HostedContribution
    chf: HostedContribution


def adopt_offerings(
    *,
    host: EmbeddedHost,
    requested_requirements: tuple[ResourceRequirement, ...],
) -> Offerings:
    """No SDK discovery: the provider explicitly chooses classes and model code."""
    package = files(anchor=__package__)
    settings = tomllib.loads(package.joinpath("settings.toml").read_text(encoding="utf-8"))
    # Installed-wheel files remain available for this deployment's whole lifetime.
    # Real NMRPeak supplies its admitted checkpoint mount here instead.
    hf = Hf(configuration=Settings(**settings["hf"]), checkpoint_path=Path(str(package.joinpath("hf.json"))), create_model=Model)
    chf = Chf(configuration=Settings(**settings["chf"]), checkpoint_path=Path(str(package.joinpath("chf.json"))), create_model=Model)
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
