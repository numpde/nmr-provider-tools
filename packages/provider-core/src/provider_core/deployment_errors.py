"""Keep deployment refusal identity stable for library and module CLI calls."""

from provider_core.container_guard import require_container

require_container()


class BindingDefinitionRejected(ValueError):
    """A safe definition refusal; native/parser evidence remains its cause."""


class RouteProjectionRejected(ValueError):
    """A safe refusal of an explicit image, identity, path or storage grant."""
