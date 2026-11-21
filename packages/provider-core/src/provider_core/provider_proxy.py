"""Serve a private CONNECT route for explicitly granted service authorities.

The host supplies admitted configuration authorities and, for a named internal
network, its observed member addresses. This process does not discover peers or
claim those observations remain current. TLS authenticates the original name in
the controller. The host must retire this exact container after controller drain;
closing a daemon-thread listener does not establish tunnel retirement.

Header, thread, device-bound DNS/TCP and relay owners are reused from
provider_core.bound_http_proxy (its pinned Magnet/SECS sources are documented there).
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import fcntl
import ipaddress
import logging
import os
import re
import reprlib
import select
import socket
import stat
import sys
import time
import tomllib
import pathlib
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus

from provider_core import bound_http_proxy
from provider_core.adapters import http_proxy
from provider_core.diagnostic_text import CredentialRedactingFormatter
from provider_core.tools import require_absolute_path, require_nonnegative_number
from provider_core.type_requirements import require_type
from provider_core.validation.upstream.dns import read_dns_labels
from provider_core.validation.upstream.http_proxy import read_port


def _proxy_routine_record(record):
    return (record.name == "provider_core.proxy") or (record.levelno >= logging.WARNING)


def _configure_proxy_logging(level):
    """Keep this proxy's routine logs and the existing dependency WARNING floor.

    Ports nmrnet_provider/runtime_logging.py at 765958643bd3371bc908218d09caaa73fcb406a4.
    This executable needs no scientific binding merely to configure its logger.
    """

    expected_levels = ("INFO", "DEBUG", "WARNING")

    if not (level in expected_levels):
        raise ValueError(f"{level=} should be in {expected_levels=}")

    handler = logging.StreamHandler(sys.stderr)

    handler.addFilter(_proxy_routine_record)
    handler.setFormatter(CredentialRedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logging.getLogger("provider_core.proxy").setLevel(level)


class ServiceRefused(ValueError):
    """A request or configuration violates the proxy's local authority policy."""


def authority_parts(authority):
    """Admit the exact ASCII host:port representation emitted by configuration."""
    (host, separator, port_text) = authority.rpartition(":")

    if not (separator):
        raise ServiceRefused(f"{reprlib.repr(authority)=} should include a port separator")

    expected_host_pattern = r"[a-z0-9][a-z0-9.-]*"

    if not re.fullmatch(expected_host_pattern, host):
        raise ServiceRefused(f"{reprlib.repr(host)=} should match {expected_host_pattern=}")

    try:
        read_dns_labels(host)
        port = read_port(port_text)
    except (UnicodeError, ValueError) as cause:
        raise ServiceRefused("Cannot admit service proxy authority: its DNS name or port is malformed.") from cause

    if not (port_text == (expected_port_text := str(port))):
        raise ServiceRefused(f"{reprlib.repr(port_text)=} should match {expected_port_text=}")

    return (host, port)


@dataclass(frozen=True, slots=True)
class ServicePolicy:
    """Exact destination grant and optional frozen local address projection."""

    authorities: frozenset[tuple[str, int]]
    addresses: tuple[tuple[str, str], ...]

    @classmethod
    def admit(cls, mode, authorities, addresses):
        expected_modes = ("local", "mapped", "external")

        if not (mode in expected_modes):
            raise ServiceRefused(f"{mode=} should be in {expected_modes=}")

        admitted = frozenset(authority_parts(authority_text) for authority_text in authorities)

        if not (admitted):
            raise ServiceRefused(f"{admitted=} should be nonempty")

        if (unique_authority_count := len(admitted)) != (configured_authority_count := len(authorities)):
            raise ServiceRefused(f"{unique_authority_count=} should match {configured_authority_count=}")

        mapped = {}

        for address_assignment in addresses:
            (host, separator, address_text) = address_assignment.partition("=")

            try:
                address = str(ipaddress.IPv4Address(address_text))
            except ipaddress.AddressValueError:
                raise ServiceRefused("Cannot configure local service proxy: each peer requires a literal IPv4 address.") from None

            if not (separator):
                raise ServiceRefused(f"{reprlib.repr(address_assignment)=} should include an address separator")

            if host in mapped:
                raise ServiceRefused(f"{host=} should have one address assignment; {mapped[host]=}")

            try:
                literal = ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError:
                literal = None

            if (literal is not None) and (str(literal) != address):
                raise ServiceRefused(f"{address=} should match {str(literal)=}")

            mapped[host] = address

        if (mode == "external") and mapped:
            raise ServiceRefused(f"{reprlib.repr(mapped)=} should be empty for {mode=}")

        if (mode in ("local", "mapped")) and (mapped.keys() != (expected_hosts := {host for (host, _) in admitted})):
            raise ServiceRefused(f"{reprlib.repr(set(mapped))=} should match {reprlib.repr(expected_hosts)=}")

        return cls(admitted, tuple(sorted(mapped.items())))

    def destination(self, header):
        if not header.startswith(expected_start := b"CONNECT "):
            raise ServiceRefused(f"Request header does not have {expected_start=}.")

        try:
            destination = http_proxy.parse_destination(header)
            (_, host, port, _) = destination
            (_, target, _) = http_proxy.request_line(header)

            if authority_parts(target) != (host, port):
                raise ValueError
        except (UnicodeError, ValueError) as cause:
            raise ServiceRefused("Cannot admit service proxy request: the CONNECT authority is malformed.") from cause

        if not ((host, port) in self.authorities):
            raise ServiceRefused(f"{(host, port)=} should be in {reprlib.repr(self.authorities)=}")

        return destination

    def connect(self, host, port, interface):
        """Explicit addresses use the existing literal shortcut, with no DNS fallback."""
        address = dict(self.addresses)[host] if self.addresses else host

        return bound_http_proxy.connect_bound(address, port, interface)


