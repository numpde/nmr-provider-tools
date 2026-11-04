"""Generated outbox histories preserve the exact outstanding handoff across restart."""

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.delivery_store import DeliveryConflict, DeliveryFull, DeliveryQueue, QueueLimits
from tests.fakes.credential import document, encoded
from tests.fakes.delivery import outcome


class DeliveryFaultSequenceTests(unittest.TestCase):
    def material(self, provider):
        fields = {"origin": "https://api.example", "profile": "dev-local", "topology": "dev-local",
                  "provider_ref": provider, "exchange_seconds": 10, "retirement_seconds": 5,
                  "credential_file": "/synthetic/credential"}
        _, _, material = api_settings_with_material(fields, Mock(return_value=encoded(document(principal_ref=provider))))
        return material

    def check_outstanding(self, queue, expected, providers):
        for provider in providers:
            entries = {item.outcome.execution_attempt_ref: (item, state)
                       for item, state in expected.values() if item.outcome.provider_ref == provider}
            rows = queue.inventory("https://api.example", provider)
            self.assertEqual({row["execution_attempt_ref"] for row in rows}, set(entries))
            for row in rows:
                item, state = entries[row["execution_attempt_ref"]]
                self.assertEqual(row["state"], state)
                self.assertEqual(row["body_sha256"], item.outcome.body_sha256)
                self.assertEqual(row["body_bytes"], len(item.outcome.body))
                self.assertEqual(queue.status(*item.outcome.key), row)
            pending = {ref: item for ref, (item, state) in entries.items() if state == "pending"}
            selected = queue.next_pending_handoff("https://api.example", provider)
            if pending:
                self.assertIsNotNone(selected)
                original = pending[selected.outcome.execution_attempt_ref]
                self.assertEqual(selected.outcome, original.outcome)
                self.assertEqual(selected.publication_bytes(), original.publication_bytes())
            else:
                self.assertIsNone(selected)

    def test_replay_conflict_capacity_and_restart_sequences(self):
        providers = ("provider:first", "provider:second", "provider:third")
        materials = {provider: (self.material(provider), self.material(provider)) for provider in providers}
        limits = QueueLimits(provider_items=3, provider_bytes=16384, total_items=7, total_bytes=65536)
        for seed in range(16):
            randomizer, expected, trace = random.Random(seed), {}, []
            with tempfile.TemporaryDirectory() as directory:
                queue = DeliveryQueue(Path(directory), limits=limits)
                try:
                    for step in range(64):
                        action = randomizer.choice(("enqueue", "enqueue", "replay", "conflict", "hold", "publish", "reopen"))
                        provider = randomizer.choice(providers)
                        suffix = format(randomizer.randrange(8), "x")
                        # silent-defaults: allow-contract -- New intake and restart have no preselected outstanding record.
                        existing_key = (randomizer.choice(tuple(expected))
                                        if expected and action not in ("enqueue", "reopen") else None)
                        # silent-defaults: allow-contract -- Record the actual old key or the attempted new identity.
                        trace.append((action, existing_key if existing_key is not None else (provider, suffix)))
                        with self.subTest(seed=seed, step=step, trace=tuple(trace)):
                            if action == "reopen":
                                queue.close()
                                queue = DeliveryQueue(Path(directory), limits=limits)
                            elif action == "enqueue" or not expected:
                                item = DeliveryHandoff(outcome(provider=provider, suffix=suffix, message=f"result-{seed}-{step}"),
                                                       materials[provider][0])
                                occupied = item.outcome.key in expected
                                provider_count = sum(old.outcome.provider_ref == provider for old, _ in expected.values())
                                if occupied:
                                    with self.assertRaises(DeliveryConflict):
                                        queue.enqueue_handoff(item)
                                elif provider_count >= 3 or len(expected) >= 7:
                                    with self.assertRaises(DeliveryFull):
                                        queue.enqueue_handoff(item)
                                else:
                                    self.assertEqual(queue.enqueue_handoff(item), item.acceptance())
                                    expected[item.outcome.key] = (item, "pending")
                            else:
                                key = existing_key
                                item, state = expected[key]
                                if action == "replay":
                                    self.assertEqual(queue.enqueue_handoff(item), item.acceptance())
                                elif action == "conflict":
                                    changed = DeliveryHandoff(item.outcome, materials[item.outcome.provider_ref][1])
                                    with self.assertRaises(DeliveryConflict):
                                        queue.enqueue_handoff(changed)
                                    changed_body = outcome(provider=item.outcome.provider_ref,
                                                           suffix=item.outcome.execution_attempt_ref[-1], message="different bytes")
                                    with self.assertRaises(DeliveryConflict):
                                        queue.remove_published(changed_body)
                                elif action == "hold":
                                    queue.hold(item.outcome, "Synthetic terminal refusal.")
                                    expected[key] = (item, "held")
                                elif state == "pending":
                                    queue.remove_published(item.outcome)
                                    del expected[key]
                            self.check_outstanding(queue, expected, providers)
                    queue.close()
                    queue = DeliveryQueue(Path(directory), limits=limits)
                    with self.subTest(seed=seed, phase="final restart", trace=tuple(trace)):
                        self.check_outstanding(queue, expected, providers)
                finally:
                    queue.close()
