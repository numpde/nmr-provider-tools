"""A model child receives exactly the admitted TLS material and supported policy.

Memory BIO handshakes exercise certificate/hostname verification without opening
a network socket. Real socket, SNI, proxy and HTTP qualification lives alongside
the child transport integration scenarios.
"""

import base64
from datetime import UTC, datetime, timedelta
import gc
import json
from pathlib import Path
import ssl
import tempfile
import unittest
import weakref
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from provider_core.api_config import https_trust
from provider_core.chat_tls import CapturedSSLContext, TLSCaptureRejected, capture_trust, restore_trust


def certificate_material():
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "captured-model-fixture")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("model.fixture")]), critical=False)
        .sign(key, None)
    )
    crl = (
        x509.CertificateRevocationListBuilder().issuer_name(name)
        .last_update(now - timedelta(minutes=1)).next_update(now + timedelta(hours=1))
        .sign(key, None)
    )
    return (certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()),
            crl.public_bytes(serialization.Encoding.PEM))


def handshake(client_context, certificate, key, hostname):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "cert.pem").write_bytes(certificate)
        (root / "key.pem").write_bytes(key)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(root / "cert.pem", root / "key.pem")
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(20):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            # silent-defaults: allow-contract -- This incomplete handshake requires the next BIO input; completion is asserted only after both peers finish.
            except ssl.SSLWantReadError:
                pass
        if client_out.pending:
            server_in.write(client_out.read())
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            # silent-defaults: allow-contract -- This incomplete handshake requires the next BIO input; completion is asserted only after both peers finish.
            except ssl.SSLWantReadError:
                pass
        if server_out.pending:
            client_in.write(server_out.read())
        if client_done and server_done:
            return client.version()
    raise AssertionError("Memory TLS handshake did not complete")