@dataclass(frozen=True, slots=True)
class LiveProxySettings:
    policy: ServicePolicy
    max_connections: int
    log_level: str
    max_admission_delay_seconds: float
    admission_order: str


class LiveProxyConfiguration:
    """Read the host-owned proxy settings and apply changes between accepts."""

    def __init__(self, path, mode):
        (self.path, self.mode) = (path, mode)
        self.next_poll = 0.0

    def read(self):
        descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)

        try:
            if not stat.S_ISREG(observed_stat_mode := os.fstat(descriptor).st_mode):
                raise ServiceRefused(f"Proxy configuration must be a regular file but {observed_stat_mode=}.")

            max_configuration_bytes = 65536
            raw = bytearray()

            while len(raw) <= max_configuration_bytes:
                part = os.read(descriptor, max_configuration_bytes + 1 - len(raw))

                if not part:
                    break

                raw.extend(part)
        finally:
            os.close(descriptor)

        if (configuration_bytes := len(raw)) > max_configuration_bytes:
            raise ServiceRefused(f"{configuration_bytes=} should be at most {max_configuration_bytes=}")

        try:
            proxy_configuration = tomllib.loads(raw.decode("utf-8"))
        except RecursionError:
            raise ServiceRefused("Proxy configuration is too deeply nested.") from None

        proxy_fields = proxy_configuration.get("api_proxy")

        require_type(proxy_fields, dict, error_factory=ServiceRefused)

        max_connections = proxy_fields.get("max_connections")
        log_level = proxy_fields.get("log_level")

        # silent-defaults: allow-contract -- Existing settings gain the seven-second ramp when the runtime is updated.
        max_admission_delay_seconds = proxy_fields.get("max_admission_delay_seconds", 7.0)

        # silent-defaults: allow-contract -- Existing settings use the ready-first order when the runtime is updated.
        admission_order = proxy_fields.get("admission_order", "ready")

        authorities = proxy_fields.get("allowed_authorities")

        require_type(max_connections, int, error_factory=ServiceRefused)

        if max_connections <= 0:
            raise ServiceRefused(f"{max_connections=} should be positive")

        try:
            require_nonnegative_number(max_admission_delay_seconds)
        except (TypeError, ValueError, OverflowError) as cause:
            raise ServiceRefused("Cannot configure max_admission_delay_seconds") from cause

        if not (max_admission_delay_seconds <= bound_http_proxy.HEADER_SECONDS):
            raise ServiceRefused(f"{max_admission_delay_seconds=} should be at most {bound_http_proxy.HEADER_SECONDS=}")

        expected_admission_orders = ("ready", "fifo")

        if not (admission_order in expected_admission_orders):
            raise ServiceRefused(f"{admission_order=} should be in {expected_admission_orders=}")

        expected_log_levels = ("INFO", "DEBUG", "WARNING")

        if not (log_level in expected_log_levels):
            raise ServiceRefused(f"{log_level=} should be in {expected_log_levels=}")

        require_type(authorities, list, error_factory=ServiceRefused)

        for authority in authorities:
            require_type(authority, str, error_factory=ServiceRefused)

        # silent-defaults: allow-contract -- External routes need no mapped address table.
        route = proxy_fields.get("route", {})  # silent-defaults: allow-empty-get -- External routes need no mapping table.

        require_type(route, dict, error_factory=ServiceRefused)

        # silent-defaults: allow-contract -- External routes have no mapped addresses.
        addresses = route.get("addresses", [])  # silent-defaults: allow-empty-get -- External routes have no mapped addresses.

        require_type(addresses, list, error_factory=ServiceRefused)

        for route_address in addresses:
            require_type(route_address, str, error_factory=ServiceRefused)

        if (self.mode == "external") and addresses:
            raise ServiceRefused(f"{reprlib.repr(addresses)=} should be empty for {self.mode=}")

        hosts = {authority_parts(authority)[0] for authority in authorities}

        selected = (addresses if self.mode == "external" else
                    [address for address in addresses if address.partition("=")[0] in hosts])

        policy = (ServicePolicy.admit(self.mode, authorities, selected) if authorities
                  else ServicePolicy(frozenset(), ()))

        return LiveProxySettings(policy, max_connections, log_level, max_admission_delay_seconds, admission_order)

    def apply(self, server, settings):
        server.policy = settings.policy

        server._slots.set_limit(settings.max_connections)
        server.max_admission_delay_seconds = settings.max_admission_delay_seconds
        server.admission_order = settings.admission_order

        logging.getLogger("provider_core.proxy").setLevel(settings.log_level)

    def refresh(self, server):
        now = time.monotonic()

        if now < self.next_poll:
            return

        self.next_poll = now + 5

        try:
            settings = self.read()
        except ServiceRefused as error:
            (
                logging
                .getLogger("provider_core.proxy")
                .warning("Cannot reload proxy settings: %s Retaining the current settings.", error)
            )

            return
        except (OSError, UnicodeError, ValueError) as error:
            (
                logging
                .getLogger("provider_core.proxy")
                .warning("Cannot reload proxy settings; retaining current settings", exc_info=error)
            )

            return

        self.apply(server, settings)


