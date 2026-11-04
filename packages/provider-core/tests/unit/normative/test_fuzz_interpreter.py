"""Composed retry, repair and handoff histories must stop at accepted preparation."""

import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from provider_core.chat_errors import ChatFailed
from provider_core.conversation import ToolFinished, ToolRejected, ToolReply
from provider_core.interpreter import Interpreter
from tests.fakes.conversation import call, endpoint


TOOLS = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
         for name in ("inspect", "prepare")]


class InterpreterFaultSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_histories_accept_once_after_retired_failures(self):
        for seed in range(32):
            randomizer = random.Random(seed)
            models, factories, trace, events, effects, deadlines = [], [], [], [], [], {}
            expected_inspections = 0
            visits = randomizer.randint(1, 4)
            for visit in range(visits):
                actions = ["inspect"] * randomizer.randrange(4) + ["reject"] * randomizer.randrange(3)
                randomizer.shuffle(actions)
                replies = []
                for index, action in enumerate(actions):
                    if randomizer.choice((False, True)):
                        replies.append(ChatFailed("http_status", "Synthetic unavailable response.",
                                                  response=SimpleNamespace(status=503, headers=((b"Retry-After", b"0"),))))
                    # silent-defaults: allow-contract -- Rejection scenarios invoke preparation; inspection has no effect.
                    replies.append(call(f"{visit}-{index}", "inspect" if action == "inspect" else "prepare",
                                        {"accepted": False}))
                expected_inspections += actions.count("inspect")
                final = visit == visits - 1
                replies.append(call(f"{visit}-finish", "prepare", {"accepted": True}) if final else
                               ChatFailed("invalid_response", "Synthetic incomplete model reply."))
                trace.append((visit, tuple(actions), len(replies), final))
                model = endpoint(f"model-{visit}", *replies)
                original = model.complete

                # silent-defaults: allow-contract -- Freeze this generated visit and endpoint before the loop advances.
                async def complete(*args, _visit=visit, _original=original, **kwargs):
                    if ("failed request", _visit) in events:
                        previous_failure = max(index for index, event in enumerate(events)
                                               if event == ("failed request", _visit))
                        self.assertIn(("retire", _visit), events[previous_failure + 1:])
                    events.append(("request", _visit))
                    # silent-defaults: allow-contract -- First observation creates this visit's deadline history.
                    deadlines.setdefault(_visit, []).append(kwargs["not_after"])
                    try:
                        return await _original(*args, **kwargs)
                    except ChatFailed:
                        events.append(("failed request", _visit))
                        raise

                model.complete = AsyncMock(side_effect=complete)
                model.retire.side_effect = lambda _visit=visit: events.append(("retire", _visit))

                # silent-defaults: allow-contract -- Freeze the generated factory's identity, not a runtime policy default.
                def create(_model=model, _visit=visit, **grant):
                    if _visit:
                        self.assertEqual(events[-1], ("retire", _visit - 1))
                    events.append(("create", _visit))
                    return _model

                models.append(model)
                model.expected_requests = len(replies)
                factories.append(create)
            unused = AsyncMock(side_effect=AssertionError("Accepted work must not visit another model."))

            async def dispatch(name, arguments, *, not_after):
                events.append((name, arguments["accepted"]))
                if name == "inspect":
                    return ToolReply({"source": "synthetic observation"})
                if not arguments["accepted"]:
                    raise ToolRejected("Use the source's supported interpretation.")
                effects.append("one accepted preparation")
                return ToolFinished(effects[0])

            with self.subTest(seed=seed, trace=tuple(trace)):
                owner = Interpreter(tuple(factories) + (unused,), timeout_seconds=240, context_bytes=1024 * 1024, secrets=())
                with patch("provider_core.interpreter.asyncio.sleep", new=AsyncMock()):
                    result = await owner.run([], TOOLS, dispatch)
                self.assertEqual(effects, ["one accepted preparation"])
                self.assertEqual(result.value, effects[0])
                self.assertEqual(result.model_visit, visits)
                self.assertTrue(owner.retirement_confirmed)
                unused.assert_not_called()
                self.assertEqual(sum(event[0] == "inspect" for event in events), expected_inspections)
                accepted = events.index(("prepare", True))
                self.assertTrue(all(event[0] == "retire" for event in events[accepted + 1:]))
                for visit, values in deadlines.items():
                    self.assertEqual(len(set(values)), 1)
                    self.assertEqual(models[visit].complete.await_count, models[visit].expected_requests)
