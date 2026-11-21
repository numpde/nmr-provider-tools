"""Capture the TLS authority granted to one fresh model HTTP interpreter.

SSLContext cannot export arbitrary client keys, callbacks or lazy trust stores.
The context built here records its actual trust inputs while retaining the
normal SSLContext interface. A child receives immutable material and settings,
not a path to reopen or permission to consult its ambient trust configuration.
"""

from provider_core.container_guard import require_container

require_container()

import base64
import os
import pathlib
import re
import reprlib
import ssl
import tempfile
import weakref

from provider_core.type_requirements import require_instance, require_type


# Fixed serialization allowances bound a single immutable TLS grant.
# repo-check: allow-module-constant
MAX_TRUST_BYTES = 4 * 1024 * 1024

# repo-check: allow-module-constant
MAX_TRUST_MATERIALS = 4096

# Native OpenSSL hash-directory lookup names, not configurable destinations.
# repo-check: allow-module-constant
_HASHED_TRUST_FILE = re.compile(r"[0-9a-fA-F]{8}\.(?:r)?[0-9]+\Z")

# Replay uses the native context implementation without ambient root loading.
# repo-check: allow-module-constant
_SSL_CONTEXT = ssl.SSLContext

# Supported public TLS settings form a closed snapshot schema.
# repo-check: allow-module-constant
_FIELDS = (
    "options",
    "verify_flags",
    "minimum_version",
    "maximum_version",
    "hostname_checks_common_name",
    "post_handshake_auth",
)


class TLSCaptureRejected(ValueError):
    """This context cannot be reproduced without changing its granted authority."""


def _refuse(reason):
    return TLSCaptureRejected("Cannot isolate model HTTPS: " + reason)


def _bounded_file(trust_file_path):
    with open(trust_file_path, "rb") as stream:
        trust_file_bytes = stream.read(MAX_TRUST_BYTES + 1)

    if len(trust_file_bytes) > MAX_TRUST_BYTES:
        raise _refuse(f"{len(trust_file_bytes)=} should be at most {MAX_TRUST_BYTES=}")

    return trust_file_bytes


def _file_materials(cafile, capath):
    materials = []

    if cafile is not None:
        materials.append(("file", _bounded_file(cafile)))

    if capath is not None:
        # Preserve names and directory groups. Eager cafile loading would grant
        # authority to wrong hash names and suffixes beyond native lookup gaps.
        hashed_trust_paths = []

        for trust_path in pathlib.Path(capath).iterdir():
            if _HASHED_TRUST_FILE.fullmatch(trust_path.name) is not None:
                hashed_trust_paths.append(trust_path)

                if len(hashed_trust_paths) > MAX_TRUST_MATERIALS:
                    raise _refuse(f"{len(hashed_trust_paths)=} should be at most {MAX_TRUST_MATERIALS=}")

        hashed_trust_paths.sort(key=lambda trust_path: trust_path.name)
        captured = []

        for trust_path in hashed_trust_paths:
            if not trust_path.is_file():
                raise _refuse(f"{reprlib.repr(trust_path)=} should be a regular file")

            captured.append((trust_path.name, _bounded_file(trust_path)))
            _require_material_budget((*materials, ("capath", tuple(captured))))

        materials.append(("capath", tuple(captured)))

    _require_material_budget(materials)

    return tuple(materials)


def _require_material_budget(materials):
    trust_material_count = len(materials)
    trust_material_byte_count = 0

    for (material_kind, trust_material) in materials:
        if material_kind == "capath":
            trust_material_count += len(trust_material)
            trust_material_byte_count += sum(len(raw) for (_name, raw) in trust_material)
        else:
            trust_material_byte_count += len(trust_material)

    if not (trust_material_count <= MAX_TRUST_MATERIALS):
        raise _refuse(f"{trust_material_count=} should be at most {MAX_TRUST_MATERIALS=}")

    if not (trust_material_byte_count <= MAX_TRUST_BYTES):
        raise _refuse(f"{trust_material_byte_count=} should be at most {MAX_TRUST_BYTES=}")