class ServiceProxyHandler(bound_http_proxy.ProxyHandler):
    def destination(self, header):
        self.policy = self.server.policy

        return self.policy.destination(header)

    def connect(self, host, port):
        return self.policy.connect(host, port, self.server.outbound_interface)

    def admission_reply(self, error):
        if isinstance(error, ServiceRefused):
            return http_proxy.closing_response(HTTPStatus.FORBIDDEN)

        return super().admission_reply(error)

class ServiceProxyServer(bound_http_proxy.ProxyServer):
    """Reuse bounded workers/relay on an exclusively created Unix listener."""

    address_family = socket.AF_UNIX
    request_handler = ServiceProxyHandler

    # silent-defaults: allow-contract -- Existing private proxy callers retain their private socket unless a shared grant is requested.
    def __init__(self, path, interface, policy, *, status_directory, live_configuration=None, shared_socket=False):
        from provider_core.process_status import ProcessStatus

        self.policy = policy
        self.live_configuration = live_configuration
        self.shared_socket = shared_socket
        self.max_admission_delay_seconds = 0.0
        self.admission_order = "ready"
        self.pending = deque()

        super().__init__(str(path), interface)

        try:
            self.process_status = ProcessStatus(status_directory, "proxy")

            self.process_status.refresh("ready")
        except BaseException:
            self.server_close()
            raise

    def service_actions(self):
        self.process_status.refresh("ready")

        if self.live_configuration is not None:
            self.live_configuration.refresh(self)

        self._start_waiting()

    def process_request(self, request, client_address):
        if (self.live_configuration is None) and (not self.shared_socket):
            return super().process_request(request, client_address)

        self._start_waiting()
        (active, capacity) = self._slots.load()

        if (active * 2) <= capacity:
            fraction = 0.0
        elif active >= capacity:
            fraction = 1.0
        else:
            fraction = 2 * active / capacity - 1

        delay_seconds = self.max_admission_delay_seconds * fraction

        if (delay_seconds == 0) and (self.admission_order == "ready") and self._slots.acquire():
            self._start_reserved(request, client_address)

            return

        if len(self.pending) >= capacity:
            self.shutdown_request(request)

            return

        ready_at = time.monotonic() + delay_seconds

        self.pending.append((ready_at, ready_at + bound_http_proxy.HEADER_SECONDS, request, client_address))
        self._start_waiting()

    def _start_waiting(self):
        poller = select.poll()

        for (_, _, client_socket, _) in self.pending:
            poller.register(client_socket, select.POLLHUP | select.POLLERR)

        closed = {fd for (fd, events) in poller.poll(0) if events & (select.POLLHUP | select.POLLERR | select.POLLNVAL)}
        blocked = False

        for _ in range(len(self.pending)):
            (ready_at, expires_at, client_socket, client_address) = self.pending.popleft()
            now = time.monotonic()

            if (client_socket.fileno() in closed) or (now >= expires_at):
                self.shutdown_request(client_socket)
                continue

            if (blocked) or (ready_at > now) or (not self._slots.acquire()):
                self.pending.append((ready_at, expires_at, client_socket, client_address))

                if self.admission_order == "fifo":
                    blocked = True

                continue

            self._start_reserved(client_socket, client_address)

    def _start_reserved(self, client_socket, client_address):
        try:
            self.start_reserved_request(client_socket, client_address)
        except BaseException as error:
            self.shutdown_request(client_socket)

            if not isinstance(error, Exception):
                raise

            self.handle_error(client_socket, client_address)

    def server_close(self):
        while self.pending:
            (_, _, client_socket, _) = self.pending.popleft()

            self.shutdown_request(client_socket)

        super().server_close()

    def server_bind(self):
        # Birth must be private too: a crash before a later chmod would leave
        # a stale public socket that the restart owner must refuse to remove.
        # silent-defaults: allow-contract -- Socket birth mode follows the explicit private or shared grant.
        previous = os.umask(0o117 if self.shared_socket else 0o177)

        try:
            super().server_bind()
        finally:
            os.umask(previous)


