"""Route acquisition HTTP/CONNECT traffic through one explicit interface.

DNS and TCP sockets bind the selected device without an unbound resolver
fallback. A private Unix listener can serve a loopback relay in a networkless
worker namespace. The acquisition launcher owns that isolation and lifetime.
"""

# Adapted from the SECS interface-bound proxy so acquisition uses the same
# SO_BINDTODEVICE transport; this repository runs the proxy inside a container.
# https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tools/bound_http_proxy.py

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import argparse
import ipaddress
import logging
import os
import re
import reprlib
import secrets
import select
import socket
import socketserver
import sys
import tempfile
import threading
import time
import pathlib
from http import HTTPStatus

from provider_core.adapters import dns as dns_adapter, http_proxy
from provider_core.diagnostic_text import CredentialRedactingFormatter
from provider_core.type_requirements import require_type
from provider_core.validation.upstream import dns
from provider_core.validation.upstream.host_resolver import nameserver_tokens
from provider_core.validation.upstream.http_proxy import header_end

# Header admission owns this fixed byte ceiling independently of coalesced body
# bytes; changing it would alter the bounded acquisition protocol.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
MAX_HEADER_BYTES = 64 * 1024

# Relay owns one bounded forwarding chunk rather than an accumulated body.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
BUFFER_BYTES = 128 * 1024

# Header reads share one absolute admission budget across all fragments.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
HEADER_SECONDS = 30

# DNS and destination addresses share the connection owner's finite budget.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
CONNECT_SECONDS = 30

# Relay renews this inactivity budget only after a completed bounded write.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
RELAY_IDLE_SECONDS = 120

# The listener reserves this fixed worker capacity before allocating threads.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
MAX_CONNECTIONS = 32


class ProxyResolutionFailed(OSError):
    """Safe owned DNS reason; native query failures remain private evidence."""

    # silent-defaults: allow-contract -- Missing resolver configuration has no native query failures; failed queries supply their original exceptions.
    def __init__(self, message, failures=()):
        super().__init__(message)
        self.failures = tuple(failures)


class ProxyResolutionDeadline(ProxyResolutionFailed, TimeoutError):
    """DNS aggregate expiry retains earlier causes and the timeout contract."""


def _remaining(deadline: float, operation: str) -> float:
    """Return seconds left in one absolute budget, rejecting completed deadlines."""
    remaining = deadline - time.monotonic()

    if remaining <= 0:
        raise TimeoutError(f"Cannot complete proxy {operation = }: its deadline elapsed.")

    return remaining


def receive_headers(client: socket.socket) -> tuple[bytes, bytes]:
    """Return the complete header including its terminator and any received body.

    One absolute 30-second budget covers every fragment. The byte limit covers
    only the header. Body bytes received with its final fragment remain
    available to the forwarding caller. EOF before the header
    completes raises ConnectionError; an oversized header raises ValueError.
    """
    deadline = time.monotonic() + HEADER_SECONDS
    request_buffer = bytearray()

    while True:
        # A fragment arriving after the deadline cannot complete admission.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L54-L56
        client.settimeout(_remaining(deadline, "header read"))

        chunk = client.recv(8192)

        _remaining(deadline, "header read")

        if not (chunk):
            raise ConnectionError(f"{len(chunk)=} should be positive before request headers are complete")

        request_buffer.extend(chunk)

        header_end_offset = header_end(request_buffer)

        # Coalesced body bytes belong to forwarding, not header admission.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L88
        header_byte_count = header_end_offset if header_end_offset is not None else len(request_buffer)

        if not (header_byte_count <= MAX_HEADER_BYTES):
            raise ValueError(f"{header_byte_count=} should be at most {MAX_HEADER_BYTES=}")

        if header_end_offset is not None:
            return (bytes(request_buffer[:header_byte_count]), bytes(request_buffer[header_byte_count:]))


def _dns_servers(resolver_file: pathlib.Path | None = None) -> tuple[str, ...]:
    """Read explicit captured resolvers, or existing service resolver configuration."""

    if resolver_file is not None:
        resolver_paths = (resolver_file,)
    else:
        resolver_paths = (pathlib.Path("/run/systemd/resolve/resolv.conf"), pathlib.Path("/etc/resolv.conf"))

    for resolver_path in resolver_paths:
        try:
            resolver_text = resolver_path.read_text(encoding="ascii")
        except OSError:
            continue

        servers = []

        for resolver_token in nameserver_tokens(resolver_text):
            try:
                address = ipaddress.ip_address(resolver_token)
            except ValueError:
                continue

            if (address.version == 4) and (not address.is_loopback):
                servers.append(str(address))

        if servers:
            return tuple(dict.fromkeys(servers))

    raise ProxyResolutionFailed(f"No non-loopback IPv4 DNS server found in {reprlib.repr(resolver_paths)=}")


