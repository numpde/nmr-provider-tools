"""Bounded data-only messages for one model HTTP child; never deserialize code."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import base64
import json
import reprlib
import socket
import ssl
import struct

import anyio
import httpcore

from provider_core.network import NetworkIOObservation
from provider_core.decoded_body import DecodedBodyRejected
from provider_core.response_json import read_document
from provider_core.http_response import HttpResponse, HttpResponseHead
from provider_core.transport_errors import ExchangeFailed
from provider_core.validation.upstream.chat_completions.response import is_completion_success
from provider_core.validation.upstream.http import require_http_status
from provider_core.type_requirements import require_type


# The data-only child protocol bounds metadata independently of decoded body limits.
# repo-check: allow-module-constant
METADATA_BYTES = 1024 * 1024

# Request and reply writers and readers share this fixed wire revision
# repo-check: allow-module-constant
_PROTOCOL_VERSION = 1

# Failure evidence remains a bounded graph, independent of endpoint retry policy.
# repo-check: allow-module-constant
_ERROR_NODES = 32

# Protected exception descriptions have a fixed protocol bound.
# repo-check: allow-module-constant
_ERROR_TEXT = 4096


class RemoteExchangeError(Exception):
    """Protected child evidence whose type grants no retry or cleanup authority."""


# Only these reviewed native types retain existing availability classification.
# repo-check: allow-module-constant
_ERROR_TYPES = {
    (kind.__module__ + "." + kind.__qualname__): kind for kind in (
        Exception,
        RuntimeError,
        ValueError,
        TypeError,
        TimeoutError,
        OSError,
        ConnectionError,
        ConnectionRefusedError,
        ConnectionResetError,
        ConnectionAbortedError,
        BrokenPipeError,
        FileNotFoundError,
        PermissionError,
        socket.gaierror,
        ssl.SSLError,
        ssl.SSLCertVerificationError,
        ssl.SSLEOFError,
        ssl.SSLZeroReturnError,
        ssl.SSLWantReadError,
        ssl.SSLWantWriteError,
        asyncio.CancelledError,
        httpcore.ConnectError,
        httpcore.ReadError,
        httpcore.WriteError,
        httpcore.ConnectTimeout,
        httpcore.ReadTimeout,
        httpcore.WriteTimeout,
        httpcore.PoolTimeout,
        httpcore.RemoteProtocolError,
        httpcore.LocalProtocolError,
        httpcore.UnsupportedProtocol,
        anyio.BrokenResourceError,
        anyio.EndOfStream,
    )
}


def encoded_bytes(content_bytes):
    return base64.b64encode(content_bytes).decode("ascii")


def decoded_bytes(encoded_content, max_decoded_bytes):
    require_type(encoded_content, str)

    max_encoded_characters = 4 * ((max_decoded_bytes + 2) // 3)

    if not (len(encoded_content) <= max_encoded_characters):
        raise ValueError(f"{len(encoded_content)=} should not exceed {max_encoded_characters=}")

    decoded_content = base64.b64decode(encoded_content, validate=True)

    if not (len(decoded_content) <= max_decoded_bytes):
        raise ValueError(f"{len(decoded_content)=} should not exceed {max_decoded_bytes=}")

    return decoded_content


def message_bytes(wire_message):
    encoded_message = json.dumps(wire_message, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    max_framed_bytes = 0xffffffff

    if not (len(encoded_message) <= max_framed_bytes):
        raise ValueError(f"{len(encoded_message)=} should not exceed {max_framed_bytes=}")

    return encoded_message


def framed(raw):
    return struct.pack("!I", len(raw)) + raw


def write_message(stream, wire_message):
    stream.write(framed(message_bytes(wire_message)))
    stream.flush()


def _size(header, max_message_bytes):
    if not (len(header) == (expected_header_bytes := struct.calcsize("!I"))):
        raise ValueError(f"{len(header)=} ≠ {expected_header_bytes=}")

    (message_byte_count,) = struct.unpack("!I", header)

    if not (0 < message_byte_count <= max_message_bytes):
        raise ValueError(f"{message_byte_count=} should be positive and not exceed {max_message_bytes=}")

    return message_byte_count


def read_request(stream, maximum_bytes):
    request_byte_count = _size(stream.read(4), maximum_bytes)
    request_bytes = stream.read(request_byte_count)

    if not (len(request_bytes) == request_byte_count):
        raise ValueError(f"{len(request_bytes)=} ≠ {request_byte_count=}")

    # Probe EOF only after the complete body; a short body must not trigger another read
    if not (not (trailing_byte := stream.read(1))):
        raise ValueError(f"{trailing_byte=} should be empty after the request")

    request_message = read_document(request_bytes, maximum_bytes)

    require_type(request_message, dict)

    require_type(request_message.get("version"), int)

    if not (request_message["version"] == _PROTOCOL_VERSION):
        raise ValueError(f"{request_message['version']=} ≠ {_PROTOCOL_VERSION=}")

    if not (request_message.get("kind") == (expected_request_kind := "request")):
        raise ValueError(f"{reprlib.repr(request_message.get('kind'))=} ≠ {expected_request_kind=}")

    return request_message


async def read_message(reader, maximum_bytes):
    size = _size(await reader.readexactly(4), maximum_bytes)

    return read_document(await reader.readexactly(size), maximum_bytes)


def _headers(headers):
    return [[encoded_bytes(header_name), encoded_bytes(header_value)] for (header_name, header_value) in headers]


def _read_headers(headers):
    require_type(headers, list)

    if not (len(headers) <= (max_header_count := 65536)):
        raise ValueError(f"{len(headers)=} should not exceed {max_header_count=}")

    decoded_headers = []
    remaining_metadata_bytes = METADATA_BYTES

    for pair in headers:
        require_type(pair, list)

        if not (len(pair) == (expected_header_parts := 2)):
            raise ValueError(f"{len(pair)=} ≠ {expected_header_parts=}")

        (header_name, header_value) = (decoded_bytes(part, remaining_metadata_bytes) for part in pair)
        remaining_metadata_bytes -= len(header_name) + len(header_value)

        if not (0 <= remaining_metadata_bytes):
            raise ValueError(f"{remaining_metadata_bytes=} should be nonnegative")

        decoded_headers.append((header_name, header_value))

    return tuple(decoded_headers)


def response_message(response):
    return {
        "version": _PROTOCOL_VERSION,
        "kind": "response",
        "status": response.status,
        "headers": _headers(response.headers),
        "body": encoded_bytes(response.body)
    }


def failure_message(failure):
    """Keep typed cause/reference relationships inside a bounded inert graph."""
    (nodes, seen) = ([], {})

    def reference(error):
        if error is None:
            return None

        identity = id(error)

        if identity in seen:
            return seen[identity]

        if not (len(nodes) < _ERROR_NODES):
            raise ValueError(f"{len(nodes)=} should be below {_ERROR_NODES=} before adding another failure")

        index = seen[identity] = len(nodes)
        kind = type(error).__module__ + "." + type(error).__qualname__

        node = {
            "type": kind,
            "text": str(error)[:_ERROR_TEXT],
                # silent-defaults: allow-contract -- Only a real integer OSError errno is portable native evidence; None records its absence.
            "errno": error.errno if isinstance(error, OSError) and (type(error.errno) is int) else None
        }

        nodes.append(node)

        if type(error) is DecodedBodyRejected:
            node["reason"] = error.reason

        (node["cause"], node["context"]) = (reference(error.__cause__), reference(error.__context__))
        node["suppress_context"] = error.__suppress_context__

        if isinstance(error, BaseExceptionGroup):
            node["children"] = [reference(child) for child in error.exceptions]

        return index

    primary = reference(failure.primary)
    cleanup = [reference(error) for error in failure.cleanup_failures]
    (interruption, network_failure) = (reference(failure.interruption), reference(failure.network_failure))
    network = failure.network_io

    if network is not None:
        network = {
            "stage": network.stage,
            "error": reference(network.error),
            "cause": reference(network.cause),
            "context": reference(network.context),
            "unix_path": network.unix_path,
            "configured_unix_path": network.configured_unix_path,
            "eof": network.eof,
            "deadline_cancelled": network.deadline_cancelled
        }

    head = failure.response_head

    if head is not None:
        head = {"status": head.status, "headers": _headers(head.headers)}

    return {
        "version": _PROTOCOL_VERSION,
        "kind": "failure",
        "message": str(failure)[:_ERROR_TEXT],
        "nodes": nodes,
        "primary": primary,
        "cleanup": cleanup,
        "interruption": interruption,
        "head": head,
        "proxy_status": failure.proxy_connect_status,
        "network_failure": network_failure,
        "network": network,
        "caller_interruption": reference(failure.caller_interruption)
    }


def _read_failure(failure_reply):
    nodes = failure_reply["nodes"]

    require_type(nodes, list)

    if not (len(nodes) <= _ERROR_NODES):
        raise ValueError(f"{len(nodes)=} should not exceed {_ERROR_NODES=}")

    errors = [None] * len(nodes)
    constructing_error_indices = set()

    def error(node_index):
        if node_index is None:
            return None

        require_type(node_index, int)

        if not (0 <= node_index < len(nodes)):
            raise ValueError(f"{node_index=} should be nonnegative and below {len(nodes)=}")

        if errors[node_index] is not None:
            return errors[node_index]

        if not (node_index not in constructing_error_indices):
            raise ValueError(f"{node_index=} is already in {constructing_error_indices=}")

        constructing_error_indices.add(node_index)
        node = nodes[node_index]
        (remote_error_type, remote_error_text) = (node["type"], node["text"])

        require_type(remote_error_type, str)

        require_type(remote_error_text, str)

        if not (len(remote_error_text) <= _ERROR_TEXT):
            raise ValueError(f"{len(remote_error_text)=} should not exceed {_ERROR_TEXT=}")

        if remote_error_type == "provider_core.decoded_body.DecodedBodyRejected":
            reconstructed_error = DecodedBodyRejected(node["reason"])
        elif remote_error_type in ("builtins.ExceptionGroup", "builtins.BaseExceptionGroup"):
            children = node["children"]

            require_type(children, list)

            if not (0 < len(children) <= _ERROR_NODES):
                raise ValueError(f"{len(children)=} should be positive and not exceed {_ERROR_NODES=}")

            reconstructed_error = BaseExceptionGroup(remote_error_text, [error(child) for child in children])
        else:
            # silent-defaults: allow-contract -- Unreviewed child error types retain protected evidence without acquiring retry authority.
            constructor = _ERROR_TYPES.get(remote_error_type, RemoteExchangeError)

            remote_errno = node["errno"]

            if remote_errno is not None:
                require_type(remote_errno, int)

                # repo-check: retain-type-guard -- Errno reconstruction accepts only OS errors or the wire's dedicated remote-error type
                if (not (
                    issubclass(constructor, OSError) or (constructor is RemoteExchangeError))):
                    raise ValueError(f"{constructor=} cannot carry {remote_errno=}")

            reconstructed_error = constructor(remote_errno, remote_error_text) if (remote_errno is not None) and (issubclass(constructor, OSError)) else constructor(remote_error_text)

            if constructor is RemoteExchangeError:
                reconstructed_error.remote_type = remote_error_type
                reconstructed_error.remote_errno = remote_errno

        errors[node_index] = reconstructed_error

        constructing_error_indices.remove(node_index)

        return reconstructed_error

    for index in range(len(nodes)):
        error(index)

    for (current, node) in zip(errors, nodes):
        (current.__cause__, current.__context__) = (error(node["cause"]), error(node["context"]))

        require_type((node_suppress_context := node["suppress_context"]), bool)

        current.__suppress_context__ = node["suppress_context"]

    head = failure_reply["head"]

    if head is not None:
        head = HttpResponseHead(require_http_status(head["status"]), _read_headers(head["headers"]))

    network = failure_reply["network"]

    if network is not None:
        for name in ("stage", "unix_path", "configured_unix_path"):
            if network[name] is not None:
                require_type((network_item := network[name]), str)

        require_type((network_eof := network["eof"]), bool)

        require_type((network_deadline_cancelled := network["deadline_cancelled"]), bool)

        network = NetworkIOObservation(
            stage=network["stage"],
            error=error(network["error"]),
            cause=error(network["cause"]),
            context=error(network["context"]),
            unix_path=network["unix_path"],
            configured_unix_path=network["configured_unix_path"],
            eof=network["eof"],
            deadline_cancelled=network["deadline_cancelled"],
        )

    proxy_status = failure_reply["proxy_status"]

    if proxy_status is not None:
        require_http_status(proxy_status)

    message = failure_reply["message"]

    require_type(message, str)

    if not (len(message) <= _ERROR_TEXT):
        raise ValueError(f"{len(message)=} should not exceed {_ERROR_TEXT=}")

    cleanup_indices = failure_reply["cleanup"]

    require_type(cleanup_indices, list)

    if not (len(cleanup_indices) <= _ERROR_NODES):
        raise ValueError(f"{len(cleanup_indices)=} should not exceed {_ERROR_NODES=}")

    if not all((node_index is not None) for node_index in cleanup_indices):
        raise ValueError(f"{reprlib.repr(cleanup_indices)=} should not contain None")

    return ExchangeFailed(
        message,
        primary=error(failure_reply["primary"]),
        cleanup_failures=[error(index) for index in cleanup_indices],
        interruption=error(failure_reply["interruption"]),
        response_head=head,
        proxy_connect_status=proxy_status,
        network_failure=error(failure_reply["network_failure"]),
        network_io=network,
        caller_interruption=error(failure_reply["caller_interruption"]),
    )


def read_reply(reply_message, *, maximum_bytes, error_maximum_bytes):
    require_type(reply_message, dict)

    require_type(reply_message.get("version"), int)

    if not (reply_message["version"] == _PROTOCOL_VERSION):
        raise ValueError(f"{reply_message['version']=} ≠ {_PROTOCOL_VERSION=}")

    if reply_message.get("kind") == "failure":
        return _read_failure(reply_message)

    if not (reply_message.get("kind") == "response"):
        expected_reply_kinds = ("response", "failure")

        raise ValueError(f"{reprlib.repr(reply_message.get('kind'))=} ∉ {expected_reply_kinds=}")

    status = require_http_status(reply_message["status"])
    body = decoded_bytes(reply_message["body"], maximum_bytes if is_completion_success(status) else error_maximum_bytes)

    return HttpResponse(status, _read_headers(reply_message["headers"]), body)
