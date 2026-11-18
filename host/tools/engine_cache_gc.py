"""Install an engine-scoped user timer backed only by native daemon GC policy."""

import hashlib
import json
import os
import pathlib
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


def native_config(daemon_config_path):
    if (
        not daemon_config_path.is_absolute()
        or daemon_config_path.resolve() != daemon_config_path
        or daemon_config_path.is_symlink()
        or not daemon_config_path.is_file()
        or daemon_config_path.stat().st_uid != os.getuid()
    ):
        raise ValueError("Select the canonical caller-owned native daemon.json file.")

    configuration_bytes = daemon_config_path.read_bytes()
    document = json.loads(configuration_bytes)

    if not (type(document) is (expected_type := dict)):
        raise TypeError(f"{type(document)=} ≠ {expected_type=}")

    return (configuration_bytes, document)


def configured(document):
    # Only these native settings change. Route, storage, logging and unrelated
    # builder settings remain their existing JSON values.
    proposed_configuration = json.loads(json.dumps(document))

    # silent-defaults: allow-contract -- Installation creates absent native GC objects; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_native_policy_merge_preserves_unrelated_fields_and_runtime_reads_it
    builder_settings = proposed_configuration.setdefault("builder", {})

    if not (type(builder_settings) is (expected_type := dict)):
        raise TypeError(f"{type(builder_settings)=} ≠ {expected_type=}")

    # silent-defaults: allow-contract -- Installation creates absent native GC objects; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_native_policy_merge_preserves_unrelated_fields_and_runtime_reads_it
    gc_settings = builder_settings.setdefault("gc", {})

    if not (type(gc_settings) is (expected_type := dict)):
        raise TypeError(f"{type(gc_settings)=} ≠ {expected_type=}")

    gc_settings.update(DEFAULTS)
    limits(proposed_configuration)

    return proposed_configuration


def limits(document):
    gc_settings = document["builder"]["gc"]

    if not (type(gc_settings) is (expected_type := dict)):
        raise TypeError(f"{type(gc_settings)=} ≠ {expected_type=}")

    if not (type((gc_settings_enabled := gc_settings.get("enabled"))) is (expected_type := bool)):
        raise TypeError(
            f"{type(gc_settings_enabled)=} ≠ {expected_type=}"
        )

    if not gc_settings["enabled"]:
        return None

    if gc_settings.get("policy"):
        raise ValueError("A custom native GC policy cannot be reproduced by this single threshold-based prune timer.")

    thresholds = []

    for setting in ("defaultReservedSpace", "defaultMaxUsedSpace", "defaultMinFreeSpace"):
        threshold = gc_settings.get(setting)

        if not (type(threshold) in (expected_threshold_type := (str, int))):
            raise TypeError(f"{type(threshold)=} ≠ {expected_threshold_type=}")

        if type(threshold) is str and (
            not threshold or any(character.isspace() or ord(character) < 32 for character in threshold)
        ):
            raise ValueError(f"Native builder.gc.{setting} requires its literal space threshold.")

        thresholds.append(str(threshold))

    return thresholds


def argument(command_argument):
    if not command_argument or any(ord(c) < 32 or ord(c) == 127 for c in command_argument):
        raise ValueError("Timer arguments cannot contain empty values or control characters.")

    # Systemd specifiers remain literal. ExecStart's ':' prefix below disables
    # environment substitution; no shell interprets these captured arguments.
    return '"' + command_argument.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def unit_files(root, context, endpoint, config):
    if not endpoint.startswith("unix:///"):
        raise ValueError("Cache collection requires the selected local Unix engine.")

    name = "engine-cache-gc-" + hashlib.sha256(endpoint.encode()).hexdigest()[:16]

    command = " ".join(
        argument(command_argument)
        for command_argument in (
            "/bin/bash",
            str(root / "host/tools/engine_cache_gc.sh"),
            "run",
            context,
            endpoint,
            str(config),
        )
    )

    service = ("[Unit]\nDescription=Unused BuildKit cache collection for one local engine\n\n"
               "[Service]\nType=oneshot\nTimeoutStartSec=360\nExecStart=:" + command + "\n")

    timer = (f"[Unit]\nDescription=Periodic unused BuildKit cache collection\n\n"
             f"[Timer]\nOnCalendar=hourly\nRandomizedDelaySec=15min\nPersistent=true\n"
             f"Unit={name}.service\n\n[Install]\nWantedBy=timers.target\n")

    return (name, service, timer)


