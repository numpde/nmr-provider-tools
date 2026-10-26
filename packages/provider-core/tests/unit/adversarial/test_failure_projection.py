"""Unreviewed exceptions cannot acquire a disclosure grant by shape or inheritance."""

import unittest
from builtins import ExceptionGroup

from provider_core.api import ApiFailure
from provider_core.failure_projection import owned_details, owned_relationships


class CoreFailureProjectionRefusalTests(unittest.TestCase):
    def test_unknown_and_subclass_objects_are_not_stringified_or_introspected(self):
        class HostileError(RuntimeError):
            def __str__(self):
                raise AssertionError("Unknown text was inspected")

            @property
            def cause(self):
                raise AssertionError("Unknown cause was inspected")

        class HostileApiSubclass(ApiFailure):
            def __str__(self):
                raise AssertionError("Unreviewed subclass text was inspected")

        for failure in (HostileError(), HostileApiSubclass("unknown", "PRIVATE")):
            with self.subTest(kind=type(failure)):
                self.assertEqual(owned_details(failure), {})
                self.assertEqual(list(owned_relationships(failure)), [])

    def test_standard_cause_context_and_groups_stay_with_the_consumer_walker(self):
        failure = ApiFailure("read state", "no admitted response")
        failure.__cause__ = ValueError("PRIVATE_CAUSE")
        failure.__context__ = TypeError("PRIVATE_CONTEXT")
        self.assertEqual(list(owned_relationships(failure)), [("owned cause", None)])
        group = ExceptionGroup("PRIVATE_GROUP", (failure,))
        self.assertEqual(owned_details(group), {})
        self.assertEqual(list(owned_relationships(group)), [])