def _load_material(context, kind, raw, directory):
    if kind == "pem":
        _SSL_CONTEXT.load_verify_locations(context, cadata=raw.decode("ascii"))
    elif kind == "der":
        _SSL_CONTEXT.load_verify_locations(context, cadata=raw)
    elif kind == "file":
        # cafile accepts trusted-certificate attributes and CRLs that cannot be
        # recovered from get_ca_certs(). Loading is eager, so close this private
        # descriptor immediately. Process termination also closes it; there is
        # no named temporary file left behind by a killed HTTP child.
        if not hasattr(os, "memfd_create"):
            raise _refuse(f"{hasattr(os, 'memfd_create')=} should be True to load file trust without retaining a path")

        descriptor = os.memfd_create("provider-model-trust", os.MFD_CLOEXEC)

        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()

            os.lseek(descriptor, 0, os.SEEK_SET)

            _SSL_CONTEXT.load_verify_locations(
                context,
                cafile=f"/proc/self/fd/{descriptor}",  # machine-string: OpenSSL consumes this proc-fd path; adding the expression name would change the path.
            )
        finally:
            os.close(descriptor)
    elif kind == "capath":
        # The caller owns this directory until the context or HTTP child has
        # retired. SIGKILL cannot strand files: child grants are removed by the
        # parent only after it confirms process and IPC retirement.
        captured = pathlib.Path(tempfile.mkdtemp(prefix="capath-", dir=directory))

        for (name, content) in raw:
            with (captured / name).open("xb") as stream:
                stream.write(content)

        _SSL_CONTEXT.load_verify_locations(context, capath=str(captured))
    else:
        expected_material_kinds = ["pem", "der", "file", "capath"]

        raise _refuse(f"{reprlib.repr(kind)=} should be one of {expected_material_kinds=}")


def _require_capturable_system_trust() -> None:
    """Require a capturable system-root source before native TLS loading."""

    if not all(
        (
            ((ambient_tls_variable_name := variable_name) not in os.environ)
            for variable_name in ["SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"]
        ),
    ):
        raise _refuse(f"{ambient_tls_variable_name=} should be unset")

    expected_platform = "posix"

    if not (os.name == expected_platform):
        raise _refuse(f"{os.name=} should match {expected_platform=}")


class CapturedSSLContext(ssl.SSLContext):
    """An ordinary client context with replayable trust and tracked opaque calls."""

    def __new__(cls, protocol):
        return super().__new__(cls, protocol)

    def __init__(self, protocol):
        self._chat_materials = ()
        self._chat_opaque = None
        self._chat_alpn = None
        self._chat_ecdh_curve = None
        self._chat_directory = None

    # Keep the standard SSLContext loader's optional selectors; absence grants no trust.
    def load_verify_locations(self, cafile=None, capath=None, cadata=None):
        materials = list(_file_materials(cafile, capath))

        if cadata is not None:
            if type(cadata) is str:
                materials.append(("pem", cadata.encode("ascii")))
            else:
                require_instance(cadata, (bytes, bytearray, memoryview))

                materials.append(("der", bytes(cadata)))

        _require_material_budget((*self._chat_materials, *materials))

        if not materials:
            return super().load_verify_locations(cafile=cafile, capath=capath, cadata=cadata)

        try:
            for (kind, raw) in materials:
                if (kind == "capath") and (self._chat_directory is None):
                    self._chat_directory = tempfile.TemporaryDirectory(prefix="provider-model-trust-")

                    weakref.finalize(self, self._chat_directory.cleanup)

                # An eager PEM/DER/file loader does not use this directory.
                # silent-defaults: allow-contract -- Only a recorded lazy store needs an owned directory; eager loaders consume their bytes immediately.
                directory = None if self._chat_directory is None else self._chat_directory.name

                _load_material(self, kind, raw, directory)
        except BaseException:
            # OpenSSL may have accepted part of an input before rejecting its
            # remainder. Ignoring that exception cannot make the result safely
            # reproducible by omitting the partially loaded authority.
            self._chat_opaque = "a trust update did not complete"

            raise

        self._chat_materials = (*self._chat_materials, *materials)

    def set_default_verify_paths(self):
        _require_capturable_system_trust()

        paths = ssl.get_default_verify_paths()

        if (paths.cafile is not None) or (paths.capath is not None):
            self.load_verify_locations(cafile=paths.cafile, capath=paths.capath)

    # silent-defaults: allow-contract -- Server authentication is SSLContext's normal client-root purpose.
    def load_default_certs(self, purpose=ssl.Purpose.SERVER_AUTH):
        _require_capturable_system_trust()

        # CPython's implementation selects its platform fallback bundle/path
        # before calling set_default_verify_paths. Both calls dispatch through
        # captured loaders; lazy lookups use only the owned captured directory.
        return super().load_default_certs(purpose)

    def set_alpn_protocols(self, protocols):
        captured = tuple(protocols)
        setter_return = super().set_alpn_protocols(captured)
        self._chat_alpn = captured

        return setter_return

    def set_ecdh_curve(self, curve_name):
        setter_return = super().set_ecdh_curve(curve_name)
        self._chat_ecdh_curve = curve_name

        return setter_return

    # Keep SSLContext's client-certificate API; refuse its authority at model capture.
    def load_cert_chain(self, certfile, keyfile=None, password=None):
        self._chat_opaque = "client certificates or private keys are not transferable"

        return super().load_cert_chain(certfile, keyfile, password)

    def load_dh_params(self, dhfile):
        self._chat_opaque = "custom DH parameters are not transferable"

        return super().load_dh_params(dhfile)

    def set_npn_protocols(self, protocols):
        self._chat_opaque = "custom NPN negotiation is not transferable"

        return super().set_npn_protocols(protocols)


