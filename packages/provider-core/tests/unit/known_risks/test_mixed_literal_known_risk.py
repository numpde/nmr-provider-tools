"""Known risk: Pydantic accepts integer 1 as the boolean literal True.

The SDK delegates literal matching to Pydantic, including this strict-mode
behavior. When an upstream fix rejects 1, replace this current-behavior test
with a normative rejection test.
"""

from dataclasses import dataclass
from typing import Literal
import unittest

from provider_core.payload import JsonDataclassCodec


@dataclass
class MixedLiteral:
    flag_or_count: Literal[True, 2]


class MixedLiteralKnownRiskTests(unittest.TestCase):
    def test_integer_one_currently_decodes_as_boolean_true(self):
        codec = JsonDataclassCodec(format="test.mixed-literal.v1", value_type=MixedLiteral)
        decoded = codec.decode(b'{"flag_or_count":1}')

        self.assertIs(expr1=True, expr2=decoded.flag_or_count)
