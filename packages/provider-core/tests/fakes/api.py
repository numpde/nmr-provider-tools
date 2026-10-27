"""Controlled authenticated replies shaped from the API revision in core README."""

import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity

from provider_core.transport import ExchangeFailed, HttpResponse


# silent-defaults: allow-contract -- This signature-window specimen selects an explicit known request and code.
def signature_window_problem(request_id="request:test", *, code="authentication_window_closed"):
    """Complete correlated refusal; arbitrary server prose must stay private."""
    return json.dumps(
        {
            "type": "urn:nmr-api:problem:authentication-failed",
            "title": "Authentication failed",
            "status": 401,
            "instance": "urn:nmr-api:request:test",
            "request_id": request_id,
            "code": code,
            "detail": "PRIVATE_SERVER_DETAIL",
        }
    ).encode()


class Transport:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []
        self.last_failure = None
        self.exchange_seconds = 30
        self.retirement_seconds = 5
        self.retirement_confirmed = True
        self.pending_owner = None

    async def retire(self, *, owner=None):
        if not self.retirement_confirmed and self.pending_owner is not owner:
            raise ExchangeFailed("Foreign exchange owner")
        self.retirement_confirmed = True
        self.pending_owner = None

    async def exchange(self, request, *, maximum_bytes, owner=None):
        if not self.retirement_confirmed:
            self.pending_owner = owner
        self.calls.append((request, maximum_bytes))
        result = next(self.replies)
        if isinstance(result, BaseException):
            raise result
        return result


# silent-defaults: allow-contract -- The ordinary synthetic HTTP response is successful; tests explicitly choose refusal status.
def reply(body, *, status=200, headers=None):
    # silent-defaults: allow-contract -- Synthetic refusals retain request correlation; successful responses omit that refusal evidence.
    return HttpResponse(
        status,
        headers
        if headers is not None
        else response_headers(status=status, request_id=b"request:test" if status != 200 else None),
        body,
    )


# silent-defaults: allow-contract -- This fixture supplies the ordinary valid dev-local response envelope.
def response_headers(*, topology=b"dev-local", request_id=None, status=200):
    """Authored valid API metadata; explicit adversarial headers stay untouched."""
    # silent-defaults: allow-contract -- The chosen response status selects protocol JSON or problem JSON media type.
    headers = (
        (b"content-type", b"application/json" if status == 200 else b"application/problem+json"),
        (b"cache-control", b"no-store"),
        (b"nmr-api-topology", topology),
    )
    # silent-defaults: allow-contract -- Absent request correlation emits no header and never fabricates an ID.
    return headers if request_id is None else (*headers, (b"x-request-id", request_id))


def api(transport, origin):
    """Use a disposable signing identity with the explicitly selected transport."""
    return ProviderAPI(
        SigningIdentity(origin, "credential:test", Ed25519PrivateKey.generate()), transport, expected_topology="dev-local"
    )
