"""Carry original exchange failure evidence without importing network libraries."""

from provider_core.container_guard import require_container

require_container()


class ExchangeFailed(RuntimeError):
    """Safe public failure text with original exceptions for protected diagnostics.

    primary, cleanup_failures and response_head may contain private data.
    Recipients must use a protected diagnostic sink, never render them directly.
    response_head describes the origin reply; proxy_connect_status is the
    separately received CONNECT status, never an API receipt.
    Transport failure does not establish whether a server mutation committed.
    """

    # silent-defaults: allow-contract -- No observed cleanup failure is an empty tuple, separate from primary and interruption evidence.
    def __init__(self, message, *, primary=None, cleanup_failures=(), interruption=None, response_head=None, proxy_connect_status=None, network_failure=None, network_io=None, caller_interruption=None):
        super().__init__(message)
        self.primary = primary
        self.cleanup_failures = tuple(cleanup_failures)
        self.interruption = interruption
        self.response_head = response_head
        self.proxy_connect_status = proxy_connect_status
        self.network_failure = network_failure
        self.network_io = network_io
        self.caller_interruption = caller_interruption
        self.child_failure = None
        self.process = None
