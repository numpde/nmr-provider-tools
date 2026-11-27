"""Advertise and decode typed tool input without duplicating its fields.

Application inputs supply a codec and tool name; scientific inputs derive both
from their compiled method. Providers retain interpretation and execution policy;
decoding a proposal never submits scientific work.
"""

from provider_core.container_guard import require_container

require_container()

from dataclasses import dataclass
import json

from provider_core.contribution import CallBinding
from provider_core.adapters.chat_completions.tools import function_tool
from provider_core.conversation_errors import ToolRejected
from provider_core.payload import JsonDataclassCodec
from provider_core.type_requirements import require_type


class _JsonInputTool:
    """Share schema and decoding behavior across explicit and call-derived tools."""

    __slots__ = ()

    @property
    def definition(self) -> dict:
        """Return a fresh tool carrier with definitions rooted beside its parameters."""

        return function_tool(
            name=self.name,
            description=self.description,
            parameters=self.codec.json_schema(),
        )

    def decode(self, *, name: str, arguments: dict) -> object:
        """Admit the selected tool's arguments using its original runtime codec.

        Structural rejection retains the codec's original exception. The provider
        decides whether a rejected proposal is correctable or ends its request.
        """

        if name != self.name:
            raise ToolRejected(f"Cannot accept {name=}; submit input using {self.name=}")

        return self.codec.decode(payload=json.dumps(obj=arguments).encode(encoding="utf-8"))


@dataclass(frozen=True, slots=True, kw_only=True)
class InputToolDefinition(_JsonInputTool):
    """Expose an application's JSON dataclass codec as a named input tool.

    Codec rejection propagates unchanged; the application decides whether to
    request correction or stop. Schema generation requires provider-core[api].
    """

    name: str
    description: str
    codec: JsonDataclassCodec

    def __post_init__(self) -> None:
        require_type(self.codec, JsonDataclassCodec)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScientificInputToolDefinition(_JsonInputTool):
    """Derive an input tool's name and codec from its compiled scientific call.

    Defaults and nullable fields retain their codec meaning; this does not force
    an endpoint's strict mode. Custom codecs are refused by this JSON adapter.
    """

    call_binding: CallBinding
    description: str

    def __post_init__(self) -> None:
        require_type(self.codec, JsonDataclassCodec)

    @property
    def name(self) -> str:
        return self.call_binding.description.name

    @property
    def codec(self) -> JsonDataclassCodec:
        return self.call_binding.input_codec
