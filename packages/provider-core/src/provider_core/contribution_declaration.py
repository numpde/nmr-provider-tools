"""Construct immutable scheduler declarations from provider-owned values."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import hashlib

from provider_core.contribution import AssetDescription, ConfigurationDescription
from provider_core.payload import JsonDataclassCodec


def _sha256_digest(content: bytes) -> str:
    if type(content) is not bytes:
        raise TypeError("content must be bytes")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_dataclass_bytes[T](
    value: T,
    *,
    format_id: str,
) -> tuple[str, bytes]:
    codec = JsonDataclassCodec(
        format=format_id,
        value_type=type(value),
    )
    return codec.format, codec.encode(value)


def json_configuration[T](
    value: T,
    *,
    format_id: str,
) -> ConfigurationDescription:
    """Describe one immutable JSON configuration value."""
    (actual_format, encoded_value) = _json_dataclass_bytes(
        value,
        format_id=format_id,
    )
    return ConfigurationDescription(
        format=actual_format,
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
    (_, encoded_value) = _json_dataclass_bytes(
        value,
        format_id=format_id,
    )
    return AssetDescription(
        name=name,
        media_type=media_type,
        digest=_sha256_digest(encoded_value),
        size_bytes=len(encoded_value),
    )
