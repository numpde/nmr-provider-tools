"""Capture proxy diagnostics through the same formatter as deployed processes."""

from contextlib import contextmanager, redirect_stderr
import logging
from unittest.mock import patch

from provider_core.diagnostic_text import CredentialRedactingFormatter


@contextmanager
def capture_proxy_logs(output):
    handler = logging.StreamHandler(output)
    handler.setFormatter(CredentialRedactingFormatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("provider_core.proxy")
    with (
        redirect_stderr(output),
        patch.object(logger, "handlers", [handler]),
        patch.object(logger, "propagate", False),
        patch.object(logger, "level", logging.WARNING),
    ):
        yield
