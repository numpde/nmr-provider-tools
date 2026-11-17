"""Read the outbox portion of the shared operator settings file."""

from provider_core.container_guard import require_container

require_container()

import os
import stat
import tomllib
from dataclasses import dataclass

from provider_core.delivery_store import QueueLimits
from provider_core.delivery_worker import require_delivery_retry_settings


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

            raw = bytearray()

            while len(raw) <= 65536:
                part = os.read(outbox_settings_fd, 65537 - len(raw))

                if not part:
                    break

                raw.extend(part)
        finally:
            os.close(outbox_settings_fd)

        if not (len(raw) <= 65536):
            raise ValueError("Outbox settings exceed the supported file size.")

        try:
            document = tomllib.loads(raw.decode("utf-8"))
            table = document["outbox"]

            if not (type(table) is (expected_type := dict)):
                raise TypeError(f"{type(table)=} ≠ {expected_type=}")

            limits = QueueLimits(table["provider_items"], table["provider_bytes"], table["total_items"], table["total_bytes"])
            (initial, maximum, level) = (table["retry_initial_seconds"], table["retry_max_seconds"], table["log_level"])

            require_delivery_retry_settings(initial, maximum)

            if not (level in ("INFO", "DEBUG", "WARNING")):
                raise ValueError
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            raise ValueError("Outbox settings are invalid; require supported queue, retry and log settings.") from None

        return SharedDeliverySettings(limits, initial, maximum, level)
