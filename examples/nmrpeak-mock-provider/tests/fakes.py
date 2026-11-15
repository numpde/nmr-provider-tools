"""Replace only the LLM carrier and report receiver with deterministic stand-ins."""

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json


class ScriptedEndpoint:
    """Implement the same ModelEndpoint contract used by the HTTP chat carrier."""

    def __init__(self, *, proposals: tuple[dict, ...]) -> None:
        self.proposals = proposals
        self.requests = []
        self.retirement_confirmed = False
        encoded = json.dumps(obj=proposals, sort_keys=True).encode(encoding="utf-8")
        self.identity = json.dumps(obj={
            "configuration_id": "recorded-nmrpeak-interpreter",
            "model": "recorded-tool-responses",
            "configuration_sha256": sha256(string=encoded).hexdigest(),
        }).encode(encoding="utf-8")

    async def complete(self, messages: list[dict], tools: list[dict], *, not_after: float) -> dict:
        index = len(self.requests)
        self.requests.append(deepcopy(x=messages))
        if index >= len(self.proposals):
            raise RuntimeError("The recorded interpreter has no further response")
        return {"role": "assistant", "tool_calls": [{
            "id": str(index), "type": "function",
            "function": {
                "name": "prepare",
                "arguments": json.dumps(obj=self.proposals[index]),
            },
        }]}

    async def retire(self) -> None:
        self.retirement_confirmed = True


@dataclass(kw_only=True)
class ScriptedEndpointFactory:
    proposals: tuple[dict, ...]
    context_bytes: int
    endpoints: list[ScriptedEndpoint] = field(default_factory=list, init=False)

    def __call__(self, *, exchange_seconds: float, connect_seconds: float) -> ScriptedEndpoint:
        endpoint = ScriptedEndpoint(proposals=self.proposals)
        self.endpoints.append(endpoint)
        return endpoint


class RecordingReceiver:
    """Return means in-memory acceptance, never durable API publication."""

    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    async def __call__(self, payload: bytes) -> None:
        self.payloads.append(payload)
