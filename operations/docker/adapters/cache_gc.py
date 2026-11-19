"""Encode Docker daemon cache GC configuration and validation commands."""

import json
from pathlib import Path
from typing import Any, TextIO

from operations.docker.validation.upstream.cache_gc import GC_THRESHOLD_FIELDS, require_gc_object
from type_requirements import require_type


def configure_gc(
    document: dict[str, Any],
    *,
    enabled: bool,
    reserved_space: str,
    max_used_space: str,
    min_free_space: str,
) -> dict[str, Any]:
    """Write caller-selected GC values into a copy of the native configuration."""

    proposed_configuration = json.loads(json.dumps(document))

    # silent-defaults: allow-contract -- Installation creates absent native GC objects; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_native_policy_merge_preserves_unrelated_fields_and_runtime_reads_it
    builder_settings = proposed_configuration.setdefault("builder", {})

    require_type(builder_settings, dict)

    # silent-defaults: allow-contract -- Installation creates absent native GC objects; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_native_policy_merge_preserves_unrelated_fields_and_runtime_reads_it
    gc_settings = require_gc_object(builder_settings.setdefault("gc", {}))

    gc_settings["enabled"] = enabled

    gc_settings.update(zip(GC_THRESHOLD_FIELDS, (reserved_space, max_used_space, min_free_space)))

    return proposed_configuration


def write_daemon_config(document: dict[str, Any], output: TextIO) -> None:
    """Write Docker's configuration JSON into the caller's staged file."""

    json.dump(document, output, indent=2)
    print(file=output)


def validate_daemon_config_command(path: Path) -> list[str]:
    """Build the dockerd command that only validates a configuration file."""

    return ["dockerd", "--validate", "--config-file", str(path)]
