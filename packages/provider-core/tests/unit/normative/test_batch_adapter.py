"""Derive queue snapshots from operation types without changing worker formats."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from provider_core.batch_call import BatchCall, BatchPolicy
from provider_core.payload import JsonDataclassCodec
from provider_core.process_call import ProcessCall
from provider_core.scientific import Contribution, HostedContribution, operation
from provider_core.scientific_call import ErrorPolicy


@dataclass
class Entry:
    value: int


@dataclass
class Inputs:
    samples: list[Entry]


@dataclass
class Outputs:
    answers: list[int | str]


@dataclass
class Scalar:
    value: int


@dataclass
class Ambiguous:
    first: list[Entry]
    second: list[Entry]


class Science(Contribution):
    @operation()
    def calculate(self, *, batch: Inputs) -> Outputs:
        raise AssertionError("Metadata inspection must not invoke science")


class BatchAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        hosted = HostedContribution(
            contribution=None, contribution_type=Science,
            host="fixture", allocation_name="fixture", requirements=frozenset(),
        )
        self.binding = hosted.call(method=Science.calculate)
        self.call = AsyncMock(spec=ProcessCall, return_value=Outputs([7, "rejected"]))
        self.call.binding = self.binding
        self.call.error_policy = ErrorPolicy()
        self.policy = BatchPolicy(
            maximum_entries=2, collection_seconds=5,
            maximum_queued_input_bytes=4096, maximum_entry_input_bytes=1024,
        )

    async def test_declared_fields_preserve_frozen_inputs_order_and_mixed_replies(self):
        original_formats = (self.binding.input_codec.format, self.binding.result_codec.format)
        batches = BatchCall.from_dataclass(call=self.call, policy=self.policy)
        self.call._claim_batch.assert_not_called()
        self.assertIs(batches.entry_codec.value_type, Entry)
        source = Entry(7)

        async with batches:
            first = await batches.prepare(source)
            second = await batches.prepare(Entry(8))
            source.value = 99
            results = await asyncio.wait_for(asyncio.gather(
                first.execute(require_execution=lambda: None),
                second.execute(require_execution=lambda: None),
            ), 1)
            self.assertEqual(results, [7, "rejected"])
            self.assertEqual(self.call.call_args.kwargs["scientific_input"], Inputs([Entry(7), Entry(8)]))
            await first.retire()
            await second.retire()

        self.assertEqual(original_formats, (self.binding.input_codec.format, self.binding.result_codec.format))
        self.call._release_batch.assert_called_once_with(batches)

    def test_rejects_ambiguous_and_non_list_wrappers_before_invocation(self):
        for value_type in (Scalar, Ambiguous):
            for side in ("input_codec", "result_codec"):
                with self.subTest(value_type=value_type, side=side):
                    codecs = dict(input_codec=self.binding.input_codec, result_codec=self.binding.result_codec)
                    codecs[side] = JsonDataclassCodec.for_type(value_type)
                    self.call.binding = SimpleNamespace(**codecs)
                    with self.assertRaises((TypeError, ValueError)):
                        BatchCall.from_dataclass(call=self.call, policy=self.policy)
        self.call.assert_not_called()

    def test_custom_codecs_require_the_explicit_adapter_interface(self):
        self.call.binding = SimpleNamespace(input_codec=object(), result_codec=self.binding.result_codec)
        with self.assertRaises(TypeError):
            BatchCall.from_dataclass(call=self.call, policy=self.policy)
        self.call.assert_not_called()

    def test_retries_remain_forbidden(self):
        self.call.error_policy = ErrorPolicy(max_attempts=2)
        with self.assertRaises(ValueError):
            BatchCall.from_dataclass(call=self.call, policy=self.policy)
        self.call.assert_not_called()

    def test_unstarted_call_cannot_supply_a_batch_binding(self):
        call = object.__new__(ProcessCall)
        call._invocation = None
        call.method = Science.calculate
        with self.assertRaises(RuntimeError):
            BatchCall.from_dataclass(call=call, policy=self.policy)