def new_https_trust(raw):
    """Build captured client trust from explicit PEM bytes or fixed system roots."""
    context = CapturedSSLContext(ssl.PROTOCOL_TLS_CLIENT)

    if raw is None:
        # This first step also rejects ambient TLS overrides before consulting
        # the native default helper, which can otherwise enable key logging.
        context.load_default_certs()

        # Runtime defaults may tighten without a deprecation period (Python
        # 3.13 adds STRICT/PARTIAL_CHAIN). Copy policy from the actual runtime,
        # while granting trust only through the captured original material.
        reference = ssl.create_default_context()

        for name in _FIELDS:
            setattr(context, name, getattr(reference, name))

        context.verify_mode = reference.verify_mode
        context.check_hostname = reference.check_hostname

        context.set_ciphers(_cipher_expression(reference))

        for setting_name in _FIELDS:
            captured_setting = getattr(context, setting_name)
            runtime_setting = getattr(reference, setting_name)

            if not (captured_setting == runtime_setting):
                raise _refuse(f"{setting_name=}: {captured_setting=} should match {runtime_setting=}")

        captured_ciphers = context.get_ciphers()
        runtime_ciphers = reference.get_ciphers()

        if not (captured_ciphers == runtime_ciphers):
            captured_cipher_count = len(captured_ciphers)
            runtime_cipher_count = len(runtime_ciphers)

            if not (captured_cipher_count == runtime_cipher_count):
                raise _refuse(f"{captured_cipher_count=} should match {runtime_cipher_count=}")

            cipher_index = next(
                (
                    index
                    for index in range(captured_cipher_count)
                    if (captured_ciphers[index] != runtime_ciphers[index])
                ),
            )

            captured_cipher = captured_ciphers[cipher_index]
            runtime_cipher = runtime_ciphers[cipher_index]

            raise _refuse(f"{cipher_index=}: {captured_cipher=} should match {runtime_cipher=}")

        if not (context.security_level == reference.security_level):
            raise _refuse(f"{context.security_level=} should match {reference.security_level=}")
    else:
        context.load_verify_locations(cadata=raw.decode("ascii"))

    return context


def _facts(context):
    return {
        "ciphers": [cipher["name"] for cipher in context.get_ciphers()],
        "security_level": context.security_level,
    }


def _cipher_expression(context):
    legacy = [cipher["name"] for cipher in context.get_ciphers() if cipher["protocol"] != "TLSv1.3"]

    return ":".join((*legacy, "@SECLEVEL=" + str(context.security_level)))


def _material_message(kind, trust_material):
    if kind == "capath":
        return {
            "kind": kind,
            "entries": [
                {"name": name, "data": base64.b64encode(raw).decode("ascii")} for (name, raw) in trust_material
        ]
        }

    return {"kind": kind, "data": base64.b64encode(trust_material).decode("ascii")}


