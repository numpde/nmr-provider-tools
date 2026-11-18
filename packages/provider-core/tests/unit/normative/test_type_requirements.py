"""Check the three type requirements and their source-aware failures."""

import linecache
import unittest
from collections.abc import Mapping
from unittest.mock import patch

from provider_core.type_requirements import require_instance, require_subclass, require_type


class TypeRequirementsTests(unittest.TestCase):
    def test_exact_type_accepts_single_type_and_tuple(self):
        self.assertIsNone(require_type(12, int))
        self.assertIsNone(require_type(12, (int, float)))
        self.assertIsNone(require_type(3.5, (int, float)))

    def test_exact_type_rejects_subclasses(self):
        with self.assertRaises(TypeError) as raised:
            require_type(True, int)

        self.assertEqual(str(raised.exception), "True: expected int, got bool")

    def test_exact_type_tuple_message(self):
        data = {"value": "hello"}

        with self.assertRaises(TypeError) as raised:
            require_type(data["value"], (int, float))

        self.assertEqual(
            str(raised.exception),
            'data["value"]: expected one of (int, float), got str',
        )

    def test_instance_accepts_subclasses_and_tuple(self):
        self.assertIsNone(require_instance(True, int))
        self.assertIsNone(require_instance(True, (int, float)))

    def test_instance_accepts_abstract_base_class(self):
        self.assertIsNone(require_instance({}, Mapping))

        with self.assertRaises(TypeError):
            require_type({}, Mapping)

    def test_instance_reports_source_expression(self):
        data = {"value": "hello"}

        with self.assertRaises(TypeError) as raised:
            require_instance(data["value"], (int, float))

        self.assertEqual(
            str(raised.exception),
            'data["value"]: expected instance of one of (int, float), got str',
        )

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

        self.assertEqual(
            str(raised.exception),
            "str: expected subclass of one of (int, float), got str",
        )

    def test_subclass_rejects_nonclass_first_argument(self):
        value = "hello"

        with self.assertRaises(TypeError) as raised:
            require_subclass(value, str)

        self.assertEqual(
            str(raised.exception),
            "value: expected subclass of str, got str instance",
        )

    def test_subclass_accepts_abstract_base_class(self):
        self.assertIsNone(require_subclass(dict, Mapping))

    def test_keyword_arguments(self):
        data = {"value": "hello"}

        with self.assertRaises(TypeError) as raised:
            require_type(T=int, expr=data["value"])

        self.assertEqual(
            str(raised.exception),
            'data["value"]: expected int, got str',
        )

        with self.assertRaises(TypeError) as raised:
            require_instance(T=int, expr=data["value"])

        self.assertEqual(
            str(raised.exception),
            'data["value"]: expected instance of int, got str',
        )

    def test_multiline_call(self):
        data = {"value": "hello"}

        with self.assertRaises(TypeError) as raised:
            require_instance(
                data.get("value"),
                (int, float),
            )

        self.assertEqual(
            str(raised.exception),
            'data.get("value"): expected instance of one of (int, float), got str',
        )

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

        self.assertEqual(one, "first: expected int, got str")
        self.assertEqual(two, "second: expected instance of int, got str")

    def test_source_unavailable_uses_repr(self):
        with patch.object(linecache, "getlines", return_value=[]):
            with self.assertRaises(TypeError) as raised:
                require_type("hello", int)

        self.assertEqual(str(raised.exception), "'hello': expected int, got str")

    def test_unavailable_source_and_unrenderable_value_still_raise_type_error(self):
        class Unrenderable:
            def __repr__(self):
                raise ValueError("Custom repr failed")

        value = Unrenderable()

        with patch.object(linecache, "getlines", return_value=[]):
            with self.assertRaises(TypeError) as raised:
                require_type(value, int)

        self.assertIn("expected int, got Unrenderable", str(raised.exception))
        self.assertNotIn("Custom repr failed", str(raised.exception))

    def test_invalid_type_specifications(self):
        for check in (require_type, require_instance, require_subclass):
            for spec in ((), (int, "str"), "int"):
                with self.subTest(check=check.__name__, spec=spec):
                    with self.assertRaisesRegex(
                        TypeError,
                        "T must be a type or a non-empty tuple of types",
                    ):
                        check(1, spec)

    def test_expression_is_evaluated_once(self):
        calls = []

        def value():
            calls.append(1)
            return "hello"

        with self.assertRaises(TypeError) as raised:
            require_instance(value(), int)

        self.assertEqual(calls, [1])
        self.assertEqual(str(raised.exception), "value(): expected instance of int, got str")
