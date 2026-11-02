# Add-on contracts and necessity ledger

## The boundary

The core is `core.py` plus the root re-exports. Its records contain kernel rows;
its `MapRows.uid_rows` and `.gid_rows` are raw iterables, not translating maps. Explicit `IdMap(rows)`
construction asserts the affine conversion precondition; it does not check it.
A supplied `read_file(name) -> bytes` must return kernel-format rows for
`uid_map` and `gid_map`. This is a narrow data boundary, not an arbitrary-file or plugin API. Supplied readers are responsible
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
   disagreement after consuming both row generators into reusable lists inside
   the scope. Deferred parsing errors are normalized there. It still returns
   raw rows, not host-qualified values.
   Context exit checks reader/process identity again.
4. `observe_host_process` wraps rows in `IdMap` only inside the qualified
   scope; successful exit earns its host conversion precondition.
5. The Docker adapter additionally requires initial PID coordinates and brackets
   the map reads with ownership-token checks **inside that same pinned scope**.
6. The managed probe releases maps only after engine/container/process checks
   and confirmed retirement. The shell adapter then projects every requested ID
   before printing anything.

An exception at any point invalidates the observation. Values captured inside a
context must not be used if its exit fails. Repeated evidence is not an atomic
snapshot and cannot detect every transient change or namespace ABA. Opening a
target namespace FD is deliberately not required: it adds ptrace permissions
that readable map files do not require.

`IdMap` is a neutral arithmetic value, not an unforgeable certificate. Direct
construction assumes usable affine ranges in the intended outside coordinates;
it does not check arbitrary data. Generic `consistent_maps` cannot earn host
provenance merely by seeing the same bytes twice. Host observations establish
that stronger precondition through their checked sources, not a specialized
map class.