def _query_ipv4(
    host: str,
    server: str,
    interface: str,
    *,
    deadline: float,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Query IPv4 DNS on the chosen device within the shared connection budget."""

    query_deadline = min(deadline, time.monotonic() + 3)

    _remaining(query_deadline, "DNS query")
    transaction = secrets.randbits(16)
    dns_query = dns_adapter.ipv4_query(host, transaction)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as resolver:
        if interface != "system":
            resolver.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")

        resolver.settimeout(_remaining(query_deadline, "DNS query"))
        resolver.connect((server, dns_adapter.PORT))
        resolver.settimeout(_remaining(query_deadline, "DNS query"))
        resolver.send(dns_query)
        resolver.settimeout(_remaining(query_deadline, "DNS query"))
        response = resolver.recv(65535)

        # Native timeouts bound the wait, but a late successful return must
        # also fail admission. This closes the inherited upstream deadline gap.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L183-L184
        _remaining(query_deadline, "DNS query")

    # Sixteen pointer hops is the existing local decoder work cap, not a DNS rule.
    return dns.read_ipv4_response(response, hostname=host, transaction=transaction, max_pointer_depth=16)


def resolve_ipv4_bound(
    host: str,
    interface: str,
    *,
    deadline: float,
    dns_servers: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    """Follow bounded IPv4 aliases without using an unbound system resolver."""

    try:
        return (str(ipaddress.IPv4Address(host)),)
    # silent-defaults: allow-contract -- A destination that is not a literal IPv4 address proceeds to interface-bound DNS; proof=repository_checks/test_proxy_posture.py::ImageBuildProxyTransportTests.test_expired_dns_budget_does_not_start_the_next_server
    except ipaddress.AddressValueError:
        pass

    normalized_host = dns.normalize_dns_name(host)
    errors = []
    failures = []

    for server in _dns_servers() if dns_servers is None else dns_servers:
        try:
            _remaining(deadline, "DNS resolution")
        except TimeoutError as expired:
            raise ProxyResolutionDeadline(
                "Cannot resolve proxy destination: the shared DNS deadline elapsed.",
                (*failures, expired)
            ) from expired

        try:
            current_dns_name = normalized_host
            visited = set()

            for _ in range(16):
                if current_dns_name in visited:
                    raise dns.DNSResponseRejected(f"{reprlib.repr(current_dns_name)=} should not already be in {reprlib.repr(visited)=}")

                visited.add(current_dns_name)
                (addresses, aliases) = _query_ipv4(current_dns_name, server, interface, deadline=deadline)

                if current_dns_name in addresses:
                    return tuple(addresses[current_dns_name])

                if current_dns_name not in aliases:
                    break

                current_dns_name = aliases[current_dns_name]

            errors.append(f"{server = }: response contains no IPv4 address for {host = }")
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{server=}: {error=!s}")
            failures.append(error)

    # silent-defaults: allow-contract -- Distinguish failed DNS queries from completed replies containing no IPv4 address; neither outcome selects another route.
    reason = "the configured DNS queries failed" if failures else "the configured resolvers did not return an IPv4 address"

    failure = ProxyResolutionFailed(f"Cannot resolve proxy destination on the selected interface: {reason = }.", failures)

    # Preserve source-bearing evidence privately; service logs use only the
    # owned reason above and bounded native exception classes/errno.
    failure.add_note("; ".join(errors))

    raise failure


def connect_bound(host: str, port: int, interface: str, *, dns_servers: tuple[str, ...] | None = None) -> socket.socket:
    """Return one device-bound connection within a shared DNS/address budget."""

    deadline = time.monotonic() + CONNECT_SECONDS
    last_error: OSError | None = None

    for address in resolve_ipv4_bound(host, interface, deadline=deadline, dns_servers=dns_servers):
        _remaining(deadline, "connection")
        outbound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            if interface != "system":
                outbound.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")

            outbound.settimeout(_remaining(deadline, "connection"))
            outbound.connect((address, port))

            # A late successful connect still owns an open socket; reject it
            # inside this try so the existing failure path closes the socket.
            # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L267-L272
            _remaining(deadline, "connection")

            return outbound
        except OSError as error:
            last_error = error

            outbound.close()

    raise last_error or OSError("Cannot connect proxy destination: DNS returned no IPv4 address.")


def log_failure(operation, error):
    """Pass the original failure to the process's credential-scrubbing formatter."""
    logging.getLogger("provider_core.proxy").error("%s failed", operation, exc_info=error)


def relay(left: socket.socket, right: socket.socket) -> None:
    """Preserve TCP half-closes within the 120-second forwarding inactivity budget."""

    readers = [left, right]
    deadline = time.monotonic() + RELAY_IDLE_SECONDS

    while readers:
        (readable, _, _) = select.select(readers, (), (), _remaining(deadline, "relay"))

        if not (readable):
            raise TimeoutError(f"{len(readable)=} should be positive within {RELAY_IDLE_SECONDS=}")

        for source in readable:
            if source is left:
                destination = right
                direction = "client-to-upstream"
            else:
                destination = left
                direction = "upstream-to-client"

            operation = "read bytes"

            try:
                source.settimeout(_remaining(deadline, "relay read"))
                relay_chunk = source.recv(BUFFER_BYTES)

                if not relay_chunk:
                    # EOF closes only this direction; the reverse direction
                    # can still carry a response to a completed client upload.
                    readers.remove(source)

                    operation = "half-close the destination after source EOF"

                    destination.shutdown(socket.SHUT_WR)
                    continue

                operation = f"write {len(relay_chunk) = }"

                # select does not guarantee that a later write cannot stall.
                destination.settimeout(_remaining(deadline, "relay write"))

                destination.sendall(relay_chunk)
            except OSError as error:
                # Keep the native exception and its traceback. The relay knows
                # direction and operation, but cannot infer HTTP acceptance
                # from encrypted bytes or claim a failed write delivered none.
                error.add_note(f"Proxy relay {direction = }: could not {operation = }; delivery may be partial.")

                raise

            # Only a write completed within the budget can renew inactivity.
            # Upstream resets immediately after sendall, accepting late success.
            # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L296-L298
            _remaining(deadline, "relay write")

            deadline = time.monotonic() + RELAY_IDLE_SECONDS


class ProxyHandler(socketserver.BaseRequestHandler):
    def destination(self, header):
        """Acquisition accepts CONNECT and absolute HTTP destinations."""

        return http_proxy.parse_destination(header)

    def connect(self, host, port):
        """Acquire the admitted destination through this listener's device."""

        return connect_bound(host, port, self.server.outbound_interface, dns_servers=self.server.dns_servers)

    def admission_reply(self, _error):
        """Keep the acquisition listener's existing fixed failure response."""

        return http_proxy.closing_response(HTTPStatus.BAD_GATEWAY)

    def report_failure(self, phase, error):
        """Report the original cause; the log formatter scrubs credentials."""
        log_failure(f"Proxy {phase = }", error)

        if isinstance(error, ProxyResolutionFailed):
            for failure in error.failures:
                log_failure("Proxy DNS query", failure)

    def handle(self) -> None:
        """Forward one request without dumping its headers or payload.

        A fixed HTTP error is emitted only before forwarding is attempted. Once
        a write starts, its completion may be uncertain and a second HTTP status
        would corrupt the forwarded response or CONNECT tunnel.

        Forwarding gives both peer sockets a fresh bounded write timeout.
        """
        phase = "request admission"

        try:
            (header, remainder) = receive_headers(self.request)
            (method, host, port, forwarded_header) = self.destination(header)

            with self.connect(host, port) as outbound:
                # sendall may fail after writing a prefix; mark the phase before
                # the first HTTP header or CONNECT-success write is attempted.
                # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L296
                phase = "forwarding"

                # Header admission leaves its remaining timeout on this client
                # socket; reset it so forwarding gets a fresh write budget.
                # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L312-L315
                self.request.settimeout(RELAY_IDLE_SECONDS)

                outbound.settimeout(RELAY_IDLE_SECONDS)

                if method == "CONNECT":
                    phase = "forwarding CONNECT acknowledgement to client"

                    self.request.sendall(http_proxy.CONNECT_ESTABLISHED_RESPONSE)
                else:
                    phase = "forwarding request headers to upstream"

                    outbound.sendall(forwarded_header)

                if remainder:
                    phase = "forwarding initial bytes to upstream"

                    outbound.sendall(remainder)

                phase = "forwarding tunnel bytes"

                relay(self.request, outbound)
        except (ConnectionError, OSError, UnicodeError, ValueError) as error:
            if phase == "request admission":
                try:
                    self.request.settimeout(1)
                    self.request.sendall(self.admission_reply(error))
                except OSError as reply_error:
                    log_failure("Proxy admission failure reply", reply_error)

            self.report_failure(phase, error)


class ConnectionAdmission:
    """Bound active connections while allowing the owner to change the limit."""

    def __init__(self, max_connections):
        self._lock = threading.Lock()
        self._active_connection_count = 0

        self.set_limit(max_connections)

    def set_limit(self, max_connections):
        require_type(max_connections, int)

        if not (max_connections > 0):
            raise ValueError(f"{max_connections=} should be positive")

        with self._lock:
            self._connection_limit = max_connections

    def acquire(self):
        with self._lock:
            if self._active_connection_count >= self._connection_limit:
                return False

            self._active_connection_count += 1

            return True

    def load(self):
        with self._lock:
            return (self._active_connection_count, self._connection_limit)

    def release(self):
        with self._lock:
            if self._active_connection_count == 0:
                raise RuntimeError(f"{self._active_connection_count=} should be positive")

            self._active_connection_count -= 1


class ProxyServer(socketserver.ThreadingTCPServer):
    """Reserve finite worker capacity before allocating a request thread."""

    allow_reuse_address = True
    daemon_threads = True
    request_handler = ProxyHandler

    def __init__(self, address: tuple[str, int], interface: str, *, dns_servers=None):
        """Own the selected device and the bounded worker slots for this listener."""
        self.outbound_interface = interface
        self.dns_servers = dns_servers
        self._slots = ConnectionAdmission(MAX_CONNECTIONS)

        super().__init__(address, self.request_handler)

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        """Close overload immediately; transfer each reserved slot to its worker."""

        # Admission precedes ThreadingMixIn's allocation. A failed thread start
        # keeps ownership here, while a started worker releases in its finally.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L343-L365
        if not self._slots.acquire():
            self.shutdown_request(request)

            return

        self.start_reserved_request(request, client_address)

    def start_reserved_request(self, request, client_address):
        """Transfer an already reserved slot to a worker, or release it on failure."""

        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        """Release the worker's slot after success, request failure or cleanup."""

        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, _request: socket.socket, _client_address: tuple[str, int]) -> None:
        """Keep the worker failure without socketserver's client-address dump."""
        log_failure("Proxy worker", sys.exception())


