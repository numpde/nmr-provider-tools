"""Construct immutable scheduler declarations from provider-owned values."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import hashlib

from provider_core.contribution import AssetDescription, ConfigurationDescription
from provider_core.payload import JsonDataclassCodec


__all__ = (
    "binding_revision",
    "json_asset",
    "json_configuration",
)


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


def _revision_digest(parts: tuple[str, ...]) -> str:
    digest = hashlib.sha256()

    for (index, part) in enumerate(parts):
        if type(part) is not str:
            raise TypeError(f"revision part {index} must be a string")
        try:
            encoded_part = part.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError(f"revision part {index} must be ASCII") from error
        digest.update(len(encoded_part).to_bytes(8, "big"))
        digest.update(encoded_part)

    return "sha256:" + digest.hexdigest()


def json_configuration[T](
    value: T,
    *,
    format_id: str,
) -> ConfigurationDescription:
    """Describe one immutable JSON configuration value."""
    actual_format, encoded_value = _json_dataclass_bytes(
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
    _, encoded_value = _json_dataclass_bytes(
        value,
        format_id=format_id,
    )
    return AssetDescription(
        name=name,
        media_type=media_type,
        digest=_sha256_digest(encoded_value),
        size_bytes=len(encoded_value),
    )


def binding_revision(
    *,
    implementation: str,
    configuration: ConfigurationDescription,
    assets: tuple[AssetDescription, ...],
) -> str:
    """Derive a configured binding revision from code, configuration and assets."""
    if not (
        type(implementation) is str
        and implementation
        and implementation.isascii()
        and all(
            character.isprintable() and not character.isspace()
            for character in implementation
        )
    ):
        raise ValueError(
            "implementation must be a nonempty ASCII identity without whitespace"
        )
    if type(configuration) is not ConfigurationDescription:
        raise TypeError("configuration must be a ConfigurationDescription")
    if not (
        type(assets) is tuple
        and all(type(asset) is AssetDescription for asset in assets)
    ):
        raise TypeError("assets must be a tuple of AssetDescription records")

    revision_parts = (
        implementation,
        configuration.digest,
        *(asset.digest for asset in assets),
    )
    return _revision_digest(revision_parts)