The distinction is semantic, not an overflow patch. Linux
[prints only the projected first outside ID of each extent](https://github.com/torvalds/linux/blob/v6.17/kernel/user_namespace.c#L610-L650)
and preserves its count; an unmapped projection
[returns UINT32_MAX](https://github.com/torvalds/linux/blob/v6.17/kernel/user_namespace.c#L417-L445).
A fragmented sibling reader can also receive ordinary-looking rows that do not
encode the full conversion. No byte-only range check can reconstruct it. Some
noninitial namespace relationships do allow affine conversion: self-to-parent
is exercised by a real isolated-child scenario and uses the same explicit
`IdMap`. Its output-ID bounds guard refuses unusable results but does not repair
fragmented-reader information loss.

## Scenarios that earn retained capabilities

All paths below are under `tests/`. Simulated-engine evidence proves control
flow, not actual Docker isolation or daemon behavior.

| Capability | Without it → with it | Maintained evidence |
|---|---|---|
| Standalone core | No Docker/add-on installation → actual tokenless child maps still readable | `test_core.py::test_two_file_core_reads_real_tokenless_child_without_addons_or_path` physically copies only two files and empties PATH |
| Checked source / host refinement | Raw rows readable in a nested USER namespace but not certified host IDs → explicit host refinement refuses; coherent source alone still works | `test_addon_scenarios.py::test_existing_tokenless_process_needs_neither_docker_nor_host_qualification` uses genuine procfs with no substitutions |
| Raw evidence versus conversion | Real isolated child reading its ancestor sees an unmapped sentinel; legal source-model sibling readers see identical rows but require different conversions → raw row streams offer no translation, host qualification refuses the child | `test_map_boundary.py` isolates `unshare` in a subprocess; its fragmented-reader test is explicitly a kernel-source model, not a privileged integration test |
| Neutral affine arithmetic | A valid self-to-parent mapping is useful without host qualification → explicit `IdMap(rows)` converts it while host observation still refuses the source | `test_map_boundary.py::test_real_self_map_supports_explicit_nonhost_parent_conversion` creates and maps only a short-lived child user namespace; output sentinel/overflow cases are separately refused |
| Repeated observations | One sample accepts first rows → changed second UID sample is refused | `test_addon_scenarios.py::test_repeated_source_rejects_change_that_single_sample_cannot_detect` |
| Pinned process lifetime | Rows read before process exit remain ordinary raw data → checked scope refuses successful release after exit | `test_addon_scenarios.py::test_process_exit_is_detected_before_qualified_scope_returns`; `test_host_addon.py` covers PID reuse, token mismatch, proc replacement |
| Docker PID coordinates | Caller-visible process maps are meaningful without initial PID namespace → daemon host PIDs require the additional guard | `test_host_addon.py::test_nested_pid_namespace_rejected_even_if_user_initial` uses controlled namespace evidence |
| Managed image probe | Before deployment there is no application PID to inspect → temporary owned image process supplies maps, then is retired | `test_addon_scenarios.py::test_image_before_deployment_supplies_maps_without_an_existing_application` plus external consumer permission-setup scenarios |
| Selected confinement | A map-bearing privileged/mounted configuration is sufficient for mapping alone → a caller that selects restricted configuration refuses it before start; clean selected configuration succeeds | `test_addon_scenarios.py::test_confinement_is_selected_policy_not_a_map_requirement`; module-unavailable scenario proves base path independence |
| Python startup isolation | An injected `.pth` startup hook can run without `-S` → inert isolated probe does not run it | `test_python_runtime.py::test_probe_skips_site_startup_hooks` real subprocess scenario |
| Bounded client supervision | A client can hang or leave descendants after exiting → bounded output/cancellation, group signaling before leader reap, and explicit wait ownership | `test_client_supervision.py` real closed/inherited-pipe descendants, lost ownership and paired failures; `test_docker.py` timeout, cancellation and output bounds |
| Lost-reply reconciliation | Repeating create can duplicate resources; treating absent reply as absence loses ownership → token-based read-only reconciliation, no second create, honest uncertainty | `test_probe_addon.py::test_lost_creation_reply_reconciles_without_second_create`; `test_probe_faults.py` mutation fault scenarios |
| Minimal operator reference | An unresolved reply does not identify which engine/resource to inspect → emitted endpoint/daemon/token/container ID selects the owned item among foreign entries | `test_addon_scenarios.py::test_lost_reply_reference_finds_owned_resource_without_adopting_foreign_one` actually consumes every retained reference field |
| Cleanup after observation failure | A second acquisition sample can fail parsing → qualified boundary normalizes it and verified-owned removal still occurs | `test_addon_scenarios.py::test_invalid_acquisition_sample_does_not_skip_verified_cleanup` |
| Failed acquisition still owns its evidence | A second sample can expose another process, or fail before identifying a reported PID → retain every observed identity and require unresolved reported PIDs to be absent | `test_failed_acquisition_obligations.py`; `test_retirement_polling.py` preserves same-run zombie retirement while refusing new-run drift |
| Exit or restart after observation | A finite probe can exit before cleanup begins → keep completed maps only after its exact process retirement is confirmed; a detected replacement is refused and adds a retirement obligation | `test_lifecycle_outcomes.py::test_normal_exit_after_observation_needs_retirement_not_live_resampling`; restart and unidentified-PID scenarios retain both cleanup obligations |
| Argument preparation before dispatch | An unavailable selected policy can fail while generating arguments → fully consume them before creation becomes uncertain | `test_addon_scenarios.py::test_selected_confinement_is_loaded_before_creation_can_be_uncertain` |
| Atomic shell projection | Streaming IDs can leave usable-looking partial output → one unmapped projection leaves stdout empty | `test_cli.py::test_unmapped_projection_does_not_print_partial_values`; ordered/duplicate projection tests and external shell caller scenarios |
| Ownership protocol separation | A similarly named older protocol resource could be adopted → different owner protocol never authorizes start/removal | `test_addon_scenarios.py::test_previous_owner_protocol_cannot_authorize_adoption` |

## Deliberate deletions and non-guarantees

- No general hostile/saved-map validator or overlap/range framework: no supported
  caller imports arbitrary datasets. Kernel validation of stored maps does not
  make every reader-relative printed row an affine conversion.
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
