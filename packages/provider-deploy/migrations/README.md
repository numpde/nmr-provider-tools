# Two consumer migrations, one review branch

These patches belong to the same `design/provider-deploy` branch as the shared
implementation. They are candidate edits to the actual providers, not copies
installed inside the shared package. There are no separate provider review
branches or published dependency releases.

`consumers.json` pins each repository's base commit, patch digest and resulting
Git tree. Patches must be applied to those bases, not an assumed current main.
Use disposable detached worktrees so reviewing them needs no additional branch:

```sh
git -C /path/to/provider-repository worktree add --detach /path/to/clean-review BASE_COMMIT
git -C /path/to/clean-review apply --check /absolute/path/to/CONSUMER.patch
git -C /path/to/clean-review apply --index /absolute/path/to/CONSUMER.patch
git -C /path/to/clean-review write-tree
```

The last result must match the recorded `tree`. The patches include migration
instructions and consumer tests. Install the reviewed local `provider-deploy`
wheel in the chosen host environment before running those tests from each
review checkout:

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/host-python -m unittest discover -s tests/host -v
```

## What they prove

- **NMRPeak:** imports the actual host deployment module and exercises its shared
  adapter. The public probe effect is substituted; request, map and failure
  types are real. The normal host import does not load the contained scientific
  runtime. Endpoint, image, interpreter, user, executable and ACL policy stay
  provider-owned. The patch removes 255 production lines.
- **NMRNet:** executes the actual shell adapter, `finish` function and signal
  traps with the real shared CLI and a bounded synthetic probe effect. Tests
  check rootful/rootless helper selection, nonzero/error output, group signals
  and shell-only termination without proceeding to input helpers. The patch
  removes 62 production lines. The separate inverse-mapping provisioning path
  remains unchanged.

These are boundary tests, not a full deployment or real Docker qualification.
The maintained containerized provider suites and actual rootful/rootless engine
runs with the provider images and non-root host callers still gate adoption.
NMRNet gains an explicit host Python/package prerequisite. The shared probe's
64 MiB/8-PID resource profile also differs from NMRPeak's former 32 MiB/4-PID
profile; qualify that change rather than implying exact policy equivalence.

The patches deliberately remove local lifecycle implementations instead of
keeping fallback copies. A missing/incompatible shared package fails clearly;
no hidden fallback selects another engine or repeats an uncertain mutation.
