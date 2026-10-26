"""Keep reviewed generic contracts local while raw and strict findings survive.

Each case invokes the actual repository property over an isolated authored file.
Each tag requires a nonblank explanation; reviewers judge its meaning.
"""

from tools.container_guard import require_container

require_container()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import test_python as policy


class PythonContractApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.annotation = "# silent-defaults: allow-contract -- The protocol requires an origin path\n"

    def exercise_property(self, source, *, accepted):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "tools/consumer.py"
            path.parent.mkdir()
            path.write_text(source)
            with patch.object(policy, "ROOT", root), patch.object(policy, "python_files", return_value=[path]):
                raw = policy.python_default_findings(path)
                applicable = policy.python_operator_default_findings(path)
                property_test = policy.PythonSilentDefaultsTest()
                if accepted:
                    property_test.test_python_files_do_not_default_operator_inputs()
                else:
                    with self.assertRaises(AssertionError):
                        property_test.test_python_files_do_not_default_operator_inputs()
            return raw, applicable

    def test_if_header_annotation_covers_one_expression_and_preserves_body_findings(self) -> None:
        header = 'if response.headers.get("Content-Encoding", "identity") != "identity":\n'
        raw, applicable = self.exercise_property(self.annotation + header + '    raise ValueError("encoded")\n', accepted=True)
        self.assertEqual(1, len(raw))
        self.assertEqual([], applicable)
        raw, applicable = self.exercise_property(
            self.annotation + header + '    status = state.get("completed", 0)\n', accepted=False
        )
        self.assertEqual(2, len(raw))
        self.assertEqual([raw[1]], applicable)

    def test_comparison_predicates_do_not_default_create_fault_inputs(self):
        sources = (
            'if "NMRNET_FAKE_CREATE_OUTPUT" not in os.environ or os.environ["NMRNET_FAKE_CREATE_OUTPUT"] not in {\n    "exact",\n    "empty",\n    "disagree",\n    "nul",\n    "cap",\n}:\n    raise SystemExit(97)\n',
            'if (\n    "NMRNET_FAKE_CREATE_STATUS" not in os.environ\n    or re.fullmatch(r"0|[1-9][0-9]{0,2}", os.environ["NMRNET_FAKE_CREATE_STATUS"]) is None\n    or int(os.environ["NMRNET_FAKE_CREATE_STATUS"]) > 255\n):\n    raise SystemExit(97)\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=True)
                self.assertEqual([], raw)
                self.assertEqual([], applicable)

    def test_assigned_publication_failure_composes_comparison_predicates(self):
        source = (
            "publication_failure = (\n"
            '    destination.name == "status.ready" and os.environ.get("FAKE_READY_PUBLICATION_FAILURE") == "1"\n'
            ') or (destination.name == "status.release" and os.environ.get("FAKE_RELEASE_PUBLICATION_FAILURE") == "1")\n'
            "if publication_failure:\n"
            "    fail()\n"
        )
        raw, applicable = self.exercise_property(source, accepted=True)
        self.assertEqual(1, len(raw))
        self.assertIn('destination.name == "status.ready" and', raw[0])
        self.assertEqual([], applicable)

    def test_composed_comparison_predicates_apply_across_value_consumers(self):
        expression = '(os.environ["K"] == "x" and destination == "ready") or not (count < 0 or flag is None)'
        for source in (
            f"value = {expression}\n",
            f"def select():\n    return {expression}\n",
            f"consume({expression})\n",
            f"if {expression}:\n    fail()\n",
            f"if consume({expression}):\n    fail()\n",
            f"if ({expression}) == expected:\n    fail()\n",
            f"value = not ({expression})\n",
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=True)
                self.assertEqual(1, len(raw))
                self.assertEqual([], applicable)

    def test_composed_comparisons_preserve_value_arms_and_nested_defaults(self):
        for arm in ('os.environ["OTHER"]', "flag", "check()", "37", 'not os.environ["OTHER"]'):
            source = f'value = (os.environ["K"] == "x" and destination == "ready") or {arm}\n'
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(1, len(raw))
                self.assertEqual(raw, applicable)
        for arm in ('os.environ["OTHER"]', "flag", "check()", "37"):
            source = f'value = (os.environ["K"] == "x" and {arm}) or count < 0\n'
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(1, len(raw))
                self.assertEqual(raw, applicable)
        for default in ('os.environ.get("K", "x")', '(os.environ["K"] or "x")'):
            source = f'value = ({default} == "x" and destination == "ready") or count < 0\n'
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual([raw[1]], applicable)

    def test_composed_comparison_admission_preserves_function_default_finding(self):
        source = 'def select(value=(os.environ["K"] == "x" and destination == "ready") or count < 0):\n    pass\n'
        raw, applicable = self.exercise_property(source, accepted=False)
        self.assertEqual(2, len(raw))
        self.assertEqual(1, len(applicable))
        self.assertIn(": python function default:", applicable[0])
        self.assertIn(applicable[0], raw)

    def test_comparison_predicates_preserve_mixed_values_and_nested_defaults(self):
        for source, expected in (
            ('value = os.environ["K"] == "x" or 37\n', 'os.environ["K"] == "x" or 37'),
            ('value = os.environ["K"] == "x" or os.environ["OTHER"]\n', 'os.environ["K"] == "x" or os.environ["OTHER"]'),
            ('value = os.environ["K"] or "x"\n', 'os.environ["K"] or "x"'),
            ('if (os.environ["K"] or "x") == "x" or os.environ["OTHER"] == "y":\n    reject()\n', 'os.environ["K"] or "x"'),
            ('if os.environ.get("K", "x") == "x" or os.environ["OTHER"] == "y":\n    reject()\n', 'os.environ.get("K", "x")'),
            ('if not (os.environ["K"] or "x") or os.environ["OTHER"] == "y":\n    reject()\n', 'os.environ["K"] or "x"'),
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(1, len(raw))
                self.assertTrue(raw[0].endswith(": " + expected))
                self.assertEqual(raw, applicable)

    def test_not_and_chained_comparisons_combine_predicates_without_admitting_defaults(self):
        for source in (
            'value = 0 <= int(os.environ["K"]) <= 255 or os.environ["OTHER"] is None\n',
            'if not os.environ["K"] or os.environ["OTHER"] != "y":\n    reject()\n',
            'if not ("K" in os.environ) or len(os.environ["OTHER"]) > 255:\n    reject()\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=True)
                self.assertEqual([], raw)
                self.assertEqual([], applicable)
        raw, applicable = self.exercise_property(
            'if not os.environ.get("K", "x") or 0 <= int(os.environ["OTHER"]) <= 255:\n    reject()\n', accepted=False
        )
        self.assertEqual(1, len(raw))
        self.assertTrue(raw[0].endswith(': os.environ.get("K", "x")'))
        self.assertEqual(raw, applicable)

    def test_optional_env_activation_guards_keep_raw_findings_without_defaulting_inputs(self):
        for source in (
            'if destination == "ready" and (os.environ.get("READY") or os.environ.get("PREFLIGHT")):\n    inspect()\n',
            'if destination == "ready" and ((os.environ.get("READY") and package_capture) or (os.environ.get("PREFLIGHT") == "1" and preflight) or (os.environ.get("ADMISSION") and marker.exists())):\n    fail()\n',
            'if not (os.environ.get("A") or os.environ.get("B")):\n    proceed()\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=True)
                self.assertEqual(1, len(raw))
                self.assertEqual([], applicable)

    def test_env_activation_guard_classification_stops_at_value_consumers(self):
        expression = 'os.environ.get("A") or os.environ.get("B")'
        for source in (
            f"value = {expression}\n",
            f"def select():\n    return {expression}\n",
            f"consume({expression})\n",
            f"if consume({expression}):\n    proceed()\n",
            f'if ({expression}) == "x":\n    proceed()\n',
            f"if ({expression})[0]:\n    proceed()\n",
            f"if ({expression}) + suffix:\n    proceed()\n",
            f"if (value := ({expression})):\n    proceed()\n",
            'if os.environ.get("A") or check(os.environ.get("B")):\n    proceed()\n',
            'if os.environ.get("A") or os.environ.get("B").strip():\n    proceed()\n',
            'if os.environ.get("A") or "fallback":\n    proceed()\n',
            'if os.environ.get("A") or unrelated_flag:\n    proceed()\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(1, len(raw))
                self.assertEqual(raw, applicable)

    def test_env_activation_guards_preserve_nested_and_body_defaults(self):
        for source, retained in (
            (
                'if (os.environ.get("A") or "fallback") or os.environ.get("B"):\n    proceed()\n',
                'os.environ.get("A") or "fallback"',
            ),
            ('if os.environ.get("A", "fallback") or os.environ.get("B"):\n    proceed()\n', 'os.environ.get("A", "fallback")'),
            (
                'if os.environ.get("A") or os.environ.get("B"):\n    value = os.environ.get("C") or "fallback"\n',
                'os.environ.get("C") or "fallback"',
            ),
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertIn(applicable[0], raw)
                self.assertTrue(applicable[0].endswith(": " + retained))

    def test_dict_entry_contract_covers_only_one_literal_key_value_region(self):
        for entry, count in (
            ('"kind": record.get("kind", "missing"),\n', 1),
            ('"entries": [item.get("name", "missing")] if ready else [],\n', 2),
            ('37: consume(\n        payload.get("result", {}) if ready else {},\n    ),\n', 2),
            ('"config": (\n        config.get("value", "missing")\n    ),\n', 1),
        ):
            with self.subTest(entry=entry):
                source = "result = {\n    " + self.annotation + "    " + entry + "}\n"
                raw, applicable = self.exercise_property(source, accepted=True)
                self.assertEqual(count, len(raw))
                self.assertEqual([], applicable)
        # The new entry placement must not change existing statement contracts.
        raw, applicable = self.exercise_property(
            self.annotation + 'result = {"kind": record.get("kind", "missing")}\n', accepted=True
        )
        self.assertEqual(1, len(raw))
        self.assertEqual([], applicable)

    def test_dict_entry_contract_preserves_sibling_key_and_enclosing_findings(self):
        sources = (
            "result = {\n    "
            + self.annotation
            + '    "kind": record.get("kind", "missing"),\n    "entries": record.get("entries", []),\n}\n',
            "result = {\n    "
            + self.annotation
            + '    "kind": record.get("kind", "missing"), (\n        "entries"\n    ): record.get("entries", []),\n}\n',
            'result = {record.get("key", "fallback"): {\n    '
            + self.annotation
            + '    "kind": record.get("kind", "missing"),\n}}\n',
            'result = payload.get("result", {\n    ' + self.annotation + '    "kind": record.get("kind", "missing"),\n})\n',
            'if state.get("ready", True):\n    result = {\n        '
            + self.annotation
            + '        "kind": record.get("kind", "missing"),\n    }\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertIn(applicable[0], raw)
                self.assertFalse(applicable[0].endswith(': record.get("kind", "missing")'))

    def test_dict_entry_contract_keeps_operator_and_callable_values_strict(self):
        for expression in (
            'os.environ.get("PORT", "8080")',
            'payload.get("port", os.environ.get("PORT", "8080"))',
            'args[0] if args else "all"',
            "args.port or 443",
            'parser.add_argument("--port", default="8080")',
            'parser.add_argument("path", nargs="?")',
            'payload.get("callback", lambda status=0: status)',
            'payload.get("callback", lambda: required)',
            'os.environ.pop("OPTION", None)',
        ):
            with self.subTest(expression=expression):
                source = "result = {\n    " + self.annotation + '    "value": ' + expression + ",\n}\n"
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_dict_entry_contract_rejects_computed_unpack_siblings_and_ambiguous_placement(self):
        for body in (
            self.annotation + '    key: record.get("kind", "missing"),\n',
            self.annotation + '    ("key" + suffix): record.get("kind", "missing"),\n',
            self.annotation + '    ("key"): record.get("kind", "missing"),\n',
            self.annotation + '    **record.get("entries", {}),\n',
            self.annotation + '    "left": left.get("value", 0), "right": right.get("value", 0),\n',
            self.annotation + '    "left": left.get("value", 0), **right.get("values", {}),\n',
            self.annotation + '    "left": left.get("value", 0), **(\n        required\n    ),\n',
            self.annotation + '    "left": left.get("value", 0), (\n        key\n    ): required,\n',
            self.annotation + '    "left": left.get("value", 0),\n    dynamic_key: required,\n',
            self.annotation + '    "left": left.get("value", 0),\n    **required,\n',
            self.annotation + '    "values": [left.get("value", 0), right.get("value", 0)],\n',
            self.annotation + '\n    "kind": record.get("kind", "missing"),\n',
            self.annotation + '        "kind": record.get("kind", "missing"),\n',
            '"kind":\n        ' + self.annotation + '        record.get("kind", "missing"),\n',
            '"kind": record.get("kind", "missing"),  ' + self.annotation,
            self.annotation + '    "kind": required,\n',
        ):
            with self.subTest(body=body):
                raw, applicable = self.exercise_property("result = {\n    " + body + "}\n", accepted=False)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
                for finding in raw:
                    self.assertIn(finding, applicable)
        raw, applicable = self.exercise_property(
            "result = {\n    "
            + self.annotation.replace("allow-contract", "allow-pop-default")
            + '    "value": os.environ.pop("OPTION", None),\n}\n',
            accepted=False,
        )
        self.assertTrue(raw)
        for finding in raw:
            self.assertIn(finding, applicable)
        raw, applicable = self.exercise_property(
            "result = {\n    " + self.annotation + '    "key": record.get("value", "missing") for record in records\n}\n',
            accepted=False,
        )
        self.assertTrue(raw)
        for finding in raw:
            self.assertIn(finding, applicable)

    def test_dict_entry_contract_requires_a_nonblank_explanation(self):
        annotation = self.annotation.replace("The protocol requires an origin path", " ")
        raw, applicable = self.exercise_property(
            "result = {\n    " + annotation + '    "kind": record.get("kind", "missing"),\n}\n', accepted=False
        )
        self.assertEqual(1, len(raw))
        self.assertIn(raw[0], applicable)
        self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))

    def test_call_expression_contract_covers_multiline_consumer_regions(self):
        for source, count in (
            (
                'self.assertEqual(\n    0 if len(arguments) == 2 and arguments[0] == "--print" else 2,\n    result.returncode,\n    msg=result.stderr,\n)\n',
                1,
            ),
            (
                'self.assertEqual(\n    ["run", "run", "build"] + ([] if fault == "malformed-build" else ["image"]),\n    [call["arguments"][0] for call in calls],\n)\n',
                1,
            ),
            ('consume(\n    payload.get("result", {}) if ready else {},\n)\n', 2),
            ('handlers.get("name", fallback)(\n    required,\n)\n', 1),
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=True)
                self.assertEqual(count, len(raw))
                self.assertEqual([], applicable)

    def test_call_expression_contract_keeps_operator_callable_and_env_pop_boundaries_strict(self):
        for expression in (
            'os.environ.get("PORT", "8080")',
            'payload.get("port", os.environ.get("PORT", "8080"))',
            'args[0] if args else "all"',
            "args.port or 443",
            'parser.add_argument("--port", default="8080")',
            'parser.add_argument("path", nargs="?")',
            'payload.get("port", parser.add_argument("--port", default="8080"))',
            'payload.get("callback", lambda status=0: status)',
            'payload.get("result", 0), callback=lambda: required',
            'os.environ.pop("OPTION", None)',
        ):
            with self.subTest(expression=expression):
                raw, applicable = self.exercise_property(
                    self.annotation + "consume(\n    " + expression + "\n)\n", accepted=False
                )
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)
        for source in (
            'os.environ.get("CALLBACK", "fallback")(\n    required,\n)\n',
            '(lambda: required)(\n    payload.get("result", 0),\n)\n',
            'os.environ.pop(\n    "OPTION", None,\n)\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)
        pop_annotation = self.annotation.replace("allow-contract", "allow-pop-default")
        raw, applicable = self.exercise_property(pop_annotation + 'os.environ.pop(\n    "OPTION", None,\n)\n', accepted=True)
        self.assertEqual(1, len(raw))
        self.assertEqual([], applicable)
        for source in (
            'consume(\n    os.environ.pop("OPTION", None),\n)\n',
            'result = consume(\n    os.environ.pop("OPTION", None),\n)\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(pop_annotation + source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_call_expression_contract_preserves_siblings_other_shapes_and_enclosing_findings(self):
        for source in (
            'consume(\n    left.get("result", 0),\n    right.get("result", 0),\n)\n',
            'handlers.get("name", fallback)(\n    payload.get("result", 0),\n)\n',
            '{"result": payload.get("result", 0),\n}\n',
            '(\n    payload.get("result", 0),\n)\n',
            'await consume(\n    payload.get("result", 0),\n)\n',
            "consume(\n    required,\n)\n",
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
                for finding in raw:
                    self.assertIn(finding, applicable)
        for source, retained in (
            (
                self.annotation + 'consume(\n    payload.get("result", 0),\n)\nstatus = state.get("done", 0)\n',
                'state.get("done", 0)',
            ),
            (
                'if state.get("ready", True):\n    '
                + self.annotation
                + '    consume(\n        payload.get("result", 0),\n    )\n',
                'state.get("ready", True)',
            ),
            (
                "try:\n    inspect()\nexcept Exception:\n    "
                + self.annotation
                + '    consume(\n        payload.get("result", 0),\n    )\n    return "fallback"\n',
                "python exception fallback:",
            ),
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertIn(retained, applicable[0])
                self.assertIn(applicable[0], raw)

    def test_return_value_contract_covers_multiline_fixture_selection_regions(self):
        for source in (
            "return validate(\n    [batch] if batches is None else batches,\n    records,\n)\n",
            'return command, environment | {\n    "NATIVE_NEXT": "yes" if next_success else "no",\n    "OUTCOME": outcome,\n}\n',
            'return (\n    payload.get("result", {}) if ready else {}\n)\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=True)
                self.assertTrue(raw)
                self.assertEqual([], applicable)

    def test_return_value_contract_keeps_operator_and_nested_callable_defaults_strict(self):
        for expression in (
            'os.environ.get("PORT", "8080")',
            'payload.get("port", os.environ.get("PORT", "8080"))',
            'args[0] if args else "all"',
            "args.port or 443",
            'parser.add_argument("--port", default="8080")',
            'payload.get("port", parser.add_argument("--port", default="8080"))',
            'payload.get("callback", lambda status=0: status)',
        ):
            with self.subTest(expression=expression):
                raw, applicable = self.exercise_property(
                    self.annotation + "return (\n    " + expression + "\n)\n", accepted=False
                )
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_return_value_contract_preserves_siblings_adjacent_and_ancestor_findings(self):
        for source in (
            'return (\n    left.get("result", 0),\n    right.get("result", 0),\n)\n',
            "return\n",
            "return None\n",
            "return required\n",
            "return 37\n",
            'value: object = (\n    payload.get("result", 0)\n)\n',
            '{"result": payload.get("result", 0),\n}\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
                for finding in raw:
                    self.assertIn(finding, applicable)
        raw, applicable = self.exercise_property(
            self.annotation + 'return (\n    payload.get("result", 0)\n)\nstatus = state.get("done", 0)\n', accepted=False
        )
        self.assertEqual(2, len(raw))
        self.assertEqual([raw[1]], applicable)
        raw, applicable = self.exercise_property(
            "try:\n    inspect()\nexcept Exception:\n    "
            + self.annotation
            + '    return (\n        payload.get("result", 0)\n    )\n',
            accepted=False,
        )
        self.assertEqual(2, len(raw))
        self.assertEqual(1, len(applicable))
        self.assertIn("python exception fallback:", applicable[0])
        self.assertIn(applicable[0], raw)

    def test_pop_tag_accepts_only_its_discarded_fixed_key_revocation(self):
        annotation = self.annotation.replace("allow-contract", "allow-pop-default")
        for receiver in ("env", "environment", "process_env", "os.environ"):
            with self.subTest(receiver=receiver):
                raw, applicable = self.exercise_property(annotation + receiver + '.pop("MARKER", None)\n', accepted=True)
                self.assertEqual(1, len(raw))
                self.assertEqual([], applicable)
        raw, applicable = self.exercise_property(annotation + 'environment.pop(\n    "MARKER",\n    None,\n)\n', accepted=True)
        self.assertEqual(1, len(raw))
        self.assertEqual([], applicable)
        for neighbour in ('value = os.environ.get("STATUS", "0")\n', 'environment.pop("OTHER", None)\n'):
            with self.subTest(neighbour=neighbour):
                raw, applicable = self.exercise_property(
                    annotation + 'environment.pop("MARKER", None)\n' + neighbour, accepted=False
                )
                self.assertEqual(2, len(raw))
                self.assertEqual([raw[1]], applicable)

        source = 'if os.environ.get("PORT", "8080"):\n' + "    " + annotation + '    environment.pop("MARKER", None)\n'
        raw, applicable = self.exercise_property(source, accepted=False)
        self.assertEqual(2, len(raw))
        self.assertEqual(1, len(applicable))
        self.assertIn('os.environ.get("PORT", "8080")', applicable[0])
        self.assertIn(applicable[0], raw)

    def test_pop_tag_keeps_consumed_other_methods_and_nonliteral_shapes_strict(self):
        annotation = self.annotation.replace("allow-contract", "allow-pop-default")
        statements = (
            'removed = environment.pop("MARKER", None)',
            'return environment.pop("MARKER", None)',
            'consume(environment.pop("MARKER", None))',
            'environment.pop("MARKER", None) if enabled else None',
            'if environment.pop("MARKER", None):\n    pass',
            'while environment.pop("MARKER", None):\n    pass',
            '(environment.pop("MARKER", None),)',
            'environment.pop("MARKER", None); environment.pop("OTHER", None)',
            "environment.pop(key, None)",
            'environment.pop("MARKER", "")',
            'environment.pop("MARKER", False)',
            'environment.pop("MARKER", 0)',
            'environment.pop("MARKER")',
            'environment.pop("MARKER", None, None)',
            'environment.pop("MARKER", default=None)',
            'environment.pop("MARKER", None, **options)',
            'environment.get("MARKER", None)',
            'environment.setdefault("MARKER", None)',
            'payload.pop("MARKER", None)',
        )
        for statement in statements:
            with self.subTest(statement=statement):
                raw, applicable = self.exercise_property(annotation + statement + "\n", accepted=False)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_pop_tag_requires_an_explanation_and_exact_placement(self):
        annotation = self.annotation.replace("allow-contract", "allow-pop-default")
        call = 'environment.pop("MARKER", None)\n'
        sources = (
            annotation + "value = required\n",
            annotation + "\n" + call,
            "    " + annotation + call,
            call.rstrip() + "  " + annotation,
            annotation.replace("The protocol requires an origin path", " ") + call,
            annotation + 'if enabled:\n    environment.pop("MARKER", None)\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
                for finding in raw:
                    self.assertIn(finding, applicable)
        raw, applicable = self.exercise_property("note = " + repr(annotation) + "\n" + call, accepted=False)
        self.assertEqual(1, len(raw))
        self.assertEqual(raw, applicable)

    def test_typed_pass_annotation_accepts_explicit_types_and_flat_tuples(self):
        for declared in (
            "OSError",
            "ipaddress.AddressValueError",
            "(FileNotFoundError, ProcessLookupError)",
            "(errors.Missing, OSError)",
        ):
            with self.subTest(declared=declared):
                source = "try:\n    inspect()\n" + self.annotation + "except " + declared + ":\n    pass\n"
                raw, applicable = self.exercise_property(source, accepted=True)
                self.assertEqual(1, len(raw))
                self.assertIn("python exception fallback:", raw[0])
                self.assertEqual([], applicable)

    def test_typed_pass_annotation_rejects_broad_computed_nested_and_nonpass_handlers(self):
        headers = (
            "except:",
            "except Exception:",
            "except BaseException:",
            "except builtins.Exception:",
            "except ExceptionGroup:",
            "except builtins.BaseExceptionGroup:",
            "except (OSError, Exception):",
            "except (OSError, builtins.BaseExceptionGroup):",
            "except ():",
            "except factory():",
            "except errors[0]:",
            "except args.failure:",
            "except (OSError, (ValueError,)):",
            "except (OSError, *errors):",
            "except* OSError:",
        )
        sources = ["try:\n    inspect()\n" + self.annotation + header + "\n    pass\n" for header in headers]
        sources.extend(
            (
                "try:\n    inspect()\n" + self.annotation + "except OSError:\n    return 0\n",
                "try:\n    inspect()\n" + self.annotation + 'except OSError:\n    pass\n    result = state.get("done", 0)\n',
            )
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_typed_pass_annotation_keeps_protected_operator_inputs_strict(self):
        statements = (
            'value = os.environ["PORT"]',
            'value = env["PORT"]',
            'value = os.getenv("PORT")',
            'value = os.environ.get("PORT", "8080")',
            "value = args[0]",
            "value = args.port",
            'parser.add_argument("--port", default="8080")',
            'parser.add_argument("profile", nargs="?")',
            'parser.add_argument("--port", required=True)',
            "value = parser.parse_args()",
            "value = parser.parse_known_args()",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                source = "try:\n    " + statement + "\n" + self.annotation + "except KeyError:\n    pass\n"
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                if statement == statements[0]:
                    self.assertEqual(1, len(raw), msg="Required ENV access has only its swallowing-handler finding")
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_typed_pass_annotation_does_not_cover_neighbours_or_other_placements(self):
        source = 'try:\n    value = state.get("done", 0)\n' + self.annotation + "except OSError:\n    pass\n"
        raw, applicable = self.exercise_property(source, accepted=False)
        self.assertEqual(2, len(raw))
        self.assertEqual(1, len(applicable))
        self.assertIn('state.get("done", 0)', applicable[0])
        self.assertIn(applicable[0], raw)
        for tail, retained in (
            ('status = state.get("done", 0)\n', 'state.get("done", 0)'),
            ("except ValueError:\n    pass\n", "except ValueError: pass"),
        ):
            source = "try:\n    inspect()\n" + self.annotation + "except OSError:\n    pass\n" + tail
            raw, applicable = self.exercise_property(source, accepted=False)
            self.assertEqual(2, len(raw))
            self.assertEqual(1, len(applicable))
            self.assertIn(retained, applicable[0])
            self.assertIn(applicable[0], raw)
        sources = (
            self.annotation + "try:\n    inspect()\nexcept OSError:\n    pass\n",
            "try:\n    inspect()\nexcept OSError:\n    " + self.annotation + "    pass\n",
            "try:\n    inspect()\n    " + self.annotation + "except OSError:\n    pass\n",
            "try:\n    inspect()\n" + self.annotation + "\nexcept OSError:\n    pass\n",
            "try:\n    inspect()\nexcept OSError: " + self.annotation + "    pass\n",
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(1, len(raw))
                self.assertIn(raw[0], applicable)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
        for body in ("raise", "return None"):
            with self.subTest(body=body):
                raw, applicable = self.exercise_property(
                    "try:\n    inspect()\n" + self.annotation + "except OSError:\n    " + body + "\n", accepted=False
                )
                self.assertEqual([], raw)
                self.assertEqual(1, len(applicable))
                self.assertIn("python contract annotation requires", applicable[0])

    def test_if_header_annotation_rejects_multiline_and_multiple_candidates(self) -> None:
        sources = (
            self.annotation + 'if response.headers.get(\n    "Content-Encoding", "identity"\n) != "identity":\n    pass\n',
            self.annotation
            + 'if (response.headers.get("Content-Encoding", "identity") != "identity"\n    and enabled):\n    pass\n',
            self.annotation + 'if left.get("coding", "identity") == right.get("coding", "identity"):\n    pass\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_header_extension_keeps_operator_and_non_if_contracts_strict(self) -> None:
        sources = (
            self.annotation + 'if os.environ.get("PORT", "8080"):\n    pass\n',
            self.annotation + "if args.port or 443:\n    pass\n",
            self.annotation + 'if parser.add_argument("--port", default="8080"):\n    pass\n',
            self.annotation + 'def run() -> registry.get("Result", object):\n    pass\n',
            self.annotation + 'while parsed.path or "/":\n    pass\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_for_iterable_contract_accepts_only_annotated_generic_region(self):
        for expression, count in (
            ('payload.get("items", [])', 1),
            ("observed.get(identifier, set())", 1),
            ('payload.get("items", {}) if ready else []', 2),
        ):
            with self.subTest(expression=expression):
                source = "for item in " + expression + ":\n    pass\n"
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(count, len(raw))
                self.assertEqual(raw, applicable)
                annotated_raw, applicable = self.exercise_property(self.annotation + source, accepted=True)
                self.assertEqual(count, len(annotated_raw))
                self.assertEqual([], applicable)
        # This placement was formerly rejected alongside unsupported Return
        # neighbours; the For header now owns its generic iterable explicitly.
        source = 'for value in payload.get("results", []):\n    consume(value)\n'
        raw, applicable = self.exercise_property(source, accepted=False)
        self.assertEqual(1, len(raw))
        self.assertEqual(raw, applicable)
        raw, applicable = self.exercise_property(self.annotation + source, accepted=True)
        self.assertEqual(1, len(raw))
        self.assertEqual([], applicable)

    def test_for_iterable_contract_preserves_target_body_else_and_adjacent_findings(self):
        for source, retained in (
            ('for item in payload.get("items", []): status = state.get("done", 0)\n', 'state.get("done", 0)'),
            ('for targets[slots.get("slot", 0)] in payload.get("items", []): pass\n', 'slots.get("slot", 0)'),
            ('for item in payload.get("items", []):\n    status = state.get("done", 0)\n', 'state.get("done", 0)'),
            (
                'for item in payload.get("items", []):\n    pass\nelse:\n    status = state.get("done", 0)\n',
                'state.get("done", 0)',
            ),
            ('for item in payload.get("items", []): pass\nstatus = state.get("done", 0)\n', 'state.get("done", 0)'),
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertIn(applicable[0], raw)
                self.assertTrue(applicable[0].endswith(": " + retained))

    def test_for_iterable_contract_requires_a_local_explanation_and_single_line_region(self):
        loop = 'for item in payload.get("items", []):\n    pass\n'
        for source in (
            self.annotation.replace("The protocol requires an origin path", "") + loop,
            " " + self.annotation + loop,
            self.annotation + "\n" + loop,
            self.annotation + 'for item in payload.get(\n    "items", []\n):\n    pass\n',
            self.annotation + 'for item in (left.get("items", []), right.get("items", [])):\n    pass\n',
            "async def run():\n    " + self.annotation + '    async for item in payload.get("items", []):\n        pass\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))

    def test_for_iterable_contract_keeps_env_cli_and_lambda_inputs_strict(self):
        for expression in (
            'os.environ.get("ITEMS", "all")',
            'payload.get("items", os.environ.get("ITEMS", "all"))',
            "args[0] if args else []",
            "args.items or []",
            'parser.add_argument("--items", default="all")',
            'payload.get("items", parser.add_argument("--items", default="all"))',
            'payload.get("items", lambda: ())',
        ):
            with self.subTest(expression=expression):
                raw, applicable = self.exercise_property(
                    self.annotation + "for item in " + expression + ":\n    pass\n", accepted=False
                )
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))

    def test_local_review_accepts_generic_candidates_without_changing_raw_findings(self):
        for expression in ('parsed.path or "/"', 'payload.get("context", {})', 'value if value is not None else "rendered"'):
            with self.subTest(expression=expression):
                raw, applicable = self.exercise_property(self.annotation + "value = " + expression + "\n", accepted=True)
                self.assertEqual(1, len(raw))
                self.assertEqual([], applicable)

    def test_with_context_and_assignment_value_cover_one_multiline_generic_contract(self):
        sources = (
            'with os.fdopen(\n    descriptor,\n    "r+b" if flags & os.O_RDWR else "rb",\n    closefd=False,\n) as stream:\n    pass\n',
            'request = Request(\n    url,\n    headers={\n        **({"Range": f"bytes={offset}-"} if offset else {}),\n    },\n)\n',
            'path = (parsed.path\n        or "/")\n',
            'value = (\n    payload.get("value", 0) if ready else 0\n)\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=True)
                self.assertTrue(raw)
                self.assertEqual([], applicable)

    def test_with_and_assignment_regions_preserve_body_targets_and_adjacent_defaults(self):
        sources = (
            (
                'with acquire(mode if writable else "rb") as stream:\n    status = state.get("done", 0)\n',
                'state.get("done", 0)',
            ),
            (
                'with acquire(mode if writable else "rb") as targets[slots.get("slot", 0)]:\n    pass\n',
                'slots.get("slot", 0)',
            ),
            (
                'targets[slots.get("slot", 0)] = (\n    mode if writable else "rb"\n)\n',
                'slots.get("slot", 0)',
            ),
            (
                'value = (\n    mode if writable else "rb"\n)\nstatus = state.get("done", 0)\n',
                'state.get("done", 0)',
            ),
            (
                'with acquire(mode if writable else "rb"):\n    def inner(status=0):\n        pass\n',
                "python function default: 0",
            ),
        )
        for source, retained in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertIn(retained, applicable[0])
                self.assertIn(applicable[0], raw)

    def test_with_and_assignment_regions_keep_multiline_operator_and_callable_inputs_strict(self):
        expressions = (
            'os.environ.get("PORT", "8080")',
            'payload.get("port", os.environ.get("PORT", "8080"))',
            'args[0] if args else "all"',
            "args.port or 443",
            'parser.add_argument("--port", default="8080")',
            'payload.get("port", parser.add_argument("--port", default="8080"))',
            'payload.get("callback", lambda status=0: status)',
        )
        for expression in expressions:
            for source in (
                "with acquire(\n    " + expression + "\n):\n    pass\n",
                "value = (\n    " + expression + "\n)\n",
            ):
                with self.subTest(source=source):
                    raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                    self.assertTrue(raw)
                    for finding in raw:
                        self.assertIn(finding, applicable)

    def test_with_and_assignment_regions_reject_siblings_and_other_statement_kinds(self):
        sources = (
            'with acquire(left.get("mode", "rb")), acquire(right.get("mode", "rb")):\n    pass\n',
            'value = (\n    left.get("value", 0),\n    right.get("value", 0),\n)\n',
            'value: object = (\n    payload.get("value", 0)\n)\n',
            'value += (\n    payload.get("value", 0)\n)\n',
            'async with acquire(\n    payload.get("value", 0)\n):\n    pass\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_nested_generic_expression_keeps_siblings_and_strict_findings_visible(self):
        expression = '{"CDCl3": 1, "DMSO-d6": 2}.get(solvent, 3) if solvent_present else 0'
        raw, applicable = self.exercise_property(self.annotation + "category = " + expression + "\n", accepted=True)
        self.assertEqual(2, len(raw))
        self.assertEqual([], applicable)
        for source in (
            self.annotation + 'values = (left.get("value", 0), right.get("value", 0))\n',
            self.annotation + 'value = payload.get("value", 0) if ready else os.environ.get("VALUE", "0")\n',
            self.annotation + 'value = payload.get("value", 0) if args.ready else 0\n',
            self.annotation + 'value = payload.get("value", 0) if ready else parser.add_argument("--value", default="0")\n',
            self.annotation + 'value = payload.get("value", parser.add_argument("--value", default="0")) if ready else 0\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)
        raw, applicable = self.exercise_property(
            self.annotation + "def run(value=" + expression + "):\n    pass\n", accepted=True
        )
        self.assertEqual(3, len(raw))
        self.assertEqual([], applicable)
        raw, applicable = self.exercise_property(
            "try:\n    run()\nexcept Exception:\n    " + self.annotation + "    return " + expression + "\n", accepted=False
        )
        strict = [finding for finding in raw if "python exception fallback:" in finding]
        self.assertTrue(strict)
        for finding in strict:
            self.assertIn(finding, applicable)

    def test_annotation_cannot_suppress_environment_or_cli_rules(self):
        expressions = (
            'os.environ.get("PORT", "8080")',
            'os.environ["PORT"] or "8080"',
            'env["PORT"] if "PORT" in env else "8080"',
            'args[0] if args else "all"',
            "args.port or 443",
            'parser.add_argument("--port", default="8080")',
            'parser.add_argument("profile", nargs="?")',
            'parser.add_argument("--port", default=parsed.port or 443)',
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                raw, applicable = self.exercise_property(self.annotation + "value = " + expression + "\n", accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_signature_contract_and_nested_exception_annotation_have_distinct_scopes(self):
        raw, applicable = self.exercise_property(
            self.annotation + "def run(port=parsed.port or 443):\n    return port\n", accepted=True
        )
        self.assertEqual(2, len(raw))
        self.assertEqual([], applicable)
        raw, applicable = self.exercise_property(
            "try:\n    run()\nexcept Exception:\n" + "    " + self.annotation + '    return payload.get("result", [])\n',
            accepted=False,
        )
        strict = [finding for finding in raw if "python exception fallback:" in finding]
        self.assertTrue(strict)
        for finding in strict:
            self.assertIn(finding, applicable)

    def test_signature_contract_covers_sync_async_and_multiline_defaults(self):
        for definition in (
            "def read(size=-1):\n    pass\n",
            "async def read(size=-1):\n    pass\n",
            "def read(\n    size=-1, /,\n    *, depth=0, optional=None,\n):\n    pass\n",
            "def read(size=payload.get(\n    'size', 17\n)):\n    pass\n",
        ):
            with self.subTest(definition=definition):
                raw, applicable = self.exercise_property(self.annotation + definition, accepted=True)
                self.assertTrue(raw)
                self.assertEqual([], applicable)

    def test_signature_contract_keeps_body_decorator_annotation_and_neighbours_visible(self):
        for outside in (
            '    return state.get("completed", 0)\n',
            "    def inner(status=0):\n        pass\n",
            "    pass\ndef other(status=0):\n    pass\n",
        ):
            with self.subTest(outside=outside):
                raw, applicable = self.exercise_property(self.annotation + "def read(size=-1):\n" + outside, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertNotIn("-1", applicable[0])
        for source in (
            '@decorate(registry.get("decorator", object))\n' + self.annotation + "def read(size=-1):\n    pass\n",
            self.annotation + 'def read(size=-1) -> registry.get("Result", object):\n    pass\n',
            self.annotation + 'def read(size: registry.get("Size", int) = -1):\n    pass\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertIn("registry.get", applicable[0])

    def test_signature_contract_rejects_operator_defaults_and_wrong_boundaries(self):
        for expression in (
            'os.environ.get("PORT", "8080")',
            'os.environ["PORT"] or "8080"',
            'args[0] if args else "all"',
            "args.port or 443",
            'parser.add_argument("--port", default="8080")',
            'parser.add_argument("profile", nargs="?")',
            'payload.get("port", parser.add_argument("--port", default="8080"))',
            'lambda: state.get("completed", 0)',
        ):
            with self.subTest(expression=expression):
                raw, applicable = self.exercise_property(
                    self.annotation + "def run(value=" + expression + "):\n    pass\n", accepted=False
                )
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)
        for source in (
            "def record(status=0):\n    pass\n",
            self.annotation + 'def read(\n    size=-1,\n    port=os.environ.get("PORT", "8080"),\n):\n    pass\n',
            self.annotation + "def read(optional=None):\n    pass\n",
            self.annotation + "@decorate\ndef read(size=-1):\n    pass\n",
            self.annotation + "class Reader:\n    def read(self, size=-1):\n        pass\n",
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(applicable)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_bad_or_ambiguous_annotations_do_not_admit_a_candidate(self):
        statement = 'path = parsed.path or "/"\n'
        sources = (
            self.annotation.replace("The protocol requires an origin path", " ") + statement,
            self.annotation + "\n" + statement,
            statement.rstrip() + "  " + self.annotation,
            "annotation = " + repr(self.annotation) + "\n" + statement,
            self.annotation + 'paths = (left or "/", right or "/")\n',
            self.annotation + 'path = parsed.path or "/"; other = required\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_explicit_lambda_body_contract_preserves_raw_and_outside_defaults(self):
        annotation = "# silent-defaults: allow-lambda-body -- Fixed mounts resolve fixture paths\n"
        for header, footer, retained in (
            ("value = lambda path: (\n", ")\n", None),
            ("value = lambda path=defaults.get('path', 17): (\n", ")\n", "defaults.get"),
            ("with patch(target, side_effect=lambda path: (\n", ")):\n    pass\n", None),
            (
                "with patch(target, side_effect=lambda path: (\n",
                ')), acquire(other.get("value", 17)):\n    pass\n',
                "other.get",
            ),
            (
                "with patch(target, side_effect=lambda path: (\n",
                ')), patch(other, side_effect=lambda: other.get("value", 17)):\n    pass\n',
                "other.get",
            ),
        ):
            with self.subTest(header=header, footer=footer):
                source = header + "    " + annotation + "    mounts.get(path, Path(path))\n" + footer
                raw, applicable = self.exercise_property(source, accepted=retained is None)
                self.assertEqual(1 if retained is None else 2, len(raw))
                if retained is None:
                    self.assertEqual([], applicable)
                else:
                    self.assertEqual(1, len(applicable))
                    self.assertIn(retained, applicable[0])
                    self.assertIn(applicable[0], raw)

    def test_lambda_body_contract_rejects_nested_callables_operator_inputs_and_sibling_results(self):
        annotation = "# silent-defaults: allow-lambda-body -- Fixed mounts resolve fixture paths\n"
        for expression in (
            'payload.get("callback", lambda: other.get("value", 17))',
            'payload.get("value", os.environ.get("VALUE", "17"))',
            'payload.get("value", args.value or 17)',
            'payload.get("value", parser.add_argument("--value", default=17))',
            '(left.get("value", 17), right.get("value", 17))',
            'consume(payload.get("value", 17))',
            "required",
        ):
            with self.subTest(expression=expression):
                source = "value = lambda: (\n    " + annotation + "    " + expression + "\n)\n"
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(applicable)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_lambda_body_contract_requires_dedicated_immediate_same_column_placement(self):
        annotation = "# silent-defaults: allow-lambda-body -- Fixed mounts resolve fixture paths\n"
        for source in (
            annotation + 'value = lambda: payload.get("value", 17)\n',
            annotation + 'value = payload.get("value", 17)\n',
            "value = lambda: (\n    " + annotation + '\n    payload.get("value", 17)\n)\n',
            "value = lambda: (\n" + annotation + '    payload.get("value", 17)\n)\n',
            "value = lambda: (\n    " + self.annotation + '    payload.get("value", 17)\n)\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_lambda_body_contract_requires_an_explanation(self):
        for invalid in (
            "# silent-defaults: allow-lambda-body\n",
            "# silent-defaults: allow-lambda-body -- \n",
            "# silent-defaults: allow-lambda-body --    \n",
        ):
            with self.subTest(invalid=invalid):
                source = "value = lambda: (\n    " + invalid + '    payload.get("value", 17)\n)\n'
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(1, len(raw))
                self.assertIn(raw[0], applicable)
                diagnostic = next(finding for finding in applicable if "python contract annotation requires" in finding)
                self.assertIn("an explanation", diagnostic)
                self.assertNotIn("proof=", diagnostic)

    def test_stale_annotation_is_a_finding_even_without_a_raw_default(self):
        raw, applicable = self.exercise_property(self.annotation + "value = required\n", accepted=False)
        self.assertEqual([], raw)
        self.assertEqual(1, len(applicable))
        self.assertIn("python contract annotation requires", applicable[0])

    def test_accepting_one_candidate_preserves_unannotated_neighbouring_findings(self):
        source = self.annotation + 'path = parsed.path or "/"\nstatus = state.get("completed", 0)\n'
        raw, applicable = self.exercise_property(source, accepted=False)
        self.assertEqual(2, len(raw))
        self.assertEqual([raw[1]], applicable)


if __name__ == "__main__":
    unittest.main()
