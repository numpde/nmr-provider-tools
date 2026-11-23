"""Check the three type requirements and their source-aware failures."""

import linecache
import unittest
from collections.abc import Mapping
from functools import partial
from unittest.mock import Mock, patch

from provider_core.payload import PayloadRejected
from provider_core.type_requirements import require_instance, require_subclass, require_type


class TypeRequirementsTests(unittest.TestCase):
    def test_exact_type_accepts_single_type_and_tuple(self):
        self.assertIsNone(require_type(12, int))
        self.assertIsNone(require_type(12, (int, float)))
        self.assertIsNone(require_type(3.5, (int, float)))

    def test_exact_type_rejects_subclasses(self):
        with self.assertRaises(TypeError) as raised:
            require_type(True, int)

        self.assertIn("True", str(raised.exception))
        self.assertIn("bool", str(raised.exception))
        self.assertIn("int", str(raised.exception))

    def test_exact_type_tuple_message(self):
        text_fields = {"value": "hello"}

        with self.assertRaises(TypeError) as raised:
            require_type(text_fields["value"], (int, float))

        self.assertIn('text_fields["value"]', str(raised.exception))
        self.assertIn("int", str(raised.exception))
        self.assertIn("float", str(raised.exception))
        self.assertIn("str", str(raised.exception))

    def test_instance_accepts_subclasses_and_tuple(self):
        self.assertIsNone(require_instance(True, int))
        self.assertIsNone(require_instance(True, (int, float)))

    def test_instance_accepts_abstract_base_class(self):
        self.assertIsNone(require_instance({}, Mapping))

        with self.assertRaises(TypeError):
            require_type({}, Mapping)

    def test_instance_reports_source_expression(self):
        text_fields = {"value": "hello"}

        with self.assertRaises(TypeError) as raised:
            require_instance(text_fields["value"], (int, float))

        self.assertIn('text_fields["value"]', str(raised.exception))
        self.assertIn("instance of", str(raised.exception))
        self.assertIn("int", str(raised.exception))
        self.assertIn("float", str(raised.exception))
        self.assertIn("str", str(raised.exception))

    def test_subclass_accepts_inheritance_and_self(self):
        class Base:
            pass

        class Derived(Base):
            pass

        self.assertIsNone(require_subclass(Derived, Base))
        self.assertIsNone(require_subclass(Derived, (Base, str)))
        self.assertIsNone(require_subclass(Base, Base))

    def test_subclass_rejects_unrelated_classes(self):
        with self.assertRaises(TypeError) as raised:
            require_subclass(str, (int, float))

        self.assertIn("subclass of", str(raised.exception))
        self.assertIn("str", str(raised.exception))
        self.assertIn("int", str(raised.exception))
        self.assertIn("float", str(raised.exception))

    def test_subclass_rejects_nonclass_first_argument(self):
        text_instance = "hello"

        with self.assertRaises(TypeError) as raised:
            require_subclass(text_instance, str)

        self.assertIn("text_instance", str(raised.exception))

    def test_subclass_accepts_abstract_base_class(self):
        self.assertIsNone(require_subclass(dict, Mapping))

    def test_keyword_arguments(self):
        text_fields = {"value": "hello"}

        with self.assertRaises(TypeError) as raised:
            require_type(T=int, expr=text_fields["value"])

        self.assertIn('text_fields["value"]', str(raised.exception))

        with self.assertRaises(TypeError) as raised:
            require_instance(T=int, expr=text_fields["value"])

        self.assertIn('text_fields["value"]', str(raised.exception))
        self.assertIn("instance of", str(raised.exception))

    def test_multiline_call(self):
        text_fields = {"value": "hello"}

        with self.assertRaises(TypeError) as raised:
            require_instance(
                text_fields.get("value"),
                (int, float),
            )

        self.assertIn('text_fields.get("value")', str(raised.exception))

    def test_multiple_calls_on_one_line(self):
        def error(action):
            try:
                action()
            except TypeError as failure:
                return str(failure)

            self.fail("Expected TypeError")

        first = "a"
        second = "b"
        one = error(lambda: require_type(first, int)); two = error(lambda: require_instance(second, int))

        self.assertIn("first", one)
        self.assertNotIn("second", one)
        self.assertIn("second", two)
        self.assertNotIn("first", two)

    def test_source_unavailable_uses_repr(self):
        with patch.object(linecache, "getlines", return_value=[]):
            with self.assertRaises(TypeError) as raised:
                require_type("hello", int)

        self.assertIn("'hello'", str(raised.exception))

    def test_unavailable_source_and_unrenderable_value_still_raise_type_error(self):
        class Unrenderable:
            def __repr__(self):
                raise ValueError("Custom repr failed")

        unrenderable_instance = Unrenderable()

        with patch.object(linecache, "getlines", return_value=[]):
            with self.assertRaises(TypeError) as raised:
                require_type(unrenderable_instance, int)

        self.assertIn("Unrenderable", str(raised.exception))
        self.assertNotIn("Custom repr failed", str(raised.exception))

    def test_invalid_type_specifications(self):
        for check in (require_type, require_instance, require_subclass):
            for spec in ((), (int, "str"), "int"):
                with self.subTest(check=check.__name__, spec=spec):
                    with self.assertRaises(TypeError):
                        check(1, spec)

    def test_expression_is_evaluated_once(self):
        calls = []

        def produce_text():
            calls.append(1)

            return "hello"

        with self.assertRaises(TypeError) as raised:
            require_instance(produce_text(), int)

        self.assertEqual(calls, [1])
        self.assertIn("produce_text()", str(raised.exception))

    def test_exception_factory_preserves_domain_code_and_source_expression(self):
        reject = partial(PayloadRejected, code=PayloadRejected.Code.INVALID_VALUE)

        cases = [
            (require_type, True),
            (require_instance, "not a number"),
            (require_subclass, str),
            (require_subclass, "not a class"),
        ]

        for (requirement, rejected_operand) in cases:
            with self.subTest(requirement=requirement.__name__, rejected_operand=rejected_operand):
                with self.assertRaises(PayloadRejected) as raised:
                    requirement(rejected_operand, int, error_factory=reject)

                self.assertIs(PayloadRejected.Code.INVALID_VALUE, raised.exception.code)
                self.assertIn("rejected_operand", str(raised.exception))

    def test_factory_is_unused_for_matches_and_invalid_expected_types(self):
        reject = Mock(side_effect=AssertionError("Domain rejection should not be constructed"))

        for requirement in [require_type, require_instance, require_subclass]:
            with self.subTest(requirement=requirement.__name__):
                self.assertIsNone(requirement(type, type, error_factory=reject))

                with self.assertRaises(TypeError):
                    requirement(type, (type, "not a type"), error_factory=reject)

        reject.assert_not_called()
