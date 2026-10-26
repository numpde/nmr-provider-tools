"""Controlled source observations and store replies for acquisition contracts."""

import base64
import hashlib
import os
import ssl
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from provider_core.transport import HttpResponse, HttpTransport
from provider_core.upload_response import ReadGrant, UploadMember, UploadSet
from provider_core.uploads import JobUploads, UploadLimits, VerifiedUploadFile
from tests.fakes.transport import Client, Stream
from tests.fakes.uploads import UPLOAD_REF


class API:
    def __init__(self, member, grant):
        self.member = member
        self.grant = grant
        self.calls = []
        self.last_failure = None

    async def uploads(self, job_ref):
        self.calls.append("membership")
        # silent-defaults: allow-contract -- Absent membership contains no synthetic upload members.
        return UploadSet(job_ref, (self.member,) if self.member is not None else ())

    async def read_grant(self, _job_ref, _upload_ref):
        self.calls.append("grant")
        if isinstance(self.grant, Exception):
            raise self.grant
        return self.grant


class Store:
    retirement_confirmed = True
    last_failure = None

    def __init__(self, body):
        self.body = body
        self.calls = []
        self.headers = ()

    async def exchange(self, request, *, maximum_bytes, account_bytes):
        self.calls.append(request)
        self.maximum_bytes = maximum_bytes
        account_bytes(len(self.body))
        return HttpResponse(200, self.headers, self.body)


# silent-defaults: allow-contract -- The ordinary synthetic source is bounded; fault tests override individual resource limits.
def sources(*, total_bytes=100, source_bytes=100, requests=20, seconds=60, store=None):
    body = b"geometry"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    member = UploadMember(UPLOAD_REF, "private-description", len(body), digest)
    grant = ReadGrant(
        "job:example",
        UPLOAD_REF,
        len(body),
        digest,
        "https://store.example/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
        "private-bearer",
        datetime.now(UTC) + timedelta(minutes=1),
    )
    api = API(member, grant)
    if store is None:
        store = Store(body)
    owner = JobUploads(
        "job:example",
        api=api,
        store=store,
        store_origin="https://store.example",
        limits=UploadLimits(source_bytes, total_bytes, requests, seconds),
    )
    return owner, api, store


def file_sources(**limits):
    transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.05)
    owner, api, _ = sources(store=transport, **limits)
    client = Client(Stream([b"geometry"]), headers=store_headers(b"geometry"))
    return owner, api, transport, client


@contextmanager
def private_source_directory():
    with tempfile.TemporaryDirectory() as root:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            yield root, descriptor
        finally:
            os.close(descriptor)


def snapshot_source(root, data):
    path = Path(root) / "source"
    path.write_bytes(data)
    return VerifiedUploadFile("job:example", UPLOAD_REF, len(data), "sha256:" + hashlib.sha256(data).hexdigest(), path.name)


class SourceFileCalls:
    """Observe actual file syscalls under one explicitly borrowed directory."""

    def __init__(self, directory_fd):
        self.directory_fd = directory_fd
        self.names = []
        self.descriptors = []
        self.closed = []
        self.removed = []
        self._open = os.open
        self._close = os.close
        self._unlink = os.unlink

    def open(self, name, *args, **kwargs):
        descriptor = self._open(name, *args, **kwargs)
        if kwargs.get("dir_fd") == self.directory_fd:
            self.names.append(name)
            self.descriptors.append(descriptor)
        return descriptor

    def close(self, descriptor):
        if descriptor in self.descriptors:
            self.closed.append(descriptor)
        return self._close(descriptor)

    def unlink(self, name, *args, **kwargs):
        if kwargs.get("dir_fd") == self.directory_fd:
            self.removed.append(name)
        return self._unlink(name, *args, **kwargs)


@contextmanager
def observe_source_files(directory_fd):
    calls = SourceFileCalls(directory_fd)
    with (
        patch("provider_core.uploads.os.open", side_effect=calls.open),
        patch("provider_core.uploads.os.close", side_effect=calls.close),
        patch("provider_core.uploads.os.unlink", side_effect=calls.unlink),
    ):
        yield calls


def store_headers(body):
    return (
        (b"content-type", b"application/octet-stream"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode()),
        (b"content-digest", b"sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()) + b":"),
    )
