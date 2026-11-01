"""The caller's policy and the operation's small, explicit outcome vocabulary."""

from dataclasses import asdict, dataclass
from enum import StrEnum
import math
import re

from .linux import ProcessIdentity


class StepError(RuntimeError):
    """An authored, credential-free explanation suitable for operator output."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Bounds:
    operation_seconds: float = 30
    command_seconds: float = 5
    cleanup_seconds: float = 15
    lifetime_seconds: int = 90

    def validate(self) -> None:
        for value in (self.operation_seconds, self.command_seconds, self.cleanup_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 3600:
                raise StepError("invalid-policy", "time bounds must be finite and between zero and 3600 seconds")
        if type(self.lifetime_seconds) is not int or not 1 <= self.lifetime_seconds <= 3600:
            raise StepError("invalid-policy", "probe lifetime must be an integer between 1 and 3600 seconds")


def valid_id(value: int) -> bool:
    return type(value) is int and 0 <= value < 2**32 - 1


@dataclass(frozen=True)
class ProbeRequest:
    endpoint: str
    image: str
    interpreter: str
    user_uid: int
    user_gid: int
    userns_mode: str = "default"
    bounds: Bounds = Bounds()

    def validate(self) -> None:
        if type(self.bounds) is not Bounds:
            raise StepError("invalid-policy", "invalid operation bounds")
        self.bounds.validate()
        if not isinstance(self.endpoint, str) or not self.endpoint.startswith("unix:///") or len(self.endpoint) > 4096:
            raise StepError("invalid-policy", "use the caller-selected local Unix Docker endpoint")
        if not isinstance(self.image, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,511}", self.image) is None:
            raise StepError("invalid-policy", "image must be an explicit bounded identifier")
        if not valid_id(self.user_uid) or not valid_id(self.user_gid):
            raise StepError("invalid-policy", "IDs must be integers from 0 through 4294967294")
        if self.userns_mode not in ("default", "host"):
            raise StepError("invalid-policy", "user namespace policy must be default or host")
        if not isinstance(self.interpreter, str) or not self.interpreter.startswith("/") or len(self.interpreter) > 512 or any(ord(c) < 32 for c in self.interpreter):
            raise StepError("invalid-policy", "supply an absolute Python interpreter path in the probe image")



class ResourceState(StrEnum):
    NOT_CREATED = "known-not-created"
    UNRESOLVED = "uncertain-outcome"
    RETIRED = "confirmed-retired"


@dataclass(frozen=True)
class Problem:
    stage: str
    category: str
    message: str


@dataclass(frozen=True)
class Retirement:
    state: ResourceState
    container_id: str | None = None
    problems: tuple[Problem, ...] = ()


@dataclass(frozen=True)
class ProbeHandle:
    """Diagnostic ownership evidence, not an instruction to retry a mutation."""

    endpoint: str
    operation_token: str
    container_id: str | None = None
    processes: tuple[ProcessIdentity, ...] = ()
    unobserved_pids: tuple[int, ...] = ()
    daemon_id: str | None = None


class ProbeFailure(RuntimeError):
    """A failed operation never carries usable identity mappings."""

    def __init__(self, problem: Problem, retirement: Retirement, handle: ProbeHandle | None = None):
        super().__init__(problem.message)
        self.problem = problem
        self.retirement = retirement
        self.handle = handle

    def as_dict(self) -> dict:
        # silent-defaults: allow-contract -- A missing diagnostic handle is represented as JSON null, never synthesized ownership evidence; tests/test_cli.py::CliTests.
        return {"ok": False, "problem": asdict(self.problem), **asdict(self.retirement),
                "handle": None if self.handle is None else asdict(self.handle)}
