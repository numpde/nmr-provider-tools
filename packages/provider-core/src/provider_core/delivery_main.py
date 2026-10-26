"""Run shared pending-only NMR API delivery without scientific execution.

Docker owns process restart, final stop grace and bounded non-blocking logs.
This command consumes the frozen deployment configuration and grants no network
fallback, host filesystem export or Docker-engine access of its own.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from provider_core.delivery_config import DeliveryConfigurationRejected, load_delivery_service
from provider_core.delivery_logging import DeliveryNotices


async def _run(service, notices):
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, service.request_stop)
    try:
        await service.run()
        notices(None, {"event": "delivery_stopped", "retirement_confirmed": service.retirement_confirmed})
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main():
    parser = argparse.ArgumentParser(description="Deliver queued NMR API outcomes without rerunning analyses.")
    parser.add_argument("--config", type=Path, required=True, help="Frozen deployment-generated delivery configuration.")
    options = parser.parse_args()
    os.umask(0o077)
    notices = DeliveryNotices(sys.stderr, secrets=())
    service = None
    try:
        service = load_delivery_service(options.config, on_notice=notices, diagnostic_status=notices.status)
        asyncio.run(_run(service, notices))
        return 0
    # silent-defaults: allow-exception-outcome -- This process boundary reports the retained failure and exits nonzero
    except BaseException as failure:
        # The process boundary reports interruptions and grouped cleanup too.
        # Unknown exception text never reaches stderr through a raw traceback.
        notices(failure, {
            "event": "delivery_failed", "retirement_confirmed": service is None or service.retirement_confirmed,
        })
        if isinstance(failure, KeyboardInterrupt):
            return 130
        # silent-defaults: allow-contract -- Configuration refusal exits 2; other reported process failures exit 1.
        return 2 if type(failure) is DeliveryConfigurationRejected else 1
    finally:
        notices.close()


if __name__ == "__main__":
    raise SystemExit(main())
