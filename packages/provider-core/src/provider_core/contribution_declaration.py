"""Construct immutable scheduler declarations from provider-owned values."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import hashlib

from provider_core.contribution import AssetDescription, ConfigurationDescription
from provider_core.payload import JsonDataclassCodec
from provider_core.type_requirements import require_type


def _sha256_digest(content: bytes) -> str:
    require_type(content, bytes)

    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_dataclass_bytes[T](
    portable_dataclass: T,
    *,
    format_id: str,
) -> bytes:
    codec = JsonDataclassCodec(
        format=format_id,
        value_type=type(portable_dataclass),
    )

    return codec.encode(portable_dataclass)


def json_configuration[T](
    value: T,
    *,
    format_id: str,
) -> ConfigurationDescription:
    """Describe one immutable JSON configuration value."""

    encoded_value = _json_dataclass_bytes(
        value,
        format_id=format_id,
    )

    return ConfigurationDescription(
        format=format_id,
        digest=_sha256_digest(encoded_value),
    )


def json_asset[T](
    value: T,
    *,
    name: str,
    format_id: str,
    media_type: str,
) -> AssetDescription:
    """Describe one immutable JSON asset value."""

    encoded_value = _json_dataclass_bytes(
        value,
        format_id=format_id,
    )

    return AssetDescription(
        name=name,
        media_type=media_type,
        digest=_sha256_digest(encoded_value),
        size_bytes=len(encoded_value),
    )
