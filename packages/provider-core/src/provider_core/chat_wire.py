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
from provider_core.transport import HttpResponse, HttpResponseHead
from provider_core.transport_errors import ExchangeFailed


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
    kind.__module__ + "." + kind.__qualname__: kind for kind in (
        Exception, RuntimeError, ValueError, TypeError, TimeoutError,
        OSError, ConnectionError, ConnectionRefusedError, ConnectionResetError,
        ConnectionAbortedError, BrokenPipeError, FileNotFoundError, PermissionError,
        socket.gaierror, ssl.SSLError, ssl.SSLCertVerificationError,
        ssl.SSLEOFError, ssl.SSLZeroReturnError, ssl.SSLWantReadError,
        ssl.SSLWantWriteError, asyncio.CancelledError,
        httpcore.ConnectError, httpcore.ReadError, httpcore.WriteError,
        httpcore.ConnectTimeout, httpcore.ReadTimeout, httpcore.WriteTimeout,
        httpcore.PoolTimeout, httpcore.RemoteProtocolError, httpcore.LocalProtocolError,
        httpcore.UnsupportedProtocol, anyio.BrokenResourceError, anyio.EndOfStream,
    )
}


def encoded_bytes(value):
    return base64.b64encode(value).decode("ascii")


def decoded_bytes(value, maximum):
    if not (type(value) is str and len(value) <= 4 * ((maximum + 2) // 3)):
        raise ValueError("Interpreter child byte field exceeds its admitted limit")
    result = base64.b64decode(value, validate=True)
    if not len(result) <= maximum:
        raise ValueError("Interpreter child byte field exceeds its admitted limit")
    return result


def message_bytes(document):
    raw = json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    if not len(raw) <= 0xffffffff:
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
    size, = struct.unpack("!I", header)
    if not 0 < size <= maximum:
        raise ValueError("Interpreter child message exceeds its admitted limit")
    return size


def read_request(stream, maximum_bytes):
    size = _size(stream.read(4), maximum_bytes)
    raw = stream.read(size)
    if len(raw) != size or stream.read(1):
        raise ValueError("Interpreter child requires one complete request message")
    document = read_document(raw, maximum_bytes)
    if (type(document) is not dict or type(document.get("version")) is not int
            or document["version"] != 1 or document.get("kind") != "request"):
        raise ValueError("Interpreter child request has an unknown schema")
    return document


async def read_message(reader, maximum_bytes):
    size = _size(await reader.readexactly(4), maximum_bytes)
    return read_document(await reader.readexactly(size), maximum_bytes)


def _headers(headers):
    return [[encoded_bytes(name), encoded_bytes(value)] for name, value in headers]


def _read_headers(headers):
    if not (type(headers) is list and len(headers) <= 65536):
        raise ValueError("Interpreter child headers exceed their admitted limit")
    result = []
    remaining = METADATA_BYTES
    for pair in headers:
        if type(pair) is not list or len(pair) != 2:
            raise ValueError("Interpreter child header is malformed")
        name, value = (decoded_bytes(part, remaining) for part in pair)
        remaining -= len(name) + len(value)
        if not 0 <= remaining:
            raise ValueError("Interpreter child headers exceed their admitted limit")
        result.append((name, value))
    return tuple(result)


def _status(value):
    if type(value) is not int or not 100 <= value <= 599:
        raise ValueError("Interpreter child returned an invalid HTTP status")
    return value


def response_message(response):
    return {"version": 1, "kind": "response", "status": response.status,
            "headers": _headers(response.headers), "body": encoded_bytes(response.body)}


def failure_message(failure):
    """Keep typed cause/reference relationships inside a bounded inert graph."""
    nodes, seen = [], {}

    def reference(error):
        if error is None:
            return None
        identity = id(error)
        if identity in seen:
            return seen[identity]
        if not len(nodes) < _ERROR_NODES:
            raise ValueError("Interpreter child failure evidence exceeds its graph limit")
        index = seen[identity] = len(nodes)
        kind = type(error).__module__ + "." + type(error).__qualname__
        node = {"type": kind, "text": str(error)[:_ERROR_TEXT],
                # silent-defaults: allow-contract -- Only a real integer OSError errno is portable native evidence; None records its absence.
                "errno": error.errno if isinstance(error, OSError) and type(error.errno) is int else None}
        nodes.append(node)
        if type(error) is DecodedBodyRejected:
            node["reason"] = error.reason
        node["cause"], node["context"] = reference(error.__cause__), reference(error.__context__)
        node["suppress_context"] = error.__suppress_context__
        if isinstance(error, BaseExceptionGroup):
            node["children"] = [reference(child) for child in error.exceptions]
        return index

    primary = reference(failure.primary)
    cleanup = [reference(error) for error in failure.cleanup_failures]
    interruption, network_failure = reference(failure.interruption), reference(failure.network_failure)
    network = failure.network_io
    if network is not None:
        network = {"stage": network.stage, "error": reference(network.error),
                   "cause": reference(network.cause), "context": reference(network.context),
                   "unix_path": network.unix_path, "configured_unix_path": network.configured_unix_path,
                   "eof": network.eof, "deadline_cancelled": network.deadline_cancelled}
    head = failure.response_head
    if head is not None:
        head = {"status": head.status, "headers": _headers(head.headers)}
    return {"version": 1, "kind": "failure", "message": str(failure)[:_ERROR_TEXT],
            "nodes": nodes, "primary": primary, "cleanup": cleanup, "interruption": interruption,
            "head": head, "proxy_status": failure.proxy_connect_status,
            "network_failure": network_failure, "network": network,
            "caller_interruption": reference(failure.caller_interruption)}


def _read_failure(document):
    nodes = document["nodes"]
    if not (type(nodes) is list and len(nodes) <= _ERROR_NODES):
        raise ValueError("Interpreter child failure evidence exceeds its graph limit")
    errors = [None] * len(nodes)
    constructing = set()

    def error(index):
        if index is None:
            return None
        if type(index) is not int or not 0 <= index < len(nodes):
            raise ValueError("Interpreter child failure reference is malformed")
        if errors[index] is not None:
            return errors[index]
        if index in constructing:
            raise ValueError("Interpreter child exception group is cyclic")
        constructing.add(index)
        node = nodes[index]
        kind, text = node["type"], node["text"]
        if not (type(kind) is str and type(text) is str and len(text) <= _ERROR_TEXT):
            raise ValueError("Interpreter child failure description is malformed")
        if kind == "provider_core.decoded_body.DecodedBodyRejected":
            result = DecodedBodyRejected(node["reason"])
        elif kind in ("builtins.ExceptionGroup", "builtins.BaseExceptionGroup"):
            children = node["children"]
            if type(children) is not list or not 0 < len(children) <= _ERROR_NODES:
                raise ValueError("Interpreter child exception group is malformed")
            result = BaseExceptionGroup(text, [error(child) for child in children])
        else:
            # silent-defaults: allow-contract -- Unreviewed child error types retain protected evidence without acquiring retry authority.
            constructor = _ERROR_TYPES.get(kind, RemoteExchangeError)
            number = node["errno"]
            if number is not None and (type(number) is not int or not (
                    issubclass(constructor, OSError) or constructor is RemoteExchangeError)):
                raise ValueError("Interpreter child error number is malformed")
            result = constructor(number, text) if number is not None and issubclass(constructor, OSError) else constructor(text)
            if constructor is RemoteExchangeError:
                result.remote_type = kind
                result.remote_errno = number
        errors[index] = result
        constructing.remove(index)
        return result

    for index in range(len(nodes)):
        error(index)
    for current, node in zip(errors, nodes):
        current.__cause__, current.__context__ = error(node["cause"]), error(node["context"])
        if type(node["suppress_context"]) is not bool:
            raise ValueError("Interpreter child failure context flag is malformed")
        current.__suppress_context__ = node["suppress_context"]
    head = document["head"]
    if head is not None:
        head = HttpResponseHead(_status(head["status"]), _read_headers(head["headers"]))
    network = document["network"]
    if network is not None:
        for name in ("stage", "unix_path", "configured_unix_path"):
            if network[name] is not None and type(network[name]) is not str:
                raise ValueError("Interpreter child network description is malformed")
        if type(network["eof"]) is not bool or type(network["deadline_cancelled"]) is not bool:
            raise ValueError("Interpreter child network flags are malformed")
        network = NetworkIOObservation(
            stage=network["stage"], error=error(network["error"]), cause=error(network["cause"]),
            context=error(network["context"]), unix_path=network["unix_path"],
            configured_unix_path=network["configured_unix_path"], eof=network["eof"],
            deadline_cancelled=network["deadline_cancelled"],
        )
    proxy_status = document["proxy_status"]
    if proxy_status is not None:
        _status(proxy_status)
    message = document["message"]
    if not (type(message) is str and len(message) <= _ERROR_TEXT):
        raise ValueError("Interpreter child failure message is malformed")
    cleanup = document["cleanup"]
    if type(cleanup) is not list or len(cleanup) > _ERROR_NODES or any(index is None for index in cleanup):
        raise ValueError("Interpreter child cleanup obligations are malformed")
    return ExchangeFailed(
        message, primary=error(document["primary"]), cleanup_failures=[error(index) for index in cleanup],
        interruption=error(document["interruption"]), response_head=head, proxy_connect_status=proxy_status,
        network_failure=error(document["network_failure"]), network_io=network,
        caller_interruption=error(document["caller_interruption"]),
    )


def read_reply(document, *, maximum_bytes, error_maximum_bytes):
    if type(document) is not dict or type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("Interpreter child reply has an unknown schema")
    if document.get("kind") == "failure":
        return _read_failure(document)
    if document.get("kind") != "response":
        raise ValueError("Interpreter child reply has an unknown outcome")
    status = _status(document["status"])
    body = decoded_bytes(document["body"], maximum_bytes if status == 200 else error_maximum_bytes)
    return HttpResponse(status, _read_headers(document["headers"]), body)
