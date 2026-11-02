# Implementation design

`linux-idmap` separates reading kernel-reported rows, assigning a coordinate
system, and managing a temporary process. The [public reference](REFERENCE.md)
defines the caller-facing contracts; this document describes their implementation.

## Module boundaries

| Module | Responsibility |
|---|---|
| `core.py`, exported by `__init__.py` | Row parsing, caller-supplied file readers, raw process maps and explicit affine arithmetic. |
| `process.py` | Host-qualified observation of an existing process. |
| `probe.py` | The public create, start, observe and return workflow for a local image. |
| `_procfs.py`, `_files.py`, `_errors.py` | Pinned procfs reads, reader and process identity, repeated map samples and identity errors. |
| `_probe_process.py` | Host PID qualification, probe-token checks and process-retirement observations. |
| `_probe_scope.py` | Ownership of one probe, its recorded process lifetimes, phase budgets and cleanup. |
| `_probe_spec.py`, `_confinement.py` | The created container specification and the checks used to establish ownership and confinement. |
| `_docker.py` | Operations on the selected local engine and interpretation of Docker responses. |
| `_command.py` | Bounded Docker client subprocesses, cancellation and process-group cleanup. |
| `_probe_model.py` | Request policy, resource states, results of retirement and structured failures. |
| `_cli.py` | Argument parsing, signal handling, complete projection and output. |

The root package imports only `core.py`. Names in underscore-prefixed modules
are implementation details; callers use the root package, `process` or `probe`.
Provider-specific configuration, permission changes and deployment policy remain
outside the package.

## Raw rows and arithmetic

`read_maps(read_file)` captures the UID and GID byte strings immediately, then
returns lazy row iterators. Parsing preserves the reported numbers and order.
`IdMap` consumes an iterable into its own editable list and applies the ranges
chosen by its caller. The two operations stay separate because reported outside
coordinates depend on the reader's namespace.

## Existing-process observation

`observe_host_process` enters a checked process scope. The scope verifies procfs
and reader visibility, pins the process directory, records its PID and start
time, and requires the reader's initial user namespace. It reads both maps twice
and compares fully parsed samples while the source is still open.

Successful scope exit checks the reader and process identity again. A failed
exit prevents the observation from reaching the caller. The returned maps are
ordinary reusable `IdMap` values; the process directory belongs only to the
observation scope.

## Image observation

`probe_identity` expresses the operation directly:

1. Enter `owned_probe`, qualifying the reader and connecting the selected engine.
2. Resolve the already-local image and create its finite-lived Python probe.
3. Check ownership and confinement, then start the container.
4. Compare observations of the same container run, process identity and maps.
5. Leave the scope, retiring the owned resources and closing the client.
6. Return the maps and retired container ID after successful scope exit.

A single specification defines both creation arguments and the checks that
permit subsequent operations. The probe runs with no networking, a read-only
root filesystem, dropped capabilities and no-new-privileges. Image entrypoint
and startup hooks are replaced by the isolated Python invocation.

The owner retains every observed process identity, including evidence obtained
during a failed sample. A reported PID whose identity could not be established
remains an absence-check obligation. Normal process exit after acquisition is
compatible with success; an observed replacement or inconsistent run prevents
map delivery.

## Retirement and failure ownership

Creation becomes uncertain before dispatch. A lost create reply is reconciled
against the original ownership token, rather than issuing another create.
Retirement uses a separate budget to remove the owned container, confirm absence,
check all recorded process obligations and verify engine continuity.

A failed removal reply can be resolved by those independent checks. Other
acquisition, retirement, client-close and cancellation failures retain their
execution order in `ProbeFailure`. Resource retirement and successful measurement
are separate outcomes.

Each Docker command owns a private process group. Its transport bounds combined
output, checks cancellation, signals the group and then reaps its leader. Keeping
the leader waitable preserves the group's numeric identity until signaling.
The embedding caller must preserve that wait ownership; descendants that leave
the group are outside this supervision contract.

The CLI first completes the probe and every requested translation, then checks
cancellation and formats one output. Its numeric mode exposes no partial list
when a projection fails.
