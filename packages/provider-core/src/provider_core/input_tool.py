"""Advertise and decode a scientific call's input without duplicating its fields.

The tool name and input codec come from the selected compiled scientific method.
The provider supplies its scientific description and retains execution authority;
decoding a tool proposal never submits scientific work.
"""

from provider_core.container_guard import require_container

require_container()

from dataclasses import dataclass
import json

from provider_core.contribution import CallBinding
from provider_core.conversation import ToolRejected
from provider_core.payload import JsonDataclassCodec


@dataclass(frozen=True, slots=True, kw_only=True)
class ScientificInputToolDefinition:
    """One scientific input exposed as a language-model function tool.

    The definition uses the input record's fields directly as parameters. Its
    defaults and nullable fields retain their codec meaning; this does not force
    an endpoint's strict mode. Schema generation requires provider-core[api].
    Custom codecs are refused rather than assigned guessed JSON semantics.
    """

    call_binding: CallBinding
    description: str

    def __post_init__(self) -> None:
        if not (type(self.call_binding.input_codec) is JsonDataclassCodec):
            raise TypeError(
                f"Unexpected {type(self.call_binding.input_codec)=}; "
                "cannot advertise a scientific input without the built-in JSON dataclass codec"
            )

    @property
    def name(self) -> str:
        return self.call_binding.description.name

    @property
    def definition(self) -> dict:
        """Return a fresh tool carrier with definitions rooted beside its parameters."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.call_binding.input_codec.json_schema(),
            },
        }

    def decode(self, *, name: str, arguments: dict) -> object:
        """Admit the selected tool's arguments using its original runtime codec.

        Structural rejection retains the codec's original exception. The provider
        decides whether a rejected proposal is correctable or ends its request.
        """

        if name != self.name:
            raise ToolRejected(f"Cannot accept {name=}; submit scientific input using {self.name=}")

        return self.call_binding.input_codec.decode(payload=json.dumps(obj=arguments).encode(encoding="utf-8"))
