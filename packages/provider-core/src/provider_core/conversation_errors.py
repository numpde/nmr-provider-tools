"""Share conversation rejection types without importing transcript operations."""

from provider_core.container_guard import require_container

require_container()


class ToolRejected(ValueError):
    """An owned, public correction reason; never raw model or worker diagnostics."""


class ConversationFailed(RuntimeError):
    """Safe context with original private failures, separate from public routes."""

    # silent-defaults: allow-contract -- Local conversation refusals can precede any endpoint failure; an empty history records that fact without inventing an endpoint.
    def __init__(self, message, *, cause=None, endpoint_failures=(), correction_failures=()):
        super().__init__(message)
        self.cause = cause
        self.endpoint_failures = tuple(endpoint_failures)
        self.correction_failures = tuple(correction_failures)
