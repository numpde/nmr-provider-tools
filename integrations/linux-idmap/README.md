# Provider integrations outside the neutral package

These patches live on the same `design/provider-deploy` review branch
as `linux-idmap`. Each provider retains its endpoint/image selection, inputs,
permissions and deployment policy. Apply these patches in the provider repositories.

`consumers.json` pins each repository's base, patch digest and resulting Git tree.
Apply a patch to its exact base in a disposable detached worktree:

```sh
git -C /path/to/provider-repository worktree add --detach /path/to/clean-review BASE_COMMIT
git -C /path/to/clean-review apply --check /absolute/path/to/CONSUMER.patch
git -C /path/to/clean-review apply --index /absolute/path/to/CONSUMER.patch
git -C /path/to/clean-review write-tree
```

The resulting tree must match the manifest. Install the reviewed `linux-idmap`
0.3.0a4 wheel in the chosen host Python 3.11+ environment, then run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
  /path/to/host-python -m unittest discover -s tests/host -v
```

## Operations and scenario evidence

Both adapters use the managed probe, which returns host-coordinate `IdMap`
values after retiring its container. Tests supply synthetic maps at that boundary.

- **NMRPeak:** imports `linux_idmap.probe.probe_identity`, explicitly
  requests `confine=True`, then translates the returned host-qualified mappings
  for its existing ACL decisions. Its nine host tests import the actual
  deployment module and substitute only the managed probe effect. They check
  the selected endpoint, image, interpreter, user and executable, confinement
  selection, translations, invalid-request refusal, missing-package handling and
  unresolved cleanup diagnostics. The scientific runtime and its guard remain untouched.
- **NMRNet:** invokes `linux-idmap-probe` with direct flags and `--confine`.
  Its nine host tests execute the actual shell adapter and signal traps with
  the real CLI and a synthetic bounded probe effect. They check ordered numeric
  output, rootful/rootless helper selection, invalid-request refusal and
  nonzero/corrupt responses. Process-group and shell-only signals prevent
  subsequent permission-policy selection. The separate inverse mapping in `tools/docker.sh` remains unchanged.

The managed probe is needed because both callers have an image before their
workload process exists. The CLI is needed by the shell caller. Confinement is
selected explicitly to retain no network, a read-only root, dropped capabilities
and no-new-privileges from the former local probe policies. The shared package
now omits fixed memory/swap, PID and CPU quotas: the former local probes used
32 MiB/4 PIDs (NMRPeak) and 64 MiB/8 PIDs (NMRNet). This is a documented policy
change. Qualify it with the provider image and non-root caller, and supply a
resource policy if quotas are required.

## Verification

Before adoption, run the maintained containerized suites and actual
rootful/rootless engines with provider images and non-root callers. A missing
shared package stops the adapter. For unresolved outcomes, use the reported
engine, token and container ID to inspect the owned resource before retrying.