def install(root, context, endpoint, daemon_config_path):
    (original_bytes, document) = native_config(daemon_config_path)
    proposed_configuration = configured(document)
    (unit_name, service_document, timer_document) = unit_files(root, context, endpoint, daemon_config_path)

    (temporary_fd, temporary_name) = tempfile.mkstemp(
        prefix=".daemon-gc-",
        suffix=".json",
        dir=daemon_config_path.parent,
    )

    temporary_path = pathlib.Path(temporary_name)

    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), stat.S_IMODE(daemon_config_path.stat().st_mode))
            json.dump(proposed_configuration, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())

        # Validation never starts or restarts the selected daemon. Reject the
        # candidate before changing the operator's native policy file.
        validation = subprocess.run(
            ["dockerd", "--validate", "--config-file", str(temporary_path)],
            capture_output=True,
            timeout=30,
            check=False,
        )

        if validation.returncode:
            raise ValueError(
                "dockerd rejected the proposed native GC configuration; "
                "the original file is unchanged."
            )

        if daemon_config_path.read_bytes() != original_bytes:
            raise ValueError(
                "Native engine configuration changed during validation; "
                "retry against the current file."
            )

        temporary_path.replace(daemon_config_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    try:
        configuration_directory = subprocess.run(
            ["systemd-path", "user-configuration"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

        unit_directory = pathlib.Path(configuration_directory) / "systemd/user"

        unit_directory.mkdir(parents=True, exist_ok=True)
        (unit_directory / (unit_name + ".service")).write_text(service_document)
        (unit_directory / (unit_name + ".timer")).write_text(timer_document)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)

        subprocess.run(
            ["systemctl", "--user", "enable", "--now", unit_name + ".timer"],
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(
            "Native GC settings were saved, but timer installation or activation "
            "could not be confirmed. Inspect the user timer and retry installation; "
            "no engine was restarted."
        ) from None

    print(
        f"Installed {unit_name}.timer. The timer reads native GC thresholds each run. "
        f"Native daemon policy applies at its next planned restart; no engine was restarted."
    )


def main():
    try:
        if sys.argv[1:2] == ["policy"] and len(sys.argv) == 3:
            (_, document) = native_config(pathlib.Path(sys.argv[2]))
            thresholds = limits(document)

            if thresholds is None:
                print("disabled")
            else:
                print("\n".join(thresholds))
        elif sys.argv[1:2] == ["install"] and len(sys.argv) == 6:
            (root, context, endpoint, config) = sys.argv[2:]

            install(pathlib.Path(root), context, endpoint, pathlib.Path(config))
        else:
            raise ValueError("Use the maintained cache-GC installation target.")

        return 0
    # silent-defaults: allow-exception-outcome -- Failed installation exits with failure; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_timer_failure_reports_saved_native_policy
    except (TypeError, ValueError, RuntimeError) as failure:
        print(f"Cannot configure/run engine cache GC: {failure}", file=sys.stderr)

        return 2
    # silent-defaults: allow-exception-outcome -- Host command/config failure exits unsuccessfully; proof=tests/repository/test_checker_cache_gc.py::EngineCacheGCTests.test_invalid_dockerd_candidate_never_replaces_native_json_or_installs_timer
    except (OSError, subprocess.SubprocessError, AttributeError, KeyError):
        print("Cannot configure/run engine cache GC: a required file or command failed; inspect the native config and user timer. No engine restart was requested.", file=sys.stderr)

        return 2


if __name__ == "__main__":
    raise SystemExit(main())
