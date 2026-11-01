"""Opt-in enforcement of a caller's restricted-probe configuration.

This checks requested and reported Docker configuration. It is not a claim that
unit tests establish the engine's runtime sandbox enforcement.
"""

from .model import StepError


# Caller-selected restrictions, not requirements of identity mapping itself.
# repo-check: allow-module-constant
_RESTRICTIONS = (
    ("--network", "none", "NetworkMode", "none"),
    ("--read-only", None, "ReadonlyRootfs", True),
    ("--cap-drop", "ALL", "CapDrop", ["ALL"]),
    ("--security-opt", "no-new-privileges:true", "SecurityOpt", ["no-new-privileges:true"]),
)


def arguments() -> list[str]:
    result = []
    for flag, value, _field, _expected in _RESTRICTIONS:
        result.append(flag)
        if value is not None:
            result.append(value)
    return result


def verify(container) -> None:
    settings = container.isolation
    for _flag, _argument, field, expected in _RESTRICTIONS:
        actual = settings[field]
        if type(actual) is not type(expected) or actual != expected:
            raise StepError("confinement", "probe differs from the requested confinement policy")
    if (settings["Privileged"] is not False or settings["PidMode"] not in (None, "")
            or any(settings[key] not in (None, []) for key in ("CapAdd", "Devices", "DeviceRequests"))
            or container.mounts != []):
        raise StepError("confinement", "probe acquired forbidden privileges, devices or mounts")
