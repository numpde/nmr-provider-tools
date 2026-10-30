"""Push self-contained outcomes into the single shared delivery queue."""

from provider_core.container_guard import require_container

require_container()

from dataclasses import dataclass
from pathlib import Path

from provider_core.delivery_handoff import DeliveryHandoffRejected, MAX_HANDOFF_BYTES, read_delivery_handoff
from provider_core.delivery_ipc import DeliveryInbox, DeliveryRequestRejected
from provider_core.transport import require_unix_socket_path


@dataclass(frozen=True, slots=True)
class SharedDeliverySocket:
    socket_path: Path
    socket_group_id: int | None
    api_origin = None
    provider_ref = None

    def __post_init__(self):
        require_unix_socket_path(str(self.socket_path))
        if self.socket_group_id is not None and (
            type(self.socket_group_id) is not int or not 1 <= self.socket_group_id <= 4294967294
        ):
            raise ValueError("Delivery socket group must be an explicit nonzero GID.")


class SharedDeliveryInbox(DeliveryInbox):
    """Expose only submission and the acknowledgement of that submission."""

    max_request_bytes = MAX_HANDOFF_BYTES

    def __init__(self, queue, socket, *, request_seconds, on_accepted, on_failure):
        super().__init__(queue, (socket,), request_seconds=request_seconds, on_accepted=on_accepted,
                         on_failure=on_failure, binding_status=None)

    async def _dispatch(self, endpoint, request, body):
        if request.method != b"POST" or request.target != b"/submit":
            raise DeliveryRequestRejected("Shared delivery accepts only result submissions.")
        try:
            handoff = read_delivery_handoff(body)
        except DeliveryHandoffRejected:
            raise DeliveryRequestRejected("Shared delivery requires a complete publication handoff.") from None
        accepted = await self.storage(self.queue.enqueue_handoff, handoff)
        try:
            self.on_accepted(handoff.outcome)
        except Exception as error:
            # The transaction already committed. A failed publisher wake is a
            # service failure, not a refusal of this durable acknowledgement.
            self.failure = error
            self.failed.set()
            self.on_failure(error, {"event": "delivery_publisher_wake_failed"})
        return accepted
