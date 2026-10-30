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
import socket
import stat
import sys
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from provider_core import bound_http_proxy


def _proxy_routine_record(record):
    return record.name == "provider_core.proxy" or record.levelno >= logging.WARNING


def _configure_proxy_logging(level):
    """Keep this proxy's routine logs and the existing dependency WARNING floor.

    Ports nmrnet_provider/runtime_logging.py at 765958643bd3371bc908218d09caaa73fcb406a4.
    This executable needs no scientific binding merely to configure its logger.
    """
    if level not in ("INFO", "DEBUG", "WARNING"):
        raise ValueError("Cannot configure proxy logging: choose INFO, DEBUG or WARNING.")
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_proxy_routine_record)
    logging.basicConfig(level=logging.WARNING, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    logging.getLogger("provider_core.proxy").setLevel(level)


class ServiceRefused(ValueError):
    """Owned safe refusal; request/configuration values never enter its text."""


def native_failure_detail(error):
    """Project owned DNS reasons or native type/errno, never arbitrary text/notes."""
    if type(error) in (bound_http_proxy.DNSResponseRejected, bound_http_proxy.DNSNameRejected):
        return f"{type(error).__name__}: {error}"
    if isinstance(error, OSError):
        return f"{type(error).__name__}; errno={error.errno}"
    return type(error).__name__


def authority_parts(authority):
    """Admit the exact ASCII host:port representation emitted by configuration."""
    host, separator, port_text = authority.rpartition(":")
    if not separator or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", host):
        raise ServiceRefused(
            "Cannot admit service proxy authority: require a canonical DNS name or IPv4 address and explicit port."
        )
    try:
        bound_http_proxy._dns_name(host)
        port = int(port_text)
    except (UnicodeError, ValueError):
        raise ServiceRefused("Cannot admit service proxy authority: its DNS name or port is malformed.") from None
    if not 1 <= port <= 65535 or port_text != str(port):
        raise ServiceRefused("Cannot admit service proxy authority: require a canonical port from 1 through 65535.")
    return host, port


@dataclass(frozen=True, slots=True)
class ServicePolicy:
    """Exact destination grant and optional frozen local address projection."""

    authorities: frozenset[tuple[str, int]]
    addresses: tuple[tuple[str, str], ...]

    @classmethod
    def admit(cls, mode, authorities, addresses):
        if mode not in ("local", "mapped", "external"):
            raise ServiceRefused("Cannot configure service proxy: select local, mapped or external routing explicitly.")
        admitted = frozenset(authority_parts(value) for value in authorities)
        if not admitted or len(admitted) != len(authorities):
            raise ServiceRefused("Cannot configure service proxy: authorities must be nonempty and unique.")
        mapped = {}
        for value in addresses:
            host, separator, text = value.partition("=")
            try:
                address = str(ipaddress.IPv4Address(text))
            except ipaddress.AddressValueError:
                raise ServiceRefused("Cannot configure local service proxy: each peer requires a literal IPv4 address.") from None
            if not separator or host in mapped:
                raise ServiceRefused("Cannot configure local service proxy: peer address assignments must be unambiguous.")
            try:
                literal = ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError:
                literal = None
            if literal is not None and str(literal) != address:
                raise ServiceRefused("Cannot configure local service proxy: a literal authority must keep its own address.")
            mapped[host] = address
        if mode == "external" and mapped:
            raise ServiceRefused("Cannot configure external service proxy: local address assignments are not allowed.")
        if mode in ("local", "mapped") and mapped.keys() != {host for host, _ in admitted}:
            raise ServiceRefused("Cannot configure mapped service proxy: require exactly one address for every admitted host.")
        return cls(admitted, tuple(sorted(mapped.items())))

    def destination(self, header):
        if not header.startswith(b"CONNECT "):
            raise ServiceRefused("Cannot admit service proxy request: only CONNECT tunnels are allowed.")
        try:
            destination = bound_http_proxy.parse_destination(header)
            _, host, port, _ = destination
            target = header.split(b" ", 2)[1].decode("ascii")
            if authority_parts(target) != (host, port):
                raise ValueError
        except (UnicodeError, ValueError):
            raise ServiceRefused("Cannot admit service proxy request: the CONNECT authority is malformed.") from None
        if (host, port) not in self.authorities:
            raise ServiceRefused(
                "Cannot admit service proxy request: this destination is outside the configured authority grant."
            )
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


class LiveProxyConfiguration:
    """Read the host-owned proxy settings and apply changes between accepts."""

    def __init__(self, path, mode):
        self.path, self.mode = path, mode
        self.next_poll = 0.0

    def read(self):
        descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ServiceRefused("Proxy configuration must be a regular file.")
            raw = bytearray()
            while len(raw) <= 65536:
                part = os.read(descriptor, 65537 - len(raw))
                if not part:
                    break
                raw.extend(part)
        finally:
            os.close(descriptor)
        if len(raw) > 65536:
            raise ServiceRefused("Proxy configuration exceeds its 65536-byte limit.")
        try:
            document = tomllib.loads(raw.decode("utf-8"))
        except RecursionError:
            raise ServiceRefused("Proxy configuration is too deeply nested.") from None
        table = document.get("api_proxy")
        if type(table) is not dict:
            raise ServiceRefused("Proxy configuration requires an api_proxy table.")
        maximum = table.get("max_connections")
        level = table.get("log_level")
        authorities = table.get("allowed_authorities")
        if type(maximum) is not int or maximum <= 0:
            raise ServiceRefused("Proxy max_connections must be a positive integer.")
        if level not in ("INFO", "DEBUG", "WARNING"):
            raise ServiceRefused("Proxy log_level must be INFO, DEBUG or WARNING.")
        if type(authorities) is not list or any(type(value) is not str for value in authorities):
            raise ServiceRefused("Proxy allowed_authorities must be a list of authorities.")
        route = table.get("route", {})
        if type(route) is not dict:
            raise ServiceRefused("Proxy route settings must be a table.")
        addresses = route.get("addresses", [])
        if type(addresses) is not list or any(type(value) is not str for value in addresses):
            raise ServiceRefused("Proxy route addresses must be a list of mappings.")
        hosts = {authority_parts(value)[0] for value in authorities}
        selected = (addresses if self.mode == "external" else
                    tuple(address for address in addresses if address.partition("=")[0] in hosts))
        policy = (ServicePolicy.admit(self.mode, authorities, selected) if authorities
                  else ServicePolicy(frozenset(), ()))
        return LiveProxySettings(policy, maximum, level)

    def apply(self, server, settings):
        server.policy = settings.policy
        server._slots.set_limit(settings.max_connections)
        logging.getLogger("provider_core.proxy").setLevel(settings.log_level)

    def refresh(self, server):
        now = time.monotonic()
        if now < self.next_poll:
            return
        self.next_poll = now + 5
        try:
            settings = self.read()
        except ServiceRefused as error:
            logging.getLogger("provider_core.proxy").warning(
                "Cannot reload proxy settings: %s Retaining the current settings.", error
            )
            return
        except (OSError, UnicodeError, ValueError) as error:
            logging.getLogger("provider_core.proxy").warning(
                "Cannot reload proxy settings; retaining the current settings (%s).", type(error).__name__
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
            return b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"
        return super().admission_reply(error)

    def report_failure(self, phase, error):
        if isinstance(error, ServiceRefused):
            sys.stderr.write(str(error) + "\n")
        elif isinstance(error, bound_http_proxy.ProxyResolutionFailed):
            sys.stderr.write(str(error) + "\n")
            # Eight one-line native causes bound operator output; the DNS
            # owner retains all underlying evidence on the exception.
            for failure in error.failures[:8]:
                sys.stderr.write(f"Service proxy DNS cause: {native_failure_detail(failure)}.\n")
            if len(error.failures) > 8:
                sys.stderr.write(
                    f"Service proxy DNS: {len(error.failures) - 8} additional causes omitted from operator output.\n"
                )
        else:
            sys.stderr.write(f"Service proxy {phase} failed: {native_failure_detail(error)}. Request contents are not logged.\n")


class ServiceProxyServer(bound_http_proxy.ProxyServer):
    """Reuse bounded workers/relay on an exclusively created Unix listener."""

    address_family = socket.AF_UNIX
    request_handler = ServiceProxyHandler

    def __init__(self, path, interface, policy, *, status_directory, live_configuration=None):
        from provider_core.process_status import ProcessStatus

        self.policy = policy
        self.live_configuration = live_configuration
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

    def server_bind(self):
        # Birth must be private too: a crash before a later chmod would leave
        # a stale public socket that the restart owner must refuse to remove.
        previous = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(previous)


@contextmanager
def socket_grant(path, ready):
    """Hold exclusive directory ownership before replacing retired private leaves.

    Directory flock follows provider_workers_open's held-parent pattern at
    e1984c46f00190b1dc51a3774ea62a53f7608a4b, tools/provider_workers.sh. A live
    proxy blocks a successor without altering its listener/readiness; after exit
    the successor admits stale leaf type, owner and mode before removal. Serving
    and publication use the held directory, not a reopened replaceable ancestor.
    No host cleanup loop or extra lifetime registry participates in restart.
    """
    if (
        not path.is_absolute()
        or not ready.is_absolute()
        or path == ready
        or path.parent != ready.parent
        or "\0" in str(path) + str(ready)
    ):
        raise ServiceRefused(
            "Cannot start service proxy: socket and readiness require distinct absolute paths in one private directory."
        )
    descriptor = None
    try:
        try:
            if len(str(path).encode("utf-8")) > 107:
                raise ServiceRefused("Cannot start service proxy: the Unix socket path exceeds Linux's 107-byte pathname limit.")
            parent = path.parent.lstat()
            if (
                path.parent.resolve() != path.parent
                or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) != 0o700
            ):
                raise ServiceRefused(
                    "Cannot start service proxy: its directory must be caller-owned, private (0700), and free of symlinks."
                )
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            held = os.fstat(descriptor)
            if (held.st_dev, held.st_ino, held.st_uid, held.st_mode) != (parent.st_dev, parent.st_ino, parent.st_uid, parent.st_mode):
                raise ServiceRefused("Cannot start service proxy: its admitted directory changed before ownership was acquired.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ServiceRefused("Cannot start service proxy: another proxy owns this private route directory.") from error
            retired = []
            for leaf, required in ((path, stat.S_ISSOCK), (ready, stat.S_ISREG)):
                try:
                    observed = os.stat(leaf.name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not required(observed.st_mode) or observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o600:
                    raise ServiceRefused("Cannot start service proxy: prior route leaves have an unexpected type, owner or mode; preserve them for inspection.")
                if leaf == ready:
                    with os.fdopen(os.open(leaf.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor), "rb") as receipt:
                        if receipt.read(7) != b"ready\n":
                            raise ServiceRefused("Cannot start service proxy: prior readiness is not the owned complete receipt; preserve it for inspection.")
                retired.append(leaf.name)
            for name in retired:
                os.unlink(name, dir_fd=descriptor)
        except OSError as error:
            raise ServiceRefused(
                f"Cannot start service proxy: its private filesystem grant could not be admitted ({type(error).__name__}; errno={error.errno})."
            ) from error
        except UnicodeError as error:
            raise ServiceRefused("Cannot start service proxy: its Unix path must be valid UTF-8.") from error
        yield Path("/proc/self/fd") / str(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--mode", choices=("local", "mapped", "external"), required=True)
    parser.add_argument("--authority", action="append")
    parser.add_argument("--address", action="append")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--status-directory", type=Path, required=True)
    parser.add_argument("--log-level", choices=("INFO", "DEBUG", "WARNING"))
    parser.add_argument("--live-config", type=Path)
    args = parser.parse_args()
    if args.live_config is None and (not args.authority or not args.log_level):
        parser.error("private routes require --authority and --log-level")
    if args.live_config is not None and (args.authority is not None or args.log_level is not None):
        parser.error("live routes take authorities and log level from --live-config")
    if args.live_config is not None and args.address is not None:
        parser.error("live routes take address mappings from --live-config")
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
                raise ServiceRefused("Cannot configure service proxy: the live settings path must be absolute.")
            live = LiveProxyConfiguration(args.live_config, args.mode)
            try:
                settings = live.read()
            except ServiceRefused:
                raise
            except (UnicodeError, ValueError) as error:
                raise ServiceRefused(f"Cannot configure service proxy: invalid live settings ({type(error).__name__}).") from None
            policy = settings.policy
            _configure_proxy_logging(settings.log_level)
        try:
            bound_http_proxy.require_interface(args.interface)
        except ValueError as error:
            raise ServiceRefused(str(error)) from None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.interface.encode() + b"\0")
        with socket_grant(args.socket, args.ready_file) as parent:
            with ServiceProxyServer(parent / args.socket.name, args.interface, policy, status_directory=args.status_directory,
                                    live_configuration=live) as server:
                if live is not None:
                    live.apply(server, settings)
                bound_http_proxy.publish_readiness(parent / args.ready_file.name, "ready\n")
                logging.getLogger("provider_core.proxy").info("Service proxy ready.")
                server.serve_forever()
    except ServiceRefused as error:
        raise SystemExit(str(error)) from None
    except OSError as error:
        raise SystemExit(
            f"Cannot run service proxy: {type(error).__name__}; errno={error.errno}. Inspect the retained run before restarting."
        ) from None


if __name__ == "__main__":
    main()
