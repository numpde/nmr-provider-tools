"""Push self-contained outcomes into the single shared delivery queue."""

from provider_core.container_guard import require_container

require_container()

import pathlib
import reprlib
from dataclasses import dataclass

from provider_core.delivery_handoff import DeliveryHandoffRejected, MAX_HANDOFF_BYTES, read_delivery_handoff
from provider_core.delivery_ipc import DeliveryInbox, DeliveryRequestRejected
from provider_core.transport import require_unix_socket_path
from provider_core.type_requirements import require_type


@dataclass(frozen=True, slots=True)
class SharedDeliverySocket:
    socket_path: pathlib.Path
    socket_group_id: int | None
    api_origin = None
    provider_ref = None

    def __post_init__(self):
        require_unix_socket_path(str(self.socket_path))

        if self.socket_group_id is not None:
            require_type(self.socket_group_id, int)

            min_socket_group_id, max_socket_group_id = 1, 4294967294

            if not (min_socket_group_id <= self.socket_group_id <= max_socket_group_id):
                raise ValueError(f"{self.socket_group_id=} should be between {min_socket_group_id=} and {max_socket_group_id=}")


class SharedDeliveryInbox(DeliveryInbox):
    """Expose only submission and the acknowledgement of that submission."""

    max_request_bytes = MAX_HANDOFF_BYTES

    def __init__(self, queue, socket, *, request_seconds, on_accepted, on_failure):
        super().__init__(
            queue,
            (socket,),
            request_seconds=request_seconds,
            on_accepted=on_accepted,
            on_failure=on_failure,
            binding_status=None
        )

    async def _dispatch(self, endpoint, ipc_request, body):
        expected_method, expected_target = b"POST", b"/submit"

        if not (ipc_request.method == expected_method):
            raise DeliveryRequestRejected(f"{reprlib.repr(ipc_request.method)=} should be {expected_method=}")

        if not (ipc_request.target == expected_target):
            raise DeliveryRequestRejected(f"{reprlib.repr(ipc_request.target)=} should be {expected_target=}")

        try:
            handoff = read_delivery_handoff(body)
        except DeliveryHandoffRejected as cause:
            raise DeliveryRequestRejected("Cannot admit the publication handoff") from cause

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
