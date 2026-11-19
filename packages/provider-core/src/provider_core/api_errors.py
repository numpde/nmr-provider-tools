"""Retain API operation context and late transport evidence without retrying work."""

from provider_core.container_guard import require_container

require_container()

import time

from provider_core.transport_errors import ExchangeFailed
from provider_core.validation.upstream.http import read_retry_after
from provider_core.validation.upstream.nmr.v1.request_ids import read_request_id


class ApiFailure(RuntimeError):
    """Safe operation context with private evidence for authorized diagnostics.

    response and cause can contain private instructions or credentials. They must
    not be rendered verbatim. This owner never retries an operation implicitly.
    """

    def __init__(self, operation, reason, *, response=None, cause=None):
        super().__init__(f"Cannot {operation}: {reason}")
        self.operation = operation
        self.response = response
        self.cause = cause
        self.problem_code = None
        self.request_method = None
        self.request_target = None
        self.issuance_may_have_committed = False
        self.attempt_mutation_may_have_committed = False

    def _received_head(self):
        # Transport retirement can add late headers to the original cause
        # Read diagnostic facts from that owner; never copy them into stale API state
        if self.response is not None:
            return self.response

        # silent-defaults: allow-contract -- Only a received transport head supplies response evidence; other failures leave it absent.
        return self.cause.response_head if isinstance(self.cause, ExchangeFailed) else None

    @property
    def status(self):
        head = self._received_head()

        # silent-defaults: allow-contract -- An unobserved response has no status; absence must remain distinguishable from a received status.
        return None if head is None else head.status

    @property
    def request_id(self):
        head = self._received_head()

        # silent-defaults: allow-contract -- Request correlation exists only in an observed response.
        return None if head is None else read_request_id(head.headers)

    @property
    def retry_after_seconds(self):
        """Read server timing advice; this does not authorize another request.

        RFC 9110 section 10.2.3 permits delay-seconds or an HTTP-date. Invalid
        or ambiguous advice leaves the operation owner's local pacing intact.
        """
        head = self._received_head()

        if head is None:
            return None

        return read_retry_after(head.headers, wall_clock=time.time)
