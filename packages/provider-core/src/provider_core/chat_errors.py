"""Keep model-carrier failure identity available without HTTP dependencies."""

from provider_core.container_guard import require_container

require_container()


class ChatFailed(RuntimeError):
    """Safe operation text; original HTTP response and causes stay private."""

    def __init__(self, reason, message, *, cause=None, response=None, endpoint_error=None):
        super().__init__(message)
        self.reason = reason
        self.cause = cause
        self.response = response
        self.endpoint_error = endpoint_error
        self.interruption_cause = None
