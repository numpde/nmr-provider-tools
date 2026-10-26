"""Proposed SECS immutable-source receiving adapter; never a migrated provider.

Ported from numpde/secs-repro 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/source_access.py (SHA-256
04911c122000b0246b844ef3b2567d4e5647effabef4eb0809b2b7bbfa62ef86).
SourceRef is its input_operations.py::SourceRef at that same revision. Archive
limits, indexing, expansion accounting and narrow decoder-error classification
are retained. Only acquired-path access becomes core receipt verification and
BytesIO consumption; direct scope reads retain the same verified copy too.
Composition still owns Job association, worker supervision and source lifetime.
"""

import json
import stat
import struct
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from lzma import LZMAError
from pathlib import Path
from zipfile import BadZipFile, ZipExtFile, ZipFile, ZipInfo, is_zipfile

from provider_core.uploads import UploadFileReadFailed, VerifiedUploadFile, read_upload_file


@dataclass(frozen=True, slots=True)
class SourceRef:
    """An Upload, or an exact member inside its ZIP; never a host path."""

    upload_ref: str
    member: str | None = None


class InputReadError(ValueError):
    """The requested source cannot be read; its safe reason may guide correction."""


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """One admitted logical object without an archive-controlled host path."""

    source: SourceRef
    byte_length: int


@dataclass(frozen=True, slots=True)
class ReadSource:
    """Bytes and identity established by SourceAccess."""

    source: SourceRef
    contents: bytes
    digest: str


class ScopeLimitError(InputReadError):
    """One attributed source would exceed the aggregate inspection budget."""

    def __init__(self, source: SourceRef, limit: int):
        super().__init__(f"Cannot inspect this source: its expanded members exceed the {limit}-byte inspection limit")
        self.source = source


class ScopeReader:
    """Materialize one discovery scope under a shared expanded-byte limit."""

    def __init__(
        self,
        access: "SourceAccess",
        root: SourceRef,
        entries: tuple[SourceEntry, ...],
        contents: bytes,
        archive: ZipFile | None = None,
        members: dict[str, tuple[ZipInfo, ...]] | None = None,
    ):
        self._access = access
        self._root = root
        self.entries = entries
        self._contents = contents
        self._archive = archive
        self._members = members
        self._remaining = access.max_scope_bytes

    def read(self, source: SourceRef) -> ReadSource:
        entry, info = self._entry(source)
        if entry.byte_length > self._remaining:
            raise ScopeLimitError(source, self._access.max_scope_bytes)
        self._remaining -= entry.byte_length
        allowance = entry.byte_length + self._remaining
        try:
            if info is None:
                if source == self._root:
                    with BytesIO(self._contents) as stream:
                        read = self._access._read_stream(source, stream, allowance)
                else:
                    read = self._access.read(source, scope_bytes=allowance)
            else:
                read = self._access._read_member(self._archive, info, source, allowance)
        except ScopeLimitError:
            self._remaining = 0
            raise
        additional = len(read.contents) - entry.byte_length
        if additional > self._remaining:
            raise ScopeLimitError(source, self._access.max_scope_bytes)
        self._remaining -= max(0, additional)
        return read

    def _entry(self, source: SourceRef) -> tuple[SourceEntry, ZipInfo | None]:
        if self._archive is not None and source.upload_ref == self._root.upload_ref and source.member is not None:
            info = self._access._indexed_member(self._members, source.member)
            return SourceEntry(source, info.file_size), info
        if self._archive is None and source == self._root:
            return self.entries[0], None
        return self._access.scope(source)[0], None


