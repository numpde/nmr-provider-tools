"""Bounded protected operator evidence and owned stderr output.

Moves nmrnet-repro f8e681b94dbfb9ba21b88dad737147e8e7adec7f,
nmrnet_provider/process_reports.py::operator_redactor/failure_evidence,
interpreter.py::redact_credential_fields, and process.py's stderr writers.
Scientific projections remain with the binding; core never imports them.
"""

from provider_core.container_guard import require_container

require_container()

import errno
import json
import os
import re
import socket
import sqlite3
import ssl
import stat
import sys
import traceback
from builtins import BaseExceptionGroup
from collections import deque
from pathlib import Path

from provider_core.failure_projection import owned_details as core_owned_details
from provider_core.failure_projection import owned_relationships as core_owned_relationships

# Bound protected failure-graph traversal and diagnostic output.
# repo-check: allow-module-constant
_MAX_FAILURE_NODES = 64


class DiagnosticOutputRefused(ValueError):
    """Code-owned output limits or incomplete writes, without private payloads."""


def describe_core_failure(error, text, diagnostic_window):
    """Provide the default three-argument renderer hook for reviewed SDK facts."""
    if type(error) is DiagnosticOutputRefused:
        return {"message": str(error)}
    return core_owned_details(error)


# Redaction recognizes only this reviewed credential grammar.
# repo-check: allow-module-constant
_CREDENTIAL = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[^\s\"'<>;,]+|\bsk-[A-Za-z0-9_.-]+")
# Match an entire quoted secret, including escaped quotes, so redaction cannot
# leave its suffix visible. Pattern adapted from NMRPeak revision
# 1697dc84dd1d76ae25b04efc8183fd80614000e5,
# nmrpeak_provider/diagnostic_text.py::_CREDENTIAL.
# Redaction recognizes these reviewed secret-bearing field names.
# repo-check: allow-module-constant
_SECRET_FIELD = re.compile(
    r"(?i)(\b(?:api[_-]?key|password|token|secret|authorization)\b[\"']?\s*[:=]\s*)"
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&<>]+)"""
)


def redact_credential_fields(text):
    """Redact recognizable credentials; other private request data may remain."""
    return _SECRET_FIELD.sub(r"\1[redacted]", _CREDENTIAL.sub("[redacted]", text))


def operator_redactor(*, secrets):
    """Share known-secret spellings and credential-field scrubbing across sinks."""
    spellings = {
        spelling for secret in secrets for spelling in (secret, repr(secret)[1:-1], json.dumps(secret)[1:-1]) if spelling
    }
    spellings = sorted(spellings, key=len, reverse=True)

    def redact(text):
        for secret in spellings:
            text = text.replace(secret, "[redacted]")
        return redact_credential_fields(text)

    return redact


# silent-defaults: allow-contract -- Core uses its canonical projections; scientific bindings supply their own reviewed details and relationships without moving scientific imports into core.
def failure_evidence(error, *, secrets, describe_owner=describe_core_failure, owner_relationships=core_owned_relationships):
    """Retain a bounded causal graph and safe current facts for the private operator.

    describe_owner(error, text, diagnostic_window) returns reviewed scalar facts;
    it is distinct from the one-argument owned_details(error) projection.
    """
    redact = operator_redactor(secrets=secrets)
    seen, remaining = {}, _MAX_FAILURE_NODES

    def text(value, maximum):
        redacted = redact(value)
        return redacted if len(redacted) <= maximum else redacted[:maximum] + "[truncated]"

    def diagnostic_window(value):
        diagnostic = redact(value)
        return {
            "head": diagnostic[:4096],
            "tail": diagnostic[-4096:] if len(diagnostic) > 8192 else diagnostic[4096:],
            "omitted_characters": max(0, len(diagnostic) - 8192),
        }

    def relationships(current):
        yield "cause", current.__cause__
        yield "context", current.__context__
        yield from owner_relationships(current)
        if isinstance(current, BaseExceptionGroup):
            for member in current.exceptions:
                yield "group member", member

    def describe(current):
        nonlocal remaining
        remaining -= 1
        if id(current) in seen:
            return {"exception_reference": seen[id(current)]}
        identity = seen[id(current)] = len(seen)
        frames, count = deque(maxlen=32), 0
        for frame, line in traceback.walk_tb(current.__traceback__):
            frames.append(
                {
                    "file": text(Path(frame.f_code.co_filename).name, 128),
                    "line": line,
                    "function": text(frame.f_code.co_name, 128),
                }
            )
            count += 1
        evidence = {"exception_id": identity, "exception_type": text(type(current).__name__, 128), "frames": list(frames)}
        if count > len(frames):
            evidence["omitted_frame_count"] = count - len(frames)
        evidence.update(
            {name: text(value, 1024) if type(value) is str else value for name, value in describe_owner(current, text, diagnostic_window).items()}
        )
        if isinstance(current, OSError) and type(current.errno) is int:
            evidence["errno"] = current.errno
            if current.errno in errno.errorcode and not isinstance(current, (ssl.SSLError, socket.gaierror, socket.herror)):
                evidence["reason"] = os.strerror(current.errno)
        if type(current) is SystemExit:
            code = current.code
            if code is None:
                evidence["system_exit_code"] = None
            elif type(code) in (int, bool) and -(2 ** 63) <= code < 2 ** 63:
                evidence["system_exit_code"] = code
            else:
                evidence["system_exit_code_omitted"] = True
        if isinstance(current, sqlite3.Error):
            # Native result identity excludes SQL, paths and exception text.
            # https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Error
            code = getattr(current, "sqlite_errorcode", None)
            name = getattr(current, "sqlite_errorname", None)
            if type(code) is int:
                evidence["sqlite_errorcode"] = code
            if type(name) is str:
                evidence["sqlite_errorname"] = text(name, 128)
        if isinstance(current, ssl.SSLCertVerificationError):
            verification_code = getattr(current, "verify_code", None)
            if type(verification_code) is int:
                evidence["certificate_verification_code"] = verification_code
        children = []
        for relationship, child in relationships(current):
            if child is None:
                continue
            if remaining <= 0:
                evidence["additional_relationships_omitted"] = True
                break
            children.append({"relationship": text(relationship, 128), "failure": describe(child)})
        if children:
            evidence["relationships"] = children
        return evidence

    return describe(error)


def write_stderr(content):
    """Attempt one nonblocking stderr write without changing later writer policy.

    The process owns this descriptor on its event-loop thread; no await occurs
    while flags differ. Full pipes cannot block retirement. Callers retain failed
    output in memory and never start a logging task or write a fallback file.
    """
    descriptor = sys.stderr.fileno()
    blocking = os.get_blocking(descriptor)
    try:
        os.set_blocking(descriptor, False)
        written = os.write(descriptor, content)
        if written != len(content):
            raise DiagnosticOutputRefused(f"Cannot finish stderr diagnostic output: wrote {written} of {len(content)} bytes.")
    finally:
        os.set_blocking(descriptor, blocking)


def write_notice(content):
    """Keep pipe notices atomic so a rejected record cannot corrupt the next one."""
    descriptor = sys.stderr.fileno()
    if stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        maximum = os.fpathconf(descriptor, "PC_PIPE_BUF")
        if len(content) > maximum:
            raise DiagnosticOutputRefused(
                f"Cannot emit complete API availability notice: {len(content)} bytes exceed the pipe's {maximum}-byte atomic-write limit."
            )
    write_stderr(content)