@contextmanager
def socket_grant(path, ready, *, socket_group_id=None):
    """Hold exclusive directory ownership before replacing retired private leaves.

    Directory flock follows provider_workers_open's held-parent pattern at
    e1984c46f00190b1dc51a3774ea62a53f7608a4b, tools/provider_workers.sh. A live
    proxy blocks a successor without altering its listener/readiness; after exit
    the successor admits stale leaf type, owner and mode before removal. Serving
    and publication use the held directory, not a reopened replaceable ancestor.
    No host cleanup loop or extra lifetime registry participates in restart.
    """

    try:
        path = require_absolute_path(path)
        ready = require_absolute_path(ready)
    except (TypeError, ValueError) as cause:
        raise ServiceRefused("Cannot admit service proxy paths") from cause

    if path == ready:
        raise ServiceRefused(f"{path=} should differ from {ready=}")

    if path.parent != ready.parent:
        raise ServiceRefused(f"{path.parent=} should match {ready.parent=}")

    descriptor = None

    try:
        try:
            max_socket_path_bytes = 107

            if (socket_path_bytes := len(str(path).encode("utf-8"))) > max_socket_path_bytes:
                raise ServiceRefused(f"{socket_path_bytes=} should be at most {max_socket_path_bytes=}")

            parent = path.parent.lstat()
            shared = socket_group_id is not None

            if shared:
                require_type(
                    socket_group_id,
                    int,
                    error_factory=lambda diagnostic: ServiceRefused("Cannot start service proxy: " + diagnostic),
                )

                if socket_group_id <= 0:
                    raise ServiceRefused(f"{socket_group_id=} should be positive")

                primary_group_id = os.getegid()

                if socket_group_id != primary_group_id:
                    raise ServiceRefused(f"{socket_group_id=} should match {primary_group_id=}")

            # silent-defaults: allow-contract -- Directory modes follow the selected shared or private grant.
            parent_modes = (0o710, 0o2710) if shared else (0o700,)

            if (resolved_parent := path.parent.resolve()) != path.parent:
                raise ServiceRefused(f"{resolved_parent=} should match {path.parent=}")

            if not stat.S_ISDIR(parent.st_mode):
                raise ServiceRefused(f"{path.parent=}: {stat.S_IFMT(parent.st_mode)=:#o} should be {stat.S_IFDIR=:#o}")

            if not (parent.st_uid == (owner_uid := os.geteuid())):
                raise ServiceRefused(f"{parent.st_uid=} should match {owner_uid=}")

            if not ((parent_mode := stat.S_IMODE(parent.st_mode)) in parent_modes):
                raise ServiceRefused(f"{parent_mode=:#o} should be in {parent_modes=}")

            if shared:
                if not (parent.st_gid == socket_group_id):
                    raise ServiceRefused(f"{parent.st_gid=} should match {socket_group_id=}")

            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            held = os.fstat(descriptor)

            if (held.st_dev, held.st_ino, held.st_uid, held.st_mode) != (parent.st_dev, parent.st_ino, parent.st_uid, parent.st_mode):
                raise ServiceRefused(
                    f"{(held.st_dev, held.st_ino, held.st_uid, held.st_mode)=} "
                    f"should match {(parent.st_dev, parent.st_ino, parent.st_uid, parent.st_mode)=}"
                )

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ServiceRefused("Cannot start service proxy: another proxy owns this private route directory.") from error

            retired = []

            for (leaf, required) in ((path, stat.S_ISSOCK), (ready, stat.S_ISREG)):
                try:
                    observed = os.stat(leaf.name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue

                # silent-defaults: allow-contract -- Only the explicitly shared socket is group writable; readiness remains private.
                mode = 0o660 if shared and (leaf == path) else 0o600

                if not required(observed.st_mode):
                    raise ServiceRefused(f"{leaf=}: {observed.st_mode=:#o} should satisfy {required.__name__=}")

                if not (observed.st_uid == (owner_uid := os.geteuid())):
                    raise ServiceRefused(f"{leaf=}: {observed.st_uid=} should match {owner_uid=}")

                if not ((leaf_mode := stat.S_IMODE(observed.st_mode)) == mode):
                    raise ServiceRefused(f"{leaf=}: {leaf_mode=:#o} should be {mode=:#o}")

                if shared and (leaf == path) and (observed.st_gid != socket_group_id):
                    raise ServiceRefused(f"{leaf=}: {observed.st_gid=} should match {socket_group_id=}")

                if leaf == ready:
                    with os.fdopen(os.open(leaf.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor), "rb") as receipt:
                        expected_receipt = b"ready\n"

                        if (ready_receipt := receipt.read(7)) != expected_receipt:
                            raise ServiceRefused(f"{ready_receipt=} should match {expected_receipt=}")

                retired.append(leaf.name)

            for name in retired:
                os.unlink(name, dir_fd=descriptor)
        except OSError as error:
            cause_type = type(error).__name__

            raise ServiceRefused(
                f"Cannot start service proxy: private filesystem grant admission failed with {cause_type = } and {error.errno = }."
            ) from error
        except UnicodeError as error:
            raise ServiceRefused("Cannot start service proxy: its Unix path must be valid UTF-8.") from error

        yield pathlib.Path("/proc/self/fd") / str(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--interface", required=True)
    parser.add_argument("--mode", choices=("local", "mapped", "external"), required=True)
    parser.add_argument("--authority", action="append")
    parser.add_argument("--address", action="append")
    parser.add_argument("--socket", type=pathlib.Path, required=True)
    parser.add_argument("--ready-file", type=pathlib.Path, required=True)
    parser.add_argument("--status-directory", type=pathlib.Path, required=True)
    parser.add_argument("--log-level", choices=("INFO", "DEBUG", "WARNING"))
    parser.add_argument("--live-config", type=pathlib.Path)
    parser.add_argument("--socket-group-id", type=int)
    args = parser.parse_args()

    if (args.live_config is None) and ((not args.authority) or (not args.log_level)):
        parser.error("private routes require --authority and --log-level")

    if (args.live_config is not None) and ((args.authority is not None) or (args.log_level is not None)):
        parser.error("live routes take authorities and log level from --live-config")

    if (args.live_config is not None) and (args.address is not None):
        parser.error("live routes take address mappings from --live-config")

    # Startup errors need the same formatter before live settings can supply
    # their chosen log level. This initial level affects only configuration.
    _configure_proxy_logging("WARNING")

    try:
        # Omitted --address supplies no mappings. External mode requires none;
        # local mode still requires complete coverage in ServicePolicy.admit.
        addresses = args.address

        if addresses is None:
            addresses = ()

        live = None

        if args.live_config is None:
            policy = ServicePolicy.admit(args.mode, args.authority, addresses)

            _configure_proxy_logging(args.log_level)
        else:
            if not args.live_config.is_absolute():
                raise ServiceRefused(f"{args.live_config=} must be an absolute path.")

            live = LiveProxyConfiguration(args.live_config, args.mode)
            settings = live.read()
            policy = settings.policy

            _configure_proxy_logging(settings.log_level)

        bound_http_proxy.require_interface(args.interface)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.interface.encode() + b"\0")

        with socket_grant(args.socket, args.ready_file, socket_group_id=args.socket_group_id) as parent:
            with ServiceProxyServer(
                parent / args.socket.name,
                args.interface,
                policy,
                status_directory=args.status_directory,
                live_configuration=live,
                shared_socket=args.socket_group_id is not None
            ) as server:
                if live is not None:
                    live.apply(server, settings)

                bound_http_proxy.publish_readiness(parent / args.ready_file.name, "ready\n")
                logging.getLogger("provider_core.proxy").info("Service proxy ready.")
                server.serve_forever()
    except (ValueError, UnicodeError, OSError) as error:
        bound_http_proxy.log_failure("Starting service proxy", error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
