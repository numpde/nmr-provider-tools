"""Recognize the full Docker image and network ID spellings used by delivery."""

from provider_core.container_guard import require_container

require_container()

import re


def is_full_image_id(image_id: str) -> bool:
    """Check Docker's digest-prefixed image identifier."""

    return re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is not None


def is_full_network_id(network_id: str) -> bool:
    """Check Docker's full network identifier."""

    return re.fullmatch(r"[0-9a-f]{64}", network_id) is not None
