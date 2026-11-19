"""Bounded retries and safe diagnostics for acquisition network operations."""

import datetime
import email.utils
import errno
import http.client
import re
import socket
import ssl
import time
import urllib.error


class RequestFailure(Exception):
    def __init__(self, error):
        self.error = error


def request(operation):
    try:
        return operation()
    except (OSError, http.client.HTTPException) as error:
        raise RequestFailure(error) from None


def failure_detail(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"the server returned HTTP {error.code=}"

    reason = error.reason if isinstance(error, urllib.error.URLError) else error

    if isinstance(reason, socket.gaierror):
        return "name resolution failed"

    if isinstance(reason, TimeoutError):
        return "the connection timed out"

    if isinstance(reason, ssl.SSLError):
        return "the TLS connection failed"

    if isinstance(reason, OSError):
        if match := re.match(r"^Tunnel connection failed: ([0-9]{3})(?:\s|$)", str(reason)):
            tunnel_status = match[1]
            return f"the proxy tunnel returned HTTP {tunnel_status=}"

        if isinstance(reason.errno, int):
            error_number = reason.errno
            return f"an I/O operation failed with {error_number=}"

    if isinstance(reason, http.client.HTTPException):
        return "the HTTP connection failed"

    return "the I/O operation failed"


def retry_delay(error, attempt):
    status = None

    if isinstance(error, urllib.error.HTTPError):
        status = error.code

        if status not in (408, 429, 500, 502, 503, 504):
            return None
    else:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error

        if isinstance(reason, ssl.SSLError):
            return None

        if isinstance(reason, socket.gaierror):
            if reason.errno != socket.EAI_AGAIN:
                return None
        elif isinstance(reason, (TimeoutError, http.client.IncompleteRead, http.client.RemoteDisconnected)):
            pass
        elif isinstance(reason, OSError):
            tunnel = re.match(r"^Tunnel connection failed: ([0-9]{3})(?:\s|$)", str(reason))

            if tunnel:
                # urllib does not expose CONNECT response headers, so a
                # proxy 429/503 cannot be retried without ignoring Retry-After.
                if int(tunnel[1]) not in (408, 500, 502, 504):
                    return None
            elif reason.errno not in (
                errno.ECONNRESET,
                errno.ECONNREFUSED,
                errno.ECONNABORTED,
                errno.ETIMEDOUT,
                errno.EHOSTUNREACH,
                errno.ENETUNREACH,
                errno.EPIPE,
            ):
                return None
        else:
            return None

    delay = (1, 2)[attempt - 1]

    if status in (429, 503):
        header = None

        if error.headers:
            header = error.headers.get("Retry-After")

        if header:
            try:
                if header.isdecimal() and (len(header.lstrip("0")) > 2):
                    return -1

                requested = int(header) if header.isdecimal() else max(0, (email.utils.parsedate_to_datetime(header) - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                requested = 0

            if requested > 30:
                return -1

            delay = max(delay, requested)

    return delay


def retry(operation, operation_context):
    for retry_attempt in range(1, 4):
        try:
            return operation()
        except RequestFailure as failure:
            failure_summary = failure_detail(failure.error)
            delay = None

            if retry_attempt < 3:
                delay = retry_delay(failure.error, retry_attempt)

            if isinstance(failure.error, urllib.error.HTTPError):
                try:
                    failure.error.close()
                except OSError as close_error:
                    close_failure_summary = failure_detail(close_error)
                    raise OSError(
                        f"{operation_context=}: {failure_summary=}; HTTP error response "
                        f"could not be closed; {close_failure_summary=}."
                    ) from None

            if delay == -1:
                raise OSError(f"{operation_context=}: {failure_summary=}; requested retry delay exceeds 30 seconds.") from None

            if delay is None:
                raise OSError(f"{operation_context=}: {failure_summary=}; {retry_attempt=} of 3.") from None

            time.sleep(delay)