def capture_trust(context):
    """Freeze supported context state; refuse authority that cannot be copied."""

    require_type(context, CapturedSSLContext, error_factory=_refuse)

    if not (context._chat_opaque is None):
        raise _refuse(context._chat_opaque)

    if not (context.protocol == ssl.PROTOCOL_TLS_CLIENT):
        raise _refuse(f"{context.protocol=} should match {ssl.PROTOCOL_TLS_CLIENT=}")

    if not (context.verify_mode == ssl.CERT_REQUIRED):
        raise _refuse(f"{context.verify_mode=} should match {ssl.CERT_REQUIRED=}")

    if not (context.check_hostname):
        raise _refuse(f"{context.check_hostname=} should be True")

    if not (context.sni_callback is None):
        raise _refuse(f"{context.sni_callback is not None=} should be False")

    if not (context._msg_callback is None):
        raise _refuse(f"{context._msg_callback is not None=} should be False")

    if not (context.keylog_filename is None):
        raise _refuse(f"{reprlib.repr(context.keylog_filename)=} should be None")

    if not (context.sslsocket_class is ssl.SSLSocket):
        raise _refuse(f"{id(context.sslsocket_class)=} should match {id(ssl.SSLSocket)=}")

    if not (context.sslobject_class is ssl.SSLObject):
        raise _refuse(f"{id(context.sslobject_class)=} should match {id(ssl.SSLObject)=}")

    facts = _facts(context)

    snapshot = {
        "version": 1,
        "openssl_version": ssl.OPENSSL_VERSION,
        "materials": [_material_message(material_kind, trust_material) for (material_kind, trust_material) in context._chat_materials],
        "settings": {name: getattr(context, name) for name in _FIELDS},
        "cipher_expression": _cipher_expression(context),
        # silent-defaults: allow-contract -- No configured ALPN leaves the native default untouched; a configured sequence is replayed exactly.
        "alpn": None if context._chat_alpn is None else list(context._chat_alpn),
        "ecdh_curve": context._chat_ecdh_curve,
        "facts": facts,
    }

    # Cached CA counts change when a native capath lookup succeeds, so they are
    # not authority. Recorded original material and supported public mutations
    # establish the grant; reconstruction verifies the effective TLS policy.
    with tempfile.TemporaryDirectory(prefix="provider-model-trust-check-") as directory:
        restore_trust(snapshot, directory=directory)

    return snapshot


