"""Install an engine-scoped user timer backed only by native daemon GC policy."""

import hashlib
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

if __name__ == "__main__":
    # Load the maintained checkout and its pure type helper under isolated Python
    repository_root = pathlib.Path(__file__).resolve().parents[2]

    sys.path.insert(0, str(repository_root / "packages/type-requirements/src"))
    sys.path.insert(0, str(repository_root))

from operations.docker.adapters.cache_gc import configure_gc, validate_daemon_config_command, write_daemon_config

from operations.docker.validation.upstream.cache_gc import (
    builder_gc,
    decode_daemon_config,
    gc_enabled,
    gc_has_custom_policy,
    gc_thresholds,
)


def native_config(daemon_config_path):
    if (
        (not daemon_config_path.is_absolute()) or
        (daemon_config_path.resolve() != daemon_config_path) or
        daemon_config_path.is_symlink() or
        (not daemon_config_path.is_file()) or
        (daemon_config_path.stat().st_uid != os.getuid())
    ):
        raise ValueError("Select the canonical caller-owned native daemon.json file.")

    configuration_bytes = daemon_config_path.read_bytes()

    return (configuration_bytes, decode_daemon_config(configuration_bytes))


def configured(document):
    # Installer profile only; live collection reads the operator's native file
    proposed_configuration = configure_gc(
        document,
        enabled=True,
        reserved_space="10GB",
        max_used_space="32GB",
        min_free_space="20GB",
    )

    limits(proposed_configuration)

    return proposed_configuration


def limits(document):
    gc_settings = builder_gc(document)

    if not gc_enabled(gc_settings):
        return None

    if gc_has_custom_policy(gc_settings):
        raise ValueError("A custom native GC policy cannot be reproduced by this single threshold-based prune timer.")

    return gc_thresholds(gc_settings)


def argument(command_argument):
    if (not command_argument) or (any(ord(c) < 32 or ord(c) == 127 for c in command_argument)):
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
            str(root / "operations/docker/engine_cache_gc.sh"),
            "run",
            context,
            endpoint,
            str(config),
        )
    )

    service = (
        "[Unit]\n"
        "Description=Unused BuildKit cache collection for one local engine\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=360\n"
        "ExecStart=:" + command + "\n"
    )

    timer = (
        f"[Unit]\n"
        f"Description=Periodic unused BuildKit cache collection\n"
        f"\n"
        f"[Timer]\n"
        f"OnCalendar=hourly\n"
        f"RandomizedDelaySec=15min\n"
        f"Persistent=true\n"
        f"Unit={name}.service\n"
        f"\n"
        f"[Install]\n"
        f"WantedBy=timers.target\n"
    )

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
            write_daemon_config(proposed_configuration, output)
            output.flush()
            os.fsync(output.fileno())

        # Validation never starts or restarts the selected daemon. Reject the
        # candidate before changing the operator's native policy file.
        validation = subprocess.run(
            validate_daemon_config_command(temporary_path),
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
