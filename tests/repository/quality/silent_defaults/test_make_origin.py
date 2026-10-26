"""Prove input-origin forwarding cannot waive an actual Make fallback.

This local classification preserves the raw Dapp/Vote matchers. The current
Makefile's preceding comment records why literal command-line values are owned
inputs rather than replacement policy; native handoff probes remain independent.
"""

from tools.container_guard import require_container

require_container()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import test_make_empty_defaults as vote
from . import test_ops as dapp
from .test_shared_scanner import is_literal_make_origin_gate


class MakeOriginGateTests(unittest.TestCase):
    def findings_from_both_collectors(self, source, line):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(line + "\n")
            with patch.object(dapp, "ROOT", root):
                dapp_findings = dapp.make_function_default_findings([path])
            with patch.object(vote, "_make_surface_lines", return_value=[(source, 1, line)]):
                vote_findings = vote.make_function_default_findings()
            return dapp_findings, vote_findings

    def test_complete_origin_gates_are_classified_without_weakening_raw_matchers(self):
        expression = "$(if $(filter command line,$(origin PROFILE)),$(value PROFILE))"
        for line in (
            "export PROFILE_INPUT := " + expression,
            "override export PROFILE_INPUT := " + expression,
            "  override\texport\tPROFILE_INPUT\t:=\t" + expression,
        ):
            for source in ("Makefile", "tools/inputs.mk"):
                with self.subTest(source=source, line=line):
                    self.assertRegex(line, dapp.MAKE_FUNCTION_DEFAULT_RE)
                    self.assertRegex(line, vote.MAKE_FUNCTION_DEFAULT_RE)
                    self.assertTrue(is_literal_make_origin_gate(source, line))
                    self.assertEqual(([], []), self.findings_from_both_collectors(source, line))

    def test_any_alternate_value_or_owner_remains_a_finding_in_both_collectors(self):
        expression = "$(if $(filter command line,$(origin PROFILE)),$(value PROFILE))"
        gate = "export PROFILE_INPUT := " + expression
        lines = [
            gate[:-1] + ",demo)",
            gate[:-1] + ",)",
            gate.replace("$(value PROFILE)", "$(PROFILE)"),
            gate.replace("$(value PROFILE)", "$(shell true)"),
            gate.replace("$(value PROFILE)", "$(value OTHER)"),
            gate.replace("command line", "environment"),
            gate.replace("command line", "file"),
            "export PROFILE_INPUT := $(or " + expression + ",demo)",
            gate + "$(shell true)",
            gate + "suffix",
            gate + " ",
            gate.replace(":=", "?="),
            gate.replace(":=", "="),
            gate.replace("export ", "override override export "),
            "\t" + gate,
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertFalse(is_literal_make_origin_gate("Makefile", line))
                dapp_findings, vote_findings = self.findings_from_both_collectors("Makefile", line)
                self.assertTrue(dapp_findings)
                self.assertTrue(vote_findings)

    def test_whole_line_gate_is_not_a_waiver_in_other_source_languages(self):
        line = "export PROFILE_INPUT := $(if $(filter command line,$(origin PROFILE)),$(value PROFILE))"
        for source in ("tools/runner.sh", "tools/runner.py", "containers/Dockerfile"):
            with self.subTest(source=source):
                self.assertFalse(is_literal_make_origin_gate(source, line))
                dapp_findings, vote_findings = self.findings_from_both_collectors(source, line)
                self.assertTrue(dapp_findings)
                self.assertTrue(vote_findings)

    def test_raw_conditional_default_assignment_detector_still_matches(self):
        line = "export PROFILE_INPUT ?= $(if $(filter command line,$(origin PROFILE)),$(value PROFILE))"
        self.assertRegex(line, dapp.MAKE_DEFAULT_ASSIGNMENT_RE)
        self.assertRegex(line, vote.MAKE_DEFAULT_ASSIGNMENT_RE)
        self.assertFalse(is_literal_make_origin_gate("Makefile", line))
