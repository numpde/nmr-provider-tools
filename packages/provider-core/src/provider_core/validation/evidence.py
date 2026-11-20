"""Admit schema identities for locally retained evidence and control digests.

These bounds preserve the SDK's evidence envelope. They are local constraints,
independent of the NMR API's result-schema and failure-message fields.
"""

from provider_core.container_guard import require_container

require_container()

from provider_core.type_requirements import require_type


def require_evidence_schema_id(schema_id):
    """Require bounded Unicode scalar text without changing the evidence identity."""

    require_type(schema_id, str)

    schema_character_count = len(schema_id)
    (min_schema_characters, max_schema_characters) = (1, 1024)

    if not (min_schema_characters <= schema_character_count <= max_schema_characters):
        raise ValueError(f"{schema_character_count=} should be between {min_schema_characters=} and {max_schema_characters=}")

    if not all(((character != "\x00") and (not (0xD800 <= ord(character) <= 0xDFFF))) for character in schema_id):
        raise ValueError(f"{schema_id=} should not contain NUL or surrogate code points")