class SourceAccess:
    """Attempt-owned acquired files; all materialized paths remain private.

    The execution owner must stop active readers before releasing this object
    or its acquired files. This class does not acquire network authority.
    """

    # silent-defaults: allow-contract -- Retain SECS's fixed 128 MiB member and 256 MiB inspection limits; callers may supply tighter limits.
    def __init__(
        self,
        files: dict[str, VerifiedUploadFile],
        directory: Path,
        *,
        directory_fd: int,
        max_upload_bytes: int,
        max_member_bytes: int = 128 * 1024 * 1024,
        max_scope_bytes: int = 256 * 1024 * 1024,
    ):
        self.files = files
        # input_formats.py::prepare_representation at the pinned revision uses
        # access.directory for fixed-name scientific temporaries. That scratch
        # grant is separate from directory_fd's acquired-source read grant.
        self.directory = directory
        self.directory_fd = directory_fd
        self.max_upload_bytes = max_upload_bytes
        self.max_member_bytes = max_member_bytes
        self.max_scope_bytes = max_scope_bytes

    @contextmanager
    def scope_reader(self, source: SourceRef):
        """Admit one inventory and reuse its archive index for the inspection."""
        contents = self._upload(source.upload_ref)
        if source.member is None and not _is_zip(contents):
            entries = (SourceEntry(source, len(contents)),)
            yield ScopeReader(self, source, entries, contents)
            return
        with self._archive(contents) as archive:
            infos = archive.infolist()
            members = _member_index(infos)
            entries = self._scope_entries(source, infos, members)
            yield ScopeReader(self, source, entries, contents, archive, members)

    def scope(self, source: SourceRef) -> tuple[SourceEntry, ...]:
        """Enumerate a root scope or admit one exact member without format policy."""
        contents = self._upload(source.upload_ref)
        if source.member is not None:
            with self._archive(contents) as archive:
                info = self._member(archive, source.member)
            return (SourceEntry(source, info.file_size),)
        if not _is_zip(contents):
            return (SourceEntry(source, len(contents)),)
        with self._archive(contents) as archive:
            infos = archive.infolist()
            return self._scope_entries(source, infos, _member_index(infos))

    def _scope_entries(
        self, source: SourceRef, infos: list[ZipInfo], members: dict[str, tuple[ZipInfo, ...]]
    ) -> tuple[SourceEntry, ...]:
        if source.member is not None:
            info = self._indexed_member(members, source.member)
            return (SourceEntry(source, info.file_size),)
        if len(infos) > 4096:
            raise InputReadError("Cannot inspect this ZIP: it exceeds the 4096-member inspection limit")
        # repo-check: allow-tuple-generator -- The fixture source scope promises an immutable SourceEntry tuple after complete bounds checks.
        entries = tuple(
            SourceEntry(SourceRef(source.upload_ref, info.filename), info.file_size) for info in infos if not info.is_dir()
        )
        if sum(entry.byte_length for entry in entries) > self.max_scope_bytes:
            raise InputReadError(f"Cannot inspect this ZIP: its members exceed the {self.max_scope_bytes}-byte inspection limit")
        listing = {"members": [{"name": entry.source.member, "byte_length": entry.byte_length} for entry in entries]}
        if len(json.dumps(listing, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
            raise InputReadError(
                "Cannot inspect this ZIP: its member listing exceeds the 262144-byte inspection limit; "
                "an exact member can still be inspected if known"
            )
        return entries

    def read(self, source: SourceRef, *, scope_bytes: int | None = None) -> ReadSource:
        """Read one admitted object once, enforcing the decoder byte limit."""
        with self.open(source) as stream:
            return self._read_stream(source, stream, scope_bytes)

    def _read_stream(self, source: SourceRef, stream, scope_bytes: int | None) -> ReadSource:
        chunks = []
        total = 0
        while chunk := _read_chunk(stream, 64 * 1024):
            total += len(chunk)
            if total > self.max_member_bytes:
                raise InputReadError(f"Cannot read the selected source: it exceeds the {self.max_member_bytes}-byte member limit")
            if scope_bytes is not None and total > scope_bytes:
                raise ScopeLimitError(source, self.max_scope_bytes)
            chunks.append(chunk)
        contents = b"".join(chunks)
        return ReadSource(source, contents, "sha256:" + sha256(contents).hexdigest())

    def _read_member(self, archive: ZipFile | None, info: ZipInfo, source: SourceRef, scope_bytes: int) -> ReadSource:
        assert archive is not None
        with self._member_stream(archive, info) as stream:
            return self._read_stream(source, stream, scope_bytes)

    @contextmanager
    def open(self, source: SourceRef):
        """Open selected bytes, rejecting ambiguous or nonregular ZIP members."""
        contents = self._upload(source.upload_ref)
        if source.member is None:
            with BytesIO(contents) as stream:
                yield stream
            return
        with self._archive(contents) as archive:
            info = self._member(archive, source.member)
            with self._member_stream(archive, info) as stream:
                yield stream

    @contextmanager
    def _member_stream(self, archive: ZipFile, info: ZipInfo):
        try:
            stream = archive.open(info)
        except (BadZipFile, NotImplementedError, RuntimeError, UnicodeDecodeError) as error:
            raise InputReadError("Cannot read the selected ZIP member: decoding or integrity checking failed") from error
        with stream:
            yield stream

    def _upload(self, ref: str) -> bytes:
        try:
            receipt = self.files[ref]
        except KeyError:
            raise InputReadError("Cannot read the selected source: choose an Upload listed for this Job") from None
        if receipt.upload_ref != ref:
            raise UploadFileReadFailed("the acquired file receipt belongs to a different Upload than its source label")
        return read_upload_file(receipt, directory_fd=self.directory_fd, maximum_bytes=self.max_upload_bytes).data

    @contextmanager
    def _archive(self, contents: bytes):
        self._preflight_archive(contents)
        with BytesIO(contents) as stream:
            try:
                archive = ZipFile(stream)
            except (BadZipFile, UnicodeDecodeError) as error:
                raise InputReadError("Cannot inspect archive members: the selected Upload is not a readable ZIP") from error
            with archive:
                yield archive

    def _preflight_archive(self, contents: bytes) -> None:
        """Reject excessive entry counts before Python loads the central directory."""
        with BytesIO(contents) as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 65_557))
            tail = stream.read()
            eocd = tail.rfind(b"PK\x05\x06")
            if eocd < 0 or len(tail) - eocd < 22:
                return
            disk, central_disk, disk_entries, total_entries = struct.unpack_from("<4H", tail, eocd + 4)
            if disk or central_disk or disk_entries != total_entries:
                raise InputReadError("Cannot inspect this ZIP: multi-disk archives are unsupported")
            if total_entries == 0xFFFF:
                locator = tail.rfind(b"PK\x06\x07", 0, eocd)
                if locator < 0 or len(tail) - locator < 20:
                    raise InputReadError("Cannot inspect this ZIP: its ZIP64 entry count is unavailable")
                zip64_disk, zip64_offset, total_disks = struct.unpack_from("<IQI", tail, locator + 4)
                if zip64_disk or total_disks != 1:
                    raise InputReadError("Cannot inspect this ZIP: multi-disk archives are unsupported")
                stream.seek(zip64_offset)
                record = stream.read(56)
                if len(record) < 56 or not record.startswith(b"PK\x06\x06"):
                    raise InputReadError("Cannot inspect this ZIP: its ZIP64 entry count is unavailable")
                total_entries = struct.unpack_from("<Q", record, 32)[0]
            if total_entries > 4096:
                raise InputReadError("Cannot inspect this ZIP: it exceeds the 4096-member inspection limit")

    def _member(self, archive: ZipFile, name: str) -> ZipInfo:
        return self._indexed_member(_member_index(archive.infolist()), name)

    def _indexed_member(self, members: dict[str, tuple[ZipInfo, ...]] | None, name: str) -> ZipInfo:
        # silent-defaults: allow-contract -- An absent index or name has zero matches, so the next guard refuses the selection.
        matches = () if members is None else members.get(name, ())
        if len(matches) != 1:
            raise InputReadError("Cannot read the selected ZIP member: its name is missing or repeated")
        info = matches[0]
        mode = info.external_attr >> 16
        if info.is_dir() or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
            raise InputReadError("Cannot read the selected ZIP member: it is not a regular file")
        if info.flag_bits & 1:
            raise InputReadError("Cannot read the selected ZIP member: encrypted members are unsupported")
        if info.file_size > self.max_member_bytes:
            raise InputReadError(f"Cannot read the selected ZIP member: it exceeds the {self.max_member_bytes}-byte member limit")
        return info


def _is_zip(contents: bytes) -> bool:
    with BytesIO(contents) as stream:
        return is_zipfile(stream)


def _member_index(infos: list[ZipInfo]) -> dict[str, tuple[ZipInfo, ...]]:
    grouped: dict[str, list[ZipInfo]] = {}
    for info in infos:
        # silent-defaults: allow-contract -- Start each name's list empty and append every duplicate so selection can reject ambiguity.
        grouped.setdefault(info.filename, []).append(info)
    return {name: tuple(matches) for name, matches in grouped.items()}


def _read_chunk(stream, size: int) -> bytes:
    """Translate failures only while decoding bytes, not around the consumer."""
    try:
        return stream.read(size)
    except (BadZipFile, zlib.error, LZMAError, EOFError, OSError) as error:
        # bz2 reports corrupt compressed data as an errno-less OSError.
        # Translate that form only for ZIP decoding; filesystem errors and
        # unclassified errors from direct-file reads remain operational.
        if isinstance(error, OSError) and (not isinstance(stream, ZipExtFile) or error.errno is not None):
            raise
        raise InputReadError("Cannot read the selected ZIP member: decoding or integrity checking failed") from error
