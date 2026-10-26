"""Core operator output preserves owned causes without provider/scientific imports."""

import errno
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stderr

from provider_core.api import ApiFailure
from provider_core.delivery_worker import DeliveryWorkerFailed
from provider_core.delivery_store import DeliveryStorageFailed
from provider_core.diagnostics import failure_evidence, write_stderr
from provider_core.transport import HttpResponse


class DeliveryDiagnosticsTests(unittest.TestCase):
    def test_sqlite_native_code_and_name_survive_without_sql_or_exception_text(self):
        with sqlite3.connect(":memory:") as database:
            try:
                database.execute('SELECT * FROM "PRIVATE_SQL_TABLE"')
            except sqlite3.Error as cause:
                failure = DeliveryStorageFailed("Cannot inspect delivery storage.", cause=cause)
        report = failure_evidence(failure, secrets=())
        rendered = json.dumps(report)
        self.assertIn('"sqlite_errorcode": 1', rendered)
        self.assertIn('"sqlite_errorname": "SQLITE_ERROR"', rendered)
        self.assertNotIn("PRIVATE_SQL_TABLE", rendered)
        self.assertNotIn("SELECT", rendered)

    def test_sqlite_extended_result_codes_remain_exact_and_absent_codes_are_not_invented(self):
        with sqlite3.connect(":memory:") as database:
            database.execute("CREATE TABLE private_values(value UNIQUE)")
            database.execute("INSERT INTO private_values VALUES (1)")
            try:
                database.execute("INSERT INTO private_values VALUES (1)")
            except sqlite3.IntegrityError as failure:
                report = failure_evidence(failure, secrets=())
        self.assertEqual(report["sqlite_errorcode"], sqlite3.SQLITE_CONSTRAINT_UNIQUE)
        self.assertEqual(report["sqlite_errorname"], "SQLITE_CONSTRAINT_UNIQUE")
        unreported = failure_evidence(sqlite3.OperationalError("PRIVATE_SQL_TEXT"), secrets=())
        self.assertNotIn("sqlite_errorcode", unreported)
        self.assertNotIn("sqlite_errorname", unreported)
        self.assertNotIn("PRIVATE_SQL_TEXT", json.dumps(unreported))

    def test_api_effect_and_independent_cleanup_reach_the_core_renderer_without_payloads(self):
        api = ApiFailure("publish Attempt failure", "received API refusal", response=HttpResponse(
            429, ((b"x-request-id", b"request:delivery"), (b"authorization", b"Bearer PRIVATE_HEADER")), b"PRIVATE_BODY"
        ))
        api.attempt_mutation_may_have_committed = True
        cleanup = OSError(errno.EIO, "PRIVATE_NATIVE")
        failure = DeliveryWorkerFailed(cause=api, api_failure=api, cleanup_failures=(cleanup,), api_receipt_received=False)
        report = failure_evidence(failure, secrets=())
        rendered = json.dumps(report)
        self.assertIn('"status": 429', rendered)
        self.assertIn("request:delivery", rendered)
        self.assertIn('"errno": 5', rendered)
        self.assertIn('"attempt_mutation_may_have_committed": true', rendered)
        self.assertNotIn("PRIVATE", rendered)

    def test_unknown_exception_text_and_credential_fields_stay_out_of_operator_output(self):
        unknown = ValueError("PRIVATE_BODY token=sk-PRIVATE_VALUE")
        failure = ApiFailure("publish Attempt failure", 'secret="escaped\\\"PRIVATE_SECRET"; header Bearer PRIVATE_AUTH', cause=unknown)
        rendered = json.dumps(failure_evidence(failure, secrets=("PRIVATE_SECRET",)))
        self.assertNotIn("PRIVATE", rendered)
        self.assertIn("ValueError", rendered)

    def test_full_stderr_pipe_refuses_without_blocking_or_changing_descriptor_policy(self):
        reader, writer = os.pipe()
        try:
            os.set_blocking(writer, False)
            while True:
                try:
                    os.write(writer, b"x" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(writer, True)
            with os.fdopen(writer, "w", closefd=False) as output, redirect_stderr(output):
                with self.assertRaises(BlockingIOError):
                    write_stderr(b"owned diagnostic\n")
            self.assertTrue(os.get_blocking(writer))
        finally:
            os.close(writer)
            os.close(reader)
