"""Explicit policy and bounded, credential-free operation results."""

from dataclasses import dataclass
import math
from pathlib import Path
import re


class StepError(RuntimeError):
    """A sanitized failure suitable for structured output."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Bounds:
    operation_seconds: float
    command_seconds: float
    cleanup_seconds: float
    lifetime_seconds: int

    def validate(self) -> None:
        for value in (self.operation_seconds, self.command_seconds, self.cleanup_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 3600:
                raise StepError("invalid-policy", "time bounds must be finite and between zero and 3600 seconds")
        if type(self.lifetime_seconds) is not int or not 1 <= self.lifetime_seconds <= 3600:
            raise StepError("invalid-policy", "probe lifetime must be an integer between 1 and 3600 seconds")


@dataclass(frozen=True)
class ProbeRequest:
    context: str
    image: str
    user_uid: int
    user_gid: int
    userns_mode: str
    requested_uids: tuple[int, ...]
    requested_gids: tuple[int, ...]
    journal_path: Path
    bounds: Bounds

    def validate(self) -> None:
        if type(self.bounds) is not Bounds:
            raise StepError("invalid-policy", "explicit operation bounds are required")
        self.bounds.validate()
        if not isinstance(self.context, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.context) is None:
            raise StepError("invalid-policy", "context must be an explicit Docker context name")
        for value, label, maximum in ((self.context, "context", 128), (self.image, "image", 512)):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]*", value) or len(value) > maximum:
                raise StepError("invalid-policy", f"{label} must be an explicit bounded identifier")
        if self.userns_mode not in ("default", "host"):
            raise StepError("invalid-policy", "user namespace policy must be default or host")
        if (type(self.requested_uids) is not tuple or type(self.requested_gids) is not tuple
                or not 1 <= len(self.requested_uids) <= 256 or not 1 <= len(self.requested_gids) <= 256):
            raise StepError("invalid-policy", "request immutable tuples of 1 through 256 IDs of each kind")
        ids = (self.user_uid, self.user_gid, *self.requested_uids, *self.requested_gids)
        if any(type(value) is not int or not 0 <= value < 2**32 - 1 for value in ids):
            raise StepError("invalid-policy", "IDs must be integers from 0 through 4294967294")
        if len(set(self.requested_uids)) != len(self.requested_uids) or len(set(self.requested_gids)) != len(self.requested_gids):
            raise StepError("invalid-policy", "requested IDs must be unique")
        if (not isinstance(self.journal_path, Path) or not self.journal_path.is_absolute()
                or len(str(self.journal_path).encode("utf-8")) > 4096):
            raise StepError("invalid-policy", "journal path must be an explicit absolute Path")


class ProbeFailure(RuntimeError):
    """No mapped identity is released when any operation stage fails."""

    def __init__(self, *, category: str, message: str, state: str, token: str,
                 container_id: str | None, journal_path: str, cleanup_failures: tuple[str, ...]):
        super().__init__(message)
        self.category = category
        self.state = state
        self.token = token
        self.container_id = container_id
        self.journal_path = journal_path
        self.cleanup_failures = cleanup_failures

    def as_dict(self) -> dict:
        return {"ok": False, "category": self.category, "message": str(self), "state": self.state,
                "operation_token": self.token, "container_id": self.container_id,
                "journal_path": self.journal_path, "cleanup_failures": list(self.cleanup_failures)}
