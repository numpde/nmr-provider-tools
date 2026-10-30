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
from provider_core.shared_delivery_config import SharedDeliveryConfiguration
from provider_core.shared_delivery_ipc import SharedDeliverySocket
from provider_core.shared_delivery_service import SharedDeliveryService
from provider_core.transport import require_unix_socket_path


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
    configuration = parser.add_mutually_exclusive_group(required=True)
    configuration.add_argument("--config", type=Path, help="Frozen deployment-generated delivery configuration.")
    configuration.add_argument("--shared-config", type=Path, help="Shared operator configuration, reloaded while running.")
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--socket-group-id", type=int)
    parser.add_argument("--proxy-socket", type=Path)
    parser.add_argument("--request-seconds", type=float)
    parser.add_argument("--status-directory", type=Path)
    options = parser.parse_args()
    if options.shared_config is not None and (
        not options.shared_config.is_absolute() or options.state_directory is None or not options.state_directory.is_absolute()
        or options.socket is None or options.proxy_socket is None or options.request_seconds is None
        or options.status_directory is None or not options.status_directory.is_absolute()
    ):
        parser.error("shared delivery requires absolute configuration, state and status directories, both socket paths and request deadline")
    os.umask(0o077)
    notices = DeliveryNotices(sys.stderr, secrets=())
    service = None
    try:
        if options.config is not None:
            service = load_delivery_service(options.config, on_notice=notices, diagnostic_status=notices.status)
        else:
            require_unix_socket_path(str(options.proxy_socket))
            live = SharedDeliveryConfiguration(options.shared_config)
            settings = live.read()
            service = SharedDeliveryService(options.state_directory, limits=settings.limits,
                socket=SharedDeliverySocket(options.socket, options.socket_group_id), proxy_socket=options.proxy_socket,
                request_seconds=options.request_seconds, retry_initial_seconds=settings.retry_initial_seconds,
                retry_max_seconds=settings.retry_max_seconds, on_notice=notices, diagnostic_status=notices.status,
                live_configuration=live, status_directory=options.status_directory)
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
