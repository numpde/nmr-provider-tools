# Provider integrations outside the neutral package

These candidate patches live on the same `design/provider-deploy` review branch
as `linux-idmap`, but are not part of its source directory or wheel. Each provider
retains its endpoint/image selection, inputs, permissions and deployment policy.
There are no separate provider review branches or published dependency releases.

`consumers.json` pins each repository's base, patch digest and resulting Git tree.
Apply a patch to its exact base in a disposable detached worktree:

```sh
git -C /path/to/provider-repository worktree add --detach /path/to/clean-review BASE_COMMIT
git -C /path/to/clean-review apply --check /absolute/path/to/CONSUMER.patch
git -C /path/to/clean-review apply --index /absolute/path/to/CONSUMER.patch
git -C /path/to/clean-review write-tree
```

The resulting tree must match the manifest. Install the reviewed `linux-idmap`
0.3.0a2 wheel in the chosen host Python 3.11+ environment, then run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
  /path/to/host-python -m unittest discover -s tests/host -v
```

## Selected add-ons and scenario evidence

Neither adapter uses unqualified core row tuples as host permission authority.
The managed probe returns `IdMap` values with `.translate()` and `.rows` only
after host qualification. Synthetic fixtures construct the same affine value
explicitly; constructing `IdMap` does not establish host provenance.

- **NMRPeak:** imports `linux_idmap.addons.probe.probe_identity`, explicitly
  requests `confine=True`, then translates the returned host-qualified mappings
  for its existing ACL decisions. Its eight host tests import the actual
  deployment module and substitute only the managed probe effect. They check
  the selected endpoint, image, interpreter, user and executable, confinement
  selection, translations, missing-package refusal and unresolved cleanup
  diagnostics. The scientific runtime and its guard remain untouched.
- **NMRNet:** invokes `linux-idmap-probe` with direct flags and `--confine`.
  Its eight host tests execute the actual shell adapter and signal traps with
  the real CLI and a synthetic bounded probe effect. They check ordered numeric
  output, rootful/rootless helper selection, nonzero/corrupt responses and both
  process-group and shell-only signals without subsequent permission-policy
  selection. The separate inverse mapping in `tools/docker.sh` remains unchanged.

The managed probe is needed because both callers have an image before their
workload process exists. The CLI is needed by the shell caller. Confinement is
selected explicitly to retain no network, a read-only root, dropped capabilities
and no-new-privileges from the former local probe policies. The shared package
now omits fixed memory/swap, PID and CPU quotas: the former local probes used
32 MiB/4 PIDs (NMRPeak) and 64 MiB/8 PIDs (NMRNet). This is a documented policy
change, not a claim of equivalent resource controls. Qualify that change and
supply a separately justified deployment resource policy if quotas are required.

These are boundary tests, not full deployments or proof of Docker sandbox
behavior. Maintained containerized suites and actual rootful/rootless engines
with provider images and non-root callers remain adoption gates. A missing
shared package fails clearly; no fallback selects another engine or repeats an
uncertain mutation. Failure output keeps minimal ownership references and
independent cleanup explanations, not legacy phase labels or process inventories.
