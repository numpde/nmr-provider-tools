"""Install an engine-scoped user timer backed only by native daemon GC policy."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


# Installer profile only; live collection reads the operator's native file.
# repo-check: allow-module-constant
DEFAULTS = {
    "enabled": True,
    "defaultReservedSpace": "10GB",
    "defaultMaxUsedSpace": "32GB",
    "defaultMinFreeSpace": "20GB",
}


def native_config(path):
    if (
        not path.is_absolute()
        or path.resolve() != path
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
    ):
        raise ValueError("Select the canonical caller-owned native daemon.json file.")

    configuration_bytes = path.read_bytes()
    document = json.loads(configuration_bytes)
    if type(document) is not dict:
        raise ValueError("The native engine configuration must be a JSON object.")
    return configuration_bytes, document


def configured(document):
    # Only these native settings change. Route, storage, logging and unrelated
    # builder settings remain their existing JSON values.
    proposed_configuration = json.loads(json.dumps(document))

    # silent-defaults: allow-contract -- Installation creates absent native GC objects; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_native_policy_merge_preserves_unrelated_fields_and_runtime_reads_it
    builder_settings = proposed_configuration.setdefault("builder", {})
    if type(builder_settings) is not dict:
        raise ValueError("Native builder configuration is not an object.")

    # silent-defaults: allow-contract -- Installation creates absent native GC objects; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_native_policy_merge_preserves_unrelated_fields_and_runtime_reads_it
    gc_settings = builder_settings.setdefault("gc", {})
    if type(gc_settings) is not dict:
        raise ValueError("Native builder.gc configuration is not an object.")
    gc_settings.update(DEFAULTS)
    limits(proposed_configuration)
    return proposed_configuration


def limits(document):
    gc_settings = document["builder"]["gc"]
    if type(gc_settings) is not dict or type(gc_settings.get("enabled")) is not bool:
        raise ValueError("Native builder.gc must explicitly enable or disable cache collection.")
    if not gc_settings["enabled"]:
        return None
    if gc_settings.get("policy"):
        raise ValueError(
            "A custom native GC policy cannot be reproduced by this single threshold-based prune timer."
        )

    thresholds = []
    for setting in ("defaultReservedSpace", "defaultMaxUsedSpace", "defaultMinFreeSpace"):
        threshold = gc_settings.get(setting)
        if type(threshold) not in (str, int) or type(threshold) is str and (
            not threshold or any(character.isspace() or ord(character) < 32 for character in threshold)
        ):
            raise ValueError(f"Native builder.gc.{setting} requires its literal space threshold.")
        thresholds.append(str(threshold))
    return thresholds


def argument(value):
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Timer arguments cannot contain empty values or control characters.")
    # Systemd specifiers remain literal. ExecStart's ':' prefix below disables
    # environment substitution; no shell interprets these captured arguments.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def unit_files(root, context, endpoint, config):
    if not endpoint.startswith("unix:///"):
        raise ValueError("Cache collection requires the selected local Unix engine.")
    name = "engine-cache-gc-" + hashlib.sha256(endpoint.encode()).hexdigest()[:16]
    command = " ".join(argument(value) for value in (
        "/bin/bash", str(root / "tools/engine_cache_gc.sh"), "run", context, endpoint, str(config)))
    service = ("[Unit]\nDescription=Unused BuildKit cache collection for one local engine\n\n"
               "[Service]\nType=oneshot\nTimeoutStartSec=360\nExecStart=:" + command + "\n")
    timer = ("[Unit]\nDescription=Periodic unused BuildKit cache collection\n\n"
             "[Timer]\nOnCalendar=hourly\nRandomizedDelaySec=15min\nPersistent=true\n"
             f"Unit={name}.service\n\n[Install]\nWantedBy=timers.target\n")
    return name, service, timer


def install(root, context, endpoint, path):
    (raw, document) = native_config(path)
    candidate = configured(document)
    (name, service, timer) = unit_files(root, context, endpoint, path)
    (descriptor, temporary) = tempfile.mkstemp(prefix=".daemon-gc-", suffix=".json", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), stat.S_IMODE(path.stat().st_mode))
            json.dump(candidate, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        # Validation never starts or restarts the selected daemon. Reject the
        # candidate before changing the operator's native policy file.
        result = subprocess.run(["dockerd", "--validate", "--config-file", str(temporary)],
                                capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise ValueError("dockerd rejected the proposed native GC configuration; the original file is unchanged.")
        if path.read_bytes() != raw:
            raise ValueError("Native engine configuration changed during validation; retry against the current file.")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        configuration = subprocess.run(["systemd-path", "user-configuration"], check=True,
                                       capture_output=True, text=True, timeout=10).stdout.strip()
        units = Path(configuration) / "systemd/user"
        units.mkdir(parents=True, exist_ok=True)
        (units / (name + ".service")).write_text(service)
        (units / (name + ".timer")).write_text(timer)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
        subprocess.run(["systemctl", "--user", "enable", "--now", name + ".timer"], check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Native GC settings were saved, but timer installation or activation could not be confirmed. Inspect the user timer and retry installation; no engine was restarted.") from None
    print(f"Installed {name}.timer. The timer reads native GC thresholds each run. Native daemon policy applies at its next planned restart; no engine was restarted.")


def main():
    try:
        if sys.argv[1:2] == ["policy"] and len(sys.argv) == 3:
            (_, document) = native_config(Path(sys.argv[2]))
            values = limits(document)
            if values is None:
                print("disabled")
            else:
                print("\n".join(values))
        elif sys.argv[1:2] == ["install"] and len(sys.argv) == 6:
            (root, context, endpoint, config) = sys.argv[2:]
            install(Path(root), context, endpoint, Path(config))
        else:
            raise ValueError("Use the maintained cache-GC installation target.")
        return 0
    # silent-defaults: allow-exception-outcome -- Failed installation exits with failure; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_timer_failure_reports_saved_native_policy
    except (ValueError, RuntimeError) as failure:
        print(f"Cannot configure/run engine cache GC: {failure}", file=sys.stderr)
        return 2
    # silent-defaults: allow-exception-outcome -- Host command/config failure exits unsuccessfully; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_invalid_dockerd_candidate_never_replaces_native_json_or_installs_timer
    except (OSError, subprocess.SubprocessError, AttributeError, KeyError, TypeError):
        print("Cannot configure/run engine cache GC: a required file or command failed; inspect the native config and user timer. No engine restart was requested.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