def _material_bytes(encoded_trust_material):
    require_type(encoded_trust_material, str, error_factory=_refuse)

    max_encoded_trust_characters = ((MAX_TRUST_BYTES + 2) // 3) * 4

    if len(encoded_trust_material) > max_encoded_trust_characters:
        raise _refuse(f"{len(encoded_trust_material)=} should be at most {max_encoded_trust_characters=}")

    try:
        return base64.b64decode(encoded_trust_material, validate=True)
    except (ValueError, UnicodeError) as cause:
        raise _refuse(f"{reprlib.repr(encoded_trust_material)=} should be valid base64") from cause


# A standalone context owns its directory; a child receives a parent-owned grant.
def restore_trust(snapshot, *, directory=None):
    """Recreate captured TLS without reading default roots or original paths."""
    expected_snapshot_fields = {"version", "openssl_version", "materials", "settings", "cipher_expression", "alpn", "ecdh_curve", "facts"}

    require_type(snapshot, dict, error_factory=_refuse)

    observed_snapshot_fields = set(snapshot)

    if not (observed_snapshot_fields == expected_snapshot_fields):
        raise _refuse(f"{reprlib.repr(observed_snapshot_fields)=} should match {expected_snapshot_fields=}")

    snapshot_version = snapshot["version"]

    require_type(snapshot_version, int, error_factory=_refuse)

    expected_snapshot_version = 1

    if not (snapshot_version == expected_snapshot_version):
        raise _refuse(f"{snapshot_version=} should match {expected_snapshot_version=}")

    if not (snapshot["openssl_version"] == ssl.OPENSSL_VERSION):
        raise _refuse(f"{reprlib.repr(snapshot['openssl_version'])=} should match {ssl.OPENSSL_VERSION=}")

    settings = snapshot["settings"]

    require_type(settings, dict, error_factory=_refuse)

    observed_setting_fields = set(settings)
    expected_setting_fields = set(_FIELDS)

    if not (observed_setting_fields == expected_setting_fields):
        raise _refuse(f"{reprlib.repr(observed_setting_fields)=} should match {expected_setting_fields=}")

    for name in _FIELDS[:4]:
        setting = settings[name]

        require_type(
            setting,
            (int, ssl.Options, ssl.VerifyFlags, ssl.TLSVersion),
            error_factory=lambda diagnostic: _refuse(f"{name=}; " + diagnostic),
        )

    for name in _FIELDS[4:]:
        setting = settings[name]

        require_type(
            setting,
            bool,
            error_factory=lambda diagnostic: _refuse(f"{name=}; " + diagnostic),
        )

    material_records = snapshot["materials"]

    require_type(material_records, list, error_factory=_refuse)

    if len(material_records) > MAX_TRUST_MATERIALS:
        raise _refuse(f"{len(material_records)=} should be at most {MAX_TRUST_MATERIALS=}")

    materials = []

    for material_record in material_records:
        require_type(material_record, dict, error_factory=_refuse)

        if not ("kind" in material_record):
            raise _refuse(f"{reprlib.repr(set(material_record))=} should contain 'kind'")

        if material_record["kind"] == "capath":
            expected_capath_fields = {"kind", "entries"}

            if not (set(material_record) == expected_capath_fields):
                raise _refuse(f"{reprlib.repr(set(material_record))=} should match {expected_capath_fields=}")

            captured_entries = material_record["entries"]

            require_type(captured_entries, list, error_factory=_refuse)

            if len(captured_entries) > MAX_TRUST_MATERIALS:
                raise _refuse(f"{len(captured_entries)=} should be at most {MAX_TRUST_MATERIALS=}")

            (hashed_trust_files, names) = ([], set())

            for entry in captured_entries:
                require_type(entry, dict, error_factory=_refuse)

                expected_entry_fields = {"name", "data"}

                if not (set(entry) == expected_entry_fields):
                    raise _refuse(f"{reprlib.repr(set(entry))=} should match {expected_entry_fields=}")

                name = entry["name"]

                require_type(name, str, error_factory=_refuse)

                if not _HASHED_TRUST_FILE.fullmatch(name):
                    raise _refuse(f"{reprlib.repr(name)=} should match {_HASHED_TRUST_FILE.pattern=}")

                if not (name not in names):
                    raise _refuse(f"{name=} should be unique")

                names.add(name)
                hashed_trust_files.append((name, _material_bytes(entry["data"])))
                _require_material_budget((*materials, ("capath", tuple(hashed_trust_files))))

            materials.append(("capath", tuple(hashed_trust_files)))
        else:
            expected_data_fields = {"kind", "data"}

            if not (set(material_record) == expected_data_fields):
                raise _refuse(f"{reprlib.repr(set(material_record))=} should match {expected_data_fields=}")

            expected_data_kinds = ["pem", "der", "file"]

            if not (material_record["kind"] in expected_data_kinds):
                raise _refuse(f"{reprlib.repr(material_record['kind'])=} should be one of {expected_data_kinds=}")

            materials.append((material_record["kind"], _material_bytes(material_record["data"])))

        _require_material_budget(materials)

    cipher_expression = snapshot["cipher_expression"]

    require_type(cipher_expression, str, error_factory=_refuse)

    cipher_expression_character_count = len(cipher_expression)
    min_cipher_expression_characters = 1
    max_cipher_expression_characters = 65536

    if not (min_cipher_expression_characters <= cipher_expression_character_count <= max_cipher_expression_characters):
        raise _refuse(
            f"{cipher_expression_character_count=} should be between "
            f"{min_cipher_expression_characters=} and {max_cipher_expression_characters=}"
        )

    alpn = snapshot["alpn"]

    if alpn is not None:
        require_type(alpn, list, error_factory=_refuse)

        for alpn_protocol in alpn:
            require_type(alpn_protocol, str, error_factory=_refuse)

    curve = snapshot["ecdh_curve"]

    if curve is not None:
        require_type(curve, str, error_factory=_refuse)

    context = _SSL_CONTEXT(ssl.PROTOCOL_TLS_CLIENT)
    owned_directory = None

    if any((kind == "capath") for (kind, _certificate_bytes) in materials):
        if directory is None:
            owned_directory = tempfile.TemporaryDirectory(prefix="provider-model-trust-")
            directory = owned_directory.name
            context._chat_directory = owned_directory

            weakref.finalize(context, owned_directory.cleanup)
        else:
            require_type(directory, str, error_factory=_refuse)

            if not pathlib.Path(directory).is_absolute():
                raise _refuse(f"{reprlib.repr(directory)=} should be absolute")

            if not pathlib.Path(directory).is_dir():
                raise _refuse(f"{reprlib.repr(directory)=} should be a directory")

    for (kind, raw) in materials:
        _load_material(context, kind, raw, directory)

    for name in _FIELDS:
        setattr(context, name, settings[name])

    context.set_ciphers(cipher_expression)

    if alpn is not None:
        context.set_alpn_protocols(alpn)

    if curve is not None:
        context.set_ecdh_curve(curve)

    restored_tls_facts = _facts(context)

    if not (restored_tls_facts == snapshot["facts"]):
        raise _refuse(f"{restored_tls_facts=} should match {reprlib.repr(snapshot['facts'])=}")

    for setting_name in _FIELDS:
        restored_setting = getattr(context, setting_name)
        captured_setting = settings[setting_name]

        if not (restored_setting == captured_setting):
            raise _refuse(f"{setting_name=}: {restored_setting=} should match {captured_setting=}")

    return context
