"""Build and verify the selected restricted-probe configuration."""

from collections.abc import Iterator

from .model import StepError


# Creation flags and corresponding inspected settings.
# repo-check: allow-module-constant
_RESTRICTIONS = (
    ("--network", "none", "NetworkMode", "none"),
    ("--read-only", None, "ReadonlyRootfs", True),
    ("--cap-drop", "ALL", "CapDrop", ["ALL"]),
    ("--security-opt", "no-new-privileges:true", "SecurityOpt", ["no-new-privileges:true"]),
)


def arguments() -> Iterator[str]:
    for flag, value, _field, _expected in _RESTRICTIONS:
        yield flag
        if value is not None:
            yield value


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
