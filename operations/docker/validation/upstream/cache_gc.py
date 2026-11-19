"""Read the Docker daemon GC fields used by threshold-based cache collection."""

import json
import reprlib
from typing import Any

from type_requirements import require_type


# repo-check: allow-module-constant
GC_THRESHOLD_FIELDS = ("defaultReservedSpace", "defaultMaxUsedSpace", "defaultMinFreeSpace")


def decode_daemon_config(raw: bytes) -> dict[str, Any]:
    """Read a Docker daemon configuration document."""

    document = json.loads(raw)

    require_type(document, dict)

    return document


def require_gc_object(gc_settings: object) -> dict[str, Any]:
    """Admit the native GC object when reading or updating it."""

    require_type(gc_settings, dict)

    return gc_settings


def builder_gc(document: dict[str, Any]) -> dict[str, Any]:
    """Read the native builder.gc object without creating missing fields."""

    return require_gc_object(document["builder"]["gc"])


def gc_enabled(gc_settings: dict[str, Any]) -> bool:
    """Read the native enable flag before interpreting thresholds."""

    gc_settings_enabled = gc_settings.get("enabled")

    require_type(gc_settings_enabled, bool)

    return gc_settings_enabled


def gc_has_custom_policy(gc_settings: dict[str, Any]) -> bool:
    """Report whether the native configuration supplies a custom GC policy."""

    return bool(gc_settings.get("policy"))


def gc_thresholds(gc_settings: dict[str, Any]) -> list[str]:
    """Admit the selected prune profile in reserved, used, and free space order."""

    thresholds = []

    for gc_threshold_name in GC_THRESHOLD_FIELDS:
        gc_threshold = gc_settings.get(gc_threshold_name)

        require_type(gc_threshold, (str, int))

        if (
            (type(gc_threshold) is str) and
            ((not gc_threshold) or (any(character.isspace() or ord(character) < 32 for character in gc_threshold)))
        ):
            raise ValueError(f"{gc_threshold_name=}: {reprlib.repr(gc_threshold)=} should be nonempty without whitespace or control characters")

        thresholds.append(str(gc_threshold))

    return thresholds
