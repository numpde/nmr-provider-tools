"""Generate structural schemas without changing scientific object construction.

Pydantic owns annotation-to-schema translation. The SDK closes dataclass objects
because its JSON codec rejects unknown fields, including in nested records.
This optional API dependency is loaded only when schema export is requested.
"""

from provider_core.container_guard import require_container

require_container()

from functools import cache

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema


class _ClosedDataclasses(GenerateJsonSchema):
    def dataclass_schema(self, schema: core_schema.DataclassSchema) -> JsonSchemaValue:
        """Reflect the SDK codec's unknown-field rule without editing provider types."""
        dataclass_definition = super().dataclass_schema(schema=schema)
        dataclass_definition["additionalProperties"] = False

        return dataclass_definition


@cache
def _adapter(*, value_type: type) -> TypeAdapter:
    return TypeAdapter(type=value_type)


def dataclass_json_schema(*, value_type: type) -> dict:
    """Return a fresh validation schema, preserving defaults, tuples and references.

    This describes JSON structure. Exact integer decoding, duplicate keys and
    scientific validity remain obligations of the runtime codec and science.
    """

    return _adapter(value_type=value_type).json_schema(
        by_alias=False,
        mode="validation",
        schema_generator=_ClosedDataclasses,
    )