class CapturedModelTrustTests(unittest.TestCase):
    def setUp(self):
        self.certificate, self.key, self.crl = certificate_material()

    def test_non_ca_leaf_survives_json_transfer_and_enforces_hostname(self):
        trust = https_trust(self.certificate, label="fixture")
        leaf_der = ssl.PEM_cert_to_DER_cert(self.certificate.decode("ascii"))
        self.assertNotIn(leaf_der, trust.get_ca_certs(binary_form=True))
        snapshot = json.loads(json.dumps(capture_trust(trust)))
        self.assertEqual(base64.b64decode(snapshot["materials"][0]["data"]), self.certificate)
        restored = restore_trust(snapshot)
        self.assertEqual(restored.cert_store_stats(), trust.cert_store_stats())
        self.assertIn(handshake(restored, self.certificate, self.key, "model.fixture"), ("TLSv1.2", "TLSv1.3"))
        with self.assertRaises(ssl.SSLCertVerificationError):
            handshake(restored, self.certificate, self.key, "wrong.fixture")

    def test_default_builder_keeps_legacy_public_settings_with_selected_fixed_bundle(self):
        legacy = ssl.create_default_context()
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle.pem"
            bundle.write_bytes(self.certificate)
            selected = ssl.DefaultVerifyPaths(str(bundle), None, "SSL_CERT_FILE", str(bundle), "SSL_CERT_DIR", directory)
            with patch("provider_core.chat_tls.ssl.get_default_verify_paths", return_value=selected):
                captured = https_trust(None, label="fixture")
        for name in ("options", "minimum_version", "maximum_version", "verify_flags",
                     "verify_mode", "check_hostname", "hostname_checks_common_name", "post_handshake_auth"):
            self.assertEqual(getattr(captured, name), getattr(legacy, name), msg=name)
        self.assertEqual(captured.get_ciphers(), legacy.get_ciphers())
        restored = restore_trust(capture_trust(captured))
        self.assertIn(handshake(restored, self.certificate, self.key, "model.fixture"), ("TLSv1.2", "TLSv1.3"))

    def test_stricter_native_runtime_defaults_are_preserved_without_changing_explicit_pem_policy(self):
        reference = ssl.create_default_context()
        reference.verify_flags |= ssl.VERIFY_X509_STRICT | ssl.VERIFY_X509_PARTIAL_CHAIN
        reference.minimum_version = ssl.TLSVersion.TLSv1_3
        reference.hostname_checks_common_name = False
        reference.post_handshake_auth = True
        reference.set_ciphers("ECDHE-RSA-AES128-GCM-SHA256:@SECLEVEL=2")
        with patch("provider_core.chat_tls.ssl.create_default_context", return_value=reference):
            captured = https_trust(None, label="fixture")
            explicit = https_trust(self.certificate, label="fixture")
        for context in (captured, restore_trust(capture_trust(captured))):
            for name in ("options", "minimum_version", "maximum_version", "verify_flags",
                         "verify_mode", "check_hostname", "hostname_checks_common_name", "post_handshake_auth"):
                self.assertEqual(getattr(context, name), getattr(reference, name), msg=name)
            self.assertEqual(context.get_ciphers(), reference.get_ciphers())
            self.assertEqual(context.security_level, reference.security_level)
        constructor_policy = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(explicit.verify_flags, constructor_policy.verify_flags)
        self.assertEqual(explicit.minimum_version, constructor_policy.minimum_version)

    def test_captured_bundle_and_redundant_hash_files_survive_path_removal_with_crl(self):
        trust = CapturedSSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle.pem"
            bundle.write_bytes(self.certificate + self.crl)
            (root / "01234567.0").write_bytes(self.certificate)
            (root / "01234567.r0").write_bytes(self.crl)
            trust.load_verify_locations(cafile=bundle, capath=root)
            snapshot = json.loads(json.dumps(capture_trust(trust)))
        restored = restore_trust(snapshot)
        self.assertEqual(restored.cert_store_stats(), trust.cert_store_stats())
        self.assertEqual(1, restored.cert_store_stats()["crl"])
        self.assertIn(handshake(restored, self.certificate, self.key, "model.fixture"), ("TLSv1.2", "TLSv1.3"))

    def test_lazy_hash_names_and_suffix_gaps_do_not_become_eager_trust(self):
        # 44a46a84 is OpenSSL's subject-name hash for this fixed fixture DN;
        # its generated key is not an input to the subject-name hash.
        for name in ("FFFFFFFF.0", "44a46a84.1"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / name).write_bytes(self.certificate)
                trust = CapturedSSLContext(ssl.PROTOCOL_TLS_CLIENT)
                trust.load_verify_locations(capath=root)
                snapshot = json.loads(json.dumps(capture_trust(trust)))
                for context in (trust, restore_trust(snapshot)):
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        handshake(context, self.certificate, self.key, "model.fixture")

    def test_lazy_only_trust_survives_source_removal_and_a_parent_cache_fill(self):
        trust = CapturedSSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "44a46a84.0").write_bytes(self.certificate)
            trust.load_verify_locations(capath=root)
            self.assertIn(handshake(trust, self.certificate, self.key, "model.fixture"), ("TLSv1.2", "TLSv1.3"))
            snapshot = json.loads(json.dumps(capture_trust(trust)))
        with tempfile.TemporaryDirectory() as grant:
            restored = restore_trust(snapshot, directory=grant)
            files = list(Path(grant).glob("capath-*/*"))
            self.assertEqual([path.name for path in files], ["44a46a84.0"])
            self.assertEqual(files[0].read_bytes(), self.certificate)
            self.assertIn(handshake(restored, self.certificate, self.key, "model.fixture"), ("TLSv1.2", "TLSv1.3"))

    def test_representability_check_cleans_its_directory_and_standalone_context_owns_its_clone(self):
        trust = CapturedSSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "44a46a84.0").write_bytes(self.certificate)
            trust.load_verify_locations(capath=root)
        created = []
        native_directory = tempfile.TemporaryDirectory

        def observed_directory(*args, **kwargs):
            result = native_directory(*args, **kwargs)
            created.append(Path(result.name))
            return result

        with patch("provider_core.chat_tls.tempfile.TemporaryDirectory", side_effect=observed_directory):
            snapshot = capture_trust(trust)
        self.assertEqual(1, len(created))
        self.assertFalse(created[0].exists())
        restored = restore_trust(snapshot)
        restored_directory = Path(restored._chat_directory.name)
        self.assertTrue(restored_directory.is_dir())
        reference = weakref.ref(restored)
        del restored
        gc.collect()
        self.assertIsNone(reference())
        self.assertFalse(restored_directory.exists())

    def test_trusted_certificate_reject_attributes_are_preserved_in_lazy_material(self):
        # X509_CERT_AUX: SEQUENCE { reject [0] { id-kp-serverAuth } }.
        # Identical certificate DER with this AUX suffix rejects server use.
        der = ssl.PEM_cert_to_DER_cert(self.certificate.decode("ascii"))
        encoded = base64.b64encode(der + bytes.fromhex("300ca00a06082b06010505070301"))
        trusted = b"-----BEGIN TRUSTED CERTIFICATE-----\n" + encoded + b"\n-----END TRUSTED CERTIFICATE-----\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "44a46a84.0").write_bytes(trusted)
            trust = CapturedSSLContext(ssl.PROTOCOL_TLS_CLIENT)
            trust.load_verify_locations(capath=root)
            snapshot = json.loads(json.dumps(capture_trust(trust)))
            self.assertEqual(base64.b64decode(snapshot["materials"][0]["entries"][0]["data"]), trusted)
            with self.assertRaises(ssl.SSLCertVerificationError) as original:
                handshake(trust, self.certificate, self.key, "model.fixture")
        restored = restore_trust(snapshot)
        with self.assertRaises(ssl.SSLCertVerificationError) as replayed:
            handshake(restored, self.certificate, self.key, "model.fixture")
        self.assertEqual(replayed.exception.verify_code, original.exception.verify_code)

    def test_boolean_version_is_not_an_integer_protocol_version(self):
        snapshot = capture_trust(https_trust(self.certificate, label="fixture"))
        snapshot["version"] = True
        with self.assertRaisesRegex(TLSCaptureRejected, "schema"):
            restore_trust(snapshot)

    def test_message_callback_and_loaded_client_key_are_explicitly_unsupported(self):
        trust = https_trust(self.certificate, label="fixture")
        trust._msg_callback = lambda *args: None
        with self.assertRaisesRegex(TLSCaptureRejected, "callbacks"):
            capture_trust(trust)
        trust._msg_callback = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cert.pem").write_bytes(self.certificate)
            (root / "key.pem").write_bytes(self.key)
            trust.load_cert_chain(root / "cert.pem", root / "key.pem")
        with self.assertRaisesRegex(TLSCaptureRejected, "client certificates"):
            capture_trust(trust)


if __name__ == "__main__":
    unittest.main()
