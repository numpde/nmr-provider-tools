"""Bounded data-only messages for one model HTTP child; never deserialize code."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import base64
import json
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


def encoded_bytes(value):
    return base64.b64encode(value).decode("ascii")


def decoded_bytes(value, maximum):
    require_type(value, str)

    if not (len(value) <= (4 * ((maximum + 2) // 3))):
        raise ValueError("Interpreter child byte field exceeds its admitted limit")

    decoded_content = base64.b64decode(value, validate=True)

    if not (len(decoded_content) <= maximum):
        raise ValueError("Interpreter child byte field exceeds its admitted limit")

    return decoded_content


def message_bytes(document):
    raw = json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")

    if not (len(raw) <= 0xffffffff):
        raise ValueError("Interpreter child message exceeds its framing limit")

    return raw


def framed(raw):
    return struct.pack("!I", len(raw)) + raw


def write_message(stream, document):
    stream.write(framed(message_bytes(document)))
    stream.flush()


def _size(header, maximum):
    if len(header) != 4:
        raise ValueError("Interpreter child message ended before its length")

    (size,) = struct.unpack("!I", header)

    if not (0 < size <= maximum):
        raise ValueError("Interpreter child message exceeds its admitted limit")

    return size


def read_request(stream, maximum_bytes):
    size = _size(stream.read(4), maximum_bytes)
    raw = stream.read(size)

    if (len(raw) != size) or stream.read(1):
        raise ValueError("Interpreter child requires one complete request message")

    request_message = read_document(raw, maximum_bytes)

    require_type(request_message, dict)

    require_type((document_version := request_message.get("version")), int)

    if (request_message["version"] != 1) or (request_message.get("kind") != "request"):
        raise ValueError("Interpreter child request has an unknown schema")

    return request_message


async def read_message(reader, maximum_bytes):
    size = _size(await reader.readexactly(4), maximum_bytes)

    return read_document(await reader.readexactly(size), maximum_bytes)


def _headers(headers):
    return [[encoded_bytes(header_name), encoded_bytes(header_value)] for (header_name, header_value) in headers]


def _read_headers(headers):
    require_type(headers, list)

    if not (len(headers) <= 65536):
        raise ValueError("Interpreter child headers exceed their admitted limit")

    decoded_headers = []
    remaining = METADATA_BYTES

    for pair in headers:
        require_type(pair, list)

        if len(pair) != 2:
            raise ValueError("Interpreter child header is malformed")

        (header_name, header_value) = (decoded_bytes(part, remaining) for part in pair)
        remaining -= len(header_name) + len(header_value)

        if not (0 <= remaining):
            raise ValueError("Interpreter child headers exceed their admitted limit")

        decoded_headers.append((header_name, header_value))

    return tuple(decoded_headers)


def response_message(response):
    return {
        "version": 1,
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
            raise ValueError("Interpreter child failure evidence exceeds its graph limit")

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
        "version": 1,
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
        raise ValueError("Interpreter child failure evidence exceeds its graph limit")

    errors = [None] * len(nodes)
    constructing = set()

    def error(index):
        if index is None:
            return None

        require_type(index, int)

        if not (0 <= index < len(nodes)):
            raise ValueError("Interpreter child failure reference is malformed")

        if errors[index] is not None:
            return errors[index]

        if index in constructing:
            raise ValueError("Interpreter child exception group is cyclic")

        constructing.add(index)
        node = nodes[index]
        (kind, text) = (node["type"], node["text"])

        require_type(kind, str)

        require_type(text, str)

        if not (len(text) <= _ERROR_TEXT):
            raise ValueError("Interpreter child failure description is malformed")

        if kind == "provider_core.decoded_body.DecodedBodyRejected":
            reconstructed_error = DecodedBodyRejected(node["reason"])
        elif kind in ("builtins.ExceptionGroup", "builtins.BaseExceptionGroup"):
            children = node["children"]

            require_type(children, list)

            if not (0 < len(children) <= _ERROR_NODES):
                raise ValueError("Interpreter child exception group is malformed")

            reconstructed_error = BaseExceptionGroup(text, [error(child) for child in children])
        else:
            # silent-defaults: allow-contract -- Unreviewed child error types retain protected evidence without acquiring retry authority.
            constructor = _ERROR_TYPES.get(kind, RemoteExchangeError)

            number = node["errno"]

            if number is not None:
                require_type(number, int)

                # repo-check: retain-type-guard -- Errno reconstruction accepts only OS errors or the wire's dedicated remote-error type
                if (not (
                    issubclass(constructor, OSError) or (constructor is RemoteExchangeError))):
                    raise ValueError("Interpreter child error number is malformed")

            reconstructed_error = constructor(number, text) if (number is not None) and (issubclass(constructor, OSError)) else constructor(text)

            if constructor is RemoteExchangeError:
                reconstructed_error.remote_type = kind
                reconstructed_error.remote_errno = number

        errors[index] = reconstructed_error

        constructing.remove(index)

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
        raise ValueError("Interpreter child failure message is malformed")

    cleanup = failure_reply["cleanup"]

    require_type(cleanup, list)

    if (len(cleanup) > _ERROR_NODES) or any((index is None) for index in cleanup):
        raise ValueError("Interpreter child cleanup obligations are malformed")

    return ExchangeFailed(
        message,
        primary=error(failure_reply["primary"]),
        cleanup_failures=[error(index) for index in cleanup],
        interruption=error(failure_reply["interruption"]),
        response_head=head,
        proxy_connect_status=proxy_status,
        network_failure=error(failure_reply["network_failure"]),
        network_io=network,
        caller_interruption=error(failure_reply["caller_interruption"]),
    )


def read_reply(document, *, maximum_bytes, error_maximum_bytes):
    require_type(document, dict)

    require_type((document_version := document.get("version")), int)

    if document["version"] != 1:
        raise ValueError("Interpreter child reply has an unknown schema")

    if document.get("kind") == "failure":
        return _read_failure(document)

    if document.get("kind") != "response":
        raise ValueError("Interpreter child reply has an unknown outcome")

    status = require_http_status(document["status"])
    body = decoded_bytes(document["body"], maximum_bytes if is_completion_success(status) else error_maximum_bytes)

    return HttpResponse(status, _read_headers(document["headers"]), body)
