# Implementation and scenario tests

## How the operations compose

`read_maps(read_file)` calls the supplied reader for `uid_map` and `gid_map`.
The reader returns bytes; the result is `MapRows` containing row iterables.
The process reader supplies bytes from a pinned procfs directory.

The public modules contain the operation implementations: `process.py` reads an
existing process; `probe.py` owns the temporary image probe. The root package
imports only `core.py`. Shared checks, transport and policy records live in
private `_*.py` modules.

The host observation follows this sequence:

1. `coherent_process(pid)` checks procfs and reader visibility, pins the process
   directory, and records its live PID and start time.
2. `qualified_process(pid)` also requires the reader's initial user namespace.
3. `consistent_maps(source.read)` parses both maps twice, consumes the rows
   inside the scope, and rejects differing samples. Context exit checks the
   reader and process identity again.
4. `observe_host_process(pid)` returns the process identity and `IdMap` values
   after the checked scope exits successfully.

The Docker probe adds initial PID-namespace qualification and checks its
ownership token around reads from the same pinned process directory. It releases
maps after container removal, process retirement and engine continuity checks.
The shell adapter translates every requested ID before printing the results.

Use values captured in a checked scope only after successful context exit.
For the relationship between reported rows and affine conversion, see
[Row coordinates](README.md#row-coordinates).

## Scenario tests

All paths below are under `tests/`.

| Capability | Scenario | Tests |
|---|---|---|
| Standalone core | Read a tokenless child’s maps with only the two core files available and an empty PATH. | `test_core.py::test_two_file_core_reads_real_tokenless_child_without_addons_or_path` |
| Host coordinates | Read rows in a nested user namespace; `observe_host_process` rejects that reader. | `test_addon_scenarios.py::test_existing_tokenless_process_needs_neither_docker_nor_host_qualification` |
| Row coordinates | Read an ancestor’s unmapped sentinel in an isolated child; model sibling namespaces whose identical printed rows require different conversions. | `test_map_boundary.py` (real child and kernel-source model) |
| Affine arithmetic | Translate a child’s self-map to its parent’s coordinates. | `test_map_boundary.py::test_real_self_map_supports_explicit_nonhost_parent_conversion` |
| Repeated observations | Reject a changed second UID-map sample. | `test_addon_scenarios.py::test_repeated_source_rejects_change_that_single_sample_cannot_detect` |
| Pinned process lifetime | Detect process exit before returning an observation. | `test_addon_scenarios.py::test_process_exit_is_detected_before_qualified_scope_returns`; `test_host_addon.py` also covers PID reuse and proc replacement |
| Docker PID coordinates | Refuse a daemon host PID when the reader occupies a nested PID namespace. | `test_host_addon.py::test_nested_pid_namespace_rejected_even_if_user_initial` (controlled namespace evidence) |
| Managed image probe | Obtain maps from a local image before an application process exists, then retire the probe. | `test_addon_scenarios.py::test_image_before_deployment_supplies_maps_without_an_existing_application` |
| Selected confinement | Refuse privileged or mounted configuration when confinement is selected; run the base path with the optional module unavailable. | `test_addon_scenarios.py::test_confinement_is_selected_policy_not_a_map_requirement` and the module-unavailable scenario |
| Python startup isolation | Skip an injected `.pth` startup hook. | `test_python_runtime.py::test_probe_skips_site_startup_hooks` |
| Client supervision | Bound a hung client, signal its private group and reap the client, preserving ordered operation and cleanup failures. | `test_client_supervision.py`; `test_docker.py` |
| Lost-reply reconciliation | Find the owned resource after a lost create reply without issuing a second create. | `test_probe_addon.py::test_lost_creation_reply_reconciles_without_second_create`; `test_probe_faults.py` |
| Operator reference | Use endpoint, daemon, token and container ID to locate the owned resource among foreign entries. | `test_addon_scenarios.py::test_lost_reply_reference_finds_owned_resource_without_adopting_foreign_one` |
| Cleanup after failed observation | Remove the owned probe after parsing its second map sample fails. | `test_addon_scenarios.py::test_invalid_acquisition_sample_does_not_skip_verified_cleanup` |
| Process retirement | Retain both identities when samples disagree; check absence of reported PIDs that could not be identified. | `test_failed_acquisition_obligations.py`; `test_retirement_polling.py` |
| Exit and restart | Accept normal exit after observation once retirement is confirmed; reject a detected replacement and retire both processes. | `test_lifecycle_outcomes.py::test_normal_exit_after_observation_needs_retirement_not_live_resampling` and restart scenarios |
| Argument preparation | Fail a selected-policy import before dispatching creation. | `test_addon_scenarios.py::test_selected_confinement_is_loaded_before_creation_can_be_uncertain` |
| Complete shell output | Leave stdout empty when one requested ID is unmapped. | `test_cli.py::test_unmapped_projection_does_not_print_partial_values` |
| Ownership protocol | Refuse start or removal of a resource labeled with an older ownership protocol. | `test_addon_scenarios.py::test_previous_owner_protocol_cannot_authorize_adoption` |