class UnixProxyServer(ProxyServer):
    """Serve the same destination proxy through a caller-owned private socket."""

    address_family = socket.AF_UNIX

    def __init__(self, address: pathlib.Path, interface: str, *, dns_servers=None):
        super().__init__(str(address), interface, dns_servers=dns_servers)

    def server_bind(self):
        # Never unlink an existing path. The launcher owns the private parent
        # and removes it only after every container borrower has retired.
        super().server_bind()

        pathlib.Path(self.server_address).chmod(0o600)


class UnixRelayHandler(socketserver.BaseRequestHandler):
    """Forward bytes to the Unix gateway, with no IP destination fallback."""

    def handle(self):
        phase = "gateway connection"

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as gateway:
                gateway.settimeout(CONNECT_SECONDS)
                gateway.connect(self.server.gateway_socket)
                phase = "forwarding"

                relay(self.request, gateway)
        except OSError as error:
            if phase == "gateway connection":
                try:
                    self.request.settimeout(1)

                    self.request.sendall(http_proxy.closing_response(HTTPStatus.BAD_GATEWAY))
                except OSError as reply_error:
                    log_failure("Proxy relay admission failure reply", reply_error)

            log_failure(f"Proxy relay {phase = }", error)


class UnixRelayServer(ProxyServer):
    """Bound loopback TCP workers and carry their proxy traffic over Unix."""

    request_handler = UnixRelayHandler

    def __init__(self, address: tuple[str, int], gateway_socket: pathlib.Path):
        self.gateway_socket = str(gateway_socket)

        super().__init__(address, "system")


