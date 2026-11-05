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
from pathlib import Path
import re
import ssl
import tempfile
import weakref


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
    "options", "verify_flags", "minimum_version", "maximum_version",
    "hostname_checks_common_name", "post_handshake_auth",
)


class TLSCaptureRejected(ValueError):
    """This context cannot be reproduced without changing its granted authority."""


def _refuse(reason):
    return TLSCaptureRejected(
        "Cannot isolate model HTTPS with this TLS context: " + reason
        + ". Build model trust with provider_core.api_config.https_trust and supported explicit TLS settings."
    )


def _bounded_file(path):
    with open(path, "rb") as stream:
        material = stream.read(MAX_TRUST_BYTES + 1)
    if len(material) > MAX_TRUST_BYTES:
        raise _refuse("captured trust material exceeds the 4 MiB allowance")
    return material


def _file_materials(cafile, capath):
    materials = []
    if cafile is not None:
        materials.append(("file", _bounded_file(cafile)))
    if capath is not None:
        # Preserve names and directory groups. Eager cafile loading would grant
        # authority to wrong hash names and suffixes beyond native lookup gaps.
        entries = []
        for entry in Path(capath).iterdir():
            if _HASHED_TRUST_FILE.fullmatch(entry.name) is not None:
                entries.append(entry)
                if len(entries) > MAX_TRUST_MATERIALS:
                    raise _refuse("the configured hashed trust directory exceeds its fixed allowance")
        entries.sort(key=lambda item: item.name)
        captured = []
        for entry in entries:
            if not entry.is_file():
                raise _refuse("a configured hashed trust entry is not a readable regular file")
            captured.append((entry.name, _bounded_file(entry)))
            _require_material_budget((*materials, ("capath", tuple(captured))))
        materials.append(("capath", tuple(captured)))
    _require_material_budget(materials)
    return tuple(materials)


