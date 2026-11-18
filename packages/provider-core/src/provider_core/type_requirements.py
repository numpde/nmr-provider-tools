"""Expose the shared type requirements through the SDK."""

from provider_core.container_guard import require_container

require_container()

from provider_core._type_requirements import require_instance, require_subclass, require_type

__all__ = ["require_type", "require_instance", "require_subclass"]
