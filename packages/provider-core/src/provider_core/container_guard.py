"""Check launcher policy; actual isolation belongs to the container composition."""

import os
import reprlib


def require_container():
    """Reject accidental host execution without depending on a repository checkout."""

    provider_core_container_marker = os.environ.get("PROVIDER_CORE_IN_CONTAINER")

    if not (provider_core_container_marker == "1"):
        raise SystemExit(
            f"{reprlib.repr(provider_core_container_marker)=} should be '1'; "
            f"use the consumer's maintained contained launcher"
        )
