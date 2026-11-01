# Add-on contracts and necessity ledger

## The boundary

The core is `core.py` plus the root re-exports. Its records contain kernel rows;
it samples, parses and translates. A supplied `read_file(name) -> bytes` must
return kernel-format rows for `uid_map` and `gid_map`. This is a narrow data
boundary, not an arbitrary-file or plugin API. Supplied readers are responsible
for provenance and lifetime; the core does not silently certify them.

An **add-on** is separately imported code that supplies that reader inside an
explicit lifetime, refines a result with an additional check, or composes these
operations for a demonstrated caller. It must state its extra guarantee and
show a scenario in which omitting it loses that guarantee. No mixins, registry
or special-case dispatch table is needed. The one optional configuration uses
an explicit `confine` choice and imports its policy only when selected.

Composition order matters:

1. `coherent_process(pid)` checks genuine procfs and reader visibility, pins the
   process directory and records live PID/start time.
2. `qualified_process(pid)` additionally requires initial USER coordinates.
3. `consistent_maps(source.read)` uses the **core parser twice** and rejects
   disagreement. Context exit checks reader/process identity again.
4. The Docker adapter additionally requires initial PID coordinates and brackets
   the map reads with ownership-token checks **inside that same pinned scope**.
5. The managed probe releases maps only after engine/container/process checks
   and confirmed retirement. The shell adapter then projects every requested ID
   before printing anything.

An exception at any point invalidates the observation. Values captured inside a
context must not be used if its exit fails. Repeated evidence is not an atomic
snapshot and cannot detect every transient change or namespace ABA. Opening a
target namespace FD is deliberately not required: it adds ptrace permissions
that readable map files do not require.

## Scenarios that earn retained capabilities

All paths below are under `tests/`. Simulated-engine evidence proves control
flow, not actual Docker isolation or daemon behavior.

| Capability | Without it → with it | Maintained evidence |
|---|---|---|
| Standalone core | No Docker/add-on installation → actual tokenless child maps still readable | `test_core.py::test_two_file_core_reads_real_tokenless_child_without_addons_or_path` physically copies only two files and empties PATH |
| Checked source / host refinement | Raw rows readable in a nested USER namespace but not certified host IDs → explicit host refinement refuses; coherent source alone still works | `test_addon_scenarios.py::test_existing_tokenless_process_needs_neither_docker_nor_host_qualification` uses genuine procfs with no substitutions |
| Repeated observations | One sample accepts first rows → changed second UID sample is refused | `test_addon_scenarios.py::test_repeated_source_rejects_change_that_single_sample_cannot_detect` |
| Pinned process lifetime | Rows read before process exit remain ordinary raw data → checked scope refuses successful release after exit | `test_addon_scenarios.py::test_process_exit_is_detected_before_qualified_scope_returns`; `test_host_addon.py` covers PID reuse, token mismatch, proc replacement |
| Docker PID coordinates | Caller-visible process maps are meaningful without initial PID namespace → daemon host PIDs require the additional guard | `test_host_addon.py::test_nested_pid_namespace_rejected_even_if_user_initial` uses controlled namespace evidence |
| Managed image probe | Before deployment there is no application PID to inspect → temporary owned image process supplies maps, then is retired | `test_addon_scenarios.py::test_image_before_deployment_supplies_maps_without_an_existing_application` plus external consumer permission-setup scenarios |
| Selected confinement | A map-bearing privileged/mounted configuration is sufficient for mapping alone → a caller that selects restricted configuration refuses it before start; clean selected configuration succeeds | `test_addon_scenarios.py::test_confinement_is_selected_policy_not_a_map_requirement`; module-unavailable scenario proves base path independence |
| Python startup isolation | An injected `.pth` startup hook can run without `-S` → inert isolated probe does not run it | `test_python_runtime.py::test_probe_skips_site_startup_hooks` real subprocess scenario |
| Bounded client supervision | A hanging client or descendant holding pipes can block forever → deadlines/cancellation kill the private group and reap the direct child | `test_docker.py` real timeout, cancellation, output-bound and descendant-pipe scenarios |
| Lost-reply reconciliation | Repeating create can duplicate resources; treating absent reply as absence loses ownership → token-based read-only reconciliation, no second create, honest uncertainty | `test_probe_addon.py::test_lost_creation_reply_reconciles_without_second_create`; `test_probe_faults.py` mutation fault scenarios |
| Minimal operator reference | An unresolved reply does not identify which engine/resource to inspect → emitted endpoint/daemon/token/container ID selects the owned item among foreign entries | `test_addon_scenarios.py::test_lost_reply_reference_finds_owned_resource_without_adopting_foreign_one` actually consumes every retained reference field |
| Cleanup after observation failure | A later parse failure could skip retirement → qualified boundary normalizes it and verified-owned removal still occurs | `test_addon_scenarios.py::test_invalid_retirement_sample_does_not_skip_verified_cleanup` |
| Atomic shell projection | Streaming IDs can leave usable-looking partial output → one unmapped projection leaves stdout empty | `test_cli.py::test_unmapped_projection_does_not_print_partial_values`; ordered/duplicate projection tests and external shell caller scenarios |
| Ownership protocol separation | A similarly named older protocol resource could be adopted → different owner protocol never authorizes start/removal | `test_addon_scenarios.py::test_previous_owner_protocol_cannot_authorize_adoption` |

## Deliberate deletions and non-guarantees

- No general hostile/saved-map validator or overlap/range framework: no supported
  caller imports arbitrary datasets. The kernel already validates its ID maps.
- No fixed memory, CPU or PID quota: the old numbers had no demonstrated
  identity-mapping requirement. This is a documented policy change, not an
  equivalence claim. Callers needing resource budgets need separate qualification.
- No phase taxonomy or serialized process inventory. Cleanup still tracks its
  process obligations internally; the public diagnostic keeps only the minimal
  operator reference and retirement status/problems. It is not a recovery API.
- No root CLI, provider aliases or compatibility layer for the unreleased old API.
- No image pulls, deployment, automatic permission mutation or volume lifecycle.
- Real Docker, rootless/remapped engines and runtime confinement remain an
  explicit qualification gate. Passing mocks and genuine-procfs tests cannot
  substitute for those scenarios.
