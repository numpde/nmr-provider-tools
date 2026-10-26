"""Emit safe delivery notices through one ordinary Docker-backed stream handler.

Plain handler precedent: magnet-deploy0619d9ffd92a4326758b442c532aa09496161098,
magnet_provider/provider_process.py::_configure_process_logging, lines718-742.
The deployment owns bounded Docker non-blocking logging. Python cannot detect
Docker's full-buffer drops and does not promise diagnostic durability:
https://docs.docker.com/engine/logging/configure/#configure-the-delivery-mode-of-log-messages-from-container-to-log-driver
No whole-record PIPE_BUF gate, fd-mode changes or background queue belongs here.
"""

from provider_core.container_guard import require_container

require_container()

import json
import logging
import sys

from provider_core.diagnostics import failure_evidence, operator_redactor


class _DeliveryStreamHandler(logging.StreamHandler):
    """Retain output failure instead of logging's recursive stderr traceback."""

    last_failure = None

    def handleError(self, record):
        self.last_failure = sys.exception()


class DeliveryNotices:
    """Keep delivery independent of log output; retain only its latest failure."""

    def __init__(self, stream, *, secrets):
        self.secrets = tuple(secrets)
        self.redact = operator_redactor(secrets=self.secrets)
        self.handler = _DeliveryStreamHandler(stream)
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.Logger("provider_core.delivery", logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)
        self.output_failures, self.latest_failure = 0, None

    def _failed(self, failure, facts, output_failure):
        self.output_failures += 1
        self.latest_failure = (failure, facts, output_failure)

    def __call__(self, failure, facts):
        # All callers emit on the service loop; StreamHandler owns serialized
        # writes. Serialization/output failure affects diagnostics, never the
        # API receipt, queue transaction or another publication decision.
        try:
            document = {name: self.redact(value) if type(value) is str else value for name, value in facts.items()}
            if failure is not None:
                document["failure"] = failure_evidence(failure, secrets=self.secrets)
            self.handler.last_failure = None
            self.logger.info("%s", json.dumps(document, allow_nan=False, separators=(",", ":")))
            if self.handler.last_failure is not None:
                self._failed(failure, facts, self.handler.last_failure)
        except Exception as output_failure:
            self._failed(failure, facts, output_failure)

    def status(self):
        """Observed local output errors; zero does not establish retained logs."""
        failure_type = failure_errno = failed_event = None
        if self.latest_failure is not None:
            _, facts, error = self.latest_failure
            failure_type = type(error).__name__[:128]
            if isinstance(error, OSError) and type(error.errno) is int:
                failure_errno = error.errno
            if type(facts.get("event")) is str:
                failed_event = self.redact(facts["event"])[:128]
        return {
            "output_failures": self.output_failures, "last_failure_type": failure_type,
            "last_failure_errno": failure_errno, "last_failed_event": failed_event,
        }

    def close(self):
        """Release this handler without closing the process-owned stderr stream."""
        self.logger.removeHandler(self.handler)
        self.handler.close()