def require_interface_name(interface: str) -> None:
    """Admit the Linux interface grammar without claiming a host device exists."""

    # Native lookup admits the same device name consumed by SO_BINDTODEVICE.
    # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L373-L381
    interface_pattern = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}"

    if not re.fullmatch(interface_pattern, interface):
        raise ValueError(f"{reprlib.repr(interface)=} should match {interface_pattern=}")


def require_interface(interface: str) -> None:
    """Admit one existing native Linux interface without filesystem lookup."""
    require_interface_name(interface)

    try:
        socket.if_nametoindex(interface)
    except OSError:
        raise ValueError(f"Cannot find {reprlib.repr(interface)=}") from None


def publish_readiness(ready_file, content):
    """Publish complete private readiness without replacing any prior receipt."""

    try:
        (descriptor, temporary) = tempfile.mkstemp(prefix=".proxy-ready.", dir=ready_file.parent)
    except OSError:
        raise SystemExit("Cannot start proxy: a private readiness staging file could not be created.") from None

    published = False

    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as ready:
            ready.write(content)

        # mkstemp creates private bytes. Linking after close publishes
        # only complete content and cannot overwrite a file or symlink.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L398-L410
        os.link(temporary, ready_file, follow_symlinks=False)

        published = True
    except OSError as error:
        error_type = type(error).__name__

        raise SystemExit(f"Cannot start proxy: readiness publication failed ({error_type = }).") from None
    finally:
        try:
            pathlib.Path(temporary).unlink()
        except OSError:
            # Upstream can report creation failure after link succeeded.
            # The complete receipt remains usable; only private staging-alias
            # cleanup is unconfirmed. Never roll back by a replaceable path.
            # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L406-L410
            # silent-defaults: allow-contract -- Report the observed publication outcome independently of failed temporary-alias cleanup; proof=repository_checks/test_proxy_posture.py::ImageBuildProxyWorkerTests.test_readiness_publication_failure_survives_temporary_cleanup_failure
            outcome = (
                "Readiness was published; serving will continue." if published else "Readiness publication did not complete."
            )

            sys.stderr.write(f"{outcome = } Cannot confirm temporary readiness cleanup at {temporary = }.\n")