def _require_material_budget(materials):
    count = len(materials)
    total = 0
    for kind, value in materials:
        if kind == "capath":
            count += len(value)
            total += sum(len(raw) for _name, raw in value)
        else:
            total += len(value)
    if count > MAX_TRUST_MATERIALS or total > MAX_TRUST_BYTES:
        raise _refuse("captured trust material exceeds its fixed allowance")


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
            raise _refuse("this platform cannot load captured file trust without retained filesystem paths")
        descriptor = os.memfd_create("provider-model-trust", os.MFD_CLOEXEC)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
            os.lseek(descriptor, 0, os.SEEK_SET)
            _SSL_CONTEXT.load_verify_locations(context, cafile=f"/proc/self/fd/{descriptor}")
        finally:
            os.close(descriptor)
    elif kind == "capath":
        # The caller owns this directory until the context or HTTP child has
        # retired. SIGKILL cannot strand files: child grants are removed by the
        # parent only after it confirms process and IPC retirement.
        captured = Path(tempfile.mkdtemp(prefix="capath-", dir=directory))
        for name, content in raw:
            with (captured / name).open("xb") as stream:
                stream.write(content)
        _SSL_CONTEXT.load_verify_locations(context, capath=str(captured))
    else:
        raise _refuse("the captured trust material kind is unsupported")


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
            elif isinstance(cadata, (bytes, bytearray, memoryview)):
                materials.append(("der", bytes(cadata)))
            else:
                raise TypeError("CA data must be ASCII PEM text or DER bytes")
        _require_material_budget((*self._chat_materials, *materials))
        if not materials:
            return super().load_verify_locations(cafile=cafile, capath=capath, cadata=cadata)
        try:
            for kind, raw in materials:
                if kind == "capath" and self._chat_directory is None:
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
        if any(name in os.environ for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE")):
            raise _refuse("ambient TLS configuration is not an explicit trust grant")
        if os.name != "posix":
            raise _refuse("this platform's system trust store cannot be captured")
        paths = ssl.get_default_verify_paths()
        if paths.cafile is not None or paths.capath is not None:
            self.load_verify_locations(cafile=paths.cafile, capath=paths.capath)

    # silent-defaults: allow-contract -- Server authentication is SSLContext's normal client-root purpose.
    def load_default_certs(self, purpose=ssl.Purpose.SERVER_AUTH):
        if any(name in os.environ for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE")):
            raise _refuse("ambient TLS configuration is not an explicit trust grant")
        if os.name != "posix":
            raise _refuse("this platform's system trust store cannot be captured")
        # CPython's implementation selects its platform fallback bundle/path
        # before calling set_default_verify_paths. Both calls dispatch through
        # captured loaders; lazy lookups use only the owned captured directory.
        return super().load_default_certs(purpose)

    def set_alpn_protocols(self, protocols):
        captured = tuple(protocols)
        result = super().set_alpn_protocols(captured)
        self._chat_alpn = captured
        return result

    def set_ecdh_curve(self, curve_name):
        result = super().set_ecdh_curve(curve_name)
        self._chat_ecdh_curve = curve_name
        return result

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
        if (any(getattr(context, name) != getattr(reference, name) for name in _FIELDS)
                or context.get_ciphers() != reference.get_ciphers()
                or context.security_level != reference.security_level):
            raise _refuse("the runtime's default TLS policy cannot be reproduced exactly")
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


def _material_message(kind, value):
    if kind == "capath":
        return {"kind": kind, "entries": [
            {"name": name, "data": base64.b64encode(raw).decode("ascii")} for name, raw in value
        ]}
    return {"kind": kind, "data": base64.b64encode(value).decode("ascii")}


def capture_trust(context):
    """Freeze supported context state; refuse authority that cannot be copied."""
    if type(context) is not CapturedSSLContext:
        raise _refuse("the context has no captured original trust material")
    if context._chat_opaque is not None:
        raise _refuse(context._chat_opaque)
    if (context.protocol != ssl.PROTOCOL_TLS_CLIENT or context.verify_mode != ssl.CERT_REQUIRED
            or not context.check_hostname):
        raise _refuse("certificate and hostname verification must remain enabled")
    if (context.sni_callback is not None or context._msg_callback is not None or context.keylog_filename is not None
            or context.sslsocket_class is not ssl.SSLSocket or context.sslobject_class is not ssl.SSLObject):
        raise _refuse("TLS callbacks, key logging or custom socket classes are not transferable")
    facts = _facts(context)
    snapshot = {
        "version": 1,
        "openssl_version": ssl.OPENSSL_VERSION,
        "materials": [_material_message(kind, value) for kind, value in context._chat_materials],
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


def _material_bytes(value):
    if type(value) is not str or len(value) > ((MAX_TRUST_BYTES + 2) // 3) * 4:
        raise _refuse("captured trust material exceeds its fixed allowance")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError) as cause:
        raise _refuse("captured trust material is not valid base64") from cause


# A standalone context owns its directory; a child receives a parent-owned grant.
def restore_trust(snapshot, *, directory=None):
    """Recreate captured TLS without reading default roots or original paths."""
    expected = {"version", "openssl_version", "materials", "settings", "cipher_expression", "alpn", "ecdh_curve", "facts"}
    if (type(snapshot) is not dict or set(snapshot) != expected
            or type(snapshot["version"]) is not int or snapshot["version"] != 1
            or snapshot["openssl_version"] != ssl.OPENSSL_VERSION):
        raise _refuse("the captured TLS schema or OpenSSL runtime does not match")
    settings = snapshot["settings"]
    if type(settings) is not dict or set(settings) != set(_FIELDS):
        raise _refuse("the captured verification settings are incomplete")
    if (any(type(settings[name]) not in (int, ssl.Options, ssl.VerifyFlags, ssl.TLSVersion)
            for name in _FIELDS[:4])
            or any(type(settings[name]) is not bool for name in _FIELDS[4:])):
        raise _refuse("the captured verification settings are invalid")
    material_records = snapshot["materials"]
    if type(material_records) is not list or len(material_records) > MAX_TRUST_MATERIALS:
        raise _refuse("the captured trust material collection is invalid")
    materials = []
    for item in material_records:
        if type(item) is not dict or "kind" not in item:
            raise _refuse("a captured trust material record is invalid")
        if item["kind"] == "capath":
            if (set(item) != {"kind", "entries"} or type(item["entries"]) is not list
                    or len(item["entries"]) > MAX_TRUST_MATERIALS):
                raise _refuse("a captured hashed trust collection is invalid")
            entries, names = [], set()
            for entry in item["entries"]:
                if (type(entry) is not dict or set(entry) != {"name", "data"}
                        or type(entry["name"]) is not str
                        or _HASHED_TRUST_FILE.fullmatch(entry["name"]) is None
                        or entry["name"] in names):
                    raise _refuse("a captured hashed trust name is invalid or repeated")
                names.add(entry["name"])
                entries.append((entry["name"], _material_bytes(entry["data"])))
                _require_material_budget((*materials, ("capath", tuple(entries))))
            materials.append(("capath", tuple(entries)))
        elif set(item) == {"kind", "data"} and item["kind"] in ("pem", "der", "file"):
            materials.append((item["kind"], _material_bytes(item["data"])))
        else:
            raise _refuse("a captured trust material record is invalid")
        _require_material_budget(materials)
    _require_material_budget(materials)
    cipher_expression = snapshot["cipher_expression"]
    if type(cipher_expression) is not str or not 0 < len(cipher_expression) <= 65536:
        raise _refuse("the captured cipher selection is invalid")
    alpn = snapshot["alpn"]
    if alpn is not None and (type(alpn) is not list or any(type(value) is not str for value in alpn)):
        raise _refuse("the captured ALPN selection is invalid")
    curve = snapshot["ecdh_curve"]
    if curve is not None and type(curve) is not str:
        raise _refuse("the captured ECDH selection is invalid")
    context = _SSL_CONTEXT(ssl.PROTOCOL_TLS_CLIENT)
    owned_directory = None
    if any(kind == "capath" for kind, _value in materials):
        if directory is None:
            owned_directory = tempfile.TemporaryDirectory(prefix="provider-model-trust-")
            directory = owned_directory.name
            context._chat_directory = owned_directory
            weakref.finalize(context, owned_directory.cleanup)
        elif not (type(directory) is str and Path(directory).is_absolute() and Path(directory).is_dir()):
            raise _refuse("the captured trust directory grant is invalid")
    for kind, raw in materials:
        _load_material(context, kind, raw, directory)
    for name in _FIELDS:
        setattr(context, name, settings[name])
    context.set_ciphers(cipher_expression)
    if alpn is not None:
        context.set_alpn_protocols(alpn)
    if curve is not None:
        context.set_ecdh_curve(curve)
    if (_facts(context) != snapshot["facts"]
            or any(getattr(context, name) != settings[name] for name in _FIELDS)):
        raise _refuse("the original TLS authority or cipher settings cannot be reproduced exactly")
    return context
