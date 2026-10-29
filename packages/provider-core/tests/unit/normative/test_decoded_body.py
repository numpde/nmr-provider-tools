"""Qualify bounded coding semantics, not HTTP I/O, status or retirement policy."""
import gzip
import io
import sys
import tracemalloc
import unittest
import zlib

from provider_core.decoded_body import DecodedBody, DecodedBodyRejected


# silent-defaults: allow-contract -- These fixed gzip/size/fragment defaults define the ordinary codec specimen; fault tests override them.
def decoded(wire, *, coding=b"gzip", limit=8192, chunk=7):
    # silent-defaults: allow-contract -- None models an absent Content-Encoding header in this specimen.
    headers = () if coding is None else ((b"Content-Encoding", coding),)
    decoder = DecodedBody(headers, maximum_bytes=limit)
    result = bytearray()
    for offset in range(0, len(wire), chunk):
        result.extend(decoder.feed(wire[offset:offset + chunk]))
        result.extend(decoder.feed(b""))
    decoder.finish()
    return bytes(result)


class DecodedBodyTests(unittest.TestCase):
    def test_identity_without_header_and_explicit_identity_share_the_limit(self):
        for coding in (None, b"identity"):
            self.assertEqual(decoded(b"data", coding=coding, limit=4), b"data")
            with self.assertRaises(DecodedBodyRejected) as caught:
                decoded(b"data!", coding=coding, limit=4)
            self.assertEqual(caught.exception.reason, "size_limit")

    def test_complete_concatenated_and_empty_gzip_members_survive_fragmentation(self):
        wire = gzip.compress(b'{"answer":') + gzip.compress(b"") + gzip.compress(b'1}')
        for chunk in (1, 2, 7, len(wire)):
            with self.subTest(chunk=chunk):
                self.assertEqual(decoded(wire, chunk=chunk), b'{"answer":1}')
        self.assertEqual(decoded(gzip.compress(b""), limit=1, chunk=1), b"")

    def test_exact_limit_accepts_the_trailer_and_next_byte_rejects(self):
        for chunk in (1, 2, 65536):
            with self.subTest(chunk=chunk):
                self.assertEqual(decoded(gzip.compress(b"abc"), limit=3, chunk=chunk), b"abc")
                with self.assertRaises(DecodedBodyRejected) as caught:
                    decoded(gzip.compress(b"abcd"), limit=3, chunk=chunk)
                self.assertEqual(caught.exception.reason, "size_limit")

    def test_every_incomplete_gzip_prefix_is_rejected(self):
        wire = gzip.compress(b'{"answer":1}')
        for size in range(len(wire)):
            with self.subTest(size=size), self.assertRaises(DecodedBodyRejected) as caught:
                decoded(wire[:size], chunk=1)
            self.assertEqual(caught.exception.reason, "invalid_encoding")

    def test_corrupt_crc_and_size_retain_the_actual_zlib_cause(self):
        wire = gzip.compress(b"answer")
        for position in (-8, -4):
            corrupt = bytearray(wire)
            corrupt[position] ^= 1
            with self.subTest(position=position), self.assertRaises(DecodedBodyRejected) as caught:
                decoded(bytes(corrupt))
            self.assertEqual(caught.exception.reason, "invalid_encoding")
            self.assertIsInstance(caught.exception.__cause__, zlib.error)

    def test_nonmember_trailing_bytes_including_padding_are_rejected(self):
        for tail in (b"junk", b"\0", b"\x1f"):
            with self.subTest(tail=tail), self.assertRaises(DecodedBodyRejected) as caught:
                decoded(gzip.compress(b"answer") + tail, chunk=1)
            self.assertEqual(caught.exception.reason, "invalid_encoding")

    def test_complete_coding_stack_is_reversed_and_bounded_to_four(self):
        body = b"answer"
        wire = body
        for _ in range(4):
            wire = gzip.compress(wire)
        self.assertEqual(decoded(wire, coding=b"gzip, GZip, gzip, gzip", chunk=1), body)
        self.assertEqual(decoded(gzip.compress(body), coding=b"gzip, identity"), body)
        with self.assertRaises(DecodedBodyRejected) as caught:
            DecodedBody(((b"content-encoding", b"identity,identity,identity,identity,identity"),), maximum_bytes=8192)
        self.assertEqual(caught.exception.reason, "unsupported_encoding")

    def test_duplicate_header_fields_preserve_coding_order(self):
        body = b"answer"
        decoder = DecodedBody(((b"Content-Encoding", b"gzip"), (b"content-encoding", b"identity")), maximum_bytes=8192)
        self.assertEqual(decoder.feed(gzip.compress(body)), body)
        decoder.finish()

    def test_intermediate_metadata_can_exceed_the_cap_while_final_body_would_fit(self):
        stream = io.BytesIO()
        with gzip.GzipFile(fileobj=stream, mode="wb", filename="m" * 1024, mtime=0) as member:
            member.write(b"answer")
        intermediate = stream.getvalue()
        self.assertGreater(len(intermediate), 256)
        self.assertEqual(gzip.decompress(intermediate), b"answer")
        with self.assertRaises(DecodedBodyRejected) as caught:
            decoded(gzip.compress(intermediate), coding=b"gzip,gzip", limit=256)
        self.assertEqual(caught.exception.reason, "size_limit")

    def test_inflation_never_allocates_the_complete_oversized_entity(self):
        wire = gzip.compress(b"x" * (16 * 1024 * 1024))
        decoder = DecodedBody(((b"content-encoding", b"gzip"),), maximum_bytes=32)
        tracemalloc.start()
        try:
            with self.assertRaises(DecodedBodyRejected) as caught:
                decoder.feed(wire)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(caught.exception.reason, "size_limit")
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_closed_rejections_do_not_render_private_metadata(self):
        for coding in (b"private-coding-sentinel", b"", b"gzip,", b"\x0bgzip"):
            with self.subTest(coding=coding), self.assertRaises(DecodedBodyRejected) as caught:
                DecodedBody(((b"content-encoding", coding),), maximum_bytes=32)
            self.assertEqual(caught.exception.reason, "unsupported_encoding")
            self.assertNotIn("private-coding-sentinel", str(caught.exception))
        for reason in ("private-reason-sentinel", [], None):
            with self.assertRaises(ValueError):
                DecodedBodyRejected(reason)

    def test_configuration_errors_and_reuse_are_local_defects(self):
        for limit in (0, -1, True, 1.5, sys.maxsize, sys.maxsize + 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                DecodedBody((), maximum_bytes=limit)
        decoder = DecodedBody((), maximum_bytes=1)
        self.assertEqual(decoder.feed(b"x"), b"x")
        decoder.finish()
        with self.assertRaises(RuntimeError):
            decoder.feed(b"")
        with self.assertRaises(RuntimeError):
            decoder.finish()
        failed = DecodedBody((), maximum_bytes=1)
        with self.assertRaises(DecodedBodyRejected):
            failed.feed(b"xx")
        with self.assertRaises(RuntimeError):
            failed.finish()


class DeflateTests(unittest.TestCase):
    def raw(self, body):
        encoder = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        return encoder.compress(body) + encoder.flush()

    def test_wrapper_decision_is_independent_of_header_fragmentation(self):
        body = b"answer"
        for wire in (zlib.compress(body), self.raw(body)):
            for chunk in (1, 2, 3, len(wire)):
                with self.subTest(wire=wire, chunk=chunk):
                    self.assertEqual(decoded(wire, coding=b"deflate", chunk=chunk), body)
        for wire in (zlib.compress(b""), self.raw(b"")):
            self.assertEqual(decoded(wire, coding=b"deflate", limit=1, chunk=1), b"")

    def test_zlib_looking_raw_stream_is_an_explicit_profile_rejection(self):
        wire = b"\x78\x9c\x00\x63\xff" + b"x" * 156 + b"\x01\x00\x00\xff\xff"
        self.assertEqual(zlib.decompress(wire, -zlib.MAX_WBITS), b"x" * 156)
        for chunk in (1, 2, 7, len(wire)):
            with self.subTest(chunk=chunk), self.assertRaises(DecodedBodyRejected) as caught:
                decoded(wire, coding=b"deflate", chunk=chunk)
            self.assertEqual(caught.exception.reason, "invalid_encoding")
            self.assertIsInstance(caught.exception.__cause__, zlib.error)

    def test_exact_limit_and_overflow_for_both_formats(self):
        for encode in (zlib.compress, self.raw):
            self.assertEqual(decoded(encode(b"abc"), coding=b"deflate", limit=3, chunk=1), b"abc")
            with self.assertRaises(DecodedBodyRejected) as caught:
                decoded(encode(b"abcd"), coding=b"deflate", limit=3, chunk=1)
            self.assertEqual(caught.exception.reason, "size_limit")

    def test_truncated_streams_and_trailing_bytes_are_rejected(self):
        for encode in (zlib.compress, self.raw):
            wire = encode(b"answer")
            for size in range(len(wire)):
                with self.subTest(size=size, wire=wire), self.assertRaises(DecodedBodyRejected) as caught:
                    decoded(wire[:size], coding=b"deflate", chunk=1)
                self.assertEqual(caught.exception.reason, "invalid_encoding")
            with self.assertRaises(DecodedBodyRejected) as caught:
                decoded(wire + b"junk", coding=b"deflate", chunk=1)
            self.assertEqual(caught.exception.reason, "invalid_encoding")

    def test_bad_wrapped_integrity_never_retries_as_raw(self):
        wire = bytearray(zlib.compress(b"answer"))
        wire[-1] ^= 1
        with self.assertRaises(DecodedBodyRejected) as caught:
            decoded(bytes(wire), coding=b"deflate", chunk=1)
        self.assertEqual(caught.exception.reason, "invalid_encoding")
        self.assertIsInstance(caught.exception.__cause__, zlib.error)

    def test_mixed_coding_order(self):
        body = b"answer"
        self.assertEqual(decoded(zlib.compress(gzip.compress(body)), coding=b"gzip,deflate", chunk=1), body)
        self.assertEqual(decoded(gzip.compress(self.raw(body)), coding=b"deflate,gzip", chunk=1), body)

    def test_inflation_never_allocates_the_complete_entity(self):
        for encode in (zlib.compress, self.raw):
            wire = encode(b"x" * (16 * 1024 * 1024))
            decoder = DecodedBody(((b"content-encoding", b"deflate"),), maximum_bytes=32)
            tracemalloc.start()
            try:
                with self.assertRaises(DecodedBodyRejected) as caught:
                    decoder.feed(wire)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertEqual(caught.exception.reason, "size_limit")
            self.assertLess(peak, 2 * 1024 * 1024)

    def test_unexpected_local_failure_is_preserved_as_a_local_defect(self):
        from unittest.mock import patch
        failure = RuntimeError("Injected codec adapter defect")
        decoder = DecodedBody(((b"content-encoding", b"deflate"),), maximum_bytes=32)
        with patch("provider_core.decoded_body.zlib.decompressobj", side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                decoder.feed(zlib.compress(b"answer"))
        self.assertIs(caught.exception, failure)
        with self.assertRaises(RuntimeError):
            decoder.finish()