def main() -> None:
    """Publish exclusive private readiness after listener and device admission.

    TCP listeners explicitly supply port zero for kernel selection. A private
    Unix gateway publishes its socket path; a loopback relay publishes its port.
    The launcher grants the worker only the relay's networkless namespace.
    """
    handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(CredentialRedactingFormatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    parser = argparse.ArgumentParser()
    upstream = parser.add_mutually_exclusive_group(required=True)

    upstream.add_argument("--interface")
    upstream.add_argument("--relay-socket", type=pathlib.Path, help="Private Unix gateway for a loopback-only relay.")
    listener = parser.add_mutually_exclusive_group(required=True)

    listener.add_argument("--port", type=int, help="Listener port; 0 asks the kernel to choose a free port.")
    listener.add_argument("--unix-socket", type=pathlib.Path, help="Private Unix listener; an existing path is never replaced.")
    parser.add_argument("--ready-file", type=pathlib.Path, required=True)

    parser.add_argument(
        "--resolver-file",
        type=pathlib.Path,
        help="Captured DNS configuration; no fallback to host or container resolvers."
    )

    args = parser.parse_args()

    if args.relay_socket is not None:
        if args.unix_socket is not None:
            parser.error("--relay-socket requires a TCP --port listener")

        with UnixRelayServer(("127.0.0.1", args.port), args.relay_socket) as server:
            publish_readiness(
                args.ready_file,
                f"{server.server_address[1]}\n",  # machine-string: readiness consumers parse the port as a numeric line; debug labels would change the protocol.
            )

            server.serve_forever()

        return

    try:
        if args.interface != "system":
            require_interface(args.interface)

        dns_servers = None

        if args.resolver_file is not None:
            dns_servers = _dns_servers(args.resolver_file)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None

    # Prove interface binding before publishing readiness, without any transfer.
    if args.interface != "system":
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.interface.encode() + b"\0")

    if args.unix_socket is not None:
        with UnixProxyServer(args.unix_socket, args.interface, dns_servers=dns_servers) as server:
            publish_readiness(
                args.ready_file,
                f"{server.server_address}\n",  # machine-string: readiness consumers parse the socket address line; debug labels would change the protocol.
            )

            server.serve_forever()
    else:
        with ProxyServer(("127.0.0.1", args.port), args.interface, dns_servers=dns_servers) as server:
            publish_readiness(
                args.ready_file,
                f"{server.server_address[1]}\n",  # machine-string: readiness consumers parse the port as a numeric line; debug labels would change the protocol.
            )

            server.serve_forever()


if __name__ == "__main__":
    main()
