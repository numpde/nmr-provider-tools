"""One definition of what we create and what we are allowed to remove."""

from dataclasses import dataclass
import re

from .model import StepError, valid_id


# Shared owned-probe label wire name; creation and verification must agree.
# repo-check: allow-module-constant
OWNER_LABEL = "org.nmr.provider-deploy.operation"
# Shared operation-token label wire name; creation and verification must agree.
# repo-check: allow-module-constant
TOKEN_LABEL = "org.nmr.provider-deploy.token"
# Versioned owned-probe protocol identifier, not caller policy.
# repo-check: allow-module-constant
OWNER = "identity-v2"
# Finite inert probe payload; the caller supplies its lifetime and interpreter.
# repo-check: allow-module-constant
PROBE_SCRIPT = "import sys,time; time.sleep(int(sys.argv[1]))"
# Docker flag, argument (None for switches), inspect field, exact expected value.
# Single isolation contract for both creation flags and removal authorization.
# repo-check: allow-module-constant
_RESTRICTIONS = (
    ("--rm", None, "AutoRemove", True),
    ("--network", "none", "NetworkMode", "none"),
    ("--read-only", None, "ReadonlyRootfs", True),
    ("--cap-drop", "ALL", "CapDrop", ["ALL"]),
    ("--security-opt", "no-new-privileges:true", "SecurityOpt", ["no-new-privileges:true"]),
    ("--pids-limit", "8", "PidsLimit", 8),
    ("--memory", "64m", "Memory", 67108864),
    ("--memory-swap", "64m", "MemorySwap", 67108864),
    ("--cpus", "0.25", "NanoCpus", 250000000),
)
# Immutable inspect-field inventory derived from the isolation contract.
# repo-check: allow-tuple-generator -- Freeze the shared inspect-field inventory so projection and verification cannot diverge through mutation.
# repo-check: allow-module-constant
ISOLATION_FIELDS = tuple(row[2] for row in _RESTRICTIONS) + (
    "Privileged", "PidMode", "CapAdd", "Devices", "DeviceRequests", "RestartPolicy",
)


@dataclass(frozen=True)
class ProbeSpec:
    token: str
    image_id: str
    interpreter: str
    uid: int
    gid: int
    userns_mode: str
    lifetime_seconds: int

    def __post_init__(self):
        if (not isinstance(self.token, str) or re.fullmatch(r"[0-9a-f]{32}", self.token) is None
                or not isinstance(self.image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id) is None
                or not valid_id(self.uid) or not valid_id(self.gid)
                or self.userns_mode not in ("default", "host")
                or type(self.lifetime_seconds) is not int or not 1 <= self.lifetime_seconds <= 3600
                or not isinstance(self.interpreter, str) or not self.interpreter.startswith("/")
                or len(self.interpreter) > 512 or any(ord(c) < 32 for c in self.interpreter)):
            raise StepError("invalid-policy", "invalid owned probe specification")

    @property
    def name(self) -> str:
        return "provider-deploy-" + self.token

    @property
    def command(self) -> tuple[str, ...]:
        return ("-I", "-S", "-B", "-c", PROBE_SCRIPT, str(self.lifetime_seconds), self.token)

    def create_arguments(self) -> list[str]:
        args = ["container", "create", "--pull=never", "--name", self.name,
                "--label", OWNER_LABEL + "=" + OWNER, "--label", TOKEN_LABEL + "=" + self.token,
                "--user", f"{self.uid}:{self.gid}", "--restart", "no", "--no-healthcheck"]
        for flag, argument, _field, _expected in _RESTRICTIONS:
            args.append(flag)
            if argument is not None:
                args.append(argument)
        if self.userns_mode == "host":
            args.append("--userns=host")
        return args + ["--entrypoint", self.interpreter, self.image_id, *self.command]

    def verify(self, container, cid: str) -> None:
        # silent-defaults: allow-contract -- Docker inspect encodes the explicitly selected default namespace as empty text, and host selection as host; this is wire encoding, not policy fallback.
        expected_userns_mode = "" if self.userns_mode == "default" else "host"
        if (container.cid != cid or container.name != self.name or container.token != self.token
                or container.owner != OWNER or container.image_id != self.image_id
                or container.user != f"{self.uid}:{self.gid}"
                or container.userns_mode != expected_userns_mode
                or tuple(container.entrypoint) != (self.interpreter,)
                or tuple(container.command) != self.command):
            raise StepError("ownership", "probe ownership or configuration does not match its specification")

        isolation = container.isolation
        for _flag, _argument, field, expected in _RESTRICTIONS:
            actual = isolation.get(field)
            if type(actual) is not type(expected) or actual != expected:
                raise StepError("ownership", "probe isolation differs from its specification")
        if (isolation.get("Privileged") is not False or isolation.get("PidMode") not in (None, "")
                or any(isolation.get(key) not in (None, []) for key in ("CapAdd", "Devices", "DeviceRequests"))
                or not isinstance(isolation.get("RestartPolicy"), dict)
                or isolation["RestartPolicy"].get("Name") not in ("", "no") or container.mounts != []):
            raise StepError("ownership", "probe acquired unexpected privileges, devices or mounts")
