"""Read the outbox portion of the shared operator settings file."""

from provider_core.container_guard import require_container

require_container()

import os
import stat
import tomllib
from dataclasses import dataclass

from provider_core.delivery_store import QueueLimits
from provider_core.delivery_worker import require_delivery_retry_settings
from provider_core.type_requirements import require_type


@dataclass(frozen=True, slots=True)
class SharedDeliverySettings:
    limits: QueueLimits
    retry_initial_seconds: float
    retry_max_seconds: float
    log_level: str


class SharedDeliveryConfiguration:
    def __init__(self, path):
        self.path = path

    def read(self):
        outbox_settings_fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)

        try:
            if not stat.S_ISREG(file_mode := os.fstat(outbox_settings_fd).st_mode):
                raise ValueError(f"{outbox_settings_fd=} has bad {file_mode=}")

            outbox_settings_bytes = bytearray()
            max_settings_bytes = 65536

            while len(outbox_settings_bytes) <= max_settings_bytes:
                settings_chunk = os.read(outbox_settings_fd, (max_settings_bytes + 1) - len(outbox_settings_bytes))

                if not settings_chunk:
                    break

                outbox_settings_bytes.extend(settings_chunk)
        finally:
            os.close(outbox_settings_fd)

        if not ((settings_byte_count := len(outbox_settings_bytes)) <= max_settings_bytes):
            raise ValueError(f"{settings_byte_count=} should be at most {max_settings_bytes=}")

        try:
            operator_settings = tomllib.loads(outbox_settings_bytes.decode("utf-8"))
            outbox_settings = operator_settings["outbox"]

            require_type(outbox_settings, dict)

            queue_limits = QueueLimits(
                outbox_settings["provider_items"],
                outbox_settings["provider_bytes"],
                outbox_settings["total_items"],
                outbox_settings["total_bytes"],
            )

            (retry_initial_seconds, retry_max_seconds, log_level) = (
                outbox_settings["retry_initial_seconds"],
                outbox_settings["retry_max_seconds"],
                outbox_settings["log_level"],
            )

            require_delivery_retry_settings(retry_initial_seconds, retry_max_seconds)

            if not (log_level in (expected_log_levels := ("INFO", "DEBUG", "WARNING"))):
                raise ValueError(f"log_level should be one of {expected_log_levels=}")
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as cause:
            raise ValueError(f"Cannot read outbox settings from {self.path=}") from cause

        return SharedDeliverySettings(queue_limits, retry_initial_seconds, retry_max_seconds, log_level)
